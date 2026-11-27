"""End-to-end behavior on the paper exchange with a scripted market and fake clock."""

from decimal import Decimal as D

import pytest

from helpers import HOUR, RULES, Rig, make_cfg
from vigil.audit import verify_chain
from vigil.engine import Engine, HaltedError
from vigil.models import OrderStatus
from vigil.state import Halt


def evaluations(rig):
    return [e for e in rig.audit_events() if e["event"] == "evaluation"]


def test_startup_reconciles_then_enters_once_on_breakout(tmp_path):
    rig = Rig(tmp_path)
    assert rig.engine.startup()
    rig.engine.tick()
    st = rig.engine.st
    assert st.position is not None
    n_orders = len(rig.ex.orders)
    # More ticks within the same bar must not re-enter.
    for _ in range(5):
        rig.clock.sleep(5)
        rig.engine.tick()
    assert len(rig.ex.orders) == n_orders == 1
    ev = evaluations(rig)[0]
    for k in ("market_data", "data_checks", "signal", "risk_checks", "sizing", "decision", "equity"):
        assert k in ev["data"], k
    assert ev["data"]["decision"]["action"] == "ENTER"
    assert ev["data"]["sizing"]["details"]["cost_to_risk"]


def test_no_trading_before_bar_close_delay(tmp_path):
    rig = Rig(tmp_path, now_offset_s=2)   # delay is 5s
    rig.engine.startup()
    rig.engine.tick()
    assert rig.engine.st.position is None and not evaluations(rig)


def test_stale_data_holds_and_is_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.market.book_ts = rig.clock() - 60_000   # stale order book
    rig.engine.tick()
    assert rig.engine.st.position is None
    ev = evaluations(rig)[-1]["data"]
    assert ev["decision"]["reason"] == "data_invalid"
    assert any("stale_book" in i for i in ev["data_checks"]["book"])
    assert ev["final"] is False                 # retried while within max_bar_lag
    # Data recovers within the bar-lag window -> evaluated normally.
    rig.market.book_ts = None
    rig.clock.sleep(5)
    rig.engine.tick()
    assert rig.engine.st.position is not None


def test_stale_data_past_lag_window_consumes_bar_without_trading(tmp_path):
    rig = Rig(tmp_path, now_offset_s=200)       # > max_bar_lag_seconds (120)
    rig.engine.startup()
    rig.market.book_ts = rig.clock() - 60_000
    rig.engine.tick()
    assert rig.engine.st.position is None
    ev = evaluations(rig)[-1]["data"]
    assert ev["final"] is True and ev["decision"]["reason"] == "data_invalid"


def test_startup_mid_bar_never_enters_on_an_old_signal(tmp_path):
    rig = Rig(tmp_path, now_offset_s=2400)      # started 40 minutes after the breakout bar closed
    rig.engine.startup()
    rig.engine.tick()
    assert rig.engine.st.position is None
    ev = evaluations(rig)[-1]["data"]
    assert ev["data_checks"]["candles"] == []   # data between bars is valid
    assert ev["decision"]["reason"] == "late_bar_entry_blocked" and ev["final"] is True
    rig.clock.sleep(5)
    rig.engine.tick()
    assert len(evaluations(rig)) == 1           # bar consumed once


