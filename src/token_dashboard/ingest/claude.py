"""Claude Code adapter.

Source: ~/.claude/projects/<slug>/**/*.jsonl  (per-session transcripts; subagent
transcripts nest under <sessionId>/subagents/). Usage lives on type=="assistant"
records under message.usage.

⚠️ Dedup by requestId. One API turn is split across multiple JSONL lines (one per
content block) and every sibling line repeats the IDENTICAL usage payload. Summing
raw lines double/triple-counts. We derive event_id from requestId so any sibling
line yields the same id, and ON CONFLICT(event_id) DO NOTHING makes re-reads (even
when a turn straddles two ingest passes) idempotent. Tailing is incremental by byte
offset, processing only newline-terminated lines.
"""

from __future__ import annotations

import json
from pathlib import Path

from dateutil import parser as dtparser

from .base import Adapter, UsageEvent


class ClaudeAdapter(Adapter):
    name = "claude"
    provider = "claude"
    tool = "claude-code"
    glob = "**/*.jsonl"
    incremental = True

    def parse(self, path: Path, from_offset: int) -> tuple[list[UsageEvent], int]:
        with open(path, "rb") as f:
            f.seek(from_offset)
            raw = f.read()

        segments = raw.split(b"\n")
        complete = segments[:-1]  # last segment is a (possibly empty) partial line
        consumed = sum(len(s) + 1 for s in complete)
        new_offset = from_offset + consumed

        events: list[UsageEvent] = []
        for seg in complete:
            if not seg.strip():
                continue
            try:
                rec = json.loads(seg)
            except (json.JSONDecodeError, ValueError):
                continue
            ev = _event_from_record(rec)
            if ev is not None:
                events.append(ev)
        return events, new_offset


def _event_from_record(rec: dict) -> UsageEvent | None:
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None

    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    if inp + out + cache_create + cache_read == 0:
        return None

    cc = usage.get("cache_creation") or {}
    c5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if c5 + c1 == 0 and cache_create:
        # No split reported; Claude Code uses 5m TTL by default.
        c5 = cache_create

    ts_raw = rec.get("timestamp")
    if not ts_raw:
        return None
    try:
        ts = dtparser.isoparse(ts_raw)
    except (ValueError, TypeError):
        return None

    # `uuid` is per JSONL line, not per API request; using it here would inflate
    # split assistant turns. Skip records without a stable request/message id.
    request_id = rec.get("requestId") or msg.get("id")
    if not request_id:
        return None

    stu = usage.get("server_tool_use") or {}
    return UsageEvent(
        event_id=f"claude:{request_id}",
        provider="claude",
        tool="claude-code",
        ts=ts,
        request_id=request_id,
        session_id=rec.get("sessionId"),
        model=msg.get("model"),
        input_tokens=inp,
        output_tokens=out,
        cache_creation_tokens=cache_create or (c5 + c1),
        cache_read_tokens=cache_read,
        cache_create_5m=c5,
        cache_create_1h=c1,
        web_search_requests=int(stu.get("web_search_requests") or 0),
        web_fetch_requests=int(stu.get("web_fetch_requests") or 0),
        service_tier=usage.get("service_tier"),
        project=rec.get("cwd"),
        git_branch=rec.get("gitBranch"),
    )
