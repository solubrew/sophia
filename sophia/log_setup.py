# sophia/log_setup.py
"""File-logging bootstrap for Sophia.

Every logger in the sophia codebase comes from kahndor.logma.Logma,
which is a logging.Logger subclass that installs a single
StreamHandler at construction time. That means:

  * Log output normally goes ONLY to stdout/stderr (captured by
    docker's json-file driver — capped at 20 MB x 5 files).
  * Adding a handler to the ROOT logger propagates records from all
    Logma instances into the same file (Logma does not set
    propagate=False, so standard Logger propagation applies).

This module installs a TimedRotatingFileHandler on the root logger,
writing to ``<log_dir>/sophia.log``. Rotation is daily with a
30-day retention window — matching the LOGs/ directory convention
the operator uses for gateway logs elsewhere on this host.

Called from the container entrypoint (cluster/entrypoint.py) before
uvicorn is exec'd, and from sophia.cli.main for bare-metal runs. The
function is idempotent: calling it twice with the same directory
won't add duplicate handlers (uvicorn re-imports can trigger that).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

# Marker so a second call recognises an existing handler as ours
# instead of adding a duplicate.
_HANDLER_MARKER = "sophia_file_log_handler"

# Defaults — match the operator's LOGs convention. SOPHIA_LOG_DIR env
# var overrides; the YAML log_dir is resolved by sophia.config and
# passed in as the argument.
DEFAULT_BACKUP_COUNT = 30


def configure_file_logging(
    log_dir: Optional[str] = None,
    level: Optional[str] = None,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Optional[str]:
    """Attach a daily-rotating file handler to the root logger.

    Args:
        log_dir: Directory for sophia.log. Falls back to
            SOPHIA_LOG_DIR env var, then gives up (returns None) if
            neither is set. Created if missing.
        level: Log level for the file handler. Falls back to
            SOPHIA_LOG_LEVEL env var, then INFO.
        backup_count: How many daily files to keep (default 30).

    Returns:
        The resolved log file path on success, or None when no
        directory was configured. Failures (unwritable path, bad
        level) are swallowed but reported on stderr so a broken log
        mount never takes the gateway down — logging to stdout still
        works in that case.
    """
    resolved_dir = log_dir or os.environ.get("SOPHIA_LOG_DIR", "").strip()
    if not resolved_dir:
        return None

    resolved_level = (
        level
        or os.environ.get("SOPHIA_LOG_LEVEL", "INFO")
    ).upper()

    try:
        target = Path(resolved_dir)
        target.mkdir(parents=True, exist_ok=True)
        log_file = target / "sophia.log"

        root = logging.getLogger()

        # Idempotency guard — don't stack duplicate handlers when
        # called again (uvicorn reload, tests, double-import).
        for h in root.handlers:
            if getattr(h, _HANDLER_MARKER, False):
                return str(log_file)

        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8",
            # UTC so the rotated filename matches docker/entrypoint
            # timestamps on this host (TZ-independent).
            utc=True,
        )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            logging.Formatter(
                fmt=(
                    "%(asctime)s | %(levelname)-8s | %(name)s | "
                    "%(process)d | %(message)s"
                ),
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        try:
            handler.setLevel(resolved_level)
        except ValueError:
            handler.setLevel(logging.INFO)

        root.addHandler(handler)
        if root.level > handler.level or root.level == logging.NOTSET:
            # Don't let a low root level silently filter records the
            # file handler was asked to capture.
            root.setLevel(min(root.level, handler.level)
                          if root.level != logging.NOTSET else handler.level)

        # First line in the file helps operators correlate boots.
        logging.getLogger(__name__).info(
            "[log_setup] file logging active → %s (level=%s, rotate=daily, keep=%d)",
            log_file, resolved_level, backup_count,
        )
        return str(log_file)
    except Exception as exc:  # noqa: BLE001 — log mount failure must not kill the gateway
        import sys
        sys.stderr.write(
            f"[log_setup] WARNING: could not activate file logging at "
            f"{resolved_dir!r}: {exc!r}; stdout logging continues\n"
        )
        return None
