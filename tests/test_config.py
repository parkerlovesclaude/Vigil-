from decimal import Decimal as D
from pathlib import Path

import pytest

from vigil.config import LIVE_ACK_ENV, ConfigError, build_config, check_live_gates, load_config

ROOT = Path(__file__).resolve().parents[1]


def live_raw():
    return {"run": {"mode": "live"}, "exchange": {"name": "binance"}, "live": {"enabled": True}}


def test_defaults_are_paper_and_safe():
    cfg = build_config({})
    assert cfg.run.mode == "paper"
    assert cfg.exchange.name == "paper"
    assert cfg.live.enabled is False


def test_example_config_validates():
    cfg = load_config(ROOT / "examples" / "config.paper.toml")
    assert cfg.run.mode == "paper"


def test_unknown_keys_and_sections_rejected():
    with pytest.raises(ConfigError) as e:
        build_config({"risk": {"max_dailly_loss": 10}, "extras": {}})
    msg = str(e.value)
    assert "unknown setting risk.max_dailly_loss" in msg
    assert "unknown section [extras]" in msg


def test_type_and_range_errors_all_reported_at_once():
    with pytest.raises(ConfigError) as e:
        build_config({"risk": {"risk_per_trade_fraction": "0.5", "max_orders_per_day": "6"},
                      "costs": {"taker_fee_bps": "nan"}})
    assert len(e.value.errors) == 3


def test_bool_not_accepted_as_int():
    with pytest.raises(ConfigError):
        build_config({"risk": {"max_orders_per_day": True}})


@pytest.mark.parametrize("raw, fragment", [
    ({"strategy": {"exit_lookback": 30}}, "exit_lookback must be shorter"),
    ({"exchange": {"name": "binance"}}, "requires exchange.name='paper'"),
    ({"run": {"mode": "testnet"}}, "requires a real exchange adapter"),
    ({"live": {"enabled": True}}, "only allowed with run.mode='live'"),
    ({"run": {"mode": "live"}, "exchange": {"name": "binance"}}, "requires live.enabled=true"),
    ({"run": {"mode": "yolo"}}, "run.mode must be one of"),
    ({"exchange": {"base_url": "http://insecure"}}, "must start with https"),
    ({"costs": {"limit_price_buffer_bps": "2"}}, "limit_price_buffer_bps must cover"),
    ({"data": {"candle_fetch_limit": 50}}, "candle_fetch_limit must be"),
])
def test_cross_checks(raw, fragment):
    with pytest.raises(ConfigError) as e:
        build_config(raw)
    assert fragment in str(e.value)


def test_fingerprint_changes_with_any_setting():
    a = build_config({})
    b = build_config({"risk": {"max_daily_loss": "51"}})
    assert a.fingerprint() == build_config({}).fingerprint()
    assert a.fingerprint() != b.fingerprint()


def test_live_gates_all_required():
    cfg = build_config(live_raw())
    env = {}
    unmet = check_live_gates(cfg, cli_live_flag=False, env=env)
    assert len(unmet) == 3  # flag, ack, credentials (config itself is live)
    env = {LIVE_ACK_ENV: cfg.fingerprint(), "VIGIL_API_KEY": "k", "VIGIL_API_SECRET": "s"}
    assert check_live_gates(cfg, cli_live_flag=True, env=env) == []


def test_live_ack_is_invalidated_by_config_change():
    cfg = build_config(live_raw())
    env = {LIVE_ACK_ENV: cfg.fingerprint(), "VIGIL_API_KEY": "k", "VIGIL_API_SECRET": "s"}
    raw = live_raw()
    raw["risk"] = {"max_daily_loss": "500"}
    changed = build_config(raw)
    unmet = check_live_gates(changed, cli_live_flag=True, env=env)
    assert any("fingerprint" in u for u in unmet)


def test_paper_config_never_passes_live_gates():
    cfg = build_config({})
    env = {LIVE_ACK_ENV: cfg.fingerprint(), "VIGIL_API_KEY": "k", "VIGIL_API_SECRET": "s"}
    assert check_live_gates(cfg, cli_live_flag=True, env=env)


def test_decimal_parsing_is_exact():
    cfg = build_config({"costs": {"taker_fee_bps": 7.5}})
    assert cfg.costs.taker_fee_bps == D("7.5")
