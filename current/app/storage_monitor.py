from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] = {
    "checked_at": 0.0,
    "repo_root": None,
    "threshold": None,
    "value": None,
}


def get_snapshot_storage_alert(app) -> dict[str, Any] | None:
    if not app.config.get("SNAPSHOT_ALERT_ENABLED"):
        return None

    threshold_bytes = int(app.config.get("SNAPSHOT_ALERT_THRESHOLD_BYTES") or 0)
    if threshold_bytes <= 0:
        return None

    repo_root = Path(app.config.get("SNAPSHOT_REPO_ROOT")).resolve()
    cache_seconds = max(0, int(app.config.get("SNAPSHOT_ALERT_CACHE_SECONDS") or 0))
    now = time.time()

    if (
        _CACHE["repo_root"] == str(repo_root)
        and _CACHE["threshold"] == threshold_bytes
        and (now - float(_CACHE["checked_at"])) < cache_seconds
    ):
        return _CACHE["value"]

    git_bytes = _path_size(repo_root / ".git")
    snapshot_bytes = _path_size(repo_root / "snapshot-data")
    total_bytes = git_bytes + snapshot_bytes

    value = None
    if total_bytes >= threshold_bytes:
        value = {
            "total_bytes": total_bytes,
            "threshold_bytes": threshold_bytes,
            "git_bytes": git_bytes,
            "snapshot_bytes": snapshot_bytes,
            "total_human": _format_bytes(total_bytes),
            "threshold_human": _format_bytes(threshold_bytes),
            "git_human": _format_bytes(git_bytes),
            "snapshot_human": _format_bytes(snapshot_bytes),
        }

    _CACHE.update(
        {
            "checked_at": now,
            "repo_root": str(repo_root),
            "threshold": threshold_bytes,
            "value": value,
        }
    )
    return value



def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total



def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(num_bytes, 0))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{num_bytes} B"
