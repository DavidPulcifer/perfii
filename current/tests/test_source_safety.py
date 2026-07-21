from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.source_safety import (  # noqa: E402
    SourceSafetyError,
    _safe_git_environment,
    main,
    scan_git_history,
    scan_source_tree,
    scan_tracked_worktree,
    validate_source_content,
    validate_source_path,
)


class SourceSafetyTests(TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _git_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "source-safety@example.invalid")
        self._git(repo, "config", "user.name", "Source Safety Test")
        self._git(repo, "config", "core.autocrlf", "false")
        (repo / "README.md").write_text("# Synthetic source\n", encoding="utf-8")
        source = repo / "src" / "app.py"
        source.parent.mkdir()
        source.write_text("print('synthetic app')\n", encoding="utf-8")
        self._git(repo, "add", "--", ".")
        self._git(repo, "commit", "-q", "-m", "Synthetic source")
        return repo

    def test_path_policy_accepts_source_and_placeholder_environment_example(self) -> None:
        validate_source_path("current/app/services/example.py")
        validate_source_path("current/.env.example")

    def test_path_policy_rejects_runtime_data_and_financial_exports(self) -> None:
        with self.assertRaisesRegex(SourceSafetyError, "runtime/generated"):
            validate_source_path("current/app/user_dbs/person/ledger.txt")
        with self.assertRaisesRegex(SourceSafetyError, "financial-data extension"):
            validate_source_path("sample/transactions.csv")
        with self.assertRaisesRegex(SourceSafetyError, "owner-local profile"):
            validate_source_path("agent-config/customization-profile.local.json")
        with self.assertRaisesRegex(SourceSafetyError, "runtime/generated"):
            validate_source_path("current/instance/config.py")

    def test_content_policy_rejects_private_key_and_credential_url(self) -> None:
        private_key = ("-----" + "BEGIN PRIVATE KEY-----\nfictional\n").encode()
        with self.assertRaisesRegex(SourceSafetyError, "Private-key material"):
            validate_source_content("config.txt", private_key)

        for header in (
            "BEGIN ENCRYPTED PRIVATE KEY",
            "BEGIN PGP PRIVATE KEY BLOCK",
        ):
            with self.subTest(header=header):
                key_material = ("-----" + header + "-----\nfictional\n").encode()
                with self.assertRaisesRegex(SourceSafetyError, "Private-key material"):
                    validate_source_content("config.txt", key_material)

        credential_url = ("https://" + "agent:actual-password@example.invalid/private").encode()
        with self.assertRaisesRegex(SourceSafetyError, "Credential-bearing URL"):
            validate_source_content("config.txt", credential_url)

    def test_content_policy_rejects_binary_and_nonplaceholder_env_secret(self) -> None:
        with self.assertRaisesRegex(SourceSafetyError, "Binary file"):
            validate_source_content("image.dat", b"source\x00binary")
        with self.assertRaisesRegex(SourceSafetyError, "Non-placeholder APP_SECRET_TOKEN"):
            validate_source_content(
                "config/.env.example",
                b"APP_SECRET_TOKEN=actual-sensitive-value-12345\n",
            )

        private_noreply = ("personal-user" + "@users.noreply.github.com").encode()
        with self.assertRaisesRegex(SourceSafetyError, "Non-example email"):
            validate_source_content("README.md", private_noreply)

    def test_content_policy_rejects_exact_home_paths_and_spaces(self) -> None:
        unsafe_paths = (
            ("Windows", "C:" + r"\Users\Actual Owner"),
            ("Unix", "/" + "home/actual-owner"),
        )
        for platform, value in unsafe_paths:
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(SourceSafetyError, f"Personal {platform} home path"):
                    validate_source_content("notes.txt", value.encode("utf-8"))

    def test_safe_git_environment_removes_repository_and_config_overrides(self) -> None:
        injected = {
            "GIT_DIR": "elsewhere",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.injected.promisor",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
        }
        with patch.dict(os.environ, injected, clear=False):
            environment = _safe_git_environment()

        for name in injected:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

    def test_downloaded_tree_scan_checks_every_entry_and_reports_violations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            root = Path(raw_temp)
            (root / "README.md").write_text("Synthetic source\n", encoding="utf-8")
            runtime = root / "data"
            runtime.mkdir()
            (runtime / "ledger.sqlite").write_bytes(b"SQLite format 3\x00PRIVATE")

            report = scan_source_tree(root, use_git=False)

            self.assertFalse(report.ok)
            self.assertEqual(report.scope, "downloaded repository tree")
            self.assertEqual(report.file_count, 1)
            self.assertTrue(
                any(
                    violation.path == "data" and "runtime/generated" in violation.message
                    for violation in report.violations
                )
            )

    def test_git_scan_checks_committed_head_and_ignores_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            (repo / "untracked-private.sqlite").write_bytes(b"SQLite format 3\x00PRIVATE")

            clean_report = scan_source_tree(repo)
            self.assertTrue(clean_report.ok, clean_report.to_dict())
            self.assertIn("committed Git tree at", clean_report.scope)
            self.assertEqual(clean_report.file_count, 2)

            private_email = "owner" + "@private-finance.test"
            (repo / "README.md").write_text(private_email + "\n", encoding="utf-8")
            changed_report = scan_source_tree(repo)
            self.assertFalse(changed_report.ok)
            self.assertTrue(
                any("differs from committed HEAD" in item.message for item in changed_report.violations)
            )
            self.assertFalse(
                any("Non-example email" in item.message for item in changed_report.violations)
            )

    def test_git_scan_cannot_pass_staged_unsafe_worktree_safe_divergence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            private_email = "owner" + "@private-finance.test"
            (repo / "README.md").write_text(private_email + "\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            (repo / "README.md").write_text("# Synthetic source\n", encoding="utf-8")

            report = scan_source_tree(repo)
            development_report = scan_tracked_worktree(repo)

            self.assertFalse(report.ok)
            self.assertTrue(
                any("differs from committed HEAD" in item.message for item in report.violations)
            )
            self.assertFalse(development_report.ok)
            self.assertTrue(
                any("Index: Non-example email" in item.message for item in development_report.violations)
            )

    def test_development_scan_fails_unsafe_worktree_but_allows_benign_edit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            (repo / "README.md").write_text("# Benign edited source\n", encoding="utf-8")

            benign_report = scan_tracked_worktree(repo)

            self.assertTrue(benign_report.ok, benign_report.to_dict())
            private_email = "owner" + "@private-finance.test"
            (repo / "README.md").write_text(private_email + "\n", encoding="utf-8")
            unsafe_report = scan_tracked_worktree(repo)
            self.assertFalse(unsafe_report.ok)
            self.assertTrue(
                any(
                    "Worktree: Non-example email" in item.message
                    for item in unsafe_report.violations
                )
            )

    def test_development_scan_reports_unstaged_and_staged_deletions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            (repo / "README.md").unlink()

            unstaged = scan_tracked_worktree(repo)
            self.assertTrue(
                any(
                    item.message == "Tracked source file is missing from working tree."
                    for item in unstaged.violations
                )
            )

            self._git(repo, "add", "--update", "--", "README.md")
            staged = scan_tracked_worktree(repo)
            self.assertTrue(
                any(
                    item.message == "HEAD-tracked source file is deleted from index."
                    for item in staged.violations
                )
            )

    def test_history_scan_finds_private_content_removed_from_current_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            private_email = "owner" + "@private-finance.test"
            (repo / "README.md").write_text(private_email + "\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Historical private content")
            (repo / "README.md").write_text("# Sanitized current source\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Remove private content")

            current_report = scan_source_tree(repo)
            history_report = scan_git_history(repo)

            self.assertTrue(current_report.ok, current_report.to_dict())
            self.assertFalse(history_report.ok)
            self.assertIn("reachable Git history", history_report.scope)
            self.assertTrue(
                any("Non-example email" in item.message for item in history_report.violations)
            )

    def test_history_scan_honors_explicit_refs_and_ignores_unreachable_objects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            self._git(repo, "branch", "reviewed")
            private_email = "owner" + "@private-finance.test"
            (repo / "README.md").write_text(private_email + "\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Private branch content")

            reviewed = scan_git_history(repo, refs=["refs/heads/reviewed"])
            all_heads = scan_git_history(repo)

            self.assertTrue(reviewed.ok, reviewed.to_dict())
            self.assertFalse(all_heads.ok)

    def test_history_scan_rejects_private_commit_email(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            self._git(repo, "config", "user.email", "owner" + "@private-finance.test")
            (repo / "README.md").write_text("# Safe content, private metadata\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Safe source update")

            report = scan_git_history(repo)

            self.assertFalse(report.ok)
            self.assertTrue(
                any("email is not an example or GitHub noreply" in item.message for item in report.violations)
            )

    def test_history_scan_accepts_github_noreply_and_rejects_personal_shaped_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            self._git(
                repo,
                "config",
                "user.email",
                "12345+fixture" + "@users.noreply.github.com",
            )
            self._git(repo, "config", "user.name", "Fixture Release Bot")
            (repo / "README.md").write_text("# Public release identity\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Public release update")
            self.assertTrue(scan_git_history(repo).ok)

            self._git(repo, "config", "user.name", "Private Person")
            (repo / "README.md").write_text("# Personal-shaped identity\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Second update")
            report = scan_git_history(repo)

            self.assertFalse(report.ok)
            self.assertTrue(any("name looks personal" in item.message for item in report.violations))

    def test_history_scan_rejects_personal_path_in_commit_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            (repo / "README.md").write_text("# Safe content and metadata\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            message = "Describe files under /" + "home/actual-owner/private"
            self._git(repo, "commit", "-q", "-m", message)

            report = scan_git_history(repo)

            self.assertFalse(report.ok)
            self.assertTrue(
                any("Commit message: Personal Unix home path" in item.message for item in report.violations)
            )

    def test_history_scan_rejects_replace_refs_and_grafts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            original = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "README.md").write_text("# Replacement target\n", encoding="utf-8")
            self._git(repo, "add", "--", "README.md")
            self._git(repo, "commit", "-q", "-m", "Replacement target")
            replacement_target = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            self._git(repo, "replace", replacement_target, original)

            with self.assertRaisesRegex(SourceSafetyError, "replacement refs"):
                scan_git_history(repo, refs=["HEAD"])

            self._git(repo, "replace", "-d", replacement_target)
            grafts = repo / ".git" / "info" / "grafts"
            grafts.write_text(replacement_target + " " + original + "\n", encoding="ascii")
            with self.assertRaisesRegex(SourceSafetyError, "Git grafts"):
                scan_git_history(repo, refs=["HEAD"])

    def test_history_scan_rejects_shallow_repository_and_annotated_tag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            commit = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            shallow = repo / ".git" / "shallow"
            shallow.write_text(commit + "\n", encoding="ascii")
            with self.assertRaisesRegex(SourceSafetyError, "non-shallow"):
                scan_git_history(repo, refs=["HEAD"])

            shallow.unlink()
            self._git(repo, "tag", "-a", "reviewed", "-m", "Synthetic tag")
            with self.assertRaisesRegex(SourceSafetyError, "Annotated tag"):
                scan_git_history(repo, refs=["refs/tags/reviewed"])
            with self.assertRaisesRegex(SourceSafetyError, "exact existing ref"):
                scan_git_history(repo, refs=["refs/tags/reviewed^{}"])

    def test_history_scan_rejects_partial_clone_config_and_unsupported_commit_header(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            repo = self._git_repo(Path(raw_temp))
            self._git(repo, "config", "core.repositoryformatversion", "1")
            self._git(repo, "config", "extensions.partialClone", "origin")
            with self.assertRaisesRegex(SourceSafetyError, "Partial-clone"):
                scan_git_history(repo, refs=["HEAD"])

            self._git(repo, "config", "--unset", "extensions.partialClone")
            self._git(repo, "config", "extensions.worktreeConfig", "true")
            self._git(repo, "config", "--worktree", "remote.demo.promisor", "true")
            with self.assertRaisesRegex(SourceSafetyError, "Partial-clone"):
                scan_git_history(repo, refs=["HEAD"])

            self._git(repo, "config", "--worktree", "--unset", "remote.demo.promisor")
            self._git(repo, "config", "--unset", "extensions.worktreeConfig")
            self._git(repo, "config", "core.repositoryformatversion", "0")
            tree = self._git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
            identity = "Source Safety Test <source-safety@example.invalid> 1700000000 +0000"
            raw_commit = (
                f"tree {tree}\n"
                f"author {identity}\n"
                f"committer {identity}\n"
                "encoding UTF-8\n\n"
                "Synthetic message\n"
            )
            created = subprocess.run(
                ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                cwd=repo,
                input=raw_commit.encode("utf-8"),
                check=True,
                capture_output=True,
            ).stdout.decode("ascii").strip()
            self._git(repo, "update-ref", "refs/heads/unsafe", created)

            report = scan_git_history(repo, refs=["refs/heads/unsafe"])

            self.assertFalse(report.ok)
            self.assertTrue(any("unsupported or malformed" in item.message for item in report.violations))

    def test_report_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            root = Path(raw_temp)
            (root / "README.md").write_text("Synthetic source\n", encoding="utf-8")
            report = scan_source_tree(root, use_git=False)

            payload = json.loads(json.dumps(report.to_dict()))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["file_count"], 1)

    def test_extracted_cli_json_returns_zero_for_safe_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-safety-test-") as raw_temp:
            root = Path(raw_temp)
            (root / "README.md").write_text("Synthetic source\n", encoding="utf-8")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["--root", str(root), "--extracted", "--json"])

            self.assertEqual(exit_code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["ok"])

    def test_ref_without_history_is_a_cli_usage_error(self) -> None:
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["--ref", "refs/heads/main"])

        self.assertEqual(exit_code, 2)
        self.assertIn("--ref can be used only with --history", stderr.getvalue())
