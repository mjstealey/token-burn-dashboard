"""OpenAI Codex CLI adapter.

Source: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl. Token usage lives on
event_msg records where payload.type == "token_count".

⚠️ Two correctness traps:
  1. payload.info.total_token_usage is a CUMULATIVE running total within the file —
     summing it across records squares the count. Use the per-turn delta in
     payload.info.last_token_usage instead.
  2. The model is NOT on the token record. It's carried on the most recent preceding
     turn_context.payload.model, which we track as we scan the file.

The file is re-parsed from the start whenever it changes (incremental=False) because
a token_count's model depends on an earlier turn_context. event_id is
codex:<session>:<ordinal> where ordinal counts token_count records in file order, so
re-parsing is idempotent via ON CONFLICT(event_id) DO NOTHING.

OpenAI accounting: last_token_usage.input_tokens is the full prompt; cached_input_tokens
is the cached portion (billed cheaper). We store uncached input in input_tokens and the
cached portion in cache_read_tokens. output_tokens already includes reasoning tokens
(billed at the output rate); reasoning_output_tokens is kept for display only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dateutil import parser as dtparser

from .base import Adapter, UsageEvent

_FILE_UUID = re.compile(r"rollout-.*-([0-9a-fA-F-]{36})\.jsonl$")


class CodexAdapter(Adapter):
    name = "codex"
    provider = "openai"
    tool = "codex-cli"
    glob = "**/rollout-*.jsonl"
    incremental = False  # a token_count depends on an earlier turn_context

    def parse(self, path: Path, from_offset: int) -> tuple[list[UsageEvent], int]:
        m = _FILE_UUID.search(path.name)
        session_id = m.group(1) if m else path.stem
        current_model: str | None = None
        ordinal = 0
        events: list[UsageEvent] = []

        with open(path, "rb") as f:
            raw = f.read()
        new_offset = len(raw)

        for seg in raw.split(b"\n"):
            if not seg.strip():
                continue
            try:
                rec = json.loads(seg)
            except (json.JSONDecodeError, ValueError):
                continue

            rtype = rec.get("type")
            payload = rec.get("payload") or {}

            if rtype == "session_meta":
                session_id = payload.get("id") or session_id
                continue
            if rtype == "turn_context":
                current_model = payload.get("model") or current_model
                continue
            if rtype != "event_msg" or payload.get("type") != "token_count":
                continue

            ordinal += 1  # increment for every token_count to keep event_id stable
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            input_total = int(last.get("input_tokens") or 0)
            cached = int(last.get("cached_input_tokens") or 0)
            output = int(last.get("output_tokens") or 0)
            reasoning = int(last.get("reasoning_output_tokens") or 0)
            if input_total == 0 and output == 0:
                continue

            ts_raw = rec.get("timestamp")
            try:
                ts = dtparser.isoparse(ts_raw) if ts_raw else None
            except (ValueError, TypeError):
                ts = None
            if ts is None:
                continue

            events.append(
                UsageEvent(
                    event_id=f"codex:{session_id}:{ordinal}",
                    provider="openai",
                    tool="codex-cli",
                    ts=ts,
                    session_id=session_id,
                    model=current_model,
                    input_tokens=max(input_total - cached, 0),
                    output_tokens=output,
                    cache_read_tokens=cached,
                    reasoning_tokens=reasoning,
                    project=payload.get("cwd"),
                )
            )
        return events, new_offset
