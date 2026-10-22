from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .errors import ConfigurationError


class AppSection(BaseModel):
    environment: Literal["local", "liara"] = "local"
    timezone: str = "Asia/Tehran"
    data_dir: Path = Path("./data")
    logs_dir: Path = Path("./logs")
    reports_dir: Path = Path("./reports")


class ChallengeSection(BaseModel):
    allowed_markets: list[str] = Field(default_factory=list)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    initial_capital_tmn: Decimal = Decimal("5000000")

    @model_validator(mode="after")
    def validate_ordering(self) -> ChallengeSection:
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("challenge.starts_at must be before challenge.ends_at")
        return self


class ExchangeSection(BaseModel):
    rest_url: str = "https://api.wallex.ir"
    websocket_url: str = "wss://api.wallex.ir/ws"
    request_timeout_seconds: float = 10
    max_retries: int = 3


class StrategySection(BaseModel):
    signal_timeframe_minutes: int = 5
    trend_timeframe_minutes: int = 60
    max_spread_bps: Decimal = Decimal("25")
    volume_z_min: Decimal = Decimal("0")
    safety_factor: Decimal = Decimal("1.5")
    stop_atr: Decimal = Decimal("1.5")
    trend_target_r: Decimal = Decimal("2")
    time_stop_bars: int = 12
    # Entry is allowed while the EMA 9/21 cross is younger than this many bars (1 = same bar).
    cross_window_bars: int = Field(default=1, ge=1)
    # Enter when every indicator vote is bullish; exit when every vote turns bearish.
    confluence_enabled: bool = False


class RiskSection(BaseModel):
    risk_per_trade: Decimal = Decimal("0.01")
    max_symbol_exposure: Decimal = Decimal("0.20")
    max_total_exposure: Decimal = Decimal("0.60")
    min_tmn_reserve: Decimal = Decimal("0.40")
    max_positions: int = 3
    min_hold_minutes: int = 10
    cooldown_minutes: int = 15
    loss_cooldown_minutes: int = 60
    daily_loss_stop: Decimal = Decimal("0.03")
    halve_size_drawdown: Decimal = Decimal("0.06")
    kill_switch_drawdown: Decimal = Decimal("0.10")


class ExecutionSection(BaseModel):
    entry_timeout_seconds: int = 20
    max_reprice_attempts: int = 2
    modeled_slippage_bps: Decimal = Decimal("8")
    reconcile_interval_seconds: int = 60
    canary: bool = True
    canary_max_positions: int = 1
    canary_symbol_exposure: Decimal = Decimal("0.05")
    canary_total_exposure: Decimal = Decimal("0.15")
    canary_risk_per_trade: Decimal = Decimal("0.0025")


class AISection(BaseModel):
    enabled: bool = True
    provider: Literal["liara", "boxapi", "openai", "deterministic"] = "liara"
    model: str = "openai/gpt-6-luna"
    # Liara assigns this per service. Keep it unset in source control and supply
    # the dashboard value through config.yaml or LIARA_AI_BASE_URL.
    base_url: str | None = None
    timeout_seconds: float = 12
    temperature: float = 0
    seed: int | None = 42
    prompt_version: str = "regime-v1"
    schema_version: str = "decisions-v1"
    fallback_size_multiplier: Decimal = Decimal("0.50")


