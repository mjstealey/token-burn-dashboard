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

        pc = client.get("/api/punchcard").json()["punchcard"]
        assert len(pc) == 1 and {"dow", "hour", "cost", "tokens", "events"} <= set(
            pc[0]
        )
        assert client.get("/api/punchcard?days=1").json()["punchcard"] == []

        # Heat-map click-to-filter params.
        assert (
            "claude"
            in client.get("/api/heatmap?model=claude-opus-4-8").json()["providers"]
        )
        assert all(
            row["tokens"] == 0
            for row in client.get("/api/heatmap?model=nope").json()["combined"]
        )
        assert "claude" in client.get("/api/heatmap?project=/proj").json()["providers"]

        # CSV export: header + the seeded event; range/filter params scope it.
        ex = client.get("/api/export.csv")
        assert ex.status_code == 200
        assert ex.headers["content-type"].startswith("text/csv")
        assert "attachment" in ex.headers["content-disposition"]
        lines = ex.text.strip().splitlines()
        assert lines[0].startswith("ts,provider,tool,model,project")
        assert len(lines) == 2 and "claude-opus-4-8" in lines[1]
        assert len(client.get("/api/export.csv?days=1").text.strip().splitlines()) == 1
        assert (
            len(client.get("/api/export.csv?model=nope").text.strip().splitlines()) == 1
        )


def test_invalid_query_parameters_return_422(tmp_path):
    with TestClient(_isolated_app(tmp_path)) as client:
        for path in [
            "/api/heatmap?days=0",
            "/api/heatmap?days=3661",
            "/api/heatmap?metric=invalid",
            "/api/models?days=-1",
            "/api/projects?limit=0",
            "/api/sessions?limit=1001",
            "/api/turns?limit=-2",
            "/api/export.csv?days=-1",
            "/api/punchcard?days=9999999999",
        ]:
            assert client.get(path).status_code == 422, path


def test_failed_file_reported_without_losing_healthy_files(tmp_path):
    app = _isolated_app(tmp_path)
    write_jsonl(
        tmp_path / "claude" / "bad.jsonl",
        [{"type": "assistant", "message": {"usage": {"input_tokens": "bad"}}}],
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        report = health["last_ingest"]["claude"]
        assert report["files_failed"] == 1
        assert report["events_inserted"] == 1
        assert report["errors"][0]["type"] == "ValueError"
        (tmp_path / "claude" / "bad.jsonl").unlink()
        assert client.post("/api/ingest").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"
