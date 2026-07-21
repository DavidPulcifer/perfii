#!/usr/bin/env python3
"""Check whether a finance-app repository contains unsafe source material.

The module is dependency-free and read-only. In a Git checkout, automatic mode scans committed ``HEAD``
and refuses tracked index/worktree divergence; it never opens untracked or
ignored files. In an extracted repository download, it scans every entry beneath the
selected root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_FILE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_TREE_BYTES = 50 * 1024 * 1024
MAX_HISTORY_SOURCE_BYTES = 500 * 1024 * 1024
MAX_COMMIT_OBJECT_BYTES = 1 * 1024 * 1024
MAX_HISTORY_COMMIT_BYTES = 25 * 1024 * 1024

GIT_ENVIRONMENT_OVERRIDES = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}

# These locations commonly hold runtime state, private records, dependencies,
# generated output, caches, or secrets rather than repository source.
PROHIBITED_PATH_COMPONENTS = {
    ".git",
    ".idea",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "backups",
    "build",
    "data",
    "dist",
    "htmlcov",
    "instance",
    "node_modules",
    "outputs",
    "snapshot-data",
    "uploads",
    "user_dbs",
    "venv",
}
PROHIBITED_FILENAMES = {
    ".ds_store",
    ".env",
    ".flaskenv",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "thumbs.db",
}
PROHIBITED_SUFFIXES = {
    ".7z",
    ".avi",
    ".bak",
    ".backup",
    ".cer",
    ".crt",
    ".csv",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".doc",
    ".docx",
    ".dll",
    ".dylib",
    ".exe",
    ".gz",
    ".key",
    ".log",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp4",
    ".mp3",
    ".msi",
    ".ofx",
    ".p12",
    ".pdf",
    ".pem",
    ".pfx",
    ".qfx",
    ".qif",
    ".rar",
    ".shm",
    ".so",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".tar",
    ".tgz",
    ".tsv",
    ".wal",
    ".wav",
    ".webm",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".zip",
}
ALLOWED_DOTENV_SUFFIXES = (".example", ".sample", ".template")
PLACEHOLDER_SECRET_MARKERS = {
    "change-me",
    "changeme",
    "dummy",
    "example",
    "not-set",
    "placeholder",
    "replace",
    "test-only",
    "your-",
}
GENERIC_HOME_NAMES = {
    "default",
    "example",
    "name",
    "public",
    "test",
    "user",
    "username",
    "your-name",
}
EXAMPLE_EMAIL_DOMAINS = {
    "example.com",
    "example.invalid",
    "example.net",
    "example.org",
    "localhost",
}
ALLOWED_PUBLIC_EMAIL_ADDRESSES: set[str] = set()

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:DSA |EC |ENCRYPTED |OPENSSH |PGP |RSA )?PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)
CREDENTIAL_URL_RE = re.compile(
    r"\b(?:https?|postgres(?:ql)?|mysql|redis)://[^\s/:@]+:[^\s/@]+@",
    re.IGNORECASE,
)
WINDOWS_USER_HOME_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]Users[\\/]([^\\/\r\n]+?)"
    r"(?=[\\/]|[\"'`<>{}\[\]()]|$)",
    re.IGNORECASE,
)
# Split the literals so this scanner does not flag the source of its own rule.
UNIX_USER_HOME_RE = re.compile(
    r"(?:/" r"Users/([^/\r\n]+?)(?=/|[\"'`<>{}\[\]()]|$)|"
    r"/" r"home/([^/\r\n]+?)(?=/|[\"'`<>{}\[\]()]|$))"
)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)
COMMIT_IDENTITY_RE = re.compile(
    r"^(author|committer) ([^<>\r\n]+) <([^<>\s]+)> ([0-9]+) ([+-][0-9]{4})$"
)
PERSONAL_NAME_SHAPED_RE = re.compile(
    r"^[A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+)+$"
)
GENERIC_COMMIT_NAME_MARKERS = {
    "bot",
    "build",
    "contributor",
    "finance",
    "github",
    "release",
    "safety",
    "test",
}
SENSITIVE_ENV_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:API_KEY|PASSWORD|SECRET|TOKEN)[A-Za-z0-9_]*)"
    r"\s*=\s*(.*?)\s*$",
    re.IGNORECASE,
)
TOKEN_PATTERNS = (
    ("AWS access-key-shaped token", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token-shaped value", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token-shaped value", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("Google API-key-shaped value", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token-shaped value", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Stripe live-secret-shaped value", re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b")),
)

PUBLIC_COMMIT_EMAIL_DOMAINS = EXAMPLE_EMAIL_DOMAINS | {
    "users.noreply.github.com",
}
GITHUB_NOREPLY_IDENTITY_RE = re.compile(
    r"^(?:[0-9]+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)


class SourceSafetyError(ValueError):
    """Raised when a source path or file violates the repository-safety policy."""


def _safe_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in list(environment):
        if (
            name in GIT_ENVIRONMENT_OVERRIDES
            or name in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_REPLACE_REF_BASE"}
            or name.startswith("GIT_CONFIG_KEY_")
            or name.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    return environment


@dataclass(frozen=True)
class SourceSafetyViolation:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True)
class SourceSafetyReport:
    root: Path
    scope: str
    file_count: int
    source_bytes: int
    violations: tuple[SourceSafetyViolation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "root": str(self.root),
            "scope": self.scope,
            "file_count": self.file_count,
            "source_bytes": self.source_bytes,
            "violations": [violation.to_dict() for violation in self.violations],
        }


def validate_source_path(relative_path: str) -> None:
    """Validate one forward-slash source path or raise ``SourceSafetyError``."""

    try:
        relative_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceSafetyError("Source path is not valid UTF-8.") from exc

    if (
        not relative_path
        or "\\" in relative_path
        or any(ord(character) < 32 or ord(character) == 127 for character in relative_path)
    ):
        raise SourceSafetyError("Unsafe or non-portable source path.")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SourceSafetyError("Unsafe or non-relative source path.")

    lowered_parts = [part.lower() for part in pure.parts]
    blocked_parts = sorted(set(lowered_parts) & PROHIBITED_PATH_COMPONENTS)
    if blocked_parts:
        raise SourceSafetyError(
            f"Prohibited runtime/generated path component: {blocked_parts[0]}"
        )

    filename = lowered_parts[-1]
    if filename.startswith(".env") and not filename.endswith(ALLOWED_DOTENV_SUFFIXES):
        raise SourceSafetyError("Prohibited environment filename.")
    if filename.startswith(".flaskenv"):
        raise SourceSafetyError("Prohibited Flask environment filename.")
    if filename.endswith(".local.json"):
        raise SourceSafetyError("Prohibited owner-local profile filename.")
    if filename in PROHIBITED_FILENAMES:
        raise SourceSafetyError("Prohibited secret/runtime filename.")
    matched_suffix = next(
        (suffix for suffix in sorted(PROHIBITED_SUFFIXES) if filename.endswith(suffix)),
        None,
    )
    if matched_suffix:
        raise SourceSafetyError(
            f"Prohibited non-source or financial-data extension: {matched_suffix}"
        )


def _is_generic_home_name(value: str) -> bool:
    normalized = value.strip("<>{}[]()%$").lower()
    return normalized in GENERIC_HOME_NAMES or "example" in normalized or "placeholder" in normalized


def _is_placeholder_secret(value: str) -> bool:
    normalized = value.strip().strip("'\"").lower()
    if not normalized:
        return True
    if normalized.startswith("${") or normalized.startswith("%") or "<" in normalized:
        return True
    return any(marker in normalized for marker in PLACEHOLDER_SECRET_MARKERS)


def _validate_text_content(relative_path: str, text: str) -> None:
    if PRIVATE_KEY_RE.search(text):
        raise SourceSafetyError("Private-key material detected.")
    if CREDENTIAL_URL_RE.search(text):
        raise SourceSafetyError("Credential-bearing URL detected.")

    for label, pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            raise SourceSafetyError(f"{label} detected.")

    for match in WINDOWS_USER_HOME_RE.finditer(text):
        if not _is_generic_home_name(match.group(1)):
            raise SourceSafetyError("Personal Windows home path detected.")
    for match in UNIX_USER_HOME_RE.finditer(text):
        home_name = match.group(1) or match.group(2)
        if not _is_generic_home_name(home_name):
            raise SourceSafetyError("Personal Unix home path detected.")

    for match in EMAIL_RE.finditer(text):
        address = match.group(0).lower()
        if (
            address not in ALLOWED_PUBLIC_EMAIL_ADDRESSES
            and match.group(2).lower() not in EXAMPLE_EMAIL_DOMAINS
        ):
            raise SourceSafetyError("Non-example email address detected.")

    filename = PurePosixPath(relative_path).name.lower()
    if filename.startswith(".env"):
        for line in text.splitlines():
            match = SENSITIVE_ENV_RE.match(line)
            if match and not _is_placeholder_secret(match.group(2)):
                raise SourceSafetyError(
                    f"Non-placeholder {match.group(1)} value detected in environment example."
                )


def validate_source_content(relative_path: str, data: bytes) -> None:
    """Validate one file's bytes or raise ``SourceSafetyError``.

    Call ``validate_source_path`` first when using this function independently.
    ``scan_source_tree`` always performs both checks.
    """

    if len(data) > MAX_SOURCE_FILE_BYTES:
        raise SourceSafetyError(
            f"File exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit."
        )
    if data.startswith(b"SQLite format 3\x00"):
        raise SourceSafetyError("Embedded SQLite database detected.")
    if b"\x00" in data:
        raise SourceSafetyError("Binary file is outside the repository source policy.")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceSafetyError("Non-UTF-8 file is outside the repository source policy.") from exc
    _validate_text_content(relative_path, text)


def _has_git_metadata(root: Path) -> bool:
    return (root / ".git").exists()


def _run_git(
    root: Path,
    args: Sequence[str],
    *,
    text: bool = False,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=text,
            env=_safe_git_environment(),
        )
    except FileNotFoundError as exc:
        raise SourceSafetyError("Git is required for this source-safety check.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if text and exc.stderr else ""
        suffix = f" ({detail})" if detail else ""
        raise SourceSafetyError(f"Git could not inspect the requested source{suffix}.") from exc


def _git_blob_sizes(root: Path, object_ids: Sequence[str]) -> dict[str, int]:
    requested = list(dict.fromkeys(object_ids))
    if not requested:
        return {}
    payload = ("\n".join(requested) + "\n").encode("ascii")
    try:
        result = subprocess.run(
            [
                "git",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ],
            cwd=root,
            input=payload,
            check=True,
            capture_output=True,
            env=_safe_git_environment(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SourceSafetyError("Git could not inspect source blob sizes.") from exc

    lines = result.stdout.splitlines()
    if len(lines) != len(requested):
        raise SourceSafetyError("Git returned an incomplete source blob-size response.")
    sizes: dict[str, int] = {}
    for expected, raw_line in zip(requested, lines):
        try:
            actual, object_type, raw_size = raw_line.decode("ascii").split(" ", 2)
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError("Git returned an invalid source blob-size response.") from exc
        if actual.lower() != expected.lower() or object_type != "blob" or size < 0:
            raise SourceSafetyError("Git source object is not the expected regular blob.")
        sizes[expected] = size
    return sizes


def _git_commit_sizes(root: Path, object_ids: Sequence[str]) -> dict[str, int]:
    requested = list(dict.fromkeys(object_ids))
    if not requested:
        return {}
    payload = ("\n".join(requested) + "\n").encode("ascii")
    try:
        result = subprocess.run(
            [
                "git",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ],
            cwd=root,
            input=payload,
            check=True,
            capture_output=True,
            env=_safe_git_environment(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SourceSafetyError("Git could not inspect commit sizes.") from exc

    lines = result.stdout.splitlines()
    if len(lines) != len(requested):
        raise SourceSafetyError("Git returned an incomplete commit-size response.")
    sizes: dict[str, int] = {}
    for expected, raw_line in zip(requested, lines):
        try:
            actual, object_type, raw_size = raw_line.decode("ascii").split(" ", 2)
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError("Git returned an invalid commit-size response.") from exc
        if actual.lower() != expected.lower() or object_type != "commit" or size < 0:
            raise SourceSafetyError("Git history object is not the expected commit.")
        sizes[expected] = size
    return sizes


def _git_blob_contents(root: Path, object_ids: Sequence[str]) -> dict[str, bytes]:
    requested = list(dict.fromkeys(object_ids))
    if not requested:
        return {}
    payload = ("\n".join(requested) + "\n").encode("ascii")
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=root,
            input=payload,
            check=True,
            capture_output=True,
            env=_safe_git_environment(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SourceSafetyError("Git could not read committed source blobs.") from exc

    response = result.stdout
    offset = 0
    blobs: dict[str, bytes] = {}
    for expected in requested:
        header_end = response.find(b"\n", offset)
        if header_end < 0:
            raise SourceSafetyError("Git returned an incomplete source blob response.")
        try:
            actual, object_type, raw_size = response[offset:header_end].decode("ascii").split(" ", 2)
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError("Git returned an invalid source blob response.") from exc
        if actual.lower() != expected.lower() or object_type != "blob" or size < 0:
            raise SourceSafetyError("Git source object is not the expected regular blob.")
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(response) or response[content_end : content_end + 1] != b"\n":
            raise SourceSafetyError("Git returned a truncated source blob response.")
        blobs[expected] = response[content_start:content_end]
        offset = content_end + 1
    if offset != len(response):
        raise SourceSafetyError("Git returned unexpected trailing source blob data.")
    return blobs


def _git_commit_contents(root: Path, object_ids: Sequence[str]) -> dict[str, bytes]:
    """Read raw commit objects without relying on delimiter-unsafe log output."""

    requested = list(dict.fromkeys(object_ids))
    if not requested:
        return {}
    payload = ("\n".join(requested) + "\n").encode("ascii")
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=root,
            input=payload,
            check=True,
            capture_output=True,
            env=_safe_git_environment(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SourceSafetyError("Git could not read commit metadata.") from exc

    response = result.stdout
    offset = 0
    commits: dict[str, bytes] = {}
    for expected in requested:
        header_end = response.find(b"\n", offset)
        if header_end < 0:
            raise SourceSafetyError("Git returned incomplete commit metadata.")
        try:
            actual, object_type, raw_size = response[offset:header_end].decode("ascii").split(" ", 2)
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError("Git returned invalid commit metadata.") from exc
        if actual.lower() != expected.lower() or object_type != "commit" or size < 0:
            raise SourceSafetyError("Git history object is not the expected commit.")
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(response) or response[content_end : content_end + 1] != b"\n":
            raise SourceSafetyError("Git returned truncated commit metadata.")
        commits[expected] = response[content_start:content_end]
        offset = content_end + 1
    if offset != len(response):
        raise SourceSafetyError("Git returned unexpected trailing commit metadata.")
    return commits


def _name_matches_github_noreply_identity(name: str, email: str) -> bool:
    """Return whether a public GitHub username intentionally exposes this name."""

    match = GITHUB_NOREPLY_IDENTITY_RE.fullmatch(email)
    if match is None:
        return False
    normalized_name = re.sub(r"[^a-z0-9]", "", name.casefold())
    normalized_username = re.sub(r"[^a-z0-9]", "", match.group(1).casefold())
    return bool(normalized_name) and normalized_name == normalized_username


def _validate_commit_content(commit: str, data: bytes) -> list[SourceSafetyViolation]:
    """Validate one raw commit's identities and message for public release."""

    context = f"commit {commit[:12]}"
    violations: list[SourceSafetyViolation] = []
    header, separator, message = data.partition(b"\n\n")
    if not separator:
        return [SourceSafetyViolation(context, "Commit object has no message separator.")]

    identity_lines: dict[str, list[bytes]] = {"author": [], "committer": []}
    tree_lines = 0
    for line in header.splitlines():
        if re.fullmatch(rb"tree [0-9a-fA-F]{40,64}", line):
            tree_lines += 1
        elif re.fullmatch(rb"parent [0-9a-fA-F]{40,64}", line):
            continue
        elif line.startswith(b"author "):
            identity_lines["author"].append(line)
        elif line.startswith(b"committer "):
            identity_lines["committer"].append(line)
        else:
            violations.append(
                SourceSafetyViolation(
                    context,
                    "Commit contains an unsupported or malformed metadata header.",
                )
            )
    if tree_lines != 1:
        violations.append(
            SourceSafetyViolation(context, "Commit must have exactly one valid tree header.")
        )

    for role_name in ("author", "committer"):
        lines = identity_lines[role_name]
        if len(lines) != 1:
            violations.append(
                SourceSafetyViolation(
                    context,
                    f"Commit must have exactly one {role_name} identity line.",
                )
            )
            continue
        try:
            identity = lines[0].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            violations.append(
                SourceSafetyViolation(context, f"Commit has a non-UTF-8 {role_name} identity.")
            )
            continue
        identity_match = COMMIT_IDENTITY_RE.fullmatch(identity)
        if identity_match is None or identity_match.group(1) != role_name:
            violations.append(
                SourceSafetyViolation(context, f"Commit has a malformed {role_name} identity.")
            )
            continue
        name = identity_match.group(2).strip()
        email = identity_match.group(3).lower()
        if not name:
            violations.append(SourceSafetyViolation(context, f"Commit has an empty {role_name} name."))
        else:
            try:
                validate_source_content(
                    f"git-{role_name}-identity-{commit[:12]}.txt",
                    name.encode("utf-8"),
                )
            except SourceSafetyError as exc:
                violations.append(SourceSafetyViolation(context, f"Commit {role_name} name: {exc}"))
            name_words = {part.casefold() for part in re.findall(r"[A-Za-z]+", name)}
            if (
                PERSONAL_NAME_SHAPED_RE.fullmatch(name)
                and not name_words.intersection(GENERIC_COMMIT_NAME_MARKERS)
                and not _name_matches_github_noreply_identity(name, email)
            ):
                violations.append(
                    SourceSafetyViolation(
                        context,
                        f"Commit {role_name} name looks personal; use a reviewed public username or neutral project identity.",
                    )
                )
        email_match = EMAIL_RE.fullmatch(email)
        if email_match is None or email_match.group(2).lower() not in PUBLIC_COMMIT_EMAIL_DOMAINS:
            violations.append(
                SourceSafetyViolation(
                    context,
                    f"Commit {role_name} email is not an example or GitHub noreply address.",
                )
            )

    try:
        validate_source_content(f"git-message-{commit[:12]}.txt", message)
    except SourceSafetyError as exc:
        violations.append(SourceSafetyViolation(context, f"Commit message: {exc}"))
    return violations


