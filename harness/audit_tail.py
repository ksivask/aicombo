"""Consume AGW governance logs via docker logs subprocess.

AGW's tracing subscriber emits governance events in one of two shapes:

  (A) JSON-per-line (when RUST_LOG_FORMAT=json is honored):
      {"timestamp":"…","target":"agentgateway::governance","fields":{…}}

  (B) Structured-text (AGW's default fmt layer; what we actually see in
      practice for this cidgar build):
      2026-04-22T06:06:09.533010Z  info  governance
      timestamp=… phase="llm_request" cid=Some("ib_…")
      backend=…  trace_id=None  body={…json…}

Correlation: cidgar's governance log does NOT include HTTP headers, so
we cannot demux directly by X-Harness-Trial-ID. Instead, audit_tail
buffers the last N governance entries with captured_at timestamps, and
callers (the runner at trial-completion) claim entries in their
[started_at, finished_at] window. This is lossy under concurrency, but
Plan A is serial (MAX_CONCURRENT_TRIALS=1) so it's sufficient.

If MAX_CONCURRENT_TRIALS > 1 at some point, either:
- cidgar needs to include request headers in governance log, OR
- we add a stateful correlation layer (trial currently-running flag on
  top of time-window).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Callable

log = logging.getLogger("aiplay.audit_tail")

# Regex for shape (B). Captures the key=value pairs we care about.
_STRUCTURED_RE = re.compile(
    r"""
    ^(?P<ts>\S+)\s+                          # ISO timestamp
    (?P<level>\w+)\s+                        # log level
    governance\s+                            # target literal
    (?P<kv>.*)$                              # rest: key=value pairs
    """,
    re.VERBOSE,
)
# Extract embedded body={...} JSON; body is the last field so greedy match works.
_BODY_RE = re.compile(r"body=(\{.*\})\s*$")
# Simpler field extractors for cid + phase + backend
_PHASE_RE = re.compile(r'phase="([^"]+)"')
_CID_RE = re.compile(r'cid=(?:Some\(")?([^")\s]+)(?:"\))?')
_BACKEND_RE = re.compile(r'backend=(\S+)')


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Return a normalized governance entry or None if the line isn't relevant.

    Handles both JSON-per-line AND AGW's default structured-text format.
    """
    if not line or not line.strip():
        return None

    # Shape (A): JSON-per-line
    if line.lstrip().startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if obj.get("target") != "agentgateway::governance":
            return None
        fields = obj.get("fields", {}) or {}
        # Try to pull trial_id from body.headers (future-proofing)
        body = _safe_body(fields.get("body"))
        trial_id, turn_id = _extract_correlation(body)
        return {
            "timestamp": obj.get("timestamp"),
            "phase": fields.get("phase"),
            "cid": fields.get("cid"),
            "backend": fields.get("backend"),
            "body": body,
            "trial_id": trial_id,
            "turn_id": turn_id,
            "raw": obj,
            "captured_at": time.time(),
        }

    # Shape (B): structured text with 'governance' as target
    if "governance" not in line:
        return None
    m = _STRUCTURED_RE.match(line)
    if not m:
        return None
    kv = m.group("kv")
    phase_m = _PHASE_RE.search(kv)
    cid_m = _CID_RE.search(kv)
    backend_m = _BACKEND_RE.search(kv)
    body_m = _BODY_RE.search(kv)
    cid = cid_m.group(1) if cid_m else None
    if cid in ("None", "null"):
        cid = None
    body = _safe_body(body_m.group(1)) if body_m else None
    trial_id, turn_id = _extract_correlation(body)
    return {
        "timestamp": m.group("ts"),
        "phase": phase_m.group(1) if phase_m else None,
        "cid": cid,
        "backend": backend_m.group(1) if backend_m else None,
        "body": body,
        "trial_id": trial_id,
        "turn_id": turn_id,
        "raw": {"line": line},
        "captured_at": time.time(),
    }


