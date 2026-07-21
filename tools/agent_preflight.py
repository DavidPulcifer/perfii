#!/usr/bin/env python3
"""Run safe, repeatable checks for a current or downloaded repository.

Quick mode is dependency-free and does not write bytecode.  It validates the
agent-facing configuration, theme, source syntax, required paths, repository
privacy policy, and (when available) Git whitespace/status state.  Full mode
adds clean schema/demo workspace checks and the regression suite beneath one
external temporary directory that is removed on exit.

The tool intentionally works both in a normal Git checkout and in a downloaded
repository tree.  In a checkout, privacy inspection covers tracked working copies
and untracked/ignored state is reported by Git.  Without Git metadata, every
file in the downloaded tree is inspected.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PATHS = (
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "agent-config/project-manifest.json",
    "agent-config/customization-profile.schema.json",
    "agent-config/customization-profile.template.json",
    "agent-config/customization-profile.pay-yourself-first.example.json",
    "agent-config/theme-profile.example.json",
    "docs/agent/README.md",
    "docs/agent/CUSTOMIZATION.md",
    "docs/agent/PLAYBOOKS.md",
    "docs/agent/DOMAIN_MAP.md",
    "docs/agent/DATA_OPERATIONS.md",
    "docs/agent/THEMING.md",
    "docs/agent/DEPLOYMENT.md",
    "tools/source_safety.py",
    "tools/customize_theme.py",
    "tools/validate_agent_config.py",
    "tools/agent_preflight.py",
    "current/app/base_schema.sql",
    "current/scripts/bootstrap_workspace.py",
    "current/scripts/doctor.py",
    "current/scripts/run_local.py",
    "current/tests",
)

COMPILE_SKIPPED_COMPONENTS = {
    ".git",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "venv",
}

MAX_REPORTED_DETAILS = 20


@dataclass(frozen=True)
class CheckResult:
    """One preflight check result suitable for human and JSON output."""

    name: str
    status: str
    summary: str
    details: tuple[str, ...] = ()
    duration_ms: int = 0

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreflightReport:
    mode: str
    repository_root: str
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return not any(check.failed for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status == "warn" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "repository_root": self.repository_root,
            "summary": {
                "passed": sum(check.status == "pass" for check in self.checks),
                "warnings": self.warning_count,
                "skipped": sum(check.status == "skip" for check in self.checks),
                "failed": sum(check.status == "fail" for check in self.checks),
            },
            "checks": [check.to_dict() for check in self.checks],
        }


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = previous_dont_write
    return module


def _timed_check(
    name: str,
    check: Callable[[], tuple[str, str, Iterable[str]]],
) -> CheckResult:
    started = time.monotonic()
    try:
        status, summary, details = check()
    except Exception as exc:  # preflight must aggregate unexpected checker failures
        status = "fail"
        summary = f"{type(exc).__name__}: {exc}"
        details = ()
    duration_ms = round((time.monotonic() - started) * 1000)
    return CheckResult(
        name=name,
        status=status,
        summary=summary,
        details=tuple(details)[:MAX_REPORTED_DETAILS],
        duration_ms=duration_ms,
    )


def _check_required_paths(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    missing = [relative for relative in REQUIRED_PATHS if not (repo_root / relative).exists()]
    if missing:
        return "fail", f"{len(missing)} required agent/source path(s) are missing", missing
    return "pass", f"{len(REQUIRED_PATHS)} required agent/source paths are present", ()


def _check_agent_config(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    tool_path = repo_root / "tools" / "validate_agent_config.py"
    validator = _load_module("_finance_app_preflight_config", tool_path)
    profile_paths = list(validator.DEFAULT_PROFILES)
    local_profile = repo_root / "agent-config" / "customization-profile.local.json"
    if local_profile.is_file():
        profile_paths.append(local_profile)

    for profile_path in profile_paths:
        validator.validate_profile(
            profile_path,
            schema_path=repo_root / "agent-config" / "customization-profile.schema.json",
            repo_root=repo_root,
            require_ready=(profile_path == local_profile),
        )
    validator.validate_manifest(
        repo_root / "agent-config" / "project-manifest.json",
        repo_root=repo_root,
    )
    validator.validate_compatibility_alias(repo_root)
    local_note = " including the local requirements profile" if local_profile.is_file() else ""
    return (
        "pass",
        f"Validated {len(profile_paths)} requirements profile(s), manifest, and references{local_note}",
        (),
    )


def _check_theme(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    theme_tool = _load_module(
        "_finance_app_preflight_theme", repo_root / "tools" / "customize_theme.py"
    )
    example_profile = repo_root / "agent-config" / "theme-profile.example.json"
    generated_stylesheet = repo_root / "current" / "app" / "static" / "theme.generated.css"
    profile = theme_tool.load_and_validate(example_profile)
    rendered = theme_tool.render_stylesheet(profile)
    if not generated_stylesheet.is_file():
        return "fail", "The generated theme stylesheet is missing", (str(generated_stylesheet),)
    if generated_stylesheet.read_text(encoding="utf-8") != rendered:
        return (
            "fail",
            "The generated theme stylesheet is stale",
            ("Review the theme preview, then apply the tracked example profile.",),
        )

    local_profile = repo_root / "agent-config" / "theme-profile.local.json"
    if local_profile.is_file():
        theme_tool.load_and_validate(local_profile)
    local_note = "; local profile is also valid" if local_profile.is_file() else ""
    return "pass", f"Tracked theme and generated stylesheet agree{local_note}", ()


def _iter_python_sources(repo_root: Path) -> Iterable[Path]:
    for path in repo_root.rglob("*.py"):
        try:
            relative = path.relative_to(repo_root)
        except ValueError:  # pragma: no cover - rglob cannot normally escape its root
            continue
        if any(part.lower() in COMPILE_SKIPPED_COMPONENTS for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


def _check_python_compilation(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    failures: list[str] = []
    count = 0
    for path in sorted(_iter_python_sources(repo_root)):
        count += 1
        try:
            with tokenize.open(path) as handle:
                source = handle.read()
            compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError) as exc:
            relative = path.relative_to(repo_root).as_posix()
            failures.append(f"{relative}: {type(exc).__name__}: {exc}")
    if failures:
        return "fail", f"{len(failures)} of {count} Python source file(s) failed compilation", failures
    if count == 0:
        return "fail", "No Python source files were found", ()
    return "pass", f"Compiled {count} Python source file(s) in memory; no bytecode written", ()


def _has_git_metadata(repo_root: Path) -> bool:
    return (repo_root / ".git").exists()


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _check_privacy(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    source_safety = _load_module(
        "_finance_app_preflight_source_safety",
        repo_root / "tools" / "source_safety.py",
    )

    if not _has_git_metadata(repo_root):
        report = source_safety.scan_source_tree(repo_root, use_git=False)
        details = [
            f"{violation.path}: {violation.message}"
            if violation.path
            else violation.message
            for violation in report.violations
        ]
        if not report.ok:
            return (
                "fail",
                f"Repository privacy scan found {len(report.violations)} issue(s) in {report.scope}",
                details,
            )
        return (
            "pass",
            f"Repository privacy policy accepted {report.file_count} file(s) in {report.scope}",
            (),
        )

    committed_report = source_safety.scan_source_tree(repo_root, use_git=True)
    development_report = source_safety.scan_tracked_worktree(repo_root)
    divergence_prefix = "Tracked index/worktree source differs from committed HEAD"
    deletion_messages = {
        "Tracked source file is missing from working tree.",
        "HEAD-tracked source file is deleted from index.",
    }
    committed_divergence = [
        violation
        for violation in committed_report.violations
        if violation.message.startswith(divergence_prefix)
    ]
    committed_blockers = [
        violation
        for violation in committed_report.violations
        if violation not in committed_divergence
    ]
    development_divergence = [
        violation
        for violation in development_report.violations
        if violation.message in deletion_messages
    ]
    development_blockers = [
        violation
        for violation in development_report.violations
        if violation not in development_divergence
    ]
    blocking_details = [
        f"HEAD {violation.path}: {violation.message}"
        for violation in committed_blockers
    ] + [
        f"{violation.path}: {violation.message}"
        for violation in development_blockers
    ]
    divergence_details = [
        f"HEAD {violation.path}: {violation.message}"
        for violation in committed_divergence
    ] + [
        f"{violation.path}: {violation.message}"
        for violation in development_divergence
    ]

    if blocking_details:
        return (
            "fail",
            f"Repository privacy scan found {len(blocking_details)} unsafe finding(s) across HEAD, index, or tracked worktree",
            blocking_details + divergence_details,
        )
    if divergence_details:
        return (
            "warn",
            "Source content is safe, with tracked changes not yet committed to HEAD",
            divergence_details,
        )
    return (
        "pass",
        f"Repository privacy policy accepted committed HEAD, index, and {development_report.file_count} tracked worktree file version(s)",
        (),
    )


def _check_git(repo_root: Path) -> tuple[str, str, Iterable[str]]:
    if not _has_git_metadata(repo_root):
        return "skip", "Git metadata is absent; Git-only checks are not needed for a downloaded repository", ()

    root_result = _run_git(repo_root, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        return "fail", "Git metadata is present but the repository cannot be inspected", (
            root_result.stderr.strip() or root_result.stdout.strip(),
        )
    discovered_root = Path(root_result.stdout.strip()).resolve()
    if discovered_root != repo_root.resolve():
        return (
            "fail",
            "The preflight root does not match the Git worktree root",
            (f"preflight={repo_root.resolve()}", f"git={discovered_root}"),
        )

    whitespace_details: list[str] = []
    for args in (
        ("diff", "--check", "--no-ext-diff"),
        ("diff", "--cached", "--check", "--no-ext-diff"),
    ):
        result = _run_git(repo_root, *args)
        if result.returncode != 0:
            output = (result.stdout + "\n" + result.stderr).strip()
            whitespace_details.extend(line for line in output.splitlines() if line.strip())
    if whitespace_details:
        return "fail", "Git diff whitespace checks failed", whitespace_details

    status_result = _run_git(
        repo_root,
        "status",
        "--short",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status_result.returncode != 0:
        return "fail", "Git status could not be read", (status_result.stderr.strip(),)
    entries = [line for line in status_result.stdout.splitlines() if line.strip()]
    if entries:
        return (
            "warn",
            f"Git whitespace checks passed; worktree has {len(entries)} changed or untracked entry/entries",
            entries,
        )
    return "pass", "Git whitespace checks passed and the worktree is clean", ()


def run_quick_checks(repo_root: Path = REPO_ROOT) -> list[CheckResult]:
    """Run all dependency-free, non-writing preflight checks."""

    root = Path(repo_root).resolve()
    previous_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return [
            _timed_check("required_paths", lambda: _check_required_paths(root)),
            _timed_check("agent_config", lambda: _check_agent_config(root)),
            _timed_check("theme", lambda: _check_theme(root)),
            _timed_check("python_compile", lambda: _check_python_compilation(root)),
            _timed_check("privacy", lambda: _check_privacy(root)),
            _timed_check("git", lambda: _check_git(root)),
        ]
    finally:
        sys.dont_write_bytecode = previous_dont_write


def _command_failure_details(result: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    return tuple(lines[-MAX_REPORTED_DETAILS:])


def _run_subprocess_check(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
) -> CheckResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name=name,
            status="fail",
            summary=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    duration_ms = round((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        return CheckResult(
            name=name,
            status="fail",
            summary=f"Command exited with status {result.returncode}",
            details=_command_failure_details(result),
            duration_ms=duration_ms,
        )
    successful_output = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    lines = [line.strip() for line in successful_output.splitlines() if line.strip()]
    summary = lines[-1] if lines else "Completed successfully"
    return CheckResult(name=name, status="pass", summary=summary, duration_ms=duration_ms)


def _full_environment(temporary_root: Path) -> dict[str, str]:
    scratch = temporary_root / "temp"
    guarded_data = temporary_root / "guarded-default-data"
    scratch.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            "APP_ENV": "development",
            "SECRET_KEY": "synthetic-agent-preflight-only",
            "APP_DATA_DIR": str(guarded_data),
            "DB_PATH": str(guarded_data / "data.sqlite"),
            "META_DB_PATH": str(guarded_data / "meta.sqlite"),
            "USER_DB_DIR": str(guarded_data / "user_dbs"),
            "UPLOAD_DIR": str(guarded_data / "uploads"),
        }
    )
    return environment


def run_full_checks(repo_root: Path = REPO_ROOT) -> list[CheckResult]:
    """Run clean-start and regression checks inside one disposable external root."""

    root = Path(repo_root).resolve()
    app_root = root / "current"
    results: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="finance-agent-preflight-") as raw_temp:
        temporary_root = Path(raw_temp).resolve()
        schema_workspace = temporary_root / "schema-workspace"
        demo_workspace = temporary_root / "demo-workspace"
        environment = _full_environment(temporary_root)
        python = sys.executable

        commands: tuple[tuple[str, tuple[str, ...], int], ...] = (
            (
                "bootstrap_schema",
                (
                    python,
                    "-B",
                    "scripts/bootstrap_workspace.py",
                    "--data-dir",
                    str(schema_workspace),
                    "--profile",
                    "schema",
                    "--allow-external",
                ),
                120,
            ),
            (
                "doctor_schema",
                (
                    python,
                    "-B",
                    "scripts/doctor.py",
                    "--data-dir",
                    str(schema_workspace),
                    "--allow-external",
                ),
                120,
            ),
            (
                "bootstrap_demo",
                (
                    python,
                    "-B",
                    "scripts/bootstrap_workspace.py",
                    "--data-dir",
                    str(demo_workspace),
                    "--profile",
                    "demo",
                    "--allow-external",
                ),
                120,
            ),
            (
                "doctor_demo",
                (
                    python,
                    "-B",
                    "scripts/doctor.py",
                    "--data-dir",
                    str(demo_workspace),
                    "--allow-external",
                ),
                120,
            ),
            (
                "local_launcher",
                (
                    python,
                    "-B",
                    "scripts/run_local.py",
                    "--data-dir",
                    str(demo_workspace),
                    "--allow-external",
                    "--check-only",
                ),
                120,
            ),
            (
                "tests",
                (python, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
                900,
            ),
        )
        for name, command, timeout in commands:
            results.append(
                _run_subprocess_check(
                    name,
                    command,
                    cwd=app_root,
                    environment=environment,
                    timeout=timeout,
                )
            )
    return results


def run_preflight(repo_root: Path = REPO_ROOT, *, mode: str = "quick") -> PreflightReport:
    if mode not in {"quick", "full"}:
        raise ValueError("mode must be 'quick' or 'full'")
    root = Path(repo_root).resolve()
    checks = run_quick_checks(root)
    if mode == "full":
        checks.extend(run_full_checks(root))
    return PreflightReport(mode=mode, repository_root=str(root), checks=tuple(checks))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a current or downloaded repository without touching normal app data."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Run dependency-free checks (default).")
    mode.add_argument(
        "--full",
        action="store_true",
        help="Also run disposable bootstrap/doctor/launcher checks and the full test suite.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    return parser


def _print_human(report: PreflightReport) -> None:
    for check in report.checks:
        print(f"[{check.status.upper()}] {check.name}: {check.summary}")
        if check.status in {"fail", "warn"}:
            for detail in check.details:
                print(f"  - {detail}")
    state = "PASSED" if report.ok else "FAILED"
    warning_note = f" with {report.warning_count} warning(s)" if report.warning_count else ""
    print(f"Agent preflight {state}{warning_note} ({report.mode}).")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode = "full" if args.full else "quick"
    report = run_preflight(REPO_ROOT, mode=mode)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