def _tracked_source_diverged(root: Path) -> bool:
    commands = (
        ["diff", "--quiet", "--no-ext-diff", "--ignore-submodules=none", "--"],
        ["diff", "--cached", "--quiet", "--no-ext-diff", "--ignore-submodules=none", "HEAD", "--"],
    )
    for args in commands:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                env=_safe_git_environment(),
            )
        except FileNotFoundError as exc:
            raise SourceSafetyError("Git is required for this source-safety check.") from exc
        if result.returncode == 1:
            return True
        if result.returncode != 0:
            raise SourceSafetyError("Git could not compare tracked source with committed HEAD.")
    return False


def _scan_committed_git_tree(root: Path) -> SourceSafetyReport:
    commit = _run_git(
        root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        text=True,
    ).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise SourceSafetyError("HEAD did not resolve to a normal Git commit.")

    violations: list[SourceSafetyViolation] = []
    if _tracked_source_diverged(root):
        violations.append(
            SourceSafetyViolation(
                ".",
                "Tracked index/worktree source differs from committed HEAD; "
                "commit and review the intended repository state before acceptance.",
            )
        )

    file_count = 0
    source_bytes = 0
    casefold_paths: dict[str, str] = {}
    regular_entries: list[tuple[str, str]] = []
    for mode, object_type, object_id, relative in _git_tree_entries(root, commit):
        folded = relative.casefold()
        prior = casefold_paths.get(folded)
        if prior is not None and prior != relative:
            violations.append(
                SourceSafetyViolation(relative, f"Case-insensitive path collision with {prior}.")
            )
            continue
        casefold_paths[folded] = relative

        try:
            validate_source_path(relative)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            violations.append(
                SourceSafetyViolation(relative, "Committed entry is not a regular source file.")
            )
            continue

        regular_entries.append((relative, object_id))

    sizes = _git_blob_sizes(root, [object_id for _, object_id in regular_entries])
    accepted_entries: list[tuple[str, str]] = []
    accepted_object_ids: list[str] = []
    for relative, object_id in regular_entries:
        size = sizes[object_id]
        if size > MAX_SOURCE_FILE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"File exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit.",
                )
            )
            continue
        if source_bytes + size > MAX_SOURCE_TREE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Source tree exceeds the {MAX_SOURCE_TREE_BYTES}-byte source limit.",
                )
            )
            continue
        accepted_entries.append((relative, object_id))
        accepted_object_ids.append(object_id)
        source_bytes += size

    blobs = _git_blob_contents(root, accepted_object_ids)
    for relative, object_id in accepted_entries:
        data = blobs[object_id]
        file_count += 1
        try:
            validate_source_content(relative, data)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))

    return SourceSafetyReport(
        root=root,
        scope=f"committed Git tree at {commit[:12]}",
        file_count=file_count,
        source_bytes=source_bytes,
        violations=tuple(violations),
    )


