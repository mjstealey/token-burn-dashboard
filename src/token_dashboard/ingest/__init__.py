"""Pluggable ingestion of local AI-coding-tool usage logs."""

from .base import UsageEvent, Adapter, ingest_all

__all__ = ["UsageEvent", "Adapter", "ingest_all"]
