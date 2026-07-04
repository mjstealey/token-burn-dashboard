from pathlib import Path

from fastapi.testclient import TestClient

from token_dashboard.config import Config
from token_dashboard.main import build_state, create_app

from conftest import REPO_ROOT, write_jsonl


def _isolated_app(tmp_path: Path):
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    write_jsonl(
        claude_root / "s.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "u1",
                "requestId": "rq1",
                "sessionId": "s1",
                "timestamp": "2026-06-18T12:00:00Z",
                "cwd": "/proj",
                "message": {
                    "model": "claude-opus-4-8",
                    "id": "m1",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ],
    )
    cfg = Config(
        db_path=":memory:",
        pricing_path=str(REPO_ROOT / "pricing.yaml"),
        scan_roots={"claude": str(claude_root), "codex": str(tmp_path / "nope")},
        ingest_interval_min=999,
    )
    return create_app(build_state(cfg))


def test_endpoints_serve(tmp_path):
    app = _isolated_app(tmp_path)
    with TestClient(app) as client:  # runs lifespan -> boot ingest over the temp root
        assert client.get("/").status_code == 200
        assert "Token" in client.get("/").text

        h = client.get("/api/health").json()
        assert h["status"] == "ok"

        s = client.get("/api/summary").json()
        assert s["all_time"]["events"] == 1
        assert s["all_time"]["tokens"] == 150
        assert "prev_7d" in s and "prev_30d" in s
        assert s["unpriced_models"] == []  # opus is priced

        hm = client.get("/api/heatmap?days=90&metric=cost").json()
        assert hm["metric"] == "cost"
        assert "combined" in hm and "providers" in hm
        assert "claude" in hm["providers"]  # only Claude was seeded

        m = client.get("/api/models").json()
        assert m["models"]
        assert "cache_savings" in m

        # The seeded event is dated 2026-06-18; a tight window must exclude it
        # while the unbounded call still returns it.
        assert client.get("/api/models?days=1").json()["models"] == []
        assert client.get("/api/sessions?days=1").json()["sessions"] == []
        assert client.get("/api/turns?days=1").json()["turns"] == []
        assert client.get("/api/projects?days=1").json()["projects"] == []
        assert client.get("/api/turns").json()["turns"]

        assert "block_5h" in client.get("/api/burn").json()
        assert "unpriced_models" in client.get("/api/health").json()