def _git_index_entries(root: Path) -> dict[str, list[tuple[str, str, int]]]:
    result = _run_git(root, ["ls-files", "--stage", "-z"])
    entries: dict[str, list[tuple[str, str, int]]] = {}
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_object_id, raw_stage = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_id = raw_object_id.decode("ascii").lower()
            stage = int(raw_stage.decode("ascii"))
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError("Git returned an unreadable index entry.") from exc
        entries.setdefault(path, []).append((mode, object_id, stage))
    return entries


def scan_tracked_worktree(root: Path) -> SourceSafetyReport:
    """Scan staged index blobs and worktree copies for tracked source paths.

    This is a development/preflight helper, not a committed-state check. It does not
    inspect untracked files. Call ``scan_source_tree`` for the strict committed-
    HEAD check, which separately rejects every tracked divergence.
    """

    resolved_root = root.resolve()
    if not resolved_root.is_dir() or not _has_git_metadata(resolved_root):
        raise SourceSafetyError("Tracked-worktree scanning requires a Git checkout.")

    commit = _run_git(
        resolved_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        text=True,
    ).stdout.strip().lower()
    index_entries = _git_index_entries(resolved_root)
    head_paths = {
        path for _, _, _, path in _git_tree_entries(resolved_root, commit)
    }
    all_paths = sorted(head_paths | set(index_entries))

    violations: list[SourceSafetyViolation] = []
    index_contexts: dict[str, str] = {}
    worktree_data: dict[str, bytes] = {}
    worktree_bytes = 0
    casefold_paths: dict[str, str] = {}

    for relative in all_paths:
        folded = relative.casefold()
        prior = casefold_paths.get(folded)
        if prior is not None and prior != relative:
            violations.append(
                SourceSafetyViolation(relative, f"Case-insensitive path collision with {prior}.")
            )
            continue
        casefold_paths[folded] = relative

        try:
            validate_source_path(relative)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))
            continue

        records = index_entries.get(relative, [])
        if not records:
            violations.append(
                SourceSafetyViolation(relative, "HEAD-tracked source file is deleted from index.")
            )
            continue
        if len(records) != 1 or records[0][2] != 0:
            violations.append(
                SourceSafetyViolation(relative, "Index contains unresolved merge-stage entries.")
            )
            continue

        mode, object_id, _ = records[0]
        if mode not in {"100644", "100755"}:
            violations.append(
                SourceSafetyViolation(relative, "Index entry is not a regular source file.")
            )
            continue
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", object_id) or set(object_id) == {"0"}:
            violations.append(
                SourceSafetyViolation(relative, "Index entry has no reviewable staged blob.")
            )
        else:
            index_contexts[relative] = object_id

        path = resolved_root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink():
            violations.append(
                SourceSafetyViolation(relative, "Worktree: symbolic links are not allowed.")
            )
            continue
        if not path.exists():
            violations.append(
                SourceSafetyViolation(relative, "Tracked source file is missing from working tree.")
            )
            continue
        if not path.is_file():
            violations.append(
                SourceSafetyViolation(relative, "Worktree: source entry is not a regular file.")
            )
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            violations.append(
                SourceSafetyViolation(relative, f"Worktree: could not inspect file: {exc}")
            )
            continue
        if size > MAX_SOURCE_FILE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Worktree: file exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit.",
                )
            )
            continue
        if worktree_bytes + size > MAX_SOURCE_TREE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Worktree source exceeds the {MAX_SOURCE_TREE_BYTES}-byte source limit.",
                )
            )
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append(
                SourceSafetyViolation(relative, f"Worktree: could not read file: {exc}")
            )
            continue
        worktree_data[relative] = data
        worktree_bytes += len(data)

    sizes = _git_blob_sizes(resolved_root, list(index_contexts.values()))
    accepted_index_paths: list[str] = []
    accepted_object_ids: list[str] = []
    index_bytes = 0
    for relative, object_id in sorted(index_contexts.items()):
        size = sizes[object_id]
        if size > MAX_SOURCE_FILE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Index: file exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit.",
                )
            )
            continue
        if index_bytes + size > MAX_SOURCE_TREE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Index source exceeds the {MAX_SOURCE_TREE_BYTES}-byte source limit.",
                )
            )
            continue
        accepted_index_paths.append(relative)
        accepted_object_ids.append(object_id)
        index_bytes += size

    blobs = _git_blob_contents(resolved_root, accepted_object_ids)
    file_count = 0
    source_bytes = 0
    for relative in accepted_index_paths:
        object_id = index_contexts[relative]
        index_data = blobs[object_id]
        file_count += 1
        source_bytes += len(index_data)
        try:
            validate_source_content(relative, index_data)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, f"Index: {exc}"))

        current_data = worktree_data.pop(relative, None)
        if current_data is not None and current_data != index_data:
            file_count += 1
            source_bytes += len(current_data)
            try:
                validate_source_content(relative, current_data)
            except SourceSafetyError as exc:
                violations.append(SourceSafetyViolation(relative, f"Worktree: {exc}"))

    # A malformed/intent-to-add index entry can still have a worktree copy worth
    # checking even though no staged blob could be read.
    for relative, current_data in sorted(worktree_data.items()):
        file_count += 1
        source_bytes += len(current_data)
        try:
            validate_source_content(relative, current_data)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, f"Worktree: {exc}"))

    return SourceSafetyReport(
        root=resolved_root,
        scope="tracked index and worktree source",
        file_count=file_count,
        source_bytes=source_bytes,
        violations=tuple(violations),
    )


