import json

import token_dashboard.cli as cli


def _server_only_env(monkeypatch):
    # A valid TZ so load_config() succeeds; a known port to assert against.
    monkeypatch.delenv("TD_CONFIG", raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("TD_PORT", "8080")


def _fail_build(cfg):
    raise AssertionError("build_state must not run when the server handles the request")


def test_reprice_routes_to_running_server(monkeypatch, capsys):
    """With a server up, `reprice` POSTs /api/reprice and never opens the DB."""
    _server_only_env(monkeypatch)
    seen = {}

    def fake_post(port, path):
        seen["port"], seen["path"] = port, path
        return {"pricing": {"changed": True, "repriced": 4}}

    monkeypatch.setattr(cli, "_post_server", fake_post)
    monkeypatch.setattr("token_dashboard.main.build_state", _fail_build)

    assert cli.main(["reprice"]) == 0
    assert seen == {"port": 8080, "path": "/api/reprice"}
    assert json.loads(capsys.readouterr().out)["pricing"]["repriced"] == 4


def test_reprice_force_forwards_force_to_server(monkeypatch, capsys):
    """`reprice --force` forwards force=true on the server route."""
    _server_only_env(monkeypatch)
    seen = {}

    def fake_post(port, path):
        seen["path"] = path
        return {"ok": True}

    monkeypatch.setattr(cli, "_post_server", fake_post)
    monkeypatch.setattr("token_dashboard.main.build_state", _fail_build)

    assert cli.main(["reprice", "--force"]) == 0
    assert seen["path"] == "/api/reprice?force=true"


def test_reprice_direct_skips_server(monkeypatch, capsys):
    """`reprice --direct` bypasses the server and reprices the DB directly."""
    _server_only_env(monkeypatch)

    def no_server(*args, **kwargs):
        raise AssertionError("--direct must not contact the server")

    class _FakeDB:
        def close(self):
            pass

    class _FakeState:
        db = _FakeDB()

        def sync_pricing(self, force=False):
            return {"changed": False, "repriced": 0, "forced": force}

    monkeypatch.setattr(cli, "_post_server", no_server)
    monkeypatch.setattr("token_dashboard.main.build_state", lambda cfg: _FakeState())

    assert cli.main(["reprice", "--direct", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["pricing"]["forced"] is True


def test_reprice_lock_error_points_at_reprice_endpoint(monkeypatch, capsys):
    """No server reachable and the DB is locked -> friendly, reprice-specific message."""
    _server_only_env(monkeypatch)

    def locked(cfg):
        raise RuntimeError("Could not set lock on file ./data/token.duckdb")

    monkeypatch.setattr(cli, "_post_server", lambda port, path: None)
    monkeypatch.setattr("token_dashboard.main.build_state", locked)

    assert cli.main(["reprice"]) == 2
    assert "POST /api/reprice" in capsys.readouterr().err
