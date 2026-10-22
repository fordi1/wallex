from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import websockets

from ..errors import ExchangeError, StaleMarketDataError
from ..models import (
    Balance,
    BookLevel,
    Candle,
    FeeSchedule,
    Fill,
    MarketSpec,
    OrderBookSnapshot,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    Side,
)


class WallexClient:
    """The only module allowed to speak Wallex HTTP or WebSocket protocols."""

    def __init__(
        self,
        rest_url: str,
        websocket_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 10,
        max_retries: int = 3,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.websocket_url = websocket_url
        self.max_retries = max_retries
        self.request_timeout_seconds = timeout_seconds
        headers = {"Accept": "application/json", "User-Agent": "wallex-ai-engine/0.1"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=self.rest_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def __aenter__(self) -> WallexClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        private: bool = False,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        if private and "X-API-Key" not in self._http.headers:
            raise ExchangeError("authenticated Wallex request refused: API key is missing")
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._http.request(method, path, params=params, json=json_body)
                if allow_not_found and response.status_code == 404:
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"temporary Wallex response {response.status_code}"
                    if attempt < self.max_retries:
                        retry_after = float(response.headers.get("Retry-After", 0) or 0)
                        delay = max(retry_after, min(8.0, 0.5 * (2**attempt)))
                        await asyncio.sleep(delay + random.uniform(0, 0.15))
                        continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise ExchangeError(str(payload.get("message") or "Wallex rejected request"))
                return payload
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = type(exc).__name__
                if attempt < self.max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    continue
            except httpx.HTTPStatusError as exc:
                raise ExchangeError(f"Wallex HTTP error {exc.response.status_code}") from exc
        raise ExchangeError(f"Wallex request failed after retries: {last_error}")

    async def get_markets(self) -> dict[str, MarketSpec]:
        payload, active_payload = await asyncio.gather(
            self._request("GET", "/v1/markets"),
            self._request("GET", "/hector/web/v1/markets"),
        )
        active_result = _unwrap(active_payload, "result")
        active_items = active_result.get("markets", []) if isinstance(active_result, dict) else []
        active_symbols = {
            str(item.get("symbol", "")).upper()
            for item in active_items
            if isinstance(item, dict) and item.get("is_spot") is True
        }
        result = _unwrap(payload, "result")
        raw_symbols = result.get("symbols", result) if isinstance(result, dict) else result
        if isinstance(raw_symbols, dict):
            items = raw_symbols.values()
        elif isinstance(raw_symbols, list):
            items = raw_symbols
        else:
            raise ExchangeError("Wallex markets response has an unknown shape")
        markets: dict[str, MarketSpec] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                symbol = str(_first(raw, "symbol", "name")).upper()
                base = str(_first(raw, "baseAsset", "base_asset", "baseCurrency"))
                quote = str(_first(raw, "quoteAsset", "quote_asset", "quoteCurrency"))
                market = MarketSpec(
                    symbol=symbol,
                    base_asset=base.upper(),
                    quote_asset=quote.upper(),
                    active=symbol in active_symbols,
                    tick_size=_precision_quantum(raw, "tickSize", "tick_size"),
                    step_size=_precision_quantum(raw, "stepSize", "step_size"),
                    min_quantity=_required_decimal(raw, "minQty", "min_quantity", "minAmount"),
                    min_notional=_required_decimal(raw, "minNotional", "min_notional", "minTotal"),
                )
            except (KeyError, ValueError, InvalidOperation):
                continue
            markets[market.symbol] = market
        return markets

    async def get_tickers(self, symbols: Sequence[str]) -> dict[str, dict[str, Decimal]]:
        """Last price and 24h stats per market; markets without a valid last price are skipped."""
        payload = await self._request("GET", "/v1/markets")
        markets = _unwrap(payload, "result").get("symbols", {})
        tickers: dict[str, dict[str, Decimal]] = {}
        for symbol in symbols:
            stats = (markets.get(symbol.upper()) or {}).get("stats") or {}
            try:
                tickers[symbol.upper()] = {
                    "last": _decimal(stats["lastPrice"]),
                    "change_24h": _decimal(stats.get("24h_ch", 0)),
                    "high_24h": _decimal(stats.get("24h_highPrice", 0)),
                    "low_24h": _decimal(stats.get("24h_lowPrice", 0)),
                    "volume_tmn": _decimal(stats.get("24h_quoteVolume", 0)),
                }
            except (KeyError, ArithmeticError, ValueError):
                continue
        return tickers

    async def get_candles(
        self, symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> list[Candle]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("history bounds must be timezone-aware")
        resolution = _resolution(timeframe_minutes)
        window = timedelta(minutes=timeframe_minutes * 900)
        cursor = start.astimezone(UTC)
        by_time: dict[datetime, Candle] = {}
        while cursor < end.astimezone(UTC):
            upper = min(cursor + window, end.astimezone(UTC))
            payload = await self._request(
                "GET",
                "/v1/udf/history",
                params={
                    "symbol": symbol.upper(),
                    "resolution": resolution,
                    "from": int(cursor.timestamp()),
                    "to": int(upper.timestamp()),
                },
            )
            if isinstance(payload, dict) and payload.get("s") in {"no_data", "error"}:
                cursor = upper
                continue
            timestamps = payload.get("t", []) if isinstance(payload, dict) else []
            opens = payload.get("o", []) if isinstance(payload, dict) else []
            highs = payload.get("h", []) if isinstance(payload, dict) else []
            lows = payload.get("l", []) if isinstance(payload, dict) else []
            closes = payload.get("c", []) if isinstance(payload, dict) else []
            volumes = payload.get("v", []) if isinstance(payload, dict) else []
            for values in zip(timestamps, opens, highs, lows, closes, volumes, strict=False):
                ts, open_, high, low, close, volume = values
                opened_at = datetime.fromtimestamp(int(ts), tz=UTC)
                by_time[opened_at] = Candle(
                    symbol=symbol.upper(),
                    timeframe_minutes=timeframe_minutes,
                    opened_at=opened_at,
                    open=_decimal(open_),
                    high=_decimal(high),
                    low=_decimal(low),
                    close=_decimal(close),
                    volume=_decimal(volume),
                    completed=opened_at + timedelta(minutes=timeframe_minutes)
                    <= datetime.now(tz=UTC),
                )
            cursor = upper
        return [by_time[key] for key in sorted(by_time)]

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        payload = await self._request(
            "GET", "/v1/depth", params={"symbol": symbol.upper(), "limit": depth}
        )
        result = _unwrap(payload, "result")
        raw_bids = result.get("bids") or result.get("bid") or []
        raw_asks = result.get("asks") or result.get("ask") or []
        bids = _book_levels(raw_bids)[:depth]
        asks = _book_levels(raw_asks)[:depth]
        if not bids or not asks:
            raise StaleMarketDataError(f"empty order book for {symbol}")
        return OrderBookSnapshot(
            symbol=symbol.upper(),
            captured_at=datetime.now(tz=UTC),
            sequence=_optional_int(result.get("sequence") or result.get("lastUpdateId")),
            bids=bids,
            asks=asks,
        )

    async def get_balances(self) -> list[Balance]:
        payload = await self._request("GET", "/v1/account/balances", private=True)
        raw = _unwrap(payload, "result")
        items = raw.get("balances", raw) if isinstance(raw, dict) else raw
        if isinstance(items, dict):
            items = [dict(value, asset=key) for key, value in items.items()]
        return [
            Balance(
                asset=str(_first(item, "asset", "symbol")).upper(),
                available=_available(item),
                locked=_decimal(_first(item, "locked", "freeze", default=0)),
            )
            for item in items or []
        ]

    async def get_fees(self, symbol: str | None = None) -> FeeSchedule:
        payload = await self._request("GET", "/v1/account/fee", private=True)
        raw = _unwrap(payload, "result")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        per_symbol = {
            key.upper(): value
            for key, value in raw.items()
            if isinstance(value, Mapping) and "makerFeeRate" in value
        }
        if per_symbol:
            # Wallex returns one fee entry per market; unknown symbols get the worst rate.
            rows = [per_symbol[symbol.upper()]] if symbol and symbol.upper() in per_symbol else []
            rows = rows or list(per_symbol.values())
            return FeeSchedule(
                maker=max(_rate(row["makerFeeRate"]) for row in rows),
                taker=max(_rate(row["takerFeeRate"]) for row in rows),
            )
        return FeeSchedule(
            maker=_rate(_first(raw, "makerFeeRate", "maker", "makerFee")),
            taker=_rate(_first(raw, "takerFeeRate", "taker", "takerFee")),
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderRecord]:
        params = {"symbol": symbol.upper()} if symbol else None
        payload = await self._request("GET", "/v1/account/openOrders", private=True, params=params)
        raw = _unwrap(payload, "result")
        return [_parse_order(item) for item in _order_items(raw)]

    async def get_order(
        self, *, order_id: str | None = None, client_id: str | None = None
    ) -> OrderRecord | None:
        if not order_id and not client_id:
            raise ValueError("order_id or client_id is required")
        lookup_id = client_id or order_id
        payload = await self._request(
            "GET",
            f"/v1/account/orders/{lookup_id}",
            private=True,
            allow_not_found=True,
        )
        return _parse_order(_unwrap(payload, "result")) if payload is not None else None

    async def get_fills(self, symbol: str | None = None) -> list[Fill]:
        params = {"symbol": symbol.upper()} if symbol else None
        payload = await self._request("GET", "/v1/account/trades", private=True, params=params)
        raw = _unwrap(payload, "result")
        items = raw.get("trades", raw) if isinstance(raw, dict) else raw
        return [_parse_fill(item) for item in items or []]

    async def create_order(self, intent: OrderIntent) -> OrderRecord:
        body: dict[str, Any] = {
            "client_id": intent.client_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "type": intent.order_type.value,
            "quantity": _wire_decimal(intent.quantity),
        }
        if intent.price is not None:
            body["price"] = _wire_decimal(intent.price)
        if intent.stop_price is not None:
            body["stop_Price"] = _wire_decimal(intent.stop_price)
        payload = await self._request("POST", "/v1/account/orders", private=True, json_body=body)
        return _parse_order(_unwrap(payload, "result"), fallback=intent)

    async def cancel_order(
        self, *, order_id: str | None = None, client_id: str | None = None
    ) -> OrderRecord:
        if not order_id and not client_id:
            raise ValueError("order_id or client_id is required")
        params = {"id": order_id} if order_id else {"clientOrderId": client_id}
        payload = await self._request("DELETE", "/v1/account/orders", private=True, params=params)
        return _parse_order(_unwrap(payload, "result"))

    async def stream_books(self, symbols: Sequence[str]) -> AsyncIterator[OrderBookSnapshot]:
        # Wallex currently documents the raw @trade channel. Each trade is used
        # as a freshness trigger for a bounded REST depth refresh. Native depth
        # messages are still parsed when the exchange emits them.
        channels = [f"{symbol.upper()}@trade" for symbol in symbols]
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.websocket_url, ping_interval=20, ping_timeout=10, close_timeout=5
                ) as socket:
                    for channel in channels:
                        await socket.send(json.dumps(["subscribe", {"channel": channel}]))
                    delay = 1.0
                    last_sequence: dict[str, int] = {}
                    last_rest_refresh: dict[str, datetime] = {}
                    async for message in socket:
                        payload = json.loads(message)
                        parsed = _parse_ws_book(payload)
                        if parsed is None:
                            symbol = _trade_symbol(payload)
                            refreshed_at = last_rest_refresh.get(symbol) if symbol else None
                            if symbol and (
                                refreshed_at is None
                                or datetime.now(tz=UTC) - refreshed_at >= timedelta(seconds=1)
                            ):
                                parsed = await self._bounded_book_refresh(symbol)
                                if parsed is None:
                                    continue
                                last_rest_refresh[symbol] = parsed.captured_at
                            else:
                                continue
                        previous = last_sequence.get(parsed.symbol)
                        if parsed.sequence is not None and previous is not None:
                            if parsed.sequence <= previous:
                                continue
                            if parsed.sequence > previous + 1:
                                refreshed = await self._bounded_book_refresh(parsed.symbol)
                                if refreshed is None:
                                    continue
                                parsed = refreshed
                        if parsed.sequence is not None:
                            last_sequence[parsed.symbol] = parsed.sequence
                        yield parsed
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay + random.uniform(0, 0.25))
                delay = min(30.0, delay * 2)

    async def _bounded_book_refresh(self, symbol: str) -> OrderBookSnapshot | None:
        try:
            return await asyncio.wait_for(
                self.get_order_book(symbol),
                timeout=min(5.0, self.request_timeout_seconds),
            )
        except (TimeoutError, ExchangeError, ValueError):
            return None


def _unwrap(payload: Any, key: str) -> Any:
    if isinstance(payload, dict) and key in payload:
        return payload[key]
    return payload


def _first(data: Mapping[str, Any], *keys: str, default: Any = ...) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    if default is not ...:
        return default
    raise KeyError(keys[0])


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _required_decimal(data: Mapping[str, Any], *keys: str) -> Decimal:
    value = _decimal(_first(data, *keys))
    if value <= 0:
        raise ValueError(f"required market constraint {keys[0]} must be positive")
    return value


def _precision_quantum(data: Mapping[str, Any], *keys: str) -> Decimal:
    """Wallex v1 returns tickSize/stepSize as decimal-place counts."""
    precision = _decimal(_first(data, *keys))
    if precision != precision.to_integral_value() or not Decimal(0) <= precision <= 18:
        raise ValueError(f"invalid precision {keys[0]}")
    return Decimal(1).scaleb(-int(precision))


def _available(item: Mapping[str, Any]) -> Decimal:
    explicit = _first(item, "free", "available", default=None)
    if explicit is not None:
        return _decimal(explicit)
    # Wallex balances report the total as "value"; the free part excludes "locked".
    total = _decimal(_first(item, "value", default=0))
    return max(Decimal("0"), total - _decimal(_first(item, "locked", "freeze", default=0)))


def _rate(value: Any) -> Decimal:
    rate = _decimal(value)
    return rate / Decimal("100") if rate > Decimal("0.1") else rate


def _wire_decimal(value: Decimal) -> str:
    return format(value, "f")


def _resolution(minutes: int) -> str:
    return {1: "1", 5: "5", 15: "15", 30: "30", 60: "60", 240: "240", 1440: "1D"}.get(
        minutes, str(minutes)
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _parse_order(raw: Mapping[str, Any], fallback: OrderIntent | None = None) -> OrderRecord:
    status_text = str(_first(raw, "status", default="UNKNOWN")).upper()
    status = {
        "NEW": OrderStatus.OPEN,
        "OPEN": OrderStatus.OPEN,
        "PARTIALLY_FILLED": OrderStatus.PARTIAL,
        "PARTIAL": OrderStatus.PARTIAL,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "CANCELLED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
    }.get(status_text, OrderStatus.UNKNOWN)
    timestamp = _first(
        raw,
        "updatedAt",
        "updated_at",
        "createdAt",
        "created_at",
        "transactTime",
        "timestamp",
        default=None,
    )
    updated_at = _parse_datetime(timestamp) if timestamp else datetime.now(tz=UTC)
    return OrderRecord(
        client_id=str(
            _first(
                raw, "clientOrderId", "client_id", default=fallback.client_id if fallback else ""
            )
        ),
        exchange_order_id=str(_first(raw, "id", "orderId", default="")) or None,
        status=status,
        symbol=str(_first(raw, "symbol", default=fallback.symbol if fallback else "")).upper(),
        side=Side(
            str(_first(raw, "side", default=fallback.side.value if fallback else "BUY")).upper()
        ),
        requested_quantity=_decimal(
            _first(raw, "origQty", "quantity", default=fallback.quantity if fallback else 0)
        ),
        filled_quantity=_decimal(_first(raw, "executedQty", "filledQuantity", default=0)),
        average_price=_optional_decimal(
            _first(raw, "executedPrice", "avgPrice", "price", default=None)
        ),
        raw_status=status_text,
        updated_at=updated_at,
    )


def _parse_fill(raw: Mapping[str, Any]) -> Fill:
    return Fill(
        fill_id=str(_first(raw, "id", "tradeId")),
        client_id=str(_first(raw, "clientOrderId", "client_id", default="")),
        symbol=str(_first(raw, "symbol")).upper(),
        side=Side(str(_first(raw, "side")).upper()),
        quantity=_decimal(_first(raw, "quantity", "qty")),
        price=_decimal(_first(raw, "price")),
        fee_quantity=_decimal(_first(raw, "fee", "commission", default=0)),
        fee_asset=str(_first(raw, "feeAsset", "commissionAsset", default="TMN")).upper(),
        fee_tmn=_decimal(_first(raw, "feeTmn", default=0)),
        filled_at=_parse_datetime(_first(raw, "timestamp", "createdAt")),
    )


def _order_items(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if not isinstance(raw, Mapping):
        return []
    if isinstance(raw.get("orders"), list):
        return [item for item in raw["orders"] if isinstance(item, Mapping)]
    items: list[Mapping[str, Any]] = []
    for value in raw.values():
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping) and "clientOrderId" in value:
            items.append(value)
    return items


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) or str(value).isdigit():
        numeric = int(value)
        if numeric > 10_000_000_000:
            numeric //= 1000
        return datetime.fromtimestamp(numeric, tz=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, "") else _decimal(value)


def _parse_ws_book(payload: Any) -> OrderBookSnapshot | None:
    if isinstance(payload, list) and len(payload) >= 2:
        payload = {"channel": str(payload[0]), "data": payload[1]}
    if not isinstance(payload, dict):
        return None
    channel = str(payload.get("channel") or payload.get("event") or "")
    data = payload.get("data") or payload.get("result") or payload
    if "depth" not in channel.lower() or not isinstance(data, dict):
        return None
    symbol = str(data.get("symbol") or channel.split("@")[0]).upper()
    raw_bids = data.get("bids") or data.get("bid") or []
    raw_asks = data.get("asks") or data.get("ask") or []
    bids = _book_levels(raw_bids)
    asks = _book_levels(raw_asks)
    if not bids or not asks:
        return None
    return OrderBookSnapshot(
        symbol=symbol,
        captured_at=datetime.now(tz=UTC),
        sequence=_optional_int(data.get("sequence") or data.get("lastUpdateId")),
        bids=bids,
        asks=asks,
    )


def _trade_symbol(payload: Any) -> str | None:
    if isinstance(payload, list) and len(payload) >= 2:
        channel = str(payload[0])
    elif isinstance(payload, dict):
        channel = str(payload.get("channel") or payload.get("event") or "")
    else:
        return None
    return channel.split("@")[0].upper() if channel.lower().endswith("@trade") else None


def _book_levels(rows: Any) -> tuple[BookLevel, ...]:
    levels: list[BookLevel] = []
    for row in rows:
        if isinstance(row, Mapping):
            price, quantity = row.get("price"), row.get("quantity")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, quantity = row[0], row[1]
        else:
            continue
        if price is not None and quantity is not None:
            levels.append(BookLevel(price=_decimal(price), quantity=_decimal(quantity)))
    return tuple(levels)