def _extract_correlation(body: Any) -> tuple[str | None, str | None]:
    """Try to pull X-Harness-Trial-ID + Turn-ID from governance body.headers.

    In v1 cidgar this will not be set — the governance log doesn't capture
    request headers. Returns (None, None) in that case, and callers fall
    back to time-window correlation."""
    if not isinstance(body, dict):
        return None, None
    headers = body.get("headers", {}) or {}
    if not isinstance(headers, dict):
        return None, None
    tid = (headers.get("X-Harness-Trial-ID")
           or headers.get("x-harness-trial-id"))
    nid = (headers.get("X-Harness-Turn-ID")
           or headers.get("x-harness-turn-id"))
    return tid, nid


def _safe_body(body: Any) -> Any:
    """If body is a JSON string, parse it; else return as-is."""
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body
    return body


def line_matches_trial(entry: dict[str, Any], trial_id: str) -> bool:
    """Header-based matcher (preferred when headers are in log).
    Tests use this; production also uses entries_since as fallback."""
    return entry is not None and entry.get("trial_id") == trial_id


class AuditTail:
    """Background task tailing AGW container logs.

    Exposes two APIs for consumers:
      - `buffer` + `entries_since(ts)` for time-window correlation (Plan A default)
      - `subscribe(trial_id, callback)` for header-based demux (future — when
        cidgar emits headers in governance log)

    Container-name resolution: tries the configured name first, then falls
    back to any running container whose name contains 'agentgateway'. This
    handles the docker-compose prefix (e.g. `aiplay-agentgateway-1`)."""

    BUFFER_SIZE = 500  # most recent governance entries

    def __init__(self, container_name: str = "agentgateway"):
        self.configured_name = container_name
        self.resolved_name: str | None = None
        self.buffer: deque[dict[str, Any]] = deque(maxlen=self.BUFFER_SIZE)
        self.subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._task: asyncio.Task | None = None

    # ── Time-window API (Plan A) ──
    def entries_since(self, ts: float) -> list[dict[str, Any]]:
        """Return all buffered entries with captured_at >= ts."""
        return [e for e in self.buffer if e.get("captured_at", 0) >= ts]

    # ── Header-demux API (future — currently only fires for JSON-format lines
    #    that include headers) ──
    def subscribe(self, trial_id: str, callback: Callable[[dict], None]) -> None:
        self.subscribers.setdefault(trial_id, []).append(callback)

    def unsubscribe(self, trial_id: str) -> None:
        self.subscribers.pop(trial_id, None)

    async def _resolve_container(self) -> str | None:
        if await self._container_exists(self.configured_name):
            return self.configured_name
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "--format", "{{.Names}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        for name in out.decode("utf-8", errors="replace").splitlines():
            name = name.strip()
            if "agentgateway" in name:
                log.info("audit_tail resolved container: %s (configured: %s)",
                         name, self.configured_name)
                return name
        return None

    async def _container_exists(self, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        return rc == 0

    async def run(self) -> None:
        while True:
            try:
                self.resolved_name = await self._resolve_container()
                if not self.resolved_name:
                    log.warning("audit_tail: no AGW container found; retrying in 5s")
                    await asyncio.sleep(5)
                    continue

                log.info("audit_tail: tailing %s", self.resolved_name)
                proc = await asyncio.create_subprocess_exec(
                    "docker", "logs", "-f", "--tail", "0", self.resolved_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                async for line in self._stream_lines(proc.stdout):
                    entry = parse_log_line(line)
                    if entry is None:
                        continue
                    self.buffer.append(entry)
                    # Header-demux path (fires only if body.headers carried IDs)
                    tid = entry.get("trial_id")
                    if tid and tid in self.subscribers:
                        for cb in self.subscribers[tid]:
                            try:
                                cb(entry)
                            except Exception:
                                log.exception("audit_tail subscriber cb failed")
                await proc.wait()
            except Exception:
                log.exception("audit_tail loop error; restarting in 2s")
                await asyncio.sleep(2)

    async def _stream_lines(self, reader) -> AsyncIterator[str]:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            yield raw.decode("utf-8", errors="replace").strip()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())
