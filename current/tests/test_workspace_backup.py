import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.bootstrap_workspace import WORKSPACE_MARKER, initialize_workspace
from scripts.doctor import run_doctor
from scripts.workspace_backup import (
    BACKUP_MARKER,
    WorkspaceBackupRefusal,
    backup_workspace,
    restore_workspace,
)


class ManagedWorkspaceBackupTests(TestCase):
    def test_fictional_demo_round_trip_preserves_ledgers_uploads_and_health(self) -> None:
        with TemporaryDirectory(prefix="finance-workspace-backup-test-") as raw_temp:
            root = Path(raw_temp)
            source = initialize_workspace(
                root / "source",
                profile="demo",
                allow_external=True,
            )
            upload = source / "uploads" / "fictional" / "demo-note.txt"
            upload.parent.mkdir(parents=True)
            upload.write_text("Fictional demonstration attachment.\n", encoding="utf-8")

            backup = root / "backup"
            backup_result = backup_workspace(source, backup, allow_external=True)

            self.assertEqual(backup_result["database_count"], 2)
            self.assertEqual(backup_result["upload_count"], 1)
            self.assertTrue((backup / BACKUP_MARKER).is_file())
            self.assertTrue((backup / WORKSPACE_MARKER).is_file())

            restored = root / "restored"
            restore_result = restore_workspace(backup, restored, allow_external=True)

            self.assertEqual(Path(restore_result["data_dir"]), restored.resolve())
            self.assertEqual(
                (restored / "uploads" / "fictional" / "demo-note.txt").read_text(
                    encoding="utf-8"
                ),
                "Fictional demonstration attachment.\n",
            )
            meta = sqlite3.connect(restored / "meta.sqlite")
            try:
                rows = meta.execute("SELECT id, db_path FROM users ORDER BY id").fetchall()
            finally:
                meta.close()
            self.assertTrue(rows)
            for _, raw_path in rows:
                ledger = Path(raw_path).resolve()
                ledger.relative_to(restored.resolve())
                self.assertTrue(ledger.is_file())

            report = run_doctor(restored, smoke=True)
            self.assertTrue(
                report["ok"],
                [item for item in report["checks"] if not item["passed"]],
            )

    def test_backup_and_restore_refuse_every_existing_destination(self) -> None:
        with TemporaryDirectory(prefix="finance-workspace-overwrite-test-") as raw_temp:
            root = Path(raw_temp)
            source = initialize_workspace(
                root / "source",
                profile="demo",
                allow_external=True,
            )
            existing_backup = root / "existing-backup"
            existing_backup.mkdir()
            backup_sentinel = existing_backup / "keep.txt"
            backup_sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceBackupRefusal, "existing destination"):
                backup_workspace(source, existing_backup, allow_external=True)
            self.assertEqual(backup_sentinel.read_text(encoding="utf-8"), "keep")

            with self.assertRaisesRegex(WorkspaceBackupRefusal, "inside the source"):
                backup_workspace(
                    source,
                    source / "nested-backup",
                    allow_external=True,
                )
            self.assertFalse((source / "nested-backup").exists())

            backup = root / "backup"
            backup_workspace(source, backup, allow_external=True)
            existing_restore = root / "existing-restore"
            existing_restore.mkdir()
            restore_sentinel = existing_restore / "keep.txt"
            restore_sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceBackupRefusal, "existing destination"):
                restore_workspace(backup, existing_restore, allow_external=True)
            self.assertEqual(restore_sentinel.read_text(encoding="utf-8"), "keep")

    def test_distinct_registered_ledger_is_snapshotted_and_rehomed(self) -> None:
        with TemporaryDirectory(prefix="finance-workspace-multi-ledger-test-") as raw_temp:
            root = Path(raw_temp)
            source = initialize_workspace(
                root / "source",
                profile="test",
                allow_external=True,
            )
            backup = root / "backup"
            backup_result = backup_workspace(source, backup, allow_external=True)
            self.assertEqual(backup_result["database_count"], 3)
            self.assertTrue((backup / "user_dbs" / "test-user.sqlite").is_file())

            restored = root / "restored"
            restore_workspace(backup, restored, allow_external=True)
            meta = sqlite3.connect(restored / "meta.sqlite")
            try:
                registered = Path(
                    meta.execute("SELECT db_path FROM users WHERE id=1").fetchone()[0]
                ).resolve()
            finally:
                meta.close()
            self.assertEqual(
                registered,
                (restored / "user_dbs" / "test-user.sqlite").resolve(),
            )
            self.assertTrue(registered.is_file())

    def test_backup_refuses_registered_ledger_that_escapes_workspace(self) -> None:
        with TemporaryDirectory(prefix="finance-workspace-escape-test-") as raw_temp:
            root = Path(raw_temp)
            source = initialize_workspace(
                root / "source",
                profile="demo",
                allow_external=True,
            )
            outside = root / "outside.sqlite"
            outside.write_bytes(b"synthetic sentinel")
            meta = sqlite3.connect(source / "meta.sqlite")
            try:
                meta.execute("UPDATE users SET db_path=?", (str(outside.resolve()),))
                meta.commit()
            finally:
                meta.close()

            backup = root / "backup"
            with self.assertRaisesRegex(WorkspaceBackupRefusal, "escapes"):
                backup_workspace(source, backup, allow_external=True)

            self.assertFalse(backup.exists())
            self.assertEqual(outside.read_bytes(), b"synthetic sentinel")

    def test_restore_rejects_tampered_manifest_without_creating_destination(self) -> None:
        with TemporaryDirectory(prefix="finance-workspace-tamper-test-") as raw_temp:
            root = Path(raw_temp)
            source = initialize_workspace(
                root / "source",
                profile="demo",
                allow_external=True,
            )
            backup = root / "backup"
            backup_workspace(source, backup, allow_external=True)
            manifest_path = backup / BACKUP_MARKER
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["users"][0]["ledger_path"] = "../outside.sqlite"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            restored = root / "restored"
            with self.assertRaisesRegex(WorkspaceBackupRefusal, "Unsafe user ledger"):
                restore_workspace(backup, restored, allow_external=True)
            self.assertFalse(restored.exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