def test_protective_stop_exits_between_bars(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    pos = rig.engine.st.position
    rig.market.book_price = pos.stop_price - D("0.5")   # mid drops through the stop
    # keep the book consistent with candles: widen allowed deviation for this test
    rig.clock.sleep(5)
    rig.engine.tick()
    assert rig.engine.st.position is None
    closed = [e for e in rig.audit_events() if e["event"] == "position_closed"]
    assert closed and closed[0]["data"]["reason"] == "stop_breached"


def test_partial_exit_is_retried_until_flat(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    st = rig.engine.st
    st.position.exit_pending, st.position.exit_reason = True, "channel_exit"
    rig.ex.faults["place"] = ["partial:0.5"]
    rig.clock.sleep(5)
    rig.engine.tick()
    assert st.position is not None and st.position.exit_attempts == 1
    rig.clock.sleep(5)
    rig.engine.tick()
    assert st.position is None
    exits = [o for o in st.orders.values() if o.purpose == "EXIT"]
    assert len(exits) == 2 and len({o.client_order_id for o in exits}) == 2


def test_repeated_transient_errors_halt_and_stop(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.ex.faults["candles"] = ["timeout"] * 10
    with pytest.raises(HaltedError):
        rig.engine.run(max_ticks=10)
    assert rig.engine.st.halt.category == "integrity"
    assert rig.engine.st.position is None


def test_recovery_after_crash_mid_order(tmp_path):
    """Process dies after the exchange filled the order but before local state learned of it."""
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.ex.faults["place"] = ["timeout_after"]
    rig.ex.faults["get"] = ["timeout"] * 100
    rig.engine.tick()                         # order ends UNKNOWN -> integrity halt
    st = rig.engine.st
    assert st.halt.active and st.position is None
    assert rig.ex.balances()["BTC"].total > 0  # exchange really holds the coin

    # Operator restarts without investigating: startup must refuse.
    rig.ex.faults["get"] = []
    eng2 = rig.new_engine()
    assert eng2.startup() is False
    # Operator investigates and resets the halt; reconciliation resolves the order.
    eng2.st.halt = Halt()
    eng2.store.save(eng2.st)
    eng3 = rig.new_engine()
    assert eng3.startup() is True
    assert eng3.st.position is not None
    assert eng3.st.position.qty == rig.ex.balances()["BTC"].total
    assert all(OrderStatus(o.status).is_terminal for o in eng3.st.orders.values())


def test_reconciliation_blocks_on_unmanaged_balance(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.balances_["BTC"] = D("1")          # coin on the account the system doesn't know about
    assert rig.engine.startup() is False
    assert rig.engine.st.halt.category == "integrity"
    rig.engine.tick()
    assert len(rig.ex.orders) == 0
    res = [e for e in rig.audit_events() if e["event"] == "reconcile_result"][-1]["data"]
    assert not res["ok"]
    assert any(c["name"] == "base_balance_matches" and not c["passed"] for c in res["checks"])


def test_reconciliation_blocks_on_unexpected_open_order(tmp_path):
    rig = Rig(tmp_path)
    from vigil.models import OrderState
    rig.ex.orders["foreign"] = OrderState("foreign", "F1", OrderStatus.NEW, D(0), None)
    assert rig.engine.startup() is False


def test_daily_loss_blocks_new_entries(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    st = rig.engine.st
    st.day = "2099-01-01"   # prevent roll; pretend the day started richer
    from datetime import datetime, timezone
    st.day = datetime.fromtimestamp(rig.clock() / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    st.day_start_equity = D("10100")
    rig.engine.tick()
    assert st.position is None
    ev = evaluations(rig)[-1]["data"]
    assert "daily_loss_below_limit" in ev["decision"]["reason"]


def test_risk_halt_allows_exit_but_not_entry(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    st = rig.engine.st
    st.halt = Halt(True, "risk", "max_drawdown_breached", rig.clock())
    st.position.exit_pending, st.position.exit_reason = True, "manual_test"
    rig.clock.sleep(5)
    rig.engine.tick()
    assert st.position is None


def test_live_mode_engine_refuses_without_gates(tmp_path):
    cfg = make_cfg({"run": {"mode": "live", "state_dir": str(tmp_path)}, "exchange": {"name": "binance"},
                    "live": {"enabled": True}})
    rig = Rig(tmp_path)
    eng = Engine(cfg, rig.ex, rig.store, rig.engine.audit, RULES, clock=rig.clock, sleep=rig.clock.sleep)
    with pytest.raises(RuntimeError):
        eng.startup()


def test_full_trade_cycle_and_audit_chain(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    assert rig.engine.st.position is not None
    for c in (111, 112, 113, 114, 115, 116, 108):   # rally, then a drop below the exit channel
        rig.add_bar(c)
        rig.engine.tick()
    assert rig.engine.st.position is None
    ok, msg = verify_chain(tmp_path / "audit.jsonl")
    assert ok, msg
    # Every order lifecycle is traceable by client_order_id.
    ids = {e["client_order_id"] for e in rig.audit_events() if e["client_order_id"]}
    assert len(ids) == 2
    for cid in ids:
        evs = [e["event"] for e in rig.audit_events() if e["client_order_id"] == cid]
        assert evs[0] == "order_intent" and "order_fill" in evs


def test_tampering_with_audit_is_detected(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    p = tmp_path / "audit.jsonl"
    lines = p.read_text().splitlines()
    lines[2] = lines[2].replace('"paper"', '"live"')
    p.write_text("\n".join(lines) + "\n")
    ok, msg = verify_chain(p)
    assert not ok and "line 3" in msg


def test_state_survives_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    pos = rig.engine.st.position
    eng2 = rig.new_engine()
    assert eng2.st.position == pos
    assert eng2.startup()
    rig.engine = eng2
    rig.clock.sleep(5)
    eng2.tick()
    assert len(rig.ex.orders) == 1   # the restarted process does not re-enter the same bar


def test_conflicting_sources_block_entry_but_not_stop_management(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.market.book_price = D("140")     # book mid far from the last close
    rig.engine.tick()
    assert rig.engine.st.position is None
    assert evaluations(rig)[-1]["data"]["decision"]["reason"] == "data_conflict"


def test_exit_reason_recorded_for_bar_close_exit(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.startup()
    rig.engine.tick()
    for c in (111, 112, 113, 114, 115, 116, 108):
        rig.add_bar(c)
        rig.engine.tick()
    closed = [e for e in rig.audit_events() if e["event"] == "position_closed"]
    assert closed[0]["data"]["reason"] in ("channel_exit", "trailing_stop", "stop_breached")
    assert rig.ex.balances()["BTC"].total < RULES.min_qty  # only dust (if any) left on the account


def test_closed_trades_are_journaled_to_obsidian(tmp_path):
    vault = tmp_path / "vault"
    cfg = make_cfg({"run": {"state_dir": str(tmp_path), "obsidian_vault": str(vault)}})
    rig = Rig(tmp_path, cfg=cfg)
    rig.engine.startup()
    rig.engine.tick()
    for c in (111, 112, 113, 114, 115, 116, 108):
        rig.add_bar(c)
        rig.engine.tick()
    notes = list((vault / "Trading" / "Trades").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "source: vigil-paper" in text and "pnl_usd:" in text and "[[Donchian Breakout (Vigil)]]" in text
    # Re-export is idempotent and never overwrites.
    from vigil.obsidian import export_trades
    assert export_trades(tmp_path / "audit.jsonl", vault) == []


def test_github_journal_creates_only_new_files(tmp_path):
    from vigil.obsidian import GitHubJournal
    store, calls = {"Trading/Trades/old.md"}, []

    def transport(method, url, body):
        path = url.split("/contents/")[1].split("?")[0]
        from urllib.parse import unquote
        path = unquote(path)
        calls.append((method, path))
        if method == "GET":
            return (200, {}) if path in store else (404, {})
        store.add(path)
        assert body["branch"] == "main" and body["content"]
        return 201, {}

    gj = GitHubJournal("parker/pk-brain", "tok", transport=transport)
    created = gj.publish([("Trading/Trades/old.md", "x"), ("Trading/Trades/new trade.md", "y")])
    assert created == ["Trading/Trades/new trade.md"]
    assert ("PUT", "Trading/Trades/old.md") not in calls
    assert gj.publish([("Trading/Trades/new trade.md", "y")]) == []


def test_journal_failure_never_affects_trading(tmp_path):
    cfg = make_cfg({"run": {"state_dir": str(tmp_path), "journal_github_repo": "parker/pk-brain"}})
    rig = Rig(tmp_path, cfg=cfg)          # no token in env -> journal errors
    rig.engine.startup()
    rig.engine.tick()
    for c in (111, 112, 113, 114, 115, 116, 108):
        rig.add_bar(c)
        rig.engine.tick()
    assert rig.engine.st.position is None and not rig.engine.st.halt.active
    errs = [e for e in rig.audit_events() if e["event"] == "obsidian_export_error"]
    assert errs and errs[-1]["data"]["target"] == "github"


def test_stdout_journal_lines_decode_to_notes(tmp_path, capsys):
    import base64, json
    cfg = make_cfg({"run": {"state_dir": str(tmp_path), "journal_stdout": True}})
    rig = Rig(tmp_path, cfg=cfg)
    rig.engine.startup()
    rig.engine.tick()
    for c in (111, 112, 113, 114, 115, 116, 108):
        rig.add_bar(c)
        rig.engine.tick()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("VIGIL_NOTE ")]
    assert lines
    note = json.loads(base64.b64decode(lines[-1].split(" ", 1)[1]))
    assert note["path"].startswith("Trading/Trades/") and "pnl_usd:" in note["text"]
