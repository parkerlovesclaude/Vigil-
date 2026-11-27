"""Durable local state, written atomically after every change that matters.

The state file is the system's memory across restarts: open position, every
order it has ever intended (with status and applied fills), idempotency keys,
halt status and daily risk counters. It is written with write-to-temp, fsync,
rename, so a crash leaves either the old or the new file, never a torn one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

STATE_VERSION = 1
MAX_KEYS = 1000


@dataclass
class Position:
    qty: Decimal
    entry_price: Decimal           # average fill price of the entry
    cost_basis: Decimal            # quote spent incl. fees
    stop_price: Decimal
    highest_close: Decimal
    entry_client_order_id: str
    opened_ms: int
    exit_pending: bool = False     # an exit decision exists that is not fully executed
    exit_reason: str | None = None
    exit_attempts: int = 0


@dataclass
class OrderRecord:
    client_order_id: str
    evaluation_id: str
    purpose: str                   # "ENTER" or "EXIT"
    symbol: str
    side: str
    qty: Decimal
    limit_price: Decimal
    status: str
    created_ms: int
    updated_ms: int
    exchange_order_id: str | None = None
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    applied_trade_ids: list = field(default_factory=list)
    stop_price: Decimal | None = None   # for ENTER: the stop to adopt once filled
    ref_price: Decimal | None = None    # signal reference price (bar close or bid at decision)
    error: str | None = None


@dataclass
class Halt:
    active: bool = False
    category: str = ""   # "risk" (exits still allowed) or "integrity" (nothing allowed)
    reason: str = ""
    since_ms: int = 0


@dataclass
class State:
    version: int = STATE_VERSION
    position: Position | None = None
    orders: dict = field(default_factory=dict)        # client_order_id -> OrderRecord
    processed_keys: list = field(default_factory=list)
    halt: Halt = field(default_factory=Halt)
    day: str = ""
    day_start_equity: Decimal | None = None
    realized_pnl_today: Decimal = Decimal(0)
    orders_today: int = 0
    peak_equity: Decimal | None = None
    consecutive_errors: int = 0
    last_evaluated_bar_ms: int = 0
    last_exit_bar_ms: int = 0
    invalid_data_since_ms: int = 0
    needs_reconcile: bool = True
    last_reconciled_ms: int = 0

    # ---- helpers -------------------------------------------------------
    def has_key(self, key: str) -> bool:
        return key in self.processed_keys

    def add_key(self, key: str) -> None:
        self.processed_keys.append(key)
        if len(self.processed_keys) > MAX_KEYS:
            del self.processed_keys[: len(self.processed_keys) - MAX_KEYS]

    def unresolved_orders(self) -> list[OrderRecord]:
        from .models import OrderStatus
        return [o for o in self.orders.values() if not OrderStatus(o.status).is_terminal]


def _enc(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj))


_DEC_FIELDS = {
    "Position": {"qty", "entry_price", "cost_basis", "stop_price", "highest_close"},
    "OrderRecord": {"qty", "limit_price", "filled_qty", "avg_price", "stop_price", "ref_price"},
    "State": {"day_start_equity", "realized_pnl_today", "peak_equity"},
}


def _dec(d: dict, kind: str) -> dict:
    out = dict(d)
    for k in _DEC_FIELDS[kind]:
        if out.get(k) is not None:
            out[k] = Decimal(out[k])
    return out


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        raw = json.loads(self.path.read_text())
        if raw.get("version") != STATE_VERSION:
            raise RuntimeError(f"unsupported state version {raw.get('version')}")
        st = State(**_dec({k: v for k, v in raw.items()
                           if k not in ("position", "orders", "halt")}, "State"))
        st.position = Position(**_dec(raw["position"], "Position")) if raw.get("position") else None
        st.orders = {k: OrderRecord(**_dec(v, "OrderRecord")) for k, v in raw.get("orders", {}).items()}
        st.halt = Halt(**raw.get("halt", {}))
        return st

    def save(self, st: State) -> None:
        atomic_write(self.path, json.dumps(asdict(st), default=_enc, indent=1, sort_keys=True))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
