from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wallex_engine.app import build_ai_service  # noqa: E402
from wallex_engine.config import RuntimeSecrets, load_config, redact_secrets  # noqa: E402
from wallex_engine.models import SignalAction  # noqa: E402
from wallex_engine.ui.market_explorer import (  # noqa: E402
    fetch_active_tmn_markets,
    fetch_market_view,
)


async def run_canary() -> None:
    config = load_config(ROOT / "config.yaml")
    secrets = RuntimeSecrets.from_environment()
    try:
        markets = await fetch_active_tmn_markets(config)
        symbols = {market.symbol for market in markets}
        symbol = "USDTTMN" if "USDTTMN" in symbols else markets[0].symbol
        view = await fetch_market_view(config, symbol)
        service = build_ai_service(config, secrets)
        regime = await service.provider.review_regime(view.feature)
        replayed_regime = await service.provider.review_regime(view.feature)
        if replayed_regime != regime:
            raise RuntimeError("cached regime replay did not match the original decision")
        review = (
            await service.provider.review_signal(view.feature, view.signal)
            if view.signal.action == SignalAction.ENTER_LONG
            else None
        )
        metadata = getattr(service.provider, "last_call", {})
        result = {
            "ok": True,
            "provider": getattr(service.provider, "name", "unknown"),
            "model": config.ai.model,
            "symbol": symbol,
            "market_count": len(markets),
            "signal": view.signal.action.value,
            "regime": regime.model_dump(mode="json"),
            "review": review.model_dump(mode="json") if review else None,
            "cache_hit": bool(metadata.get("cache_hit", False)),
            "replay_equal": replayed_regime == regime,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(2)
    except Exception as exc:
        safe = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        print(json.dumps({"ok": False, "error": safe}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(run_canary())
