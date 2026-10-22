from __future__ import annotations

import sys

import pytest

from wallex_engine.config import AppConfig
from wallex_engine.runtime.control import EngineController, preflight


def sleeper(mode: str) -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def test_preflight_explains_closed_gates() -> None:
    reasons = preflight(AppConfig(), "live", env={})
    assert any("allowed_markets" in reason for reason in reasons)
    assert any("WALLEX_API_KEY" in reason for reason in reasons)


def test_preflight_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        preflight(AppConfig(), "turbo", env={})


def test_controller_runs_one_engine_at_a_time(tmp_path) -> None:
    controller = EngineController(tmp_path, command=sleeper)
    assert controller.status() is None
    started = controller.start("shadow", env={})
    try:
        assert controller.status() == started
        assert started.mode == "shadow"
        with pytest.raises(RuntimeError, match="already running"):
            controller.start("live", env={})
    finally:
        controller.stop()
    assert controller.status() is None


def test_env_file_is_parsed_without_exporting(tmp_path) -> None:
    from wallex_engine.runtime.control import read_env_file

    (tmp_path / ".env").write_text("# comment\nA=1\nB = two \nEMPTY=\n", encoding="utf-8")
    assert read_env_file(tmp_path / ".env") == {"A": "1", "B": "two", "EMPTY": ""}
    assert read_env_file(tmp_path / "missing.env") == {}
