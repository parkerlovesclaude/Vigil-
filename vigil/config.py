"""Configuration loading and validation.

Design rules:
- Every operationally important setting is explicit, typed and bounded.
- Unknown keys are errors (a typo must never silently fall back to a default).
- All problems are reported at once, before anything starts.
- The default mode is "paper". Live mode needs three independent gates (see
  `check_live_gates`), and the config fingerprint ties the operator's
  acknowledgment to this exact configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MODES = ("paper", "testnet", "live")
INTERVALS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
LIVE_ACK_ENV = "VIGIL_LIVE_ACK"


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(errors))

    def __reduce__(self):  # keep the error list intact across process boundaries
        return (ConfigError, (self.errors,))


@dataclass(frozen=True)
class RunConfig:
    mode: str = "paper"
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    state_dir: str = "./var"
    bar_close_delay_seconds: int = 5
    loop_sleep_seconds: int = 5
    obsidian_vault: str = ""        # optional: write each closed trade as a note into this vault
    journal_stdout: bool = False    # optional: print each trade note to stdout as a VIGIL_NOTE line (for log-based sync)
    journal_github_repo: str = ""   # optional: "owner/repo" holding the vault; notes pushed there
    journal_github_branch: str = "main"
    journal_github_token_env: str = "VIGIL_GITHUB_TOKEN"

    @property
    def interval_seconds(self) -> int:
        return INTERVALS[self.interval]


@dataclass(frozen=True)
class StrategyConfig:
    entry_lookback: int = 20
    exit_lookback: int = 10
    trend_ema_period: int = 100
    atr_period: int = 14
    stop_atr_mult: Decimal = Decimal("2.0")
    trail_atr_mult: Decimal = Decimal("3.0")

    @property
    def min_history_bars(self) -> int:
        # EMA needs a warm-up of several periods to stop depending on its seed.
        return max(self.entry_lookback, self.exit_lookback, self.atr_period + 1,
                   self.trend_ema_period * 3) + 1


@dataclass(frozen=True)
class CostConfig:
    taker_fee_bps: Decimal = Decimal("10")
    extra_slippage_bps: Decimal = Decimal("2")
    max_spread_bps: Decimal = Decimal("15")
    limit_price_buffer_bps: Decimal = Decimal("25")
    max_cost_to_risk: Decimal = Decimal("0.25")


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_fraction: Decimal = Decimal("0.005")
    max_position_notional: Decimal = Decimal("1000")
    max_position_fraction: Decimal = Decimal("0.25")
    max_daily_loss: Decimal = Decimal("50")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    max_orders_per_day: int = 6
    max_consecutive_errors: int = 3
    cooldown_bars_after_exit: int = 1
    max_invalid_data_seconds_in_position: int = 900
    position_tolerance_fraction: Decimal = Decimal("0.001")


@dataclass(frozen=True)
class DataConfig:
    max_bar_lag_seconds: int = 120
    max_book_age_seconds: int = 5
    max_book_vs_close_bps: Decimal = Decimal("300")
    max_clock_skew_ms: int = 1000
    book_depth_levels: int = 20
    candle_fetch_limit: int = 400


@dataclass(frozen=True)
class ExchangeConfig:
    name: str = "paper"            # "paper" (simulated fills on live public data) or "binance"
    base_url: str = ""             # empty = default for the mode (see exchange/binance.py)
    request_timeout_seconds: int = 10
    recv_window_ms: int = 5000
    order_poll_seconds: Decimal = Decimal("1")
    order_resolve_timeout_seconds: int = 60
    api_key_env: str = "VIGIL_API_KEY"
    api_secret_env: str = "VIGIL_API_SECRET"


@dataclass(frozen=True)
class PaperConfig:
    starting_quote: Decimal = Decimal("10000")


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False


@dataclass(frozen=True)
class Config:
    run: RunConfig
    strategy: StrategyConfig
    costs: CostConfig
    risk: RiskConfig
    data: DataConfig
    exchange: ExchangeConfig
    paper: PaperConfig
    live: LiveConfig

    def fingerprint(self) -> str:
        """Stable hash of the full validated config. Changes whenever any setting changes."""
        canon = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(canon.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))


SECTIONS = {
    "run": RunConfig, "strategy": StrategyConfig, "costs": CostConfig, "risk": RiskConfig,
    "data": DataConfig, "exchange": ExchangeConfig, "paper": PaperConfig, "live": LiveConfig,
}

# (min, max) inclusive bounds. Anything outside is rejected.
BOUNDS: dict[str, tuple[Any, Any]] = {
    "run.bar_close_delay_seconds": (0, 300),
    "run.loop_sleep_seconds": (1, 3600),
    "strategy.entry_lookback": (2, 500),
    "strategy.exit_lookback": (2, 500),
    "strategy.trend_ema_period": (2, 1000),
    "strategy.atr_period": (2, 500),
    "strategy.stop_atr_mult": (Decimal("0.5"), Decimal("10")),
    "strategy.trail_atr_mult": (Decimal("0.5"), Decimal("20")),
    "costs.taker_fee_bps": (Decimal("0"), Decimal("100")),
    "costs.extra_slippage_bps": (Decimal("0"), Decimal("200")),
    "costs.max_spread_bps": (Decimal("0.1"), Decimal("500")),
    "costs.limit_price_buffer_bps": (Decimal("1"), Decimal("500")),
    "costs.max_cost_to_risk": (Decimal("0.01"), Decimal("1")),
    "risk.risk_per_trade_fraction": (Decimal("0.0001"), Decimal("0.05")),
    "risk.max_position_notional": (Decimal("1"), Decimal("10000000")),
    "risk.max_position_fraction": (Decimal("0.001"), Decimal("1")),
    "risk.max_daily_loss": (Decimal("0.01"), Decimal("10000000")),
    "risk.max_drawdown_fraction": (Decimal("0.001"), Decimal("0.9")),
    "risk.max_orders_per_day": (1, 1000),
    "risk.max_consecutive_errors": (1, 100),
    "risk.cooldown_bars_after_exit": (0, 1000),
    "risk.max_invalid_data_seconds_in_position": (10, 86400),
    "risk.position_tolerance_fraction": (Decimal("0"), Decimal("0.05")),
    "data.max_bar_lag_seconds": (1, 3600),
    "data.max_book_age_seconds": (1, 120),
    "data.max_book_vs_close_bps": (Decimal("1"), Decimal("5000")),
    "data.max_clock_skew_ms": (10, 60000),
    "data.book_depth_levels": (1, 1000),
    "data.candle_fetch_limit": (10, 1000),
    "exchange.request_timeout_seconds": (1, 120),
    "exchange.recv_window_ms": (100, 60000),
    "exchange.order_poll_seconds": (Decimal("0.1"), Decimal("30")),
    "exchange.order_resolve_timeout_seconds": (1, 3600),
    "paper.starting_quote": (Decimal("1"), Decimal("1000000000")),
}


def _coerce(value: Any, typ: type, key: str, errors: list[str]) -> Any:
    try:
        if typ is bool:
            if not isinstance(value, bool):
                raise TypeError
            return value
        if typ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError
            return value
        if typ is Decimal:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise TypeError
            d = Decimal(str(value))
            if not d.is_finite():
                raise InvalidOperation
            return d
        if typ is str:
            if not isinstance(value, str):
                raise TypeError
            return value
    except (TypeError, InvalidOperation):
        errors.append(f"{key}: expected {typ.__name__}, got {value!r}")
        return None
    raise AssertionError(typ)


def _types(cls) -> dict[str, type]:
    mapping = {"int": int, "str": str, "bool": bool, "Decimal": Decimal}
    return {f.name: mapping[f.type if isinstance(f.type, str) else f.type.__name__] for f in fields(cls)}


def build_config(raw: dict) -> Config:
    """Validate a raw dict (e.g. parsed TOML) into a Config, or raise ConfigError listing every problem."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["top level must be a table"])
    for section in raw:
        if section not in SECTIONS:
            errors.append(f"unknown section [{section}]")
    built: dict[str, Any] = {}
    for section, cls in SECTIONS.items():
        values = raw.get(section, {})
        if not isinstance(values, dict):
            errors.append(f"[{section}] must be a table")
            values = {}
        types = _types(cls)
        kwargs = {}
        for key, value in values.items():
            full = f"{section}.{key}"
            if key not in types:
                errors.append(f"unknown setting {full}")
                continue
            coerced = _coerce(value, types[key], full, errors)
            if coerced is None:
                continue
            if full in BOUNDS:
                lo, hi = BOUNDS[full]
                if not (lo <= coerced <= hi):
                    errors.append(f"{full}={coerced} out of range [{lo}, {hi}]")
                    continue
            kwargs[key] = coerced
        try:
            built[section] = cls(**kwargs)
        except TypeError as exc:  # pragma: no cover - guarded above
            errors.append(f"[{section}]: {exc}")
    if errors:
        raise ConfigError(errors)
    cfg = Config(**built)
    errors.extend(_cross_checks(cfg))
    if errors:
        raise ConfigError(errors)
    return cfg