def _extracted_tree_paths(
    root: Path,
) -> tuple[list[str], list[SourceSafetyViolation]]:
    paths: list[str] = []
    violations: list[SourceSafetyViolation] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for directory, dirnames, filenames in walker:
            directory_path = Path(directory)
            retained_dirs: list[str] = []
            for dirname in sorted(dirnames):
                path = directory_path / dirname
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    violations.append(
                        SourceSafetyViolation(relative, "Symbolic-link directory is not allowed.")
                    )
                    continue
                try:
                    validate_source_path(relative)
                except SourceSafetyError as exc:
                    violations.append(SourceSafetyViolation(relative, str(exc)))
                    continue
                retained_dirs.append(dirname)
            dirnames[:] = retained_dirs
            paths.extend(
                (directory_path / filename).relative_to(root).as_posix()
                for filename in sorted(filenames)
            )
    except OSError as exc:
        raise SourceSafetyError(f"Could not enumerate downloaded repository tree: {exc}") from exc
    return paths, violations


def scan_source_tree(
    root: Path,
    *,
    use_git: bool | None = None,
) -> SourceSafetyReport:
    """Scan a Git checkout or extracted tree without modifying it.

    ``use_git=None`` selects committed ``HEAD`` when ``root/.git`` exists and
    refuses tracked divergence; otherwise every downloaded-tree entry is scanned.
    Set ``use_git`` explicitly when a caller has established the source context.
    """

    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise SourceSafetyError(f"Source root is not a directory: {resolved_root}")

    git_mode = _has_git_metadata(resolved_root) if use_git is None else use_git
    if git_mode:
        if not _has_git_metadata(resolved_root):
            raise SourceSafetyError("Git mode requested but the source root has no .git metadata.")
        return _scan_committed_git_tree(resolved_root)

    violations: list[SourceSafetyViolation] = []
    relative_paths, directory_violations = _extracted_tree_paths(resolved_root)
    violations.extend(directory_violations)
    scope = "downloaded repository tree"

    file_count = 0
    source_bytes = 0
    casefold_paths: dict[str, str] = {}
    for relative in relative_paths:
        folded = relative.casefold()
        prior = casefold_paths.get(folded)
        if prior is not None and prior != relative:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Case-insensitive path collision with {prior}.",
                )
            )
            continue
        casefold_paths[folded] = relative

        try:
            validate_source_path(relative)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))
            continue

        path = resolved_root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink():
            violations.append(SourceSafetyViolation(relative, "Symbolic links are not allowed."))
            continue
        if not path.exists():
            violations.append(SourceSafetyViolation(relative, "Tracked source file is missing."))
            continue
        if not path.is_file():
            violations.append(SourceSafetyViolation(relative, "Source entry is not a regular file."))
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            violations.append(SourceSafetyViolation(relative, f"Could not inspect file: {exc}"))
            continue
        if size > MAX_SOURCE_FILE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"File exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit.",
                )
            )
            continue
        if source_bytes + size > MAX_SOURCE_TREE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    relative,
                    f"Source tree exceeds the {MAX_SOURCE_TREE_BYTES}-byte source limit.",
                )
            )
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))
            continue
        file_count += 1
        source_bytes += len(data)
        try:
            validate_source_content(relative, data)
        except SourceSafetyError as exc:
            violations.append(SourceSafetyViolation(relative, str(exc)))

    if not relative_paths and not violations:
        violations.append(SourceSafetyViolation(".", "Source tree contains no files."))

    return SourceSafetyReport(
        root=resolved_root,
        scope=scope,
        file_count=file_count,
        source_bytes=source_bytes,
        violations=tuple(violations),
    )


