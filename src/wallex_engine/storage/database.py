from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..models import DecisionRecord, EquitySnapshot, Fill, OrderIntent, OrderRecord, Position


class Base(DeclarativeBase):
    pass


class DecisionRow(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    reason_fa: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)


class IntentRow(Base):
    __tablename__ = "order_intents"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)


class OrderRow(Base):
    __tablename__ = "orders"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)


class FillRow(Base):
    __tablename__ = "fills"
    fill_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[str] = mapped_column(String(64))
    price: Mapped[str] = mapped_column(String(64))
    fee_tmn: Mapped[str] = mapped_column(String(64))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class PositionRow(Base):
    __tablename__ = "positions"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[str] = mapped_column(String(64))
    average_entry: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)


class EquityRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity_tmn: Mapped[str] = mapped_column(String(64))
    drawdown: Mapped[str] = mapped_column(String(64))
    daily_pnl: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)


class KillSwitchRow(Base):
    __tablename__ = "kill_switch_state"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServiceStateRow(Base):
    __tablename__ = "service_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIRequestRow(Base):
    __tablename__ = "ai_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    fallback: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[str] = mapped_column(Text)


class SqlEventStore:
    def __init__(self, database_url: str, logs_dir: Path) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._file_lock = asyncio.Lock()
        self._leader_connection: AsyncConnection | None = None

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        if self._leader_connection is not None:
            await self._leader_connection.close()
            self._leader_connection = None
        await self.engine.dispose()

    async def acquire_leader_lock(self, lock_id: int = 742_113) -> bool:
        if self.engine.url.get_backend_name() != "postgresql":
            return True
        self._leader_connection = await self.engine.connect()
        result = await self._leader_connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}
        )
        acquired = bool(result.scalar())
        if not acquired:
            await self._leader_connection.close()
            self._leader_connection = None
        return acquired

    async def append_decision(self, record: DecisionRecord) -> None:
        payload = record.model_dump_json()
        async with self.sessions() as session:
            await session.merge(
                DecisionRow(
                    decision_id=record.decision_id,
                    run_id=record.run_id,
                    created_at=record.created_at,
                    mode=record.mode,
                    symbol=record.symbol,
                    outcome=record.outcome,
                    reason_fa=record.reason_fa,
                    payload_json=payload,
                )
            )
            await session.commit()
        async with self._file_lock:
            with (self.logs_dir / "decisions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")

    async def persist_intent(self, intent: OrderIntent) -> None:
        async with self.sessions() as session:
            await session.merge(
                IntentRow(
                    client_id=intent.client_id,
                    decision_id=intent.decision_id,
                    symbol=intent.symbol,
                    created_at=intent.created_at,
                    payload_json=intent.model_dump_json(),
                )
            )
            await session.commit()

    async def persist_order(self, order: OrderRecord) -> None:
        async with self.sessions() as session:
            await session.merge(
                OrderRow(
                    client_id=order.client_id,
                    exchange_order_id=order.exchange_order_id,
                    symbol=order.symbol,
                    status=order.status.value,
                    updated_at=order.updated_at,
                    payload_json=order.model_dump_json(),
                )
            )
            await session.commit()

    async def append_fill(self, fill: Fill) -> None:
        is_new = False
        async with self.sessions() as session:
            is_new = await session.get(FillRow, fill.fill_id) is None
            await session.merge(
                FillRow(
                    fill_id=fill.fill_id,
                    client_id=fill.client_id,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    quantity=str(fill.quantity),
                    price=str(fill.price),
                    fee_tmn=str(fill.fee_tmn),
                    filled_at=fill.filled_at,
                    payload_json=fill.model_dump_json(),
                )
            )
            await session.commit()
        if is_new:
            await self._append_trade_csv(fill)

    async def _append_trade_csv(self, fill: Fill) -> None:
        path = self.logs_dir / "trades.csv"
        async with self._file_lock:
            exists = path.exists()
            with path.open("a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "fill_id",
                        "client_id",
                        "symbol",
                        "side",
                        "quantity",
                        "price",
                        "fee_tmn",
                        "filled_at",
                    ],
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    {
                        "fill_id": fill.fill_id,
                        "client_id": fill.client_id,
                        "symbol": fill.symbol,
                        "side": fill.side.value,
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "fee_tmn": str(fill.fee_tmn),
                        "filled_at": fill.filled_at.isoformat(),
                    }
                )

    async def upsert_position(self, position: Position) -> None:
        async with self.sessions() as session:
            await session.merge(
                PositionRow(
                    symbol=position.symbol,
                    quantity=str(position.quantity),
                    average_entry=str(position.average_entry),
                    updated_at=datetime.now(tz=UTC),
                    payload_json=position.model_dump_json(),
                )
            )
            await session.commit()

    async def delete_position(self, symbol: str) -> None:
        async with self.sessions() as session:
            await session.execute(delete(PositionRow).where(PositionRow.symbol == symbol))
            await session.commit()

    async def load_positions(self) -> list[Position]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(PositionRow))).all()
        return [Position.model_validate_json(row.payload_json) for row in rows]

    async def append_equity(self, snapshot: EquitySnapshot) -> None:
        async with self.sessions() as session:
            session.add(
                EquityRow(
                    run_id=snapshot.run_id,
                    captured_at=snapshot.captured_at,
                    equity_tmn=str(snapshot.equity_tmn),
                    drawdown=str(snapshot.drawdown),
                    daily_pnl=str(snapshot.daily_pnl),
                    payload_json=snapshot.model_dump_json(),
                )
            )
            await session.commit()

    async def set_kill_switch(self, active: bool, reason: str, at: datetime) -> None:
        async with self.sessions() as session:
            await session.merge(
                KillSwitchRow(singleton_id=1, active=active, reason=reason, updated_at=at)
            )
            await session.commit()

    async def kill_switch_active(self) -> bool:
        async with self.sessions() as session:
            row = await session.get(KillSwitchRow, 1)
            return bool(row and row.active)

    async def set_service_state(self, key: str, value: str) -> None:
        async with self.sessions() as session:
            await session.merge(
                ServiceStateRow(key=key, value=value, updated_at=datetime.now(tz=UTC))
            )
            await session.commit()

    async def append_ai_call(
        self,
        provider: str,
        model: str,
        cache_key: str,
        cache_hit: bool,
        fallback: bool,
        payload: dict[str, object],
    ) -> None:
        async with self.sessions() as session:
            session.add(
                AIRequestRow(
                    created_at=datetime.now(tz=UTC),
                    provider=provider,
                    model=model,
                    cache_key=cache_key,
                    cache_hit=cache_hit,
                    fallback=fallback,
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                )
            )
            await session.commit()


def write_run_manifest(path: Path, run_id: str, mode: str, config_payload: dict[str, Any]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    manifest_path = path / f"{run_id}.json"
    if manifest_path.exists():
        raise FileExistsError(f"immutable manifest already exists: {manifest_path.name}")
    canonical = json.dumps(config_payload, sort_keys=True, default=str, ensure_ascii=False)
    payload = {
        "run_id": run_id,
        "mode": mode,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "git_commit": _git_commit(),
        "python": os.sys.version,
        "config": config_payload,
    }
    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return manifest_path


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except Exception:
        return None
