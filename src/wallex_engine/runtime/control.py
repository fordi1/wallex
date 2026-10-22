"""Start/stop one engine process (backtest, shadow or live) from the dashboard.

The controller never relaxes a gate: it runs ``main.py <mode>`` which applies the
same fail-closed checks as the command line. ``preflight`` only reports, in
advance, which of those checks would refuse to start.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import AppConfig, RuntimeSecrets
from ..errors import ConfigurationError

MODES = ("backtest", "shadow", "live")


@dataclass(frozen=True)
class EngineProcess:
    pid: int
    mode: str
    started_at: str
    log_path: str


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file without touching os.environ."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def preflight(config: AppConfig, mode: str, env: Mapping[str, str]) -> list[str]:
    """Return every reason the engine would refuse to start in ``mode`` (empty = ready)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    reasons: list[str] = []
    try:
        config.require_challenge_ready(mode)
    except ConfigurationError as error:
        reasons.append(str(error))
    if mode == "live":
        try:
            RuntimeSecrets.from_environment(env).require_live_readiness(config, env)
        except ConfigurationError as error:
            reasons.extend(part.strip() for part in str(error).split(";"))
    return reasons


def mode_database_url(mode: str, env: Mapping[str, str]) -> str:
    """Separate SQLite file per mode, so shadow positions never leak into live state."""
    explicit = env.get(f"DATABASE_URL_{mode.upper()}")
    if explicit:
        return explicit
    shared = env.get("DATABASE_URL", "")
    if shared and not shared.startswith("sqlite"):
        return shared  # a server database is provisioned per deployment
    return f"sqlite+aiosqlite:///./data/{mode}.db"


def default_command(mode: str) -> list[str]:
    root = Path(__file__).resolve().parents[3]
    return [sys.executable, str(root / "main.py"), mode, "--config", "config.yaml"]


class EngineController:
    def __init__(
        self,
        state_dir: Path,
        command: Callable[[str], list[str]] = default_command,
        cwd: Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "engine-process.json"
        self.command = command
        self.cwd = cwd

    def status(self) -> EngineProcess | None:
        if not self.state_file.exists():
            return None
        process = EngineProcess(**json.loads(self.state_file.read_text(encoding="utf-8")))
        if not _is_alive(process.pid):
            self.state_file.unlink(missing_ok=True)
            return None
        return process

    def start(self, mode: str, env: Mapping[str, str]) -> EngineProcess:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        running = self.status()
        if running is not None:
            raise RuntimeError(f"engine already running in {running.mode} mode")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(tz=UTC)
        log_path = self.state_dir / f"engine-{mode}-{started:%Y%m%dT%H%M%SZ}.log"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with log_path.open("ab") as log:
            child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.command(mode),
                cwd=self.cwd,
                env={
                    **os.environ,
                    **env,
                    "DATABASE_URL": mode_database_url(mode, env),
                    "PYTHONUNBUFFERED": "1",
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        process = EngineProcess(child.pid, mode, started.isoformat(), str(log_path))
        self.state_file.write_text(json.dumps(asdict(process)), encoding="utf-8")
        return process

    def stop(self) -> None:
        process = self.status()
        if process is not None:
            os.kill(process.pid, signal.SIGTERM)
            _wait_for_exit(process.pid)
        self.state_file.unlink(missing_ok=True)


def tail(path: str | Path, lines: int = 40) -> str:
    file = Path(path)
    if not file.exists():
        return ""
    return "\n".join(file.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_exit(pid: int, attempts: int = 50) -> None:
    import time

    for _ in range(attempts):
        if not _is_alive(pid):
            return
        time.sleep(0.1)