def _resolve_history_commits(root: Path, refs: Sequence[str] | None) -> tuple[list[str], list[str]]:
    requested_refs: list[str]
    if refs:
        requested_refs = list(dict.fromkeys(refs))
    else:
        result = _run_git(
            root,
            ["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"],
            text=True,
        )
        requested_refs = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        if not requested_refs:
            requested_refs = ["HEAD"]

    commit_tips: list[str] = []
    for ref in requested_refs:
        if not ref or ref.startswith("-") or any(character in ref for character in "\r\n\x00"):
            raise SourceSafetyError(f"Unsafe history ref: {ref!r}")
        if ref != "HEAD":
            if not ref.startswith(("refs/heads/", "refs/tags/")):
                raise SourceSafetyError(f"History ref must be an exact branch or tag ref: {ref}")
            try:
                _run_git(root, ["check-ref-format", ref])
                _run_git(root, ["show-ref", "--verify", "--hash", ref])
            except SourceSafetyError as exc:
                raise SourceSafetyError(f"History ref is not an exact existing ref: {ref}") from exc
        try:
            resolved = _run_git(
                root,
                ["rev-parse", "--verify", f"{ref}^{{commit}}"],
                text=True,
            ).stdout.strip()
        except SourceSafetyError as exc:
            raise SourceSafetyError(f"History ref does not resolve to a commit: {ref}") from exc
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", resolved):
            raise SourceSafetyError(f"History ref did not resolve safely: {ref}")
        commit_tips.append(resolved.lower())

    result = _run_git(
        root,
        ["rev-list", "--topo-order", "--reverse", *commit_tips, "--"],
        text=True,
    )
    commits = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    if not commits:
        raise SourceSafetyError("The requested refs contain no reachable commits.")
    return requested_refs, commits


