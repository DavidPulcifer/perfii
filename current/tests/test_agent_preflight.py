from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "agent_preflight.py"
TOOL_SPEC = importlib.util.spec_from_file_location("finance_agent_preflight", TOOL_PATH)
if TOOL_SPEC is None or TOOL_SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"Could not load {TOOL_PATH}")
preflight = importlib.util.module_from_spec(TOOL_SPEC)
sys.modules[TOOL_SPEC.name] = preflight
TOOL_SPEC.loader.exec_module(preflight)


class AgentPreflightTests(TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            check=True,
            text=True,
        )

    def _tracked_safety_repo(self, root: Path) -> tuple[Path, Path]:
        repo = root / "repo"
        tools = repo / "tools"
        tools.mkdir(parents=True)
        shutil.copyfile(
            REPO_ROOT / "tools" / "source_safety.py",
            tools / "source_safety.py",
        )
        source = repo / "source.txt"
        source.write_text("Synthetic safe source.\n", encoding="utf-8")
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "preflight@example.invalid")
        self._git(repo, "config", "user.name", "Preflight Test")
        self._git(repo, "add", "--", ".")
        self._git(repo, "commit", "-q", "-m", "Synthetic source")
        return repo, source

    def test_python_compilation_is_in_memory_and_reports_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-compile-") as raw_temp:
            root = Path(raw_temp)
            package = root / "source"
            package.mkdir()
            valid = package / "valid.py"
            valid.write_text("answer = 42\n", encoding="utf-8")

            status, summary, details = preflight._check_python_compilation(root)

            self.assertEqual(status, "pass", (summary, tuple(details)))
            self.assertIn("no bytecode written", summary)
            self.assertEqual(tuple(details), ())
            self.assertFalse((package / "__pycache__").exists())

            (package / "invalid.py").write_text("if True print('broken')\n", encoding="utf-8")
            status, summary, details = preflight._check_python_compilation(root)

            self.assertEqual(status, "fail")
            self.assertIn("1 of 2", summary)
            self.assertIn("invalid.py", next(iter(details)))
            self.assertFalse((package / "__pycache__").exists())

    def test_downloaded_tree_privacy_scan_rejects_prohibited_file_without_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-extracted-") as raw_temp:
            root = Path(raw_temp)
            tools = root / "tools"
            tools.mkdir()
            shutil.copyfile(
                REPO_ROOT / "tools" / "source_safety.py",
                tools / "source_safety.py",
            )
            (root / "README.md").write_text("Synthetic downloaded repository.\n", encoding="utf-8")
            (root / "records.sqlite").write_bytes(b"SQLite format 3\x00synthetic")

            status, summary, details = preflight._check_privacy(root)

            self.assertEqual(status, "fail")
            self.assertIn("downloaded repository tree", summary)
            self.assertTrue(any("records.sqlite" in detail for detail in details))

    def test_normal_downloaded_repository_tree_is_accepted_without_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-extracted-clean-") as raw_temp:
            root = Path(raw_temp)
            tools = root / "tools"
            tools.mkdir()
            shutil.copyfile(
                REPO_ROOT / "tools" / "source_safety.py",
                tools / "source_safety.py",
            )
            (root / "README.md").write_text("Synthetic downloaded repository.\n", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("print('fictional demo')\n", encoding="utf-8")

            status, summary, details = preflight._check_privacy(root)

            self.assertEqual(status, "pass")
            self.assertIn("downloaded repository tree", summary)
            self.assertEqual(tuple(details), ())
            self.assertFalse((tools / "__pycache__").exists())

    def test_git_dirty_state_is_a_warning_after_whitespace_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-git-") as raw_temp:
            repo = Path(raw_temp)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "preflight@example.invalid")
            self._git(repo, "config", "user.name", "Preflight Test")
            source = repo / "source.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", "--", "source.py")
            self._git(repo, "commit", "-q", "-m", "Synthetic source")
            source.write_text("value = 2\n", encoding="utf-8")

            status, summary, details = preflight._check_git(repo)

            self.assertEqual(status, "warn")
            self.assertIn("whitespace checks passed", summary)
            self.assertTrue(any("source.py" in detail for detail in details))

    def test_benign_dirty_tracked_change_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-benign-dirty-") as raw_temp:
            repo, source = self._tracked_safety_repo(Path(raw_temp))
            source.write_text("Synthetic safe edited source.\n", encoding="utf-8")

            status, summary, details = preflight._check_privacy(repo)

            self.assertEqual(status, "warn")
            self.assertIn("not yet committed", summary)
            self.assertTrue(any("differs from committed HEAD" in detail for detail in details))

    def test_dirty_tracked_private_email_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-unsafe-dirty-") as raw_temp:
            repo, source = self._tracked_safety_repo(Path(raw_temp))
            private_email = "person" + "@" + "private.testcorp.com"
            source.write_text(f"Contact: {private_email}\n", encoding="utf-8")

            status, summary, details = preflight._check_privacy(repo)

            self.assertEqual(status, "fail")
            self.assertIn("unsafe finding", summary)
            self.assertTrue(
                any("Worktree: Non-example email address" in detail for detail in details)
            )

    def test_staged_unsafe_content_fails_even_when_worktree_is_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-unsafe-index-") as raw_temp:
            repo, source = self._tracked_safety_repo(Path(raw_temp))
            private_email = "person" + "@" + "private.testcorp.com"
            source.write_text(f"Contact: {private_email}\n", encoding="utf-8")
            self._git(repo, "add", "--", "source.txt")
            source.write_text("Synthetic safe worktree replacement.\n", encoding="utf-8")

            status, summary, details = preflight._check_privacy(repo)

            self.assertEqual(status, "fail")
            self.assertIn("unsafe finding", summary)
            self.assertTrue(
                any("Index: Non-example email address" in detail for detail in details)
            )

    def test_git_check_is_skipped_for_a_downloaded_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-preflight-no-git-") as raw_temp:
            status, summary, details = preflight._check_git(Path(raw_temp))

        self.assertEqual(status, "skip")
        self.assertIn("downloaded repository", summary)
        self.assertEqual(tuple(details), ())

    def test_full_checks_keep_every_workspace_and_default_path_external_and_temporary(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(
            name: str,
            command: tuple[str, ...],
            *,
            cwd: Path,
            environment: dict[str, str],
            timeout: int,
        ) -> preflight.CheckResult:
            calls.append(
                {
                    "name": name,
                    "command": tuple(command),
                    "cwd": cwd,
                    "environment": dict(environment),
                    "timeout": timeout,
                }
            )
            return preflight.CheckResult(name, "pass", "synthetic success")

        with tempfile.TemporaryDirectory(prefix="agent-preflight-repo-") as raw_repo:
            repo = Path(raw_repo)
            (repo / "current").mkdir()
            canonical_repo = repo.resolve()
            with mock.patch.object(preflight, "_run_subprocess_check", side_effect=fake_run):
                results = preflight.run_full_checks(repo)

            self.assertEqual(
                [result.name for result in results],
                [
                    "bootstrap_schema",
                    "doctor_schema",
                    "bootstrap_demo",
                    "doctor_demo",
                    "local_launcher",
                    "tests",
                ],
            )
            self.assertEqual(len(calls), 6)
            temporary_roots: set[Path] = set()
            for call in calls:
                self.assertEqual(call["cwd"], canonical_repo / "current")
                environment = call["environment"]
                self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
                guarded = Path(environment["APP_DATA_DIR"]).resolve()
                self.assertNotEqual(guarded, canonical_repo)
                self.assertNotIn(canonical_repo, guarded.parents)
                temporary_roots.add(guarded.parent)
                command = call["command"]
                if "--data-dir" in command:
                    data_dir = Path(command[command.index("--data-dir") + 1]).resolve()
                    self.assertNotIn(canonical_repo, data_dir.parents)
                    self.assertIn("--allow-external", command)

            self.assertEqual(len(temporary_roots), 1)
            self.assertFalse(next(iter(temporary_roots)).exists())

    def test_json_mode_emits_machine_readable_report_and_failure_exit(self) -> None:
        report = preflight.PreflightReport(
            mode="quick",
            repository_root="synthetic-root",
            checks=(preflight.CheckResult("synthetic", "fail", "expected failure"),),
        )
        output = io.StringIO()
        with mock.patch.object(preflight, "run_preflight", return_value=report):
            with contextlib.redirect_stdout(output):
                exit_code = preflight.main(["--quick", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"]["failed"], 1)
