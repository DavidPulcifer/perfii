"""Create and restore safe, portable snapshots of managed local workspaces.

SQLite files are copied with SQLite's online backup API so an active WAL-mode
database produces a coherent snapshot.  Both operations are create-only: they
refuse an existing destination and build in a private staging directory before
an atomic rename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable


SCRIPT_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from scripts.bootstrap_workspace import (
    ALL_PROFILES,
    PROJECT_ROOT,
    WORKSPACE_FORMAT_VERSION,
    WORKSPACE_MARKER,
    _is_within,
)


BACKUP_MARKER: Final = ".finance-app-backup.json"
BACKUP_FORMAT_VERSION: Final = 1
SQLITE_UPLOAD_SUFFIXES: Final = (
    ".sqlite",
    ".sqlite3",
    ".db",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db-wal",
    ".db-shm",
)


class WorkspaceBackupRefusal(RuntimeError):
    """Raised when backup or restore cannot prove that paths and data are safe."""


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _require_new_destination(
    destination: Path,
    *,
    operation_source: Path,
    allow_external: bool,
) -> Path:
    target = _resolved(destination)
    source = _resolved(operation_source)
    project_root = PROJECT_ROOT.resolve()
    if target.exists():
        raise WorkspaceBackupRefusal(
            f"Refusing existing destination: {target}. Choose a new path; no files were changed."
        )
    if target == project_root:
        raise WorkspaceBackupRefusal("Refusing to use the project root as a destination.")
    if _is_within(target, source):
        raise WorkspaceBackupRefusal(
            f"Refusing destination inside the source directory: {target}."
        )
    if not allow_external and not _is_within(target, project_root):
        raise WorkspaceBackupRefusal(
            f"Refusing destination outside the project: {target}. "
            "Pass --allow-external only after verifying the new path."
        )
    return target


def _require_existing_source(
    source: Path,
    *,
    label: str,
    allow_external: bool,
) -> Path:
    target = _resolved(source)
    if not target.is_dir():
        raise WorkspaceBackupRefusal(f"Missing {label} directory: {target}")
    if not allow_external and not _is_within(target, PROJECT_ROOT):
        raise WorkspaceBackupRefusal(
            f"Refusing {label} outside the project: {target}. "
            "Pass --allow-external only after verifying the path."
        )
    return target


def _read_workspace_marker(root: Path) -> dict[str, Any]:
    path = root / WORKSPACE_MARKER
    if not path.is_file() or path.is_symlink():
        raise WorkspaceBackupRefusal(
            f"Missing ordinary {WORKSPACE_MARKER}; this is not a verified managed workspace."
        )
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceBackupRefusal(f"Invalid managed-workspace marker: {exc}") from exc
    if not isinstance(marker, dict):
        raise WorkspaceBackupRefusal("Invalid managed-workspace marker: expected an object.")
    if marker.get("format_version") != WORKSPACE_FORMAT_VERSION:
        raise WorkspaceBackupRefusal("Unsupported managed-workspace marker version.")
    if marker.get("profile") not in ALL_PROFILES:
        raise WorkspaceBackupRefusal("Unsupported managed-workspace profile.")
    return marker


def _open_readonly(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise WorkspaceBackupRefusal(f"Could not open managed SQLite file {path.name}: {exc}") from exc


def _verify_sqlite(path: Path, *, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise WorkspaceBackupRefusal(f"Missing ordinary {label} database: {path}")
    conn = _open_readonly(path)
    try:
        integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if integrity_rows != ["ok"]:
            raise WorkspaceBackupRefusal(
                f"{label} failed SQLite integrity_check ({len(integrity_rows)} result row(s))."
            )
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise WorkspaceBackupRefusal(
                f"{label} has {len(foreign_key_rows)} foreign-key violation(s)."
            )
    except sqlite3.Error as exc:
        raise WorkspaceBackupRefusal(f"Could not verify {label}: {exc}") from exc
    finally:
        conn.close()


def _online_backup(source: Path, destination: Path, *, label: str) -> None:
    if destination.exists():
        raise WorkspaceBackupRefusal(f"Refusing to overwrite staged database: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = _open_readonly(source)
    destination_conn: sqlite3.Connection | None = None
    try:
        destination_conn = sqlite3.connect(destination, timeout=30)
        source_conn.backup(destination_conn)
        destination_conn.commit()
    except sqlite3.Error as exc:
        raise WorkspaceBackupRefusal(f"SQLite online backup failed for {label}: {exc}") from exc
    finally:
        if destination_conn is not None:
            destination_conn.close()
        source_conn.close()
    _verify_sqlite(destination, label=label)


def _managed_database_path(workspace: Path, path: Path, *, label: str) -> Path:
    if _is_link(path):
        raise WorkspaceBackupRefusal(f"Refusing linked {label}: {path}")
    resolved = path.resolve()
    if not _is_within(resolved, workspace):
        raise WorkspaceBackupRefusal(f"{label} escapes the managed workspace: {resolved}")
    if not resolved.is_file():
        raise WorkspaceBackupRefusal(f"Missing {label}: {resolved}")
    return resolved


def _resolve_registered_ledger(workspace: Path, raw_path: str, *, user_id: int) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        relative_parts = lexical.relative_to(workspace).parts
    except ValueError as exc:
        raise WorkspaceBackupRefusal(
            f"Registered ledger for user {user_id} escapes the managed workspace: {lexical}"
        ) from exc
    cursor = workspace
    for part in relative_parts:
        cursor = cursor / part
        if cursor.exists() and _is_link(cursor):
            raise WorkspaceBackupRefusal(
                f"Registered ledger for user {user_id} uses a linked path: {cursor}"
            )
    resolved = candidate.resolve()
    if not _is_within(resolved, workspace):
        raise WorkspaceBackupRefusal(
            f"Registered ledger for user {user_id} escapes the managed workspace: {resolved}"
        )
    if not resolved.is_file():
        raise WorkspaceBackupRefusal(f"Registered ledger for user {user_id} is missing: {resolved}")
    if resolved in {
        (workspace / "meta.sqlite").resolve(),
        (workspace / WORKSPACE_MARKER).resolve(),
        (workspace / BACKUP_MARKER).resolve(),
    }:
        raise WorkspaceBackupRefusal(
            f"Registered ledger for user {user_id} collides with workspace metadata."
        )
    return resolved


def _read_registered_ledgers(
    workspace: Path,
    *,
    meta_path: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    conn = _open_readonly(meta_path)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if table is None:
            raise WorkspaceBackupRefusal("meta.sqlite is missing the users table.")
        rows = conn.execute("SELECT id, db_path FROM users ORDER BY id").fetchall()
    except sqlite3.Error as exc:
        raise WorkspaceBackupRefusal(f"Could not read registered ledgers: {exc}") from exc
    finally:
        conn.close()
    if not rows:
        raise WorkspaceBackupRefusal("meta.sqlite has no registered users.")

    users: list[dict[str, Any]] = []
    ledgers: list[Path] = []
    for row in rows:
        user_id = int(row["id"])
        raw_path = str(row["db_path"] or "").strip()
        if not raw_path:
            raise WorkspaceBackupRefusal(f"User {user_id} has an empty registered ledger path.")
        ledger_path = _resolve_registered_ledger(workspace, raw_path, user_id=user_id)
        relative = ledger_path.relative_to(workspace).as_posix()
        users.append({"user_id": user_id, "ledger_path": relative})
        ledgers.append(ledger_path)
    return users, ledgers


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _walk_plain_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Return ordinary directories/files without following links or reparse points."""
    directories: list[Path] = []
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        if _is_link(directory):
            raise WorkspaceBackupRefusal(f"Refusing linked directory: {directory}")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or _is_link(path):
                    raise WorkspaceBackupRefusal(f"Refusing linked upload path: {path}")
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    raise WorkspaceBackupRefusal(f"Refusing non-regular upload path: {path}")
    return directories, files