def _cross_checks(cfg: Config) -> list[str]:
    e: list[str] = []
    r, s, c, d, x = cfg.run, cfg.strategy, cfg.costs, cfg.data, cfg.exchange
    if r.mode not in MODES:
        e.append(f"run.mode must be one of {MODES}, got {r.mode!r}")
    if r.interval not in INTERVALS:
        e.append(f"run.interval must be one of {sorted(INTERVALS)}, got {r.interval!r}")
    if not r.symbol.isalnum() or not r.symbol.isupper():
        e.append("run.symbol must be uppercase alphanumeric, e.g. BTCUSDT")
    if s.exit_lookback >= s.entry_lookback:
        e.append("strategy.exit_lookback must be shorter than strategy.entry_lookback")
    if s.trail_atr_mult < s.stop_atr_mult:
        e.append("strategy.trail_atr_mult must be >= strategy.stop_atr_mult")
    if d.candle_fetch_limit < s.min_history_bars + 1:
        e.append(f"data.candle_fetch_limit must be >= {s.min_history_bars + 1} for this strategy")
    if c.limit_price_buffer_bps < c.max_spread_bps / 2 + c.extra_slippage_bps:
        e.append("costs.limit_price_buffer_bps must cover half the max spread plus extra slippage")
    if x.name not in ("paper", "binance"):
        e.append("exchange.name must be 'paper' or 'binance'")
    # Mode/exchange consistency: paper never touches a real account; testnet/live never simulate.
    if r.mode == "paper" and x.name != "paper":
        e.append("run.mode='paper' requires exchange.name='paper'")
    if r.mode in ("testnet", "live") and x.name == "paper":
        e.append(f"run.mode='{r.mode}' requires a real exchange adapter (exchange.name='binance')")
    if r.mode != "live" and cfg.live.enabled:
        e.append("live.enabled=true is only allowed with run.mode='live' (avoid half-armed configs)")
    if r.mode == "live" and not cfg.live.enabled:
        e.append("run.mode='live' requires live.enabled=true")
    if r.journal_github_repo and r.journal_github_repo.count("/") != 1:
        e.append("run.journal_github_repo must look like 'owner/repo'")
    if x.base_url and not x.base_url.startswith("https://"):
        e.append("exchange.base_url must start with https://")
    if x.order_resolve_timeout_seconds < x.request_timeout_seconds:
        e.append("exchange.order_resolve_timeout_seconds must be >= request_timeout_seconds")
    return e


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError([f"config file not found: {p}"])
    try:
        raw = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"TOML parse error: {exc}"]) from exc
    return build_config(raw)