class BacktestSection(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    # Measured top-of-book spread per symbol; missing symbols use strategy.max_spread_bps.
    spread_bps: dict[str, Decimal] = Field(default_factory=dict)

    def spread_for(self, symbol: str) -> Decimal | None:
        return {k.upper(): v for k, v in self.spread_bps.items()}.get(symbol.upper())
    walk_forward_train_days: int = 60
    walk_forward_test_days: int = 14


class DashboardSection(BaseModel):
    refresh_seconds: int = 30


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: AppSection = Field(default_factory=AppSection)
    challenge: ChallengeSection = Field(default_factory=ChallengeSection)
    exchange: ExchangeSection = Field(default_factory=ExchangeSection)
    strategy: StrategySection = Field(default_factory=StrategySection)
    risk: RiskSection = Field(default_factory=RiskSection)
    execution: ExecutionSection = Field(default_factory=ExecutionSection)
    ai: AISection = Field(default_factory=AISection)
    backtest: BacktestSection = Field(default_factory=BacktestSection)
    dashboard: DashboardSection = Field(default_factory=DashboardSection)

    def modeled_spread_bps(self, symbol: str) -> Decimal:
        measured = self.backtest.spread_for(symbol)
        return self.strategy.max_spread_bps if measured is None else measured

    def require_challenge_ready(self, mode: str) -> None:
        missing: list[str] = []
        if not self.challenge.allowed_markets:
            missing.append("challenge.allowed_markets")
        # A backtest with its own explicit window does not depend on the live challenge times.
        own_window = mode == "backtest" and self.backtest.start and self.backtest.end
        if self.challenge.starts_at is None and not own_window:
            missing.append("challenge.starts_at")
        if self.challenge.ends_at is None and not own_window:
            missing.append("challenge.ends_at")
        if missing:
            fields = ", ".join(missing)
            raise ConfigurationError(
                f"{mode} is fail-closed: supply verified non-secret values for {fields}"
            )
        now = datetime.now(tz=ZoneInfo(self.app.timezone))
        for name, value in (
            ("challenge.starts_at", self.challenge.starts_at),
            ("challenge.ends_at", self.challenge.ends_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ConfigurationError(f"{name} must include an explicit timezone")
        if mode == "live" and now >= self.challenge.ends_at:
            raise ConfigurationError("live is fail-closed: challenge has already ended")

    def ensure_directories(self) -> None:
        for path in (self.app.data_dir, self.app.logs_dir, self.app.reports_dir):
            path.mkdir(parents=True, exist_ok=True)


class RuntimeSecrets(BaseModel):
    model_config = ConfigDict(extra="ignore")
    wallex_api_key: SecretStr | None = Field(default=None, repr=False)
    liara_ai_api_key: SecretStr | None = Field(default=None, repr=False)
    box_api_key: SecretStr | None = Field(default=None, repr=False)
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    database_url: SecretStr = Field(
        default=SecretStr("sqlite+aiosqlite:///./data/wallex.db"), repr=False
    )
    telegram_bot_token: SecretStr | None = Field(default=None, repr=False)
    telegram_chat_id: SecretStr | None = Field(default=None, repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> RuntimeSecrets:
        source = os.environ if env is None else env

        def secret(name: str) -> SecretStr | None:
            value = source.get(name)
            return SecretStr(value) if value else None

        return cls(
            wallex_api_key=secret("WALLEX_API_KEY"),
            liara_ai_api_key=secret("LIARA_AI_API_KEY"),
            box_api_key=secret("BOX_API_KEY"),
            openai_api_key=secret("OPENAI_API_KEY"),
            database_url=SecretStr(
                source.get("DATABASE_URL", "sqlite+aiosqlite:///./data/wallex.db")
            ),
            telegram_bot_token=secret("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=secret("TELEGRAM_CHAT_ID"),
        )

    def require_live_readiness(
        self, config: AppConfig, env: Mapping[str, str] | None = None
    ) -> None:
        required_flags = {
            "WALLEX_EXPOSED_KEY_REVOKED": "the exposed key must be revoked",
            "WALLEX_KEY_TRADE_ONLY": "the replacement key must be trade-only",
            "WALLEX_KEY_IP_RESTRICTED": "the replacement key must be IP-restricted",
            "SHADOW_GATE_APPROVED": "the 24-hour shadow gate must be approved",
            "LIVE_TRADING_ENABLED": "live trading must be explicitly enabled",
        }
        closed = [
            message for flag, message in required_flags.items() if not env_true(flag, env)
        ]
        if not config.execution.canary and not env_true("LIVE_RESTART_ACKNOWLEDGED", env):
            closed.append("full limits require a clean canary and restart acknowledgement")
        if self.wallex_api_key is None:
            closed.append("WALLEX_API_KEY is missing")
        if closed:
            raise ConfigurationError("live gate closed: " + "; ".join(closed))


def env_true(name: str, env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
        config = AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"invalid configuration in {config_path.name}: {exc}") from exc
    config.ensure_directories()
    return config


def redact_secrets(text: str, secrets: RuntimeSecrets) -> str:
    result = text
    for value in (
        secrets.wallex_api_key,
        secrets.liara_ai_api_key,
        secrets.box_api_key,
        secrets.openai_api_key,
        secrets.telegram_bot_token,
    ):
        if value and value.get_secret_value():
            result = result.replace(value.get_secret_value(), "[REDACTED]")
    return result
