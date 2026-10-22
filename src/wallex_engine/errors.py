class EngineError(Exception):
    """Base error safe to surface after secret redaction."""


class ConfigurationError(EngineError):
    """Configuration is invalid or a safety gate is closed."""


class ExchangeError(EngineError):
    """Wallex request failed."""


class ReconciliationError(EngineError):
    """Local and exchange state cannot be reconciled."""


class StaleMarketDataError(EngineError):
    """Market data is not fresh enough to trade."""
