"""Enabled adapters. Add a new tool by appending its Adapter subclass here."""

from __future__ import annotations

from .base import Adapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: list[Adapter] = [
    ClaudeAdapter(),
    CodexAdapter(),
]


def adapters() -> list[Adapter]:
    return ADAPTERS
