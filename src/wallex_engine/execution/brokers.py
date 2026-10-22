from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from ..errors import ExchangeError, ReconciliationError
from ..models import (
    Fill,
    MarketSpec,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from ..protocols import EventStore, ExchangeClient


def client_order_id(decision_id: str, attempt: int, purpose: str = "entry") -> str:
    digest = hashlib.sha256(f"{decision_id}|{attempt}|{purpose}".encode()).hexdigest()[:24]
    return f"wai-{digest}"


class LiveBroker:
    def __init__(self, client: ExchangeClient, store: EventStore) -> None:
        self.client = client
        self.store = store
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        lock = self._symbol_locks.setdefault(intent.symbol, asyncio.Lock())
        async with lock:
            await self.store.persist_intent(intent)
            existing = await self.client.get_order(client_id=intent.client_id)
            if existing is not None:
                await self.store.persist_order(existing)
                return existing
            await self._assert_no_opposing_order(intent)
            try:
                order = await self.client.create_order(intent)
            except Exception as exc:
                reconciled = await self.client.get_order(client_id=intent.client_id)
                if reconciled is None:
                    raise ExchangeError(
                        "ambiguous order submission could not be reconciled by client ID"
                    ) from exc
                order = reconciled
            await self.store.persist_order(order)
            return order

    async def cancel(self, order: OrderRecord) -> OrderRecord:
        canceled = await self.client.cancel_order(
            order_id=order.exchange_order_id, client_id=order.client_id
        )
        await self.store.persist_order(canceled)
        return canceled

    async def reconcile(self) -> bool:
        positions = await self.store.load_positions()
        balances = {balance.asset: balance for balance in await self.client.get_balances()}
        markets = await self.client.get_markets()
        open_orders = await self.client.get_open_orders()
        sides_by_symbol: dict[str, set[Side]] = {}
        for order in open_orders:
            sides_by_symbol.setdefault(order.symbol, set()).add(order.side)
        if any(len(sides) > 1 for sides in sides_by_symbol.values()):
            raise ReconciliationError("simultaneous opposing exchange orders detected")
        for position in positions:
            spec = markets.get(position.symbol)
            if spec is None:
                raise ReconciliationError(f"market metadata missing for {position.symbol}")
            balance = balances.get(spec.base_asset)
            exchange_quantity = (balance.available + balance.locked) if balance else Decimal(0)
            if abs(exchange_quantity - position.quantity) >= spec.step_size:
                raise ReconciliationError(f"position drift for {position.symbol}")
            sell_orders = [
                order
                for order in open_orders
                if order.symbol == position.symbol and order.side == Side.SELL
            ]
            protected_quantity = sum(
                (order.requested_quantity - order.filled_quantity for order in sell_orders),
                Decimal(0),
            )
            if protected_quantity + spec.step_size < position.quantity:
                raise ReconciliationError(
                    f"exchange-side protection missing or undersized for {position.symbol}"
                )
        for fill in await self.client.get_fills():
            valued = fill.model_copy(
                update={"fee_tmn": await fee_value_tmn(fill, markets, self.client)}
            )
            await self.store.append_fill(valued)
        return True

    async def install_protection(
        self, decision_id: str, position: Position, market: MarketSpec, attempt: int = 0
    ) -> OrderRecord:
        if position.stop_price is None or position.quantity <= 0:
            raise ValueError("filled position and stop price are required")
        limit_price = quantize_price(position.stop_price * Decimal("0.995"), market.tick_size)
        intent = OrderIntent(
            decision_id=decision_id,
            client_id=client_order_id(decision_id, attempt, "protection"),
            attempt=attempt,
            symbol=position.symbol,
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            quantity=position.quantity,
            price=limit_price,
            stop_price=quantize_price(position.stop_price, market.tick_size),
            reduce_only=True,
            created_at=datetime.now(tz=UTC),
        )
        return await self.submit(intent)

    async def _assert_no_opposing_order(self, intent: OrderIntent) -> None:
        open_orders = await self.client.get_open_orders(intent.symbol)
        if any(order.side != intent.side for order in open_orders):
            raise ReconciliationError(
                f"opposing open order exists for {intent.symbol}; new order refused"
            )


class ShadowBroker:
    def __init__(self, store: EventStore, maker_fee: Decimal = Decimal("0")) -> None:
        self.store = store
        self.maker_fee = maker_fee
        self.orders: dict[str, OrderRecord] = {}

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        await self.store.persist_intent(intent)
        price = intent.price or Decimal(0)
        order = OrderRecord(
            client_id=intent.client_id,
            exchange_order_id=f"shadow-{intent.client_id}",
            status=OrderStatus.FILLED,
            symbol=intent.symbol,
            side=intent.side,
            requested_quantity=intent.quantity,
            filled_quantity=intent.quantity,
            average_price=price,
            raw_status="SHADOW_FILLED",
            updated_at=intent.created_at,
        )
        self.orders[intent.client_id] = order
        await self.store.persist_order(order)
        fill = Fill(
            fill_id=f"shadow-fill-{intent.client_id}",
            client_id=intent.client_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=price,
            fee_quantity=intent.quantity * price * self.maker_fee,
            fee_asset="TMN",
            fee_tmn=intent.quantity * price * self.maker_fee,
            filled_at=intent.created_at,
        )
        await self.store.append_fill(fill)
        return order

    async def cancel(self, order: OrderRecord) -> OrderRecord:
        canceled = order.model_copy(
            update={"status": OrderStatus.CANCELED, "updated_at": datetime.now(tz=UTC)}
        )
        self.orders[order.client_id] = canceled
        await self.store.persist_order(canceled)
        return canceled

    async def reconcile(self) -> bool:
        return True


class BacktestBroker(ShadowBroker):
    """A deterministic simulated broker; the engine supplies next-bar prices."""


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    return (value // tick_size) * tick_size


async def fee_value_tmn(
    fill: Fill,
    markets: dict[str, MarketSpec],
    client: ExchangeClient,
) -> Decimal:
    """Conservatively value the exact fee asset at fill-time executable prices."""

    if fill.fee_tmn > 0:
        return fill.fee_tmn
    if fill.fee_quantity == 0:
        return Decimal(0)
    if fill.fee_asset == "TMN":
        return fill.fee_quantity
    traded_market = markets.get(fill.symbol)
    if traded_market and fill.fee_asset == traded_market.base_asset:
        return fill.fee_quantity * fill.price
    conversion = markets.get(f"{fill.fee_asset}TMN")
    if conversion is None or not conversion.active:
        raise ReconciliationError(f"cannot value fee asset {fill.fee_asset} in TMN")
    book = await client.get_order_book(conversion.symbol)
    return fill.fee_quantity * book.best_bid