def _copy_uploads(
    source_root: Path,
    destination_root: Path,
    *,
    database_relpaths: set[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    destination_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return [], []
    if not source_root.is_dir() or _is_link(source_root):
        raise WorkspaceBackupRefusal(f"Uploads path is not an ordinary directory: {source_root}")

    directories, files = _walk_plain_files(source_root)
    directory_entries: list[str] = []
    file_entries: list[dict[str, Any]] = []
    workspace_root = source_root.parent.resolve()
    database_companions = {
        f"{relative}{suffix}"
        for relative in database_relpaths
        for suffix in ("-wal", "-shm")
    }
    for directory in sorted(directories, key=str):
        relative = directory.resolve().relative_to(workspace_root).as_posix()
        (destination_root.parent / relative).mkdir(parents=True, exist_ok=True)
        directory_entries.append(relative)
    for source in sorted(files, key=str):
        relative = source.resolve().relative_to(workspace_root).as_posix()
        if relative in database_relpaths or relative in database_companions:
            continue
        lower_name = source.name.lower()
        if lower_name.endswith(SQLITE_UPLOAD_SUFFIXES):
            raise WorkspaceBackupRefusal(
                f"Refusing unregistered SQLite-like file under uploads: {relative}"
            )
        destination = destination_root.parent / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        file_entries.append(
            {
                "path": relative,
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
            }
        )
    return directory_entries, file_entries


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_staging(staging: Path) -> None:
    if staging.exists() and staging.name.startswith(".finance-workspace-"):
        shutil.rmtree(staging)


def backup_workspace(
    data_dir: Path,
    backup_dir: Path,
    *,
    allow_external: bool = False,
) -> dict[str, Any]:
    """Create a new portable directory backup of a managed workspace."""
    workspace = _require_existing_source(
        data_dir,
        label="managed workspace",
        allow_external=allow_external,
    )
    marker = _read_workspace_marker(workspace)
    destination = _require_new_destination(
        backup_dir,
        operation_source=workspace,
        allow_external=allow_external,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".finance-workspace-backup-", dir=destination.parent)
    ).resolve()
    try:
        marker_destination = staging / WORKSPACE_MARKER
        _write_json(marker_destination, marker)

        meta_source = _managed_database_path(
            workspace,
            workspace / "meta.sqlite",
            label="meta.sqlite",
        )
        meta_destination = staging / "meta.sqlite"
        _online_backup(meta_source, meta_destination, label="meta.sqlite")
        users, registered_ledgers = _read_registered_ledgers(
            workspace,
            meta_path=meta_destination,
        )

        default_ledger = _managed_database_path(
            workspace,
            workspace / "data.sqlite",
            label="default ledger",
        )
        ledger_paths = sorted({default_ledger, *registered_ledgers}, key=str)
        database_entries = [
            {
                "kind": "meta",
                "path": "meta.sqlite",
                "sha256": _sha256(meta_destination),
                "size": meta_destination.stat().st_size,
            }
        ]
        ledger_relpaths: set[str] = set()
        for source in ledger_paths:
            relative = source.relative_to(workspace).as_posix()
            ledger_relpaths.add(relative)
            target = staging / relative
            _online_backup(source, target, label=f"ledger {relative}")
            database_entries.append(
                {
                    "kind": "ledger",
                    "path": relative,
                    "sha256": _sha256(target),
                    "size": target.stat().st_size,
                }
            )

        upload_directories, uploads = _copy_uploads(
            workspace / "uploads",
            staging / "uploads",
            database_relpaths=ledger_relpaths,
        )
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "created_by": "scripts/workspace_backup.py",
            "databases": database_entries,
            "format_version": BACKUP_FORMAT_VERSION,
            "upload_directories": upload_directories,
            "uploads": uploads,
            "users": users,
            "workspace_marker_sha256": _sha256(marker_destination),
        }
        _write_json(staging / BACKUP_MARKER, manifest)
        staging.replace(destination)
    except Exception:
        _remove_staging(staging)
        raise
    return {
        "backup_dir": str(destination),
        "database_count": len(database_entries),
        "upload_count": len(uploads),
    }


