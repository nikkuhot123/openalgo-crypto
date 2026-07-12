"""Strategy-wise performance / trades / positions metrics.

Aggregates realized P&L, trade history and open positions **per Python strategy**.

The hard part is attribution. Trades are tagged with a strategy *display name*
(e.g. "HA 34-EMA Channel"), but:
  * NIFTY and SENSEX variants of the same script share one display name — they
    are only separable by the option symbol's underlying prefix.
  * Closing SELL orders may be tagged "AUTO_SQUARE_OFF" (the 15:15 square-off) or
    by a different strategy, losing the owner tag.
  * There is no realized-P&L column on trades.

Solution: FIFO-match BUY->SELL per (symbol, day) across *all* strategies and credit
the round-trip to the strategy that OPENED (bought) the leg. Then filter to the
requested strategy by (display_name, underlying-prefix). This makes the SELL tag
irrelevant and reconciles to the account-level daily P&L.
"""

from collections import defaultdict, deque
from datetime import datetime, timedelta

from utils.logging import get_logger

logger = get_logger(__name__)

# ── period helpers ───────────────────────────────────────────────────────────

_PERIODS = ("today", "week", "month", "all")


def _period_start(period: str):
    """Return the inclusive start date (date) for a period, or None for 'all'.
    Server runs in IST; naive now() is IST."""
    today = datetime.now().date()
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    return None


def _resolve_owner(strategy_id, strategy_configs):
    """(display_name, underlying_upper) for a strategy_id, or (None, None)."""
    cfg = strategy_configs.get(strategy_id) or {}
    display = cfg.get("name")
    underlying = (cfg.get("underlying") or "").upper()
    return display, underlying


def _owns(symbol, underlying):
    """A leg belongs to a NIFTY/SENSEX variant if its option symbol starts with
    that underlying. Empty underlying -> match anything (single-variant strategy)."""
    if not underlying:
        return True
    return (symbol or "").upper().startswith(underlying)


def _direction(symbol):
    s = (symbol or "").upper()
    if s.endswith("CE"):
        return "CE"
    if s.endswith("PE"):
        return "PE"
    return "?"


# ── core FIFO attribution ────────────────────────────────────────────────────

def _fifo_round_trips(rows):
    """rows: iterable of dicts {ts(datetime), symbol, action, qty, price, strategy}
    ordered by ts. Match BUY->SELL per symbol with a CONTINUOUS book (positions can
    open one day and close the next). Each closed round-trip is credited to the
    OPENING strategy tag and dated to its EXIT day (matching how sandbox_daily_pnl
    books realized P&L on the closing trade).

    Returns (round_trips, open_legs). round_trip: {day, symbol, dir, opener, qty,
    entry, exit, pnl, entry_ts, exit_ts}; open_leg: {day, symbol, dir, opener, qty,
    entry, entry_ts}.
    """
    ordered = sorted(rows, key=lambda r: r["ts"])
    books = defaultdict(deque)  # symbol -> deque[[qty, price, strategy, ts]]
    round_trips = []
    for r in ordered:
        sym = r["symbol"]
        if (r["action"] or "").upper() == "BUY":
            books[sym].append([r["qty"], r["price"], r.get("strategy"), r["ts"]])
        else:  # SELL closes open buys FIFO across days
            rem = r["qty"]
            while rem > 0 and books[sym]:
                lot = books[sym][0]
                m = min(lot[0], rem)
                pnl = m * (float(r["price"]) - float(lot[1]))
                exit_day = r["ts"].strftime("%Y-%m-%d") if hasattr(r["ts"], "strftime") else str(r["ts"])[:10]
                round_trips.append({
                    "day": exit_day, "symbol": sym, "dir": _direction(sym),
                    "opener": lot[2], "qty": m,
                    "entry": float(lot[1]), "exit": float(r["price"]),
                    "pnl": pnl, "entry_ts": lot[3], "exit_ts": r["ts"],
                })
                lot[0] -= m
                rem -= m
                if lot[0] == 0:
                    books[sym].popleft()
    open_legs = []
    for sym, q in books.items():
        for lot in q:
            if lot[0] > 0:
                d = lot[3].strftime("%Y-%m-%d") if hasattr(lot[3], "strftime") else str(lot[3])[:10]
                open_legs.append({
                    "day": d, "symbol": sym, "dir": _direction(sym),
                    "opener": lot[2], "qty": lot[0],
                    "entry": float(lot[1]), "entry_ts": lot[3],
                })
    return round_trips, open_legs