def _git_metadata_path(root: Path, relative: str) -> Path:
    raw_path = _run_git(root, ["rev-parse", "--git-path", relative], text=True).stdout.strip()
    if not raw_path or any(character in raw_path for character in "\r\n\x00"):
        raise SourceSafetyError("Git returned an unsafe metadata path.")
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _validate_git_history_storage(root: Path) -> None:
    shallow = _run_git(root, ["rev-parse", "--is-shallow-repository"], text=True).stdout.strip()
    if shallow != "false":
        raise SourceSafetyError("History scanning requires a complete, non-shallow repository.")

    replace_refs = _run_git(
        root,
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
        text=True,
    ).stdout.strip()
    if replace_refs:
        raise SourceSafetyError("Git replacement refs are prohibited for release validation.")

    for relative, label in (
        ("info/grafts", "Git grafts"),
        ("objects/info/alternates", "Git alternate object stores"),
    ):
        path = _git_metadata_path(root, relative)
        try:
            populated = path.is_file() and bool(path.read_bytes().strip())
        except OSError as exc:
            raise SourceSafetyError(f"Could not inspect {label.lower()}.") from exc
        if populated:
            raise SourceSafetyError(f"{label} are prohibited for release validation.")

    config_queries = (
        ["config", "--get", "extensions.partialClone"],
        ["config", "--get-regexp", r"^remote\..*\.promisor$"],
    )
    for args in config_queries:
        try:
            partial_clone = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                env=_safe_git_environment(),
            )
        except FileNotFoundError as exc:
            raise SourceSafetyError("Git is required for this source-safety check.") from exc
        if partial_clone.returncode not in {0, 1}:
            raise SourceSafetyError("Git could not inspect partial-clone configuration.")
        if partial_clone.returncode == 0:
            raise SourceSafetyError(
                "Partial-clone/promisor configuration is prohibited for release validation."
            )