def _safe_backup_path(backup: Path, raw_path: Any, *, label: str) -> tuple[Path, str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise WorkspaceBackupRefusal(f"Invalid {label} path in backup manifest.")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceBackupRefusal(f"Unsafe {label} path in backup manifest: {raw_path!r}")
    normalized = relative.as_posix()
    target = (backup / relative).resolve()
    if not _is_within(target, backup):
        raise WorkspaceBackupRefusal(f"Escaping {label} path in backup manifest: {raw_path!r}")
    cursor = backup
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and _is_link(cursor):
            raise WorkspaceBackupRefusal(f"Refusing linked {label} path in backup: {raw_path!r}")
    return target, normalized


def _read_backup_manifest(backup: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    marker = _read_workspace_marker(backup)
    manifest_path = backup / BACKUP_MARKER
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise WorkspaceBackupRefusal(f"Missing ordinary {BACKUP_MARKER}.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceBackupRefusal(f"Invalid backup manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise WorkspaceBackupRefusal("Unsupported backup manifest format.")
    if manifest.get("workspace_marker_sha256") != _sha256(backup / WORKSPACE_MARKER):
        raise WorkspaceBackupRefusal("Managed-workspace marker does not match the backup manifest.")
    return marker, manifest


def _validated_manifest_files(
    backup: Path,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    databases = manifest.get("databases")
    uploads = manifest.get("uploads")
    upload_directories = manifest.get("upload_directories", [])
    if not isinstance(databases, list) or not isinstance(uploads, list):
        raise WorkspaceBackupRefusal("Backup manifest file lists are invalid.")
    if not isinstance(upload_directories, list):
        raise WorkspaceBackupRefusal("Backup manifest upload directory list is invalid.")

    validated_databases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in databases:
        if not isinstance(entry, dict) or entry.get("kind") not in {"meta", "ledger"}:
            raise WorkspaceBackupRefusal("Backup manifest contains an invalid database entry.")
        path, relative = _safe_backup_path(backup, entry.get("path"), label="database")
        if relative in seen_paths:
            raise WorkspaceBackupRefusal(f"Duplicate path in backup manifest: {relative}")
        seen_paths.add(relative)
        if not path.is_file() or path.is_symlink():
            raise WorkspaceBackupRefusal(f"Missing ordinary database in backup: {relative}")
        if entry.get("sha256") != _sha256(path) or entry.get("size") != path.stat().st_size:
            raise WorkspaceBackupRefusal(f"Database does not match backup manifest: {relative}")
        _verify_sqlite(path, label=f"backup {relative}")
        validated_databases.append({**entry, "path": relative, "source": path})

    kinds_and_paths = {(entry["kind"], entry["path"]) for entry in validated_databases}
    if ("meta", "meta.sqlite") not in kinds_and_paths or ("ledger", "data.sqlite") not in kinds_and_paths:
        raise WorkspaceBackupRefusal("Backup must contain meta.sqlite and data.sqlite snapshots.")
    if sum(entry["kind"] == "meta" for entry in validated_databases) != 1:
        raise WorkspaceBackupRefusal("Backup must contain exactly one meta database entry.")

    validated_uploads: list[dict[str, Any]] = []
    for entry in uploads:
        if not isinstance(entry, dict):
            raise WorkspaceBackupRefusal("Backup manifest contains an invalid upload entry.")
        path, relative = _safe_backup_path(backup, entry.get("path"), label="upload")
        if not relative.startswith("uploads/") or relative in seen_paths:
            raise WorkspaceBackupRefusal(f"Unsafe or duplicate upload path: {relative}")
        seen_paths.add(relative)
        if not path.is_file() or path.is_symlink():
            raise WorkspaceBackupRefusal(f"Missing ordinary upload in backup: {relative}")
        if entry.get("sha256") != _sha256(path) or entry.get("size") != path.stat().st_size:
            raise WorkspaceBackupRefusal(f"Upload does not match backup manifest: {relative}")
        validated_uploads.append({**entry, "path": relative, "source": path})

    validated_directories: list[str] = []
    for raw_path in upload_directories:
        path, relative = _safe_backup_path(backup, raw_path, label="upload directory")
        if not relative.startswith("uploads/") or not path.is_dir():
            raise WorkspaceBackupRefusal(f"Invalid upload directory in backup: {relative}")
        validated_directories.append(relative)
    return validated_databases, validated_uploads, validated_directories


def _validate_user_manifest(
    backup: Path,
    manifest: dict[str, Any],
    *,
    database_paths: set[str],
) -> list[dict[str, Any]]:
    users = manifest.get("users")
    if not isinstance(users, list) or not users:
        raise WorkspaceBackupRefusal("Backup manifest has no registered users.")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for entry in users:
        if not isinstance(entry, dict) or not isinstance(entry.get("user_id"), int):
            raise WorkspaceBackupRefusal("Backup manifest contains an invalid user entry.")
        user_id = int(entry["user_id"])
        _, ledger_path = _safe_backup_path(backup, entry.get("ledger_path"), label="user ledger")
        if user_id in seen_ids or ledger_path not in database_paths:
            raise WorkspaceBackupRefusal("Backup user mappings are duplicated or incomplete.")
        seen_ids.add(user_id)
        normalized.append({"user_id": user_id, "ledger_path": ledger_path})

    meta = _open_readonly(backup / "meta.sqlite")
    try:
        meta_ids = [int(row[0]) for row in meta.execute("SELECT id FROM users ORDER BY id")]
    except sqlite3.Error as exc:
        raise WorkspaceBackupRefusal(f"Could not verify backup users: {exc}") from exc
    finally:
        meta.close()
    if sorted(seen_ids) != meta_ids:
        raise WorkspaceBackupRefusal("Backup user mappings do not match meta.sqlite.")
    return normalized


def _rewrite_registered_paths(
    meta_path: Path,
    users: Iterable[dict[str, Any]],
    *,
    workspace_root: Path,
) -> None:
    conn = sqlite3.connect(meta_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for entry in users:
            ledger = (workspace_root / entry["ledger_path"]).resolve()
            if not _is_within(ledger, workspace_root):
                raise WorkspaceBackupRefusal("Refusing escaping restored ledger path.")
            cursor = conn.execute(
                "UPDATE users SET db_path=? WHERE id=?",
                (str(ledger), int(entry["user_id"])),
            )
            if cursor.rowcount != 1:
                raise WorkspaceBackupRefusal(
                    f"Restored metadata is missing user {entry['user_id']}."
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _verify_sqlite(meta_path, label="restored meta.sqlite")


def _verify_restored_paths(workspace: Path, users: Iterable[dict[str, Any]]) -> None:
    expected = {int(entry["user_id"]): entry["ledger_path"] for entry in users}
    conn = _open_readonly(workspace / "meta.sqlite")
    try:
        rows = conn.execute("SELECT id, db_path FROM users ORDER BY id").fetchall()
    finally:
        conn.close()
    if {int(row["id"]) for row in rows} != set(expected):
        raise WorkspaceBackupRefusal("Restored user registry does not match the backup.")
    for row in rows:
        path = Path(str(row["db_path"])).expanduser().resolve()
        expected_path = (workspace / expected[int(row["id"])]).resolve()
        if path != expected_path or not _is_within(path, workspace) or not path.is_file():
            raise WorkspaceBackupRefusal(
                f"Restored ledger path for user {row['id']} is not safely managed."
            )


def restore_workspace(
    backup_dir: Path,
    data_dir: Path,
    *,
    allow_external: bool = False,
) -> dict[str, Any]:
    """Restore a validated backup to a new managed-workspace directory."""
    backup = _require_existing_source(
        backup_dir,
        label="backup",
        allow_external=allow_external,
    )
    destination = _require_new_destination(
        data_dir,
        operation_source=backup,
        allow_external=allow_external,
    )
    marker, manifest = _read_backup_manifest(backup)
    databases, uploads, upload_directories = _validated_manifest_files(backup, manifest)
    database_paths = {entry["path"] for entry in databases if entry["kind"] == "ledger"}
    users = _validate_user_manifest(backup, manifest, database_paths=database_paths)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".finance-workspace-restore-", dir=destination.parent)
    ).resolve()
    try:
        _write_json(staging / WORKSPACE_MARKER, marker)
        (staging / "uploads").mkdir(parents=True)
        (staging / "user_dbs").mkdir(parents=True)
        for entry in databases:
            target = staging / entry["path"]
            _online_backup(entry["source"], target, label=f"restored {entry['path']}")
        for relative in upload_directories:
            (staging / relative).mkdir(parents=True, exist_ok=True)
        for entry in uploads:
            target = staging / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry["source"], target)
            if _sha256(target) != entry["sha256"]:
                raise WorkspaceBackupRefusal(f"Restored upload verification failed: {entry['path']}")

        # Verify the fully restored synthetic workspace before making it visible.
        _rewrite_registered_paths(staging / "meta.sqlite", users, workspace_root=staging)
        _verify_restored_paths(staging, users)
        if marker.get("synthetic_data") is True and marker.get("profile") in {"demo", "test"}:
            from scripts.doctor import run_doctor

            report = run_doctor(staging, smoke=True)
            if not report["ok"]:
                failed = [item["check"] for item in report["checks"] if not item["passed"]]
                raise WorkspaceBackupRefusal(
                    "Restored synthetic workspace failed doctor: " + ", ".join(failed)
                )

        # Absolute user paths must refer to the final location after the atomic rename.
        _rewrite_registered_paths(staging / "meta.sqlite", users, workspace_root=destination)
        staging.replace(destination)
        _verify_restored_paths(destination, users)
    except Exception:
        _remove_staging(staging)
        raise
    return {
        "data_dir": str(destination),
        "database_count": len(databases),
        "upload_count": len(uploads),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or restore a portable managed-workspace backup. "
            "Destinations must be new and are never overwritten."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser(
        "backup",
        help="Snapshot a managed workspace with SQLite's online backup API.",
    )
    backup_parser.add_argument("--data-dir", type=Path, required=True, help="Managed workspace to snapshot.")
    backup_parser.add_argument(
        "--backup-dir",
        type=Path,
        required=True,
        help="New destination directory for the portable backup.",
    )
    backup_parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow verified source/destination paths outside the project.",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore a validated backup into a new managed workspace.",
    )
    restore_parser.add_argument("--backup-dir", type=Path, required=True, help="Backup directory to restore.")
    restore_parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="New destination directory for the restored workspace.",
    )
    restore_parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow verified source/destination paths outside the project.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_workspace(
                args.data_dir,
                args.backup_dir,
                allow_external=args.allow_external,
            )
            print(f"Created managed-workspace backup at {result['backup_dir']}")
            print(
                f"Verified {result['database_count']} SQLite snapshot(s) and "
                f"{result['upload_count']} upload file(s)."
            )
        else:
            result = restore_workspace(
                args.backup_dir,
                args.data_dir,
                allow_external=args.allow_external,
            )
            print(f"Restored managed workspace at {result['data_dir']}")
            print("The destination was new; no existing workspace was overwritten.")
        return 0
    except (WorkspaceBackupRefusal, OSError, sqlite3.Error) as exc:
        print(f"Workspace {args.command} refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
