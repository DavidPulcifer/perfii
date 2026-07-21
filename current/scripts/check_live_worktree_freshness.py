#!/usr/bin/env python3
"""Fail fast when a source worktree is older than the live finance app tree.

Use this before copying files from a worktree into the live project.  It catches
the dangerous case where a stale worktree lacks newer live features and a whole
file copy would silently roll them back.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_LIVE_REF = "main"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_stdout(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def repo_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Path(result.stdout.strip()).resolve()


def worktree_dirty(repo: Path) -> bool:
    return bool(git_stdout(repo, "status", "--porcelain"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a source worktree is not stale relative to live main."
    )
    parser.add_argument(
        "--source",
        default=".",
        help="source worktree to validate before deploying/copying from it",
    )
    parser.add_argument(
        "--live-repo",
        required=True,
        help="explicit live finance app Git root to compare against",
    )
    parser.add_argument(
        "--live-ref",
        default=DEFAULT_LIVE_REF,
        help=f"live branch/ref to require as ancestor (default: {DEFAULT_LIVE_REF})",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="also fail if the source worktree has uncommitted changes",
    )
    args = parser.parse_args()

    try:
        source = repo_root(Path(args.source).resolve())
        live_repo = repo_root(Path(args.live_repo).resolve())
        live_head = git_stdout(live_repo, "rev-parse", args.live_ref)
        source_head = git_stdout(source, "rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr or str(exc))
        return 2

    ancestor = git(source, "merge-base", "--is-ancestor", live_head, source_head, check=False)
    if ancestor.returncode != 0:
        sys.stderr.write(
            "STALE WORKTREE: refusing to treat this source as deployable.\n"
            f"  source:   {source}\n"
            f"  source HEAD: {source_head}\n"
            f"  live repo: {live_repo}\n"
            f"  live {args.live_ref}: {live_head}\n\n"
            "Live main is not an ancestor of the source worktree. Create a fresh "
            "worktree from live main or rebase/merge live main before copying files.\n"
        )
        return 1

    if args.require_clean and worktree_dirty(source):
        sys.stderr.write(
            "DIRTY WORKTREE: source has uncommitted changes and --require-clean was set.\n"
            f"  source: {source}\n"
        )
        return 1

    dirty_note = " (dirty)" if worktree_dirty(source) else ""
    print(
        "OK: source includes live ref.\n"
        f"  source: {source}{dirty_note}\n"
        f"  live {args.live_ref}: {live_head}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
