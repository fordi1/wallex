"""Run the TradingView-style terminal: python terminal.py, then open http://127.0.0.1:8600"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from wallex_engine.runtime.control import read_env_file  # noqa: E402
from wallex_engine.ui.terminal_api import create_terminal_app  # noqa: E402

if __name__ == "__main__":
    for key, value in read_env_file(ROOT / ".env").items():
        if value:
            os.environ.setdefault(key, value)
    os.chdir(ROOT)
    app = create_terminal_app(ROOT / os.getenv("CONFIG_PATH", "config.yaml"), ROOT)
    # Loopback only: this page can start live trading.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("TERMINAL_PORT", "8600")))
