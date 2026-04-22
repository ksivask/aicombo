"""Consume AGW JSON logs via docker logs subprocess; demux by X-Harness-Trial-ID."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Callable

log = logging.getLogger("aiplay.audit_tail")


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse a single AGW JSON log line; return None if not a governance entry."""
    if not line or not line.strip():
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    target = obj.get("target", "")
    if target != "agentgateway::governance":
        return None

    fields = obj.get("fields", {}) or {}

    # Parse body field (JSON string with headers + payload)
    body_str = fields.get("body", "")
    trial_id = None
    turn_id = None
    if isinstance(body_str, str) and body_str:
        try:
            body_obj = json.loads(body_str)
            headers = body_obj.get("headers", {}) or {}
            trial_id = headers.get("X-Harness-Trial-ID") or headers.get("x-harness-trial-id")
            turn_id = headers.get("X-Harness-Turn-ID") or headers.get("x-harness-turn-id")
        except json.JSONDecodeError:
            pass
    elif isinstance(body_str, dict):
        # Defensive: AGW may emit body as a dict directly
        headers = body_str.get("headers", {}) or {}
        trial_id = headers.get("X-Harness-Trial-ID")
        turn_id = headers.get("X-Harness-Turn-ID")

    return {
        "timestamp": obj.get("timestamp"),
        "target": target,
        "phase": fields.get("phase"),
        "cid": fields.get("cid"),
        "backend": fields.get("backend"),
        "trace_id": fields.get("trace_id"),
        "trial_id": trial_id,
        "turn_id": turn_id,
        "raw": obj,
    }


def line_matches_trial(entry: dict[str, Any], trial_id: str) -> bool:
    """Whether an entry's trial_id matches the given trial_id."""
    return entry is not None and entry.get("trial_id") == trial_id


class AuditTail:
    """Background task that tails Docker logs for the AGW container."""

    def __init__(self, container_name: str = "agentgateway"):
        self.container_name = container_name
        self.subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._task: asyncio.Task | None = None

    def subscribe(self, trial_id: str, callback: Callable[[dict], None]) -> None:
        self.subscribers.setdefault(trial_id, []).append(callback)

    def unsubscribe(self, trial_id: str) -> None:
        self.subscribers.pop(trial_id, None)

    async def run(self) -> None:
        """Run the tail loop. Uses `docker logs -f <container>` via subprocess."""
        cmd = ["docker", "logs", "-f", "--tail", "0", self.container_name]
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,  # AGW logs to stderr; merge
                )
                async for line in self._stream_lines(proc.stdout):
                    entry = parse_log_line(line)
                    if entry is None:
                        continue
                    tid = entry.get("trial_id")
                    if tid and tid in self.subscribers:
                        for cb in self.subscribers[tid]:
                            try:
                                cb(entry)
                            except Exception:
                                log.exception("audit_tail subscriber callback failed")
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
