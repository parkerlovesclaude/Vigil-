"""The trading loop. Connects data -> validation -> strategy -> costs/sizing -> risk -> orders.

One tick:
  1. If reconciliation is required, run it (no trading until it passes).
  2. Fetch candles, order book and balances; validate the data.
  3. Roll the UTC day, update peak equity, raise halts if limits are breached.
  4. Manage an open position: continue a pending exit, or exit immediately if
     the best bid is at/below the stop (protective intrabar check).
  5. Once per closed bar (after bar_close_delay_seconds): evaluate the strategy,
     price the trade including costs, size it, run risk checks, decide, act.
Every evaluation writes one `evaluation` audit record with all inputs/outputs.

Failure policy:
  * Transient exchange errors: counted; after risk.max_consecutive_errors in a
    row the system halts (integrity).
  * Integrity problems (unresolved orders, reconciliation failure, no valid data
    while in a position for too long, unexpected exceptions): halt and stop the
    process. A restart always begins with reconciliation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from . import __version__
from .audit import AuditLog
from .config import Config
from .exchange.base import ExchangeAdapter, TransientError
from .marketdata import closed_candles, validate_book, validate_candles
from .models import OrderBook, Side, SymbolRules
from .orders import IntegrityError, OrderManager
from .reconcile import reconcile
from .risk import check_entry, check_exit, halt_conditions
from .sizing import size_entry, size_exit
from .state import Halt, State, StateStore
from .strategy import Action, PositionContext, evaluate


class HaltedError(Exception):
    pass


def _book_summary(book: OrderBook | None, levels: int = 5) -> dict | None:
    if book is None:
        return None
    return {"ts_ms": book.ts_ms, "bids": [[l.price, l.qty] for l in book.bids[:levels]],
            "asks": [[l.price, l.qty] for l in book.asks[:levels]]}


class Engine:
    def __init__(self, cfg: Config, adapter: ExchangeAdapter, store: StateStore, audit: AuditLog,
                 rules: SymbolRules, *, clock: Callable[[], int] | None = None,
                 sleep: Callable[[float], None] = time.sleep, live_armed: bool = False):
        self.cfg, self.ex, self.store, self.audit, self.rules = cfg, adapter, store, audit, rules
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.live_armed = live_armed
        self.st: State = store.load()
        self.om = OrderManager(cfg, adapter, self.st, store, audit, rules, self.clock, sleep)
        self.interval_ms = cfg.run.interval_seconds * 1000
        self._data_ok_prev: bool | None = None

    # ---- lifecycle --------------------------------------------------------------
    def startup(self) -> bool:
        if self.cfg.run.mode == "live" and not self.live_armed:
            raise RuntimeError("live mode requires live gates to be checked by the caller")
        self.st.needs_reconcile = True   # always reconcile after a (re)start
        self.store.save(self.st)
        self.audit.write("run_start", {"version": __version__, "config_fingerprint": self.cfg.fingerprint(),
                                       "config": self.cfg.as_dict(), "live_armed": self.live_armed,
                                       "rules": self.rules, "halt": self.st.halt,
                                       "position": self.st.position})
        if self.audit.path.exists():
            self.journal()   # catch up on notes missed while offline (idempotent)
        if self.st.halt.active and self.st.halt.category == "integrity":
            self.audit.write("startup_refused", {"reason": "integrity halt active", "halt": self.st.halt})
            return False
        return reconcile(self)

    def run(self, max_ticks: int | None = None) -> None:
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                self.tick()
                ticks += 1
                if self.st.halt.active and self.st.halt.category == "integrity":
                    raise HaltedError(self.st.halt.reason)
                self.sleep(self.cfg.run.loop_sleep_seconds)
        finally:
            self.audit.write("run_stop", {"ticks": ticks, "halt": self.st.halt, "position": self.st.position})

    def raise_halt(self, category: str, reason: str) -> None:
        h = self.st.halt
        # Keep the first integrity reason; a risk halt can only be upgraded to integrity.
        if h.active and (h.category == "integrity" or category == "risk"):
            return
        self.st.halt = Halt(True, category, reason, self.clock())
        self.store.save(self.st)
        self.audit.write("halt", {"category": category, "reason": reason})

    # ---- one iteration ------------------------------------------------------------
    def tick(self) -> None:
        if self.st.halt.active and self.st.halt.category == "integrity":
            return
        errors_before = self.st.consecutive_errors
        try:
            if self.st.needs_reconcile and not reconcile(self):
                return
            self._tick()
            if self.st.consecutive_errors == errors_before:
                self.st.consecutive_errors = 0      # a clean tick resets the error streak
            elif self.st.consecutive_errors >= self.cfg.risk.max_consecutive_errors:
                self.raise_halt("integrity", f"consecutive_errors: {self.st.consecutive_errors}")
            self.store.save(self.st)
        except TransientError as exc:
            self.st.consecutive_errors += 1
            self.store.save(self.st)
            self.audit.write("error", {"type": "transient", "error": str(exc),
                                       "consecutive_errors": self.st.consecutive_errors})
            if self.st.consecutive_errors >= self.cfg.risk.max_consecutive_errors:
                self.raise_halt("integrity", f"consecutive_errors: {self.st.consecutive_errors}")
        except IntegrityError as exc:
            self.st.needs_reconcile = True
            self.store.save(self.st)
            self.audit.write("error", {"type": "integrity", "error": str(exc)})
            self.raise_halt("integrity", f"integrity_error: {exc}")
        except Exception as exc:  # noqa: BLE001 - record, halt, then re-raise
            self.audit.write("error", {"type": "unexpected", "error": repr(exc)})
            self.raise_halt("integrity", f"unexpected_exception: {exc!r}")
            raise

    def _tick(self) -> None:
        cfg, st, now = self.cfg, self.st, self.clock()
        raw = self.ex.candles(cfg.run.symbol, cfg.run.interval, cfg.data.candle_fetch_limit)
        candles = closed_candles(raw, now)
        book = self.ex.order_book(cfg.run.symbol, cfg.data.book_depth_levels)
        balances = self.ex.balances()
        now = self.clock()
        cchk = validate_candles(candles, now, cfg.run.interval_seconds, cfg.strategy.min_history_bars, cfg.data)
        bchk = validate_book(book, now, candles[-1].close if candles else None, cfg.data)
        data_ok = cchk.ok and bchk.ok
        book_ok = bchk.usable          # structurally valid and fresh (conflicts allowed)
        issues = cchk.issues + bchk.issues + bchk.conflicts

        if data_ok != self._data_ok_prev:
            self.audit.write("data_quality", {"ok": data_ok, "issues": issues})
            self._data_ok_prev = data_ok
        if not book_ok and st.position is not None:
            st.invalid_data_since_ms = st.invalid_data_since_ms or now
            if now - st.invalid_data_since_ms > cfg.risk.max_invalid_data_seconds_in_position * 1000:
                self.raise_halt("integrity", "no_valid_market_data_while_in_position")
                return
        elif book_ok:
            st.invalid_data_since_ms = 0

        quote = balances.get(self.rules.quote_asset)
        quote_total = quote.total if quote else Decimal(0)
        quote_free = quote.free if quote else Decimal(0)
        equity = None
        if book_ok:
            equity = quote_total + (st.position.qty * book.best_bid if st.position else Decimal(0))
            self._roll_day(now, equity)
            if st.peak_equity is None or equity > st.peak_equity:
                st.peak_equity = equity
            hc = halt_conditions(st, equity, cfg.risk)
            if hc:
                self.raise_halt(*hc)
                if hc[0] == "integrity":
                    return

        # ---- position management between bars ----
        if st.position is not None and book_ok:
            if st.position.exit_pending:
                self._exit(st.position.exit_reason or "pending_exit", book, candles)
            elif book.best_bid <= st.position.stop_price:
                st.position.exit_pending, st.position.exit_reason = True, "stop_breached"
                self.store.save(st)
                self.audit.write("evaluation", {
                    "kind": "stop_check", "book": _book_summary(book), "stop_price": st.position.stop_price,
                    "best_bid": book.best_bid, "decision": {"action": "EXIT", "reason": "stop_breached"}},
                    evaluation_id=f"{cfg.run.symbol}:STOP:{st.position.entry_client_order_id}")
                self._exit("stop_breached", book, candles)

        # ---- once per closed bar ----
        bar_close = (now // self.interval_ms) * self.interval_ms - 1
        if bar_close <= st.last_evaluated_bar_ms or now < bar_close + cfg.run.bar_close_delay_seconds * 1000:
            return
        usable = cchk.ok and book_ok and (not bchk.conflicts or st.position is not None)
        late = now > bar_close + cfg.data.max_bar_lag_seconds * 1000
        self._evaluate_bar(bar_close, candles, book, cchk.issues, bchk.issues, bchk.conflicts, equity,
                           quote_free, balances, usable or late, late)

    # ---- bar evaluation ---------------------------------------------------------------
    def _evaluate_bar(self, bar_close, candles, book, c_issues, b_issues, conflicts, equity, quote_free,
                      balances, final: bool, late: bool = False) -> None:
        cfg, st = self.cfg, self.st
        eval_id = f"{cfg.run.symbol}:{cfg.run.interval}:{bar_close}"
        rec: dict = {
            "kind": "bar", "bar_close_time_ms": bar_close, "final": final, "late": late,
            "market_data": {"last_candles": candles[-3:] if candles else [], "candle_count": len(candles),
                            "book": _book_summary(book)},
            "data_checks": {"candles": c_issues, "book": b_issues, "conflicts": conflicts},
            "equity": equity,
            "balances": {k: v for k, v in balances.items() if k in (self.rules.base_asset, self.rules.quote_asset)},
            "position_before": st.position, "halt": st.halt,
        }
        decision = {"action": "HOLD", "reason": None}

        last_ok = candles and candles[-1].close_time_ms == bar_close
        conflict_blocks = bool(conflicts) and st.position is None   # entries need agreeing sources
        # A bar first evaluated after the lag window (e.g. process started mid-bar) may still
        # manage an open position, but never opens one: late signals don't become late trades.
        late_blocks = late and st.position is None
        if c_issues or b_issues or not last_ok or conflict_blocks or late_blocks:
            decision["reason"] = ("data_invalid" if (c_issues or b_issues)
                                  else "bar_not_available" if not last_ok
                                  else "data_conflict" if conflict_blocks else "late_bar_entry_blocked")
            rec["decision"] = decision
            if final:
                st.last_evaluated_bar_ms = bar_close
                self.store.save(st)
            self.audit.write("evaluation", rec, evaluation_id=eval_id)
            return

        pos = st.position
        ctx = PositionContext(pos is not None, pos.entry_price if pos else None,
                              pos.stop_price if pos else None, pos.highest_close if pos else None)
        sig = evaluate(candles, ctx, cfg.strategy)
        rec["signal"] = {"action": sig.action.value, "reason": sig.reason, "reference_price": sig.reference_price,
                         "stop_price": sig.stop_price, "highest_close": sig.highest_close,
                         "indicators": sig.indicators}
        st.last_evaluated_bar_ms = bar_close

        if pos is not None:
            # Persist the (only-ever-rising) stop and highest close before acting.
            if sig.stop_price is not None and sig.stop_price > pos.stop_price:
                pos.stop_price = sig.stop_price
            if sig.highest_close is not None and sig.highest_close > pos.highest_close:
                pos.highest_close = sig.highest_close
            if sig.action is Action.EXIT and not pos.exit_pending:
                pos.exit_pending, pos.exit_reason = True, sig.reason
                decision = {"action": "EXIT", "reason": sig.reason}
            else:
                decision = {"action": "HOLD", "reason": sig.reason}
            rec["decision"] = decision
            self.store.save(st)
            self.audit.write("evaluation", rec, evaluation_id=eval_id)
            if pos.exit_pending:
                self._exit(pos.exit_reason, book, candles)
            return

        if sig.action is not Action.ENTER:
            rec["decision"] = {"action": "HOLD", "reason": sig.reason}
            self.store.save(st)
            self.audit.write("evaluation", rec, evaluation_id=eval_id)
            return

        risk = check_entry(st, equity, cfg.risk, bar_close, self.interval_ms)
        rec["risk_checks"] = risk.checks
        sizing = size_entry(equity=equity, quote_free=quote_free, stop=sig.stop_price, book=book,
                            rules=self.rules, costs=cfg.costs, risk=cfg.risk,
                            loss_allowance=risk.loss_allowance) if risk.ok else None
        rec["sizing"] = sizing.as_dict() if sizing else None
        key = f"ENTER:{eval_id}"
        if not risk.ok:
            decision = {"action": "HOLD", "reason": "risk_check_failed: " + ",".join(risk.failed())}
        elif not sizing.ok:
            decision = {"action": "HOLD", "reason": f"sizing_rejected: {sizing.reject_reason}"}
        elif st.has_key(key):
            decision = {"action": "HOLD", "reason": "duplicate_signal_already_acted"}
        else:
            decision = {"action": "ENTER", "reason": sig.reason, "qty": sizing.qty,
                        "limit_price": sizing.limit_price, "stop_price": sig.stop_price}
        rec["decision"] = decision
        if decision["action"] == "ENTER":
            st.add_key(key)
        self.store.save(st)
        self.audit.write("evaluation", rec, evaluation_id=eval_id)
        if decision["action"] == "ENTER":
            self.om.submit(purpose="ENTER", evaluation_id=eval_id, side=Side.BUY, qty=sizing.qty,
                           limit_price=sizing.limit_price, stop_price=sig.stop_price,
                           ref_price=sig.reference_price)
            self._after_fills(candles)

    # ---- exits ---------------------------------------------------------------------------
    def _exit(self, reason: str, book: OrderBook, candles) -> None:
        st, cfg = self.st, self.cfg
        pos = st.position
        chk = check_exit(st)
        if not chk.ok:
            self.audit.write("exit_blocked", {"reason": reason, "checks": chk.checks})
            return
        sz = size_exit(position_qty=pos.qty, book=book, rules=self.rules, costs=cfg.costs)
        attempt = pos.exit_attempts + 1
        eval_id = f"{cfg.run.symbol}:EXIT:{pos.entry_client_order_id}:{attempt}"
        self.audit.write("exit_decision", {"reason": reason, "attempt": attempt, "sizing": sz.as_dict(),
                                           "position": pos}, evaluation_id=eval_id)
        if not sz.ok:
            self._close_position("dust_remaining", candles)
            return
        pos.exit_attempts = attempt
        self.store.save(st)
        self.om.submit(purpose="EXIT", evaluation_id=eval_id, side=Side.SELL, qty=sz.qty,
                       limit_price=sz.limit_price, ref_price=book.best_bid)
        self._after_fills(candles)

    def _after_fills(self, candles) -> None:
        """Close the position record if what remains is below exchange minimums."""
        pos = self.st.position
        if pos is None or not pos.exit_pending:
            return
        price = pos.entry_price
        if pos.qty < self.rules.min_qty or pos.qty * price < self.rules.min_notional:
            self._close_position(pos.exit_reason or "exit", candles)

    def _close_position(self, reason: str, candles) -> None:
        st = self.st
        pos = st.position
        st.position = None
        st.last_exit_bar_ms = candles[-1].close_time_ms if candles else st.last_evaluated_bar_ms
        self.store.save(st)
        self.audit.write("position_closed", {"reason": reason, "remaining_qty_dust": pos.qty,
                                             "remaining_cost_basis": pos.cost_basis,
                                             "realized_pnl_today": st.realized_pnl_today})
        self.journal()

    def journal(self) -> None:
        """Write closed-trade notes. A convenience only: failures are logged, never raised."""
        run = self.cfg.run
        if run.obsidian_vault:
            try:
                from .obsidian import export_trades
                notes = export_trades(self.audit.path, run.obsidian_vault)
                self.audit.write("obsidian_export", {"target": "local", "notes": [str(n) for n in notes]})
            except Exception as exc:  # noqa: BLE001
                self.audit.write("obsidian_export_error", {"target": "local", "error": repr(exc)})
        if run.journal_stdout:
            # One line per note: "VIGIL_NOTE <base64 JSON {path, text}>". A sync job reads these
            # from the platform's logs and writes any missing notes into the vault (idempotent).
            try:
                import base64
                import json as _json
                from .obsidian import render_notes
                for rel, text in render_notes(self.audit.path):
                    payload = base64.b64encode(_json.dumps({"path": rel, "text": text}).encode()).decode()
                    print(f"VIGIL_NOTE {payload}", flush=True)
            except Exception as exc:  # noqa: BLE001
                self.audit.write("obsidian_export_error", {"target": "stdout", "error": repr(exc)})
        if run.journal_github_repo:
            try:
                import os
                from .obsidian import GitHubJournal, render_notes
                gj = GitHubJournal(run.journal_github_repo, os.environ.get(run.journal_github_token_env, ""),
                                   run.journal_github_branch)
                created = gj.publish(render_notes(self.audit.path))
                self.audit.write("obsidian_export", {"target": "github", "notes": created})
            except Exception as exc:  # noqa: BLE001
                self.audit.write("obsidian_export_error", {"target": "github", "error": repr(exc)})

    def _roll_day(self, now_ms: int, equity: Decimal) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.st.day != day:
            prev = {"day": self.st.day, "realized_pnl": self.st.realized_pnl_today,
                    "orders": self.st.orders_today}
            self.st.day, self.st.day_start_equity = day, equity
            self.st.realized_pnl_today, self.st.orders_today = Decimal(0), 0
            self.store.save(self.st)
            self.audit.write("day_roll", {"previous": prev, "day": day, "day_start_equity": equity})
