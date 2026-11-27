"""Optional: write each closed trade into an Obsidian vault as a Markdown note.

Reads only the audit log (the source of truth), so it can be re-run at any
time; notes that already exist are never overwritten. Trading never depends
on this: the engine calls it after a position closes and ignores failures
(they are recorded in the audit log as `obsidian_export_error`).

Notes go to <vault>/Trading/Trades/ and use the vault's Trade Template fields.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def _local(ts_ms: int) -> datetime:
    """Note timestamps in VIGIL_TZ (e.g. America/New_York) if set, else this machine's local time."""
    import os
    tz = os.environ.get("VIGIL_TZ")
    if tz:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(tz))
    return datetime.fromtimestamp(ts_ms / 1000)


def _d(v) -> Decimal | None:
    return Decimal(v) if v not in (None, "None") else None


def closed_trades(audit_path: str | Path) -> list[dict]:
    evals, trades, cur = {}, [], None
    with open(audit_path) as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            ev, data = e["event"], e["data"]
            if ev == "evaluation" and data.get("kind") == "bar":
                evals[e["evaluation_id"]] = data
            elif ev == "order_intent" and data.get("purpose") == "ENTER":
                ev_data = evals.get(e["evaluation_id"], {})
                sizing = (ev_data.get("sizing") or {}).get("details") or {}
                cur = {"entry_coid": e["client_order_id"], "mode": e["mode"], "symbol": e["symbol"],
                       "signal_reason": (ev_data.get("signal") or {}).get("reason"),
                       "stop": data.get("stop_price"), "risk": _d(sizing.get("worst_case_loss")),
                       "est_cost": _d(sizing.get("expected_round_trip_cost")),
                       "cost_to_risk": sizing.get("cost_to_risk"),
                       "buys": [], "sells": [], "pnl": Decimal(0), "fees": Decimal(0), "exit_reason": None}
            elif cur is None:
                continue
            elif ev == "order_fill":
                fill = (_d(data["price"]), _d(data["qty"]), e["ts_ms"])
                (cur["buys"] if data["side"] == "BUY" else cur["sells"]).append(fill)
                cur["fees"] += _d(data["fee_quote_equiv"]) or Decimal(0)
                if data.get("realized_pnl") not in (None, "None"):
                    cur["pnl"] += _d(data["realized_pnl"])
            elif ev == "exit_decision" and cur["exit_reason"] is None:
                cur["exit_reason"] = data.get("reason")
            elif ev == "position_closed":
                if cur["buys"]:
                    trades.append(cur)
                cur = None
    return trades


def _avg(fills):
    q = sum((f[1] for f in fills), Decimal(0))
    return (sum((f[0] * f[1] for f in fills), Decimal(0)) / q) if q else None


def _fmt(v, places="0.01"):
    return "" if v is None else str(Decimal(v).quantize(Decimal(places)))


def render_notes(audit_path: str | Path) -> list[tuple[str, str]]:
    """Return (vault-relative path, Markdown) for every closed trade in the audit log."""
    out = []
    for t in closed_trades(audit_path):
        opened = _local(t["buys"][0][2])
        closed = _local((t["sells"] or t["buys"])[-1][2])
        day = opened.strftime("%Y-%m-%d")
        name = f"{day} {opened.strftime('%H%M')} {t['symbol']} Long (vigil {t['mode']}).md"
        entry, exit_ = _avg(t["buys"]), _avg(t["sells"])
        r = (t["pnl"] / t["risk"]) if t["risk"] else None
        result = "win" if t["pnl"] > 0 else "loss"
        text = f"""---
tags: [trade, bot]
date: {day}
time: {opened.strftime('%H:%M')}
instrument: {t['symbol']}
direction: long
strategy: Donchian Breakout (Vigil)
setup: {t['signal_reason'] or ''}
session:
entry: {_fmt(entry)}
stop: {_fmt(_d(t['stop']))}
target: trailing
exit: {_fmt(exit_)}
risk_usd: {_fmt(t['risk'])}
pnl_usd: {_fmt(t['pnl'])}
r_multiple: {_fmt(r)}
result: {result}
source: vigil-{t['mode']}
closed: {closed.strftime('%Y-%m-%d %H:%M')}
exit_reason: {t['exit_reason'] or ''}
fees_usd: {_fmt(t['fees'])}
client_order_id: {t['entry_coid']}
---
# {t['symbol']} long: {result} ({_fmt(r)}R)

**Day:** [[{day}]] · **Strategy:** [[Donchian Breakout (Vigil)]] · **Bot:** [[Vigil Bot]]

## Why it was taken
Signal `{t['signal_reason']}`. Estimated round-trip costs {_fmt(t['est_cost'])} USD
({_fmt(_d(t['cost_to_risk']), '0.001')}R). Worst-case risk {_fmt(t['risk'])} USD.

## What happened
Entered {_fmt(entry)} → exited {_fmt(exit_)} ({t['exit_reason'] or 'n/a'}).
Net P&L {_fmt(t['pnl'])} USD after {_fmt(t['fees'])} USD fees.

## Takeaway

"""
        out.append((f"Trading/Trades/{name}", text))
    return out


def export_trades(audit_path: str | Path, vault: str | Path) -> list[Path]:
    """Write notes into a local vault folder. Existing notes are never overwritten."""
    written = []
    for rel, text in render_notes(audit_path):
        path = Path(vault) / rel
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written


class GitHubJournal:
    """Publish notes into a GitHub repository that holds the Obsidian vault.

    Uses the GitHub contents API (no git binary needed). Creates files only;
    an existing file is never modified, so it cannot clobber edits made in Obsidian.
    The token needs Contents read/write on that one repository and is read from
    the environment, never from the config file.
    """

    def __init__(self, repo: str, token: str, branch: str = "main", prefix: str = "",
                 api: str = "https://api.github.com", transport=None):
        if not token:
            raise ValueError("GitHub token missing")
        self.repo, self.token, self.branch, self.prefix, self.api = repo, token, branch, prefix.strip("/"), api
        self._transport = transport or self._urllib

    def _urllib(self, method, url, body):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vigil-journal"})
        try:
            from .exchange.binance import _tls_context
            with urllib.request.urlopen(req, timeout=20, context=_tls_context()) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def publish(self, notes: list[tuple[str, str]]) -> list[str]:
        import base64
        import urllib.parse
        created = []
        for rel, text in notes:
            path = f"{self.prefix}/{rel}" if self.prefix else rel
            url = f"{self.api}/repos/{self.repo}/contents/{urllib.parse.quote(path)}"
            status, _ = self._transport("GET", f"{url}?ref={self.branch}", None)
            if status == 200:
                continue
            if status != 404:
                raise RuntimeError(f"GitHub GET {path}: HTTP {status}")
            status, _ = self._transport("PUT", url, {
                "message": f"vigil: trade note {Path(rel).stem}", "branch": self.branch,
                "content": base64.b64encode(text.encode()).decode()})
            if status not in (200, 201):
                raise RuntimeError(f"GitHub PUT {path}: HTTP {status}")
            created.append(path)
        return created
