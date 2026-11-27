"""Append-only, hash-chained JSON Lines audit log.

Every line is one self-contained JSON record:

  {"schema": 1, "seq": 42, "record_id": "<uuid4>", "ts": "2026-11-27T01:00:05.123Z",
   "ts_ms": 1795..., "run_id": "<uuid4>", "mode": "paper", "event": "evaluation",
   "symbol": "BTCUSDT", "evaluation_id": "...", "client_order_id": null,
   "data": {...}, "prev_hash": "<sha256 of previous line's hash>", "hash": "<sha256>"}

`hash` = sha256(prev_hash + canonical JSON of the record without `hash`).
Editing or deleting any line breaks the chain, which `verify_chain` detects.
Each write is flushed and fsynced before the action it describes proceeds.
Decimals are written as strings so no precision is lost.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Callable

from . import AUDIT_SCHEMA_VERSION

GENESIS = "0" * 64


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return repr(o)


def canonical(obj) -> str:
    return json.dumps(obj, default=_default, sort_keys=True, separators=(",", ":"))


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{ts_ms % 1000:03d}Z"


class AuditLog:
    def __init__(self, path: str | Path, mode: str, symbol: str, clock: Callable[[], int],
                 run_id: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode, self.symbol, self.clock = mode, symbol, clock
        self.run_id = run_id or str(uuid.uuid4())
        self.seq, self.prev_hash = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        rec = json.loads(last)
        return rec["seq"], rec["hash"]

    def write(self, event: str, data: dict | None = None, *, evaluation_id: str | None = None,
              client_order_id: str | None = None) -> dict:
        ts_ms = self.clock()
        self.seq += 1
        rec = {
            "schema": AUDIT_SCHEMA_VERSION, "seq": self.seq, "record_id": str(uuid.uuid4()),
            "ts": iso(ts_ms), "ts_ms": ts_ms, "run_id": self.run_id, "mode": self.mode,
            "event": event, "symbol": self.symbol, "evaluation_id": evaluation_id,
            "client_order_id": client_order_id, "data": json.loads(canonical(data or {})),
            "prev_hash": self.prev_hash,
        }
        rec["hash"] = hashlib.sha256((self.prev_hash + canonical(rec)).encode()).hexdigest()
        line = canonical(rec) + "\n"
        with open(self.path, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.prev_hash = rec["hash"]
        return rec


def verify_chain(path: str | Path) -> tuple[bool, str]:
    prev, seq = GENESIS, 0
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            h = rec.pop("hash")
            if rec["prev_hash"] != prev:
                return False, f"line {n}: prev_hash mismatch"
            if rec["seq"] != seq + 1:
                return False, f"line {n}: sequence gap ({seq} -> {rec['seq']})"
            if hashlib.sha256((prev + canonical(rec)).encode()).hexdigest() != h:
                return False, f"line {n}: hash mismatch (record altered)"
            prev, seq = h, rec["seq"]
    return True, f"{seq} records verified"