def check_live_gates(cfg: Config, cli_live_flag: bool, env: dict | None = None) -> list[str]:
    """Return the list of unmet live-trading gates (empty list = all gates satisfied).

    Gate 1: config says run.mode='live' and live.enabled=true (validated in build_config).
    Gate 2: the operator passed --live on the command line for this run.
    Gate 3: env VIGIL_LIVE_ACK equals this config's fingerprint, so any config edit
            invalidates a previous acknowledgment.
    API credentials must also be present in the environment (never in the config file).
    Reconciliation must additionally pass before the first order (enforced by the engine).
    """
    env = os.environ if env is None else env
    unmet = []
    if cfg.run.mode != "live" or not cfg.live.enabled:
        unmet.append("config is not in live mode (run.mode='live' and live.enabled=true)")
    if not cli_live_flag:
        unmet.append("--live flag not given")
    if env.get(LIVE_ACK_ENV) != cfg.fingerprint():
        unmet.append(f"{LIVE_ACK_ENV} does not match config fingerprint {cfg.fingerprint()}")
    if not env.get(cfg.exchange.api_key_env) or not env.get(cfg.exchange.api_secret_env):
        unmet.append(f"API credentials missing ({cfg.exchange.api_key_env}/{cfg.exchange.api_secret_env})")
    return unmet