def _aggregate(round_trips):
    """Turn matched round-trips into a performance summary."""
    pnls = [rt["pnl"] for rt in round_trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    daily = defaultdict(float)
    for rt in round_trips:
        daily[rt["day"]] += rt["pnl"]
    return {
        "realized_pnl": round(sum(pnls), 2),
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 1) if n else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            None if gross_win == 0 else float("inf")
        ),
        "daily": [{"date": d, "pnl": round(daily[d], 2)} for d in sorted(daily)],
    }


# ── data sources ─────────────────────────────────────────────────────────────

def _sandbox_trade_rows(user_id, start_date):
    """Pull sandbox trades (analyzer mode) as normalized rows."""
    from database.sandbox_db import SandboxTrades, db_session

    q = db_session.query(SandboxTrades).filter(SandboxTrades.user_id == user_id)
    if start_date is not None:
        q = q.filter(SandboxTrades.trade_timestamp >= datetime.combine(start_date, datetime.min.time()))
    rows = []
    for t in q.order_by(SandboxTrades.trade_timestamp.asc()).all():
        rows.append({
            "ts": t.trade_timestamp,
            "symbol": t.symbol,
            "action": t.action,
            "qty": int(t.quantity or 0),
            "price": float(t.price or 0),
            "strategy": t.strategy,
        })
    return rows


def _sandbox_open_positions(user_id, underlying):
    """Open sandbox positions filtered by underlying prefix."""
    from database.sandbox_db import SandboxPositions, db_session

    out = []
    for p in db_session.query(SandboxPositions).filter(
        SandboxPositions.user_id == user_id,
        SandboxPositions.quantity != 0,
    ).all():
        if not _owns(p.symbol, underlying):
            continue
        out.append({
            "symbol": p.symbol,
            "qty": int(p.quantity or 0),
            "avg_price": float(p.average_price or 0),
            "ltp": float(p.ltp or 0),
            "pnl": float(p.pnl or 0),
            "dir": _direction(p.symbol),
        })
    return out


def _live_trade_rows(api_key, start_date):
    """Live broker tradebook. NOTE: broker trades carry no strategy tag, so the
    opener will be None and attribution collapses to underlying-prefix only.
    Kept functional so the panel works in live mode; exact per-strategy split in
    live requires order-time strategy tagging (documented limitation)."""
    from services.tradebook_service import get_tradebook

    rows = []
    try:
        ok, resp, _ = get_tradebook(api_key=api_key)
        if not ok:
            return rows
        for t in resp.get("data", []) or []:
            ts_raw = t.get("timestamp") or t.get("trade_timestamp") or t.get("order_timestamp") or ""
            try:
                ts = datetime.fromisoformat(str(ts_raw)[:19])
            except Exception:
                ts = datetime.now()
            if start_date is not None and ts.date() < start_date:
                continue
            rows.append({
                "ts": ts,
                "symbol": t.get("symbol", ""),
                "action": (t.get("action") or t.get("transaction_type") or "").upper(),
                "qty": int(float(t.get("quantity", 0) or 0)),
                "price": float(t.get("average_price", t.get("price", 0)) or 0),
                "strategy": t.get("strategy"),
            })
    except Exception as e:
        logger.warning(f"live tradebook fetch failed: {e}")
    return rows


