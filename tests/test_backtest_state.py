import random
from decimal import Decimal as D

from helpers import HOUR, RULES, T0, make_cfg
from vigil.backtest import load_candles_csv, run_backtest, write_report
from vigil.state import OrderRecord, Position, State, StateStore


def synthetic_csv(path, n=600, seed=7):
    """Synthetic random walk with drift regimes. FOR TESTING THE MACHINERY ONLY, not evidence."""
    rng = random.Random(seed)
    price = 100.0
    lines = ["open_time_ms,open,high,low,close,volume"]
    for i in range(n):
        drift = 0.002 if (i // 100) % 2 == 0 else -0.002
        o = price
        c = max(1.0, o * (1 + drift + rng.gauss(0, 0.01)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.003)))
        lines.append(f"{T0 + i * HOUR},{o:.2f},{h:.2f},{lo:.2f},{c:.2f},100")
        price = c
    path.write_text("\n".join(lines) + "\n")


def test_backtest_is_reproducible_and_charges_costs(tmp_path):
    p = tmp_path / "data.csv"
    synthetic_csv(p)
    cfg = make_cfg()
    candles, sha = load_candles_csv(p, 3600)
    r1 = run_backtest(cfg, candles, RULES, data_sha256=sha)
    r2 = run_backtest(cfg, candles, RULES, data_sha256=sha)
    assert r1 == r2
    res = r1["results"]
    assert res["trades"] > 0
    assert res["total_fees"] > 0 and res["total_slippage_cost"] > 0
    assert r1["reproducibility"]["data_sha256"] == sha
    # Higher costs can only make results worse on identical signals.
    pricey = make_cfg({"costs": {"taker_fee_bps": "40"}})
    assert run_backtest(pricey, candles, RULES, data_sha256=sha)["results"]["final_equity"] < res["final_equity"]
    sp, tp = write_report(r1, tmp_path / "out")
    assert sp.exists() and tp.read_text().count("\n") == res["trades"] + 1


def test_binance_microsecond_dump_is_normalized(tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text(f"{T0 * 1000},1,2,0.5,1.5,10,0,0,0,0,0,0\n")
    candles, _ = load_candles_csv(p, 3600)
    assert candles[0].open_time_ms == T0


def test_state_round_trip_with_decimals(tmp_path):
    store = StateStore(tmp_path / "s.json")
    st = State()
    st.position = Position(D("0.12345678"), D("100.1"), D("12.36"), D("95"), D("101"), "vgp-x", 1)
    st.orders["vgp-x"] = OrderRecord("vgp-x", "e", "ENTER", "BTCUSDT", "BUY", D("0.1234"), D("100.5"),
                                     "FILLED", 1, 2, applied_trade_ids=[["t1", "0.1234"]])
    st.add_key("ENTER:e")
    st.day_start_equity = D("10000.01")
    store.save(st)
    back = store.load()
    assert back.position == st.position
    assert back.orders["vgp-x"].limit_price == D("100.5")
    assert back.has_key("ENTER:e") and back.day_start_equity == D("10000.01")
    assert not (tmp_path / "s.json.tmp").exists()
