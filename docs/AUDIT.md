# Audit trail

File: `<state_dir>/audit.jsonl`. Append-only JSON Lines, one record per line, fsynced
before the action it describes proceeds. Every record has the same envelope:

| Field | Meaning |
|---|---|
| `schema` | audit schema version (currently 1) |
| `seq` | strictly increasing sequence number, continuous across restarts |
| `record_id` | UUIDv4 for this record |
| `ts`, `ts_ms` | UTC time the record was written (ISO-8601 ms, and epoch ms) |
| `run_id` | UUIDv4 per process start; groups one run's records |
| `mode` | `paper` / `testnet` / `live` |
| `event` | event type (table below) |
| `symbol` | trading pair |
| `evaluation_id` | stable id of the decision this record belongs to (see below), or null |
| `client_order_id` | stable id of the order this record belongs to, or null |
| `data` | event-specific payload |
| `prev_hash`, `hash` | `hash = sha256(prev_hash + canonical JSON of the record without hash)` |

All prices, quantities and money values are **strings** holding exact decimals.

## Stable identifiers

- **Bar evaluation:** `evaluation_id = "<SYMBOL>:<interval>:<bar_close_time_ms>"`, e.g. `BTCUSDT:1h:1790003599999`.
- **Intrabar stop check:** `"<SYMBOL>:STOP:<entry client_order_id>"`.
- **Exit attempt:** `"<SYMBOL>:EXIT:<entry client_order_id>:<attempt>"`.
- **Order:** `client_order_id = "vg<mode letter>-" + sha256(evaluation_id|purpose)[:24]`. It is
  derived from the evaluation, so one decision maps to exactly one order id, and the id is
  known (and logged) before the request is sent. Exchange order ids and trade ids appear
  in `order_ack` / `order_status` / `order_fill` once known.

## Events

| Event | When | Key `data` fields |
|---|---|---|
| `run_start` | process start | version, `config_fingerprint`, full `config`, `live_armed`, symbol `rules`, halt, position |
| `startup_refused` | integrity halt still active at start | halt |
| `reconcile_start` / `reconcile_result` | startup, after uncertainty, after halt reset | `ok`, `checks[]` (name, passed, detail), `evidence` (balances, open orders), local position |
| `data_quality` | data validity changes | `ok`, `issues[]` |
| `evaluation` (`kind: "bar"`) | once per closed bar (plus retries while data is invalid and `final: false`) | `market_data` (last 3 candles, top 5 book levels, timestamps), `data_checks` (candle/book issues, conflicts), `signal` (action, reason, reference price, stop, indicators), `position_before`, `equity`, `balances`, `risk_checks[]`, `sizing` (ok, qty, limit, reject reason, **all cost and sizing intermediates**), `decision` (action, reason, qty, limit, stop) |
| `evaluation` (`kind: "stop_check"`) | best bid at/below stop between bars | book, stop, best bid, decision |
| `exit_decision` | before each exit attempt | reason, attempt number, exit sizing, position |
| `exit_blocked` | exit not allowed (integrity halt, unreconciled, unresolved order) | checks |
| `order_intent` | persisted **before** any network call | purpose, side, qty, limit, TIF, stop, reference price, orders today |
| `order_submit` | just before the request | the exact request parameters |
| `order_ack` | exchange accepted the request | exchange order id, status, filled qty, avg price, raw response |
| `order_rejected` | exchange refused | reason, raw response |
| `order_uncertain` | timeout / 5xx / unparseable reply to a submit | error; the order is then queried, never resubmitted |
| `order_query_error` | a status query failed | error |
| `order_status` | any status or filled-qty change | status, filled qty, avg price |
| `order_cancel` | defensive cancel of an unexpectedly resting order | reason, resulting status |
| `order_fill` | each fill applied (exactly once per trade id) | trade id, price, qty, fee, fee asset, fee in quote, `fee_estimated`, realized PnL, `position_after` |
| `duplicate_order_prevented` | same decision attempted twice | existing status |
| `position_closed` | position fully exited (or only dust remains) | reason, dust qty, realized PnL today |
| `day_roll` | first tick of a new UTC day | previous day's PnL and orders, new day-start equity |
| `halt` / `halt_reset` | trading stopped / operator cleared it | category, reason / previous halt, operator, reason |
| `error` | transient, integrity or unexpected errors | type, error, consecutive count |
| `run_stop` | process exit | ticks, halt, position |

## Reconstructing a trade

```bash
# every decision that led to an order
jq -c 'select(.event=="evaluation" and .data.decision.action!="HOLD")' var/paper/audit.jsonl
# full lifecycle of one order
jq -c 'select(.client_order_id=="vgp-…") | [.ts,.event,.data.status,.data.qty,.data.price]' var/paper/audit.jsonl
# why entries were skipped
jq -r 'select(.event=="evaluation") | .data.decision.reason' var/paper/audit.jsonl | sort | uniq -c
# estimated vs realized costs: compare sizing.details.entry_cost_bps with order_fill prices
```

In Python: `[json.loads(l) for l in open(path)]`, then group by `evaluation_id` or
`client_order_id`.

## Integrity

`python -m vigil verify-audit <config>` re-computes the hash chain. Editing, removing or
reordering any line is reported with its line number. The chain protects against
accidental or casual edits, not against someone who can rewrite the whole file. For that,
ship the log off-host (append it to object storage or a log service).

The state file (`state.json`) is the system's working memory. The audit log is the
record. When they disagree, the audit log plus the exchange's own history is the source
of truth.
