from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wallex_engine.ui.terminal_api import create_terminal_app


class FakeController:
    def __init__(self) -> None:
        self.started: list[str] = []

    def status(self):
        return None

    def start(self, mode, env):
        self.started.append(mode)
        return type("P", (), {"pid": 1, "mode": mode, "started_at": "t", "log_path": "x.log"})()

    def stop(self) -> None:
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "challenge:\n  allowed_markets: [BTCTMN]\n"
        "app:\n  data_dir: ./data\n  logs_dir: ./logs\n  reports_dir: ./reports\n"
        "ai:\n  enabled: true\n  provider: boxapi\n  model: openai/gpt-6-luna\n",
        encoding="utf-8",
    )
    controller = FakeController()
    app = create_terminal_app(config, tmp_path, controller=controller, hosts={"testserver"})
    client = TestClient(app)
    return client, config, controller


def test_no_password_but_only_same_machine_same_page_requests(setup) -> None:
    client, _, controller = setup
    assert client.get("/api/settings").status_code == 200
    rebinding = client.get("/api/settings", headers={"Host": "evil.example"})
    assert rebinding.status_code == 403
    cross_site = client.post(
        "/api/engine/start",
        json={"mode": "shadow"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403
    assert controller.started == []
    same_page = client.post(
        "/api/engine/start", json={"mode": "shadow"}, headers={"Origin": "http://testserver"}
    )
    assert same_page.status_code in (200, 409)


def test_settings_keep_current_model_and_add_new_one(setup) -> None:
    client, config, _ = setup
    current = client.get("/api/settings").json()
    assert current["model"] == "openai/gpt-6-luna"
    assert "openai/gpt-6-luna" in current["models"]
    saved = client.post("/api/settings", json={"model": "anthropic/claude-x"})
    assert saved.status_code == 200
    assert "  model: anthropic/claude-x" in config.read_text(encoding="utf-8")
    after = client.get("/api/settings").json()
    assert after["model"] == "anthropic/claude-x"
    assert {"openai/gpt-6-luna", "anthropic/claude-x"} <= set(after["models"])
    bad = client.post("/api/settings", json={"model": "x; rm -rf /"})
    assert bad.status_code == 422


def test_live_start_needs_confirmation_and_open_gates(setup) -> None:
    client, _, controller = setup
    unconfirmed = client.post("/api/engine/start", json={"mode": "live"})
    assert unconfirmed.status_code == 400
    gated = client.post(
        "/api/engine/start", json={"mode": "live", "confirm_live": True}
    )
    assert gated.status_code == 409
    assert gated.json()["detail"]
    assert controller.started == []


def test_page_is_served(setup, tmp_path: Path) -> None:
    client, _, _ = setup
    response = client.get("/")
    assert response.status_code == 200
    assert "lightweight-charts" in response.text


def test_candles_are_aggregated_from_base_resolution() -> None:
    import pandas as pd

    from wallex_engine.ui.terminal_api import aggregate, base_resolution

    assert base_resolution(15) == 1 and base_resolution(240) == 60 and base_resolution(1440) == 1440
    start = pd.Timestamp("2026-10-23 00:00", tz="UTC")
    frame = pd.DataFrame(
        {
            "opened_at": [start + pd.Timedelta(minutes=i) for i in range(20)],
            "open": range(20), "high": [x + 1 for x in range(20)],
            "low": range(20), "close": [x + 0.5 for x in range(20)], "volume": [1.0] * 20,
        }
    )
    bars = aggregate(frame, 15)
    assert len(bars) == 2
    first, last = bars.iloc[0], bars.iloc[1]
    assert (first["open"], first["high"], first["close"], first["volume"]) == (0, 15, 14.5, 15.0)
    assert (last["open"], last["close"], last["volume"]) == (15, 19.5, 5.0)


def test_explain_rebuilds_checklist_ai_and_risk_for_a_logged_decision(setup, tmp_path, feature):
    import json

    client, _, _ = setup
    record = {
        "decision_id": "d1", "run_id": "shadow-1", "created_at": "2026-10-22T00:00:00Z",
        "mode": "shadow", "symbol": "BTCTMN", "outcome": "skipped", "reason_fa": "رد",
        "features": feature.model_dump(mode="json"),
        "signal": {"action": "ENTER_LONG"},
        "regime": {"regime": "trend", "allow_entries": False, "size_multiplier": "0",
                   "confidence": "0.8", "reason_fa": "روند نزولی"},
        "review": {"approve": False, "size_adjustment": "0", "reason_fa": "کارمزد بالاست"},
        "risk": {"approved": False, "reason_fa": "رد", "constraints": ["بررسی رژیم رد کرد."]},
    }
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "decisions.jsonl").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    body = client.get("/api/explain?symbol=BTCTMN").json()
    assert body["decision"]["mode"] == "shadow"
    assert body["rules"]["action"] == "ENTER_LONG"
    keys = {c["key"] for c in body["rules"]["checks"]}
    assert keys >= {"ema_cross", "edge_trend", "rsi_oversold"}
    assert body["rules"]["failed"]["trend"] == 0
    assert body["ai"]["regime"]["allow_entries"] is False
    assert body["risk"]["constraints"] == ["بررسی رژیم رد کرد."]


def test_risk_and_scorecard_answer_without_a_database(setup) -> None:
    client, _, _ = setup
    assert client.get("/api/risk?mode=live").json()["run_id"] is None
    assert client.get("/api/scorecard?mode=live").json()["run_id"] is None
