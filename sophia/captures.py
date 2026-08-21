"""Prompt/completion text captures — file-based, size-capped, opt-in.

When a requestor's API key has ``store_payload=True``, Sophia writes
the full request (messages) and full response (choices) to a JSON file
under ``captures_dir`` before returning the response. Files are pruned
oldest-first when the directory exceeds the byte cap.

This is intentionally separate from the SQLite DB:

- Captures are large, opaque JSON payloads; bulking them into the DB
  would dominate query time and bloat backups.
- A separate directory makes it trivial to ship captures to a
  long-term store (S3, NFS) by symlinking, and to apply per-tenant
  retention rules (different requestors → different sub-dirs).
- The DB row holds a ``captured_path`` so reports can link back.

Layout::

    SOPHIA_CAPTURES_DIR/YYYY-MM-DD/<requestor>/<completion_id>.json

A single capture file::

    {
      "requestor": "hermes-cli",
      "requestor_id": <int>,
      "model": "anthropic/claude-sonnet-4.5",
      "strategy": "least_used_weighted",
      "provider_name": "anthropic_alice",
      "created_at": "2026-08-21T...",
      "request": { "messages": [...], "temperature": 0.7, ... },
      "response": { "id": "...", "choices": [...], "usage": {...} }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_CAPTURE_DIR = Path(__file__).parent / "_data_" / "captures"
# Default cap: 1 GiB. Override with SOPHIA_CAPTURE_MAX_BYTES.
DEFAULT_MAX_BYTES = 1 << 30


@dataclass
class CaptureConfig:
    base_dir: Path
    max_bytes: int

    @classmethod
    def from_env(cls) -> "CaptureConfig":
        d = Path(os.environ.get("SOPHIA_CAPTURES_DIR", str(DEFAULT_CAPTURE_DIR)))
        m = int(os.environ.get("SOPHIA_CAPTURE_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
        d.mkdir(parents=True, exist_ok=True)
        return cls(base_dir=d, max_bytes=m)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaptureStore:
    """File-based capture store. NOT thread-safe — sophia is single-process."""

    def __init__(self, config: Optional[CaptureConfig] = None) -> None:
        self.config = config or CaptureConfig.from_env()

    def write(
        self,
        *,
        completion_id: str,
        requestor: str,
        requestor_id: int,
        model: str,
        strategy: Optional[str],
        provider_name: str,
        request_payload: dict,
        response_payload: dict,
    ) -> Optional[Path]:
        """Write a capture JSON file. Returns the path written, or None
        on failure (capture failure never breaks a request — log and move on)."""
        try:
            day = _utc_now_iso()[:10]
            sub = self.config.base_dir / day / requestor
            sub.mkdir(parents=True, exist_ok=True)
            path = sub / f"{completion_id}.json"
            blob = {
                "requestor": requestor,
                "requestor_id": requestor_id,
                "completion_id": completion_id,
                "model": model,
                "strategy": strategy,
                "provider_name": provider_name,
                "created_at": _utc_now_iso(),
                "request": request_payload,
                "response": response_payload,
            }
            # Atomic write — write to .tmp then rename so a half-written
            # capture never replaces a good one.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(blob, indent=2, ensure_ascii=False))
            tmp.rename(path)
            self._enforce_cap()
            return path
        except Exception as exc:  # noqa: BLE001 — capture must not block a request
            import logging
            logging.getLogger(__name__).warning("capture write failed: %s", exc)
            return None

    def _enforce_cap(self) -> int:
        """Walk the capture tree oldest-first; delete until we're under the cap.
        Returns number of bytes reclaimed."""
        try:
            total = sum(p.stat().st_size for p in self.config.base_dir.rglob("*") if p.is_file())
            if total <= self.config.max_bytes:
                return 0
            # Oldest mtime first. We sort by date-dir then filename.
            candidates = sorted(
                (p for p in self.config.base_dir.rglob("*.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            reclaimed = 0
            for p in candidates:
                if total - reclaimed <= self.config.max_bytes:
                    break
                size = p.stat().st_size
                p.unlink(missing_ok=True)
                reclaimed += size
            # Clean up empty date dirs left behind
            for d in sorted(self.config.base_dir.iterdir(), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            return reclaimed
        except Exception:
            return 0

    def list(self, requestor: Optional[str] = None) -> list[dict]:
        results: list[dict] = []
        for p in self.config.base_dir.rglob("*.json"):
            rel = p.relative_to(self.config.base_dir)
            parts = rel.parts  # (YYYY-MM-DD, requestor, filename)
            r = parts[1] if len(parts) >= 3 else ""
            if requestor and r != requestor:
                continue
            st = p.stat()
            results.append({
                "path": str(p),
                "requestor": r,
                "day": parts[0] if len(parts) >= 3 else "",
                "completion_id": p.stem,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
        results.sort(key=lambda d: d["mtime"], reverse=True)
        return results

    def stats(self) -> dict:
        files = [p for p in self.config.base_dir.rglob("*.json") if p.is_file()]
        total_bytes = sum(p.stat().st_size for p in files)
        return {
            "files": len(files),
            "total_bytes": total_bytes,
            "max_bytes": self.config.max_bytes,
            "pct_full": round(100 * total_bytes / self.config.max_bytes, 1) if self.config.max_bytes else 0,
            "base_dir": str(self.config.base_dir),
        }