def _reject_annotated_tag_refs(root: Path, refs: Sequence[str]) -> None:
    for ref in refs:
        object_type = _run_git(root, ["cat-file", "-t", ref], text=True).stdout.strip()
        if object_type == "tag":
            raise SourceSafetyError(
                f"Annotated tag metadata is not supported by the release scanner: {ref}"
            )


def _git_tree_entries(
    root: Path,
    commit: str,
) -> list[tuple[str, str, str, str]]:
    result = _run_git(root, ["ls-tree", "-r", "-z", "--full-tree", commit])
    entries: list[tuple[str, str, str, str]] = []
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_type, raw_object_id = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii").lower()
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSafetyError(
                f"Commit {commit[:12]} contains an unreadable tree entry."
            ) from exc
        entries.append((mode, object_type, object_id, path))
    return entries


def scan_git_history(
    root: Path,
    *,
    refs: Sequence[str] | None = None,
) -> SourceSafetyReport:
    """Scan commit identities, messages, and regular blobs reachable from public refs.

    When ``refs`` is omitted, all local branches and tags are scanned. Reflog-
    only, dangling, and otherwise unreachable objects are deliberately excluded.
    Pass exact refs to model the branches/tags intended for publication.
    """

    resolved_root = root.resolve()
    if not resolved_root.is_dir() or not _has_git_metadata(resolved_root):
        raise SourceSafetyError("History scanning requires a Git checkout at the source root.")

    _validate_git_history_storage(resolved_root)
    requested_refs, commits = _resolve_history_commits(resolved_root, refs)
    _reject_annotated_tag_refs(resolved_root, requested_refs)
    violations: list[SourceSafetyViolation] = []
    invalid_paths: set[str] = set()
    seen_path_versions: set[tuple[str, str]] = set()
    blob_contexts: dict[str, dict[str, str]] = {}

    commit_sizes = _git_commit_sizes(resolved_root, commits)
    accepted_commits: list[str] = []
    commit_bytes = 0
    for commit in commits:
        size = commit_sizes[commit]
        if size > MAX_COMMIT_OBJECT_BYTES:
            violations.append(
                SourceSafetyViolation(
                    f"commit {commit[:12]}",
                    f"Commit object exceeds the {MAX_COMMIT_OBJECT_BYTES}-byte metadata limit.",
                )
            )
            continue
        if commit_bytes + size > MAX_HISTORY_COMMIT_BYTES:
            violations.append(
                SourceSafetyViolation(
                    ".",
                    f"Commit metadata exceeds the {MAX_HISTORY_COMMIT_BYTES}-byte history limit.",
                )
            )
            break
        accepted_commits.append(commit)
        commit_bytes += size

    commit_contents = _git_commit_contents(resolved_root, accepted_commits)
    for commit in accepted_commits:
        violations.extend(_validate_commit_content(commit, commit_contents[commit]))

    for commit in commits:
        for mode, object_type, object_id, path in _git_tree_entries(resolved_root, commit):
            context = f"{path} @ {commit[:12]}"
            if object_type != "blob" or mode not in {"100644", "100755"}:
                key = (path, f"{mode}:{object_type}")
                if key not in seen_path_versions:
                    violations.append(
                        SourceSafetyViolation(
                            context,
                            "Historical entry is not a regular source file.",
                        )
                    )
                    seen_path_versions.add(key)
                continue

            version = (path, object_id)
            if version in seen_path_versions:
                continue
            seen_path_versions.add(version)
            if path not in invalid_paths:
                try:
                    validate_source_path(path)
                except SourceSafetyError as exc:
                    violations.append(SourceSafetyViolation(context, str(exc)))
                    invalid_paths.add(path)
            if path in invalid_paths:
                continue
            blob_contexts.setdefault(object_id, {}).setdefault(path, commit)

    file_count = 0
    source_bytes = 0
    object_ids = sorted(blob_contexts)
    sizes = _git_blob_sizes(resolved_root, object_ids)
    accepted_object_ids: list[str] = []
    for object_id in object_ids:
        contexts = blob_contexts[object_id]
        size = sizes[object_id]
        first_path = sorted(contexts)[0]
        first_context = f"{first_path} @ {contexts[first_path][:12]}"
        if size > MAX_SOURCE_FILE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    first_context,
                    f"Historical blob exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit.",
                )
            )
            continue
        if source_bytes + size > MAX_HISTORY_SOURCE_BYTES:
            violations.append(
                SourceSafetyViolation(
                    ".",
                    f"Reachable history exceeds the {MAX_HISTORY_SOURCE_BYTES}-byte scan limit.",
                )
            )
            break
        accepted_object_ids.append(object_id)
        source_bytes += size

    blobs = _git_blob_contents(resolved_root, accepted_object_ids)
    for object_id in accepted_object_ids:
        contexts = blob_contexts[object_id]
        data = blobs[object_id]
        file_count += 1
        for path, commit in sorted(contexts.items()):
            try:
                validate_source_content(path, data)
            except SourceSafetyError as exc:
                violations.append(
                    SourceSafetyViolation(f"{path} @ {commit[:12]}", str(exc))
                )

    scope = (
        f"reachable Git history ({len(requested_refs)} ref(s), "
        f"{len(commits)} commit(s))"
    )
    return SourceSafetyReport(
        root=resolved_root,
        scope=scope,
        file_count=file_count,
        source_bytes=source_bytes,
        violations=tuple(violations),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check a current or downloaded repository tree for source and privacy safety."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=SOURCE_ROOT,
        help="Source root to inspect (defaults to the repository containing this tool).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--git",
        dest="use_git",
        action="store_true",
        help=(
            "Scan committed HEAD, fail on tracked index/worktree divergence, and ignore "
            "untracked/ignored files."
        ),
    )
    mode.add_argument(
        "--extracted",
        dest="use_git",
        action="store_false",
        help="Scan every entry as a downloaded repository tree without Git metadata.",
    )
    mode.add_argument(
        "--history",
        action="store_true",
        help="Scan regular blobs reachable from selected Git refs.",
    )
    mode.add_argument(
        "--development",
        action="store_true",
        help="Scan both staged index blobs and current copies of tracked files.",
    )
    parser.set_defaults(use_git=None)
    parser.add_argument(
        "--ref",
        action="append",
        dest="refs",
        help=(
            "Git ref to scan; repeat for multiple refs. With --history and no "
            "--ref, all local branches and tags are scanned."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.refs and not args.history:
        print("ERROR: --ref can be used only with --history.", file=sys.stderr)
        return 2
    try:
        report = (
            scan_git_history(args.root, refs=args.refs)
            if args.history
            else (
                scan_tracked_worktree(args.root)
                if args.development
                else scan_source_tree(args.root, use_git=args.use_git)
            )
        )
    except SourceSafetyError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        status = "passed" if report.ok else "failed"
        print(
            f"Source safety {status}: inspected {report.file_count} file(s) "
            f"({report.source_bytes} bytes) in the {report.scope}."
        )
        for violation in report.violations:
            print(f"- {violation.path}: {violation.message}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