def _live_open_positions(api_key, underlying):
    from services.positionbook_service import get_positions

    out = []
    try:
        ok, resp, _ = get_positions(api_key=api_key)
        if not ok:
            return out
        for p in resp.get("data", []) or []:
            qty = int(float(p.get("quantity", 0) or 0))
            if qty == 0 or not _owns(p.get("symbol", ""), underlying):
                continue
            out.append({
                "symbol": p.get("symbol", ""),
                "qty": qty,
                "avg_price": float(p.get("average_price", 0) or 0),
                "ltp": float(p.get("ltp", 0) or 0),
                "pnl": float(p.get("pnl", 0) or 0),
                "dir": _direction(p.get("symbol", "")),
            })
    except Exception as e:
        logger.warning(f"live positionbook fetch failed: {e}")
    return out


# ── public entry point ───────────────────────────────────────────────────────

def get_strategy_metrics(strategy_id, strategy_configs, user_id="nikhil",
                         period="week", api_key=None, trade_limit=50):
    """Return {performance, trades, positions, meta} for one strategy_id.

    strategy_configs: the live STRATEGY_CONFIGS dict (passed in to avoid import cycle).
    """
    if period not in _PERIODS:
        period = "week"
    display, underlying = _resolve_owner(strategy_id, strategy_configs)
    if display is None:
        return {"status": "error", "message": "Strategy not found"}

    try:
        from database.settings_db import get_analyze_mode
        analyze = get_analyze_mode()
    except Exception:
        analyze = True

    start_date = _period_start(period)

    if analyze:
        rows = _sandbox_trade_rows(user_id, None)  # pull all for continuous FIFO
        positions = _sandbox_open_positions(user_id, underlying)
    else:
        rows = _live_trade_rows(api_key, None)
        positions = _live_open_positions(api_key, underlying)

    round_trips, open_legs = _fifo_round_trips(rows)

    # 1. Filter to this strategy's variant
    mine = [
        rt for rt in round_trips
        if (rt["opener"] == display or rt["opener"] is None) and _owns(rt["symbol"], underlying)
    ]

    # 2. Filter to period start_date (by exit day)
    if start_date is not None:
        start_str = start_date.isoformat()
        mine = [rt for rt in mine if rt["day"] >= start_str]

    perf = _aggregate(mine)

    mine_sorted = sorted(mine, key=lambda r: r["exit_ts"], reverse=True)[:trade_limit]
    trades = [{
        "date": rt["day"],
        "symbol": rt["symbol"],
        "dir": rt["dir"],
        "qty": rt["qty"],
        "entry": round(rt["entry"], 2),
        "exit": round(rt["exit"], 2),
        "pnl": round(rt["pnl"], 2),
        "exit_time": rt["exit_ts"].strftime("%H:%M") if hasattr(rt["exit_ts"], "strftime") else "",
    } for rt in mine_sorted]

    return {
        "status": "success",
        "strategy_id": strategy_id,
        "period": period,
        "mode": "analyze" if analyze else "live",
        "underlying": underlying,
        "performance": perf,
        "trades": trades,
        "positions": positions,
    }


def reconcile_check(user_id="nikhil"):
    """Diagnostic: sum of per-strategy round-trip P&L per day vs sandbox_daily_pnl.
    Returns list of {date, attributed, authoritative, diff}. For tests/validation."""
    from database.sandbox_db import SandboxDailyPnL, db_session

    rows = _sandbox_trade_rows(user_id, None)
    round_trips, _ = _fifo_round_trips(rows)
    by_day = defaultdict(float)
    for rt in round_trips:
        by_day[rt["day"]] += rt["pnl"]

    out = []
    for rec in db_session.query(SandboxDailyPnL).filter(
        SandboxDailyPnL.user_id == user_id
    ).all():
        d = str(rec.date)[:10]
        auth = float(rec.realized_pnl or 0)
        attr = round(by_day.get(d, 0.0), 2)
        out.append({"date": d, "attributed": attr, "authoritative": auth,
                    "diff": round(attr - auth, 2)})
    return sorted(out, key=lambda r: r["date"])
