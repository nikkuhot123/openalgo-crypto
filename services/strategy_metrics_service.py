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

import gzip
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

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
    display = cfg.get("name") or ""
    if "(" in display:
        display = display.split("(", 1)[0].strip()
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


# ── per-strategy symbol ownership (from each strategy's own log files) ────────

_OPT_SYMBOL_RE = re.compile(r"\b([A-Z]{3,}\d{2}[A-Z0-9]*(?:CE|PE))\b")


# Only lines that evidence an ORDER (or holding) may contribute a symbol. POV
# logs every leg it merely scans ("Tracking leg:", "Action: WAIT | Score:"), so
# scanning all lines made a strategy claim contracts it never traded (bug
# 2026-07-28: POV SENSEX, which traded nothing, claimed HA-EMA SENSEX's trade).
_TRADE_LINE_MARKERS = (
    "placing buy order", "squeeze detected", "reversal detected", "breakout signal",
    "trade entered", "entered trade", "closing position", "exit response",
    "order response", "orderid", "target reached", "target hit", "stop-loss hit",
    "premium sl hit", "monitoring trade", "monitoring:", "adopting",
)


def _strategy_log_files(strategy_id, logs_dir):
    """Every log file belonging to one strategy, whoever owns the process.

    Three shapes exist and all must be read or trade attribution silently
    under-reports:
      {id}_{YYYYMMDD_HHMMSS}_IST.log   Flask subprocess runner
      {id}.log                         systemd `StandardOutput=append:`
      {id}.log-<date>-<epoch>[.gz]     logrotate copytruncate archives

    Globbing only the dated form attributed ZERO trades to systemd-managed
    strategies; ignoring the rotated archives would drop every trade older than
    the current rotation window from a weekly/monthly view.
    """
    if not logs_dir:
        return []
    root = Path(logs_dir)
    try:
        files = list(root.glob(f"{strategy_id}_[0-9]*.log"))
        files += list(root.glob(f"{strategy_id}_[0-9]*.log-*"))
        files += list(root.glob(f"{strategy_id}.log-*"))
        flat = root / f"{strategy_id}.log"
        if flat.exists():
            files.append(flat)
        return files
    except Exception:
        return []


def _open_log(fp):
    """Text handle for a strategy log, transparently decompressing rotated .gz."""
    if str(fp).endswith(".gz"):
        return gzip.open(fp, "rt", encoding="utf-8", errors="ignore")
    return open(fp, "r", encoding="utf-8", errors="ignore")


def _strategy_log_symbols(strategy_id, start_date, logs_dir):
    """Set of option symbols THIS strategy actually TRADED or HELD, from its own logs.

    Each strategy writes `{strategy_id}_{YYYYMMDD_HHMMSS}_IST.log`, so the log is
    an authoritative per-strategy record — the live broker tradebook carries no
    strategy tag, and two same-underlying variants share a display name. Symbols
    are approximate (two strategies can trade one contract the same day), so this
    is only the fallback behind order-id matching; it is still the sole key
    available for open positions.
    """
    symbols = set()
    files = _strategy_log_files(strategy_id, logs_dir)
    if not files:
        return symbols
    cutoff = None
    if start_date is not None:
        # one-day slack: a leg opened just before the window is still ours
        cutoff = datetime.combine(start_date - timedelta(days=1), datetime.min.time())
    for fp in files:
        try:
            if cutoff is not None and datetime.fromtimestamp(fp.stat().st_mtime) < cutoff:
                continue
            with _open_log(fp) as f:
                for line in f:
                    if "CE" not in line and "PE" not in line:
                        continue
                    low = line.lower()
                    if any(m in low for m in _TRADE_LINE_MARKERS):
                        symbols.update(_OPT_SYMBOL_RE.findall(line))
        except Exception:
            continue
    return symbols


_ORDERID_RE = re.compile(r"['\"]orderid['\"]\s*:\s*['\"]?(\d{6,})['\"]?")


def _strategy_log_orderids(strategy_id, start_date, logs_dir):
    """Set of broker order IDs THIS strategy placed, from its own log files.

    Order ID is the only EXACT attribution key. Symbols are not sufficient: all
    strategies trade ATM options on the same two indices, so the same contract is
    routinely traded by two strategies on one day (bug 2026-07-28: HA-EMA bought
    NIFTY28JUL2624000CE at 09:45 and Judas bought the same symbol at 10:07 —
    symbol matching credited both round-trips to both strategies). Empty set =>
    caller falls back to symbols, then to the opener tag.
    """
    oids = set()
    files = _strategy_log_files(strategy_id, logs_dir)
    if not files:
        return oids
    cutoff = None
    if start_date is not None:
        cutoff = datetime.combine(start_date - timedelta(days=1), datetime.min.time())
    for fp in files:
        try:
            if cutoff is not None and datetime.fromtimestamp(fp.stat().st_mtime) < cutoff:
                continue
            with _open_log(fp) as f:
                for line in f:
                    if "orderid" in line:
                        oids.update(_ORDERID_RE.findall(line))
        except Exception:
            continue
    return oids


# ── core FIFO attribution ────────────────────────────────────────────────────

def _fifo_round_trips(rows):
    """rows: iterable of dicts {ts(datetime), symbol, action, qty, price, strategy}
    ordered by ts. Match BUY->SELL per symbol with a CONTINUOUS book (positions can
    open one day and close the next). Each closed round-trip is credited to the
    OPENING strategy tag and dated to its EXIT day (matching how sandbox_daily_pnl
    books realized P&L on the closing trade).

    Returns (round_trips, open_legs). round_trip: {day, symbol, dir, opener, qty,
    entry, exit, pnl, entry_ts, exit_ts, entry_oid, exit_oid}; open_leg: {day,
    symbol, dir, opener, qty, entry, entry_ts, entry_oid}.
    """
    ordered = sorted(rows, key=lambda r: r["ts"])
    books = defaultdict(deque)  # symbol -> deque[[qty, price, strategy, ts, orderid]]
    round_trips = []
    for r in ordered:
        sym = r["symbol"]
        if (r["action"] or "").upper() == "BUY":
            books[sym].append([r["qty"], r["price"], r.get("strategy"), r["ts"], r.get("orderid")])
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
                    "entry_oid": lot[4], "exit_oid": r.get("orderid"),
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
                    "entry_oid": lot[4],
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
            "orderid": str(getattr(t, "orderid", "") or "") or None,
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


_TS_FORMATS = (
    "%H:%M:%S %d-%m-%Y",   # observed live shape: "13:17:28 28-07-2026"
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
)


def _parse_broker_ts(raw):
    """Broker trade timestamp -> datetime, or None if no known format matches.

    Returning None (and dropping the row) is deliberate: a wrong timestamp is
    far worse than a missing one. FIFO matching orders BUY->SELL purely by ts,
    so a bad value silently mis-pairs legs and fabricates P&L. Bug 2026-07-28:
    fromisoformat() choked on "13:17:28 28-07-2026", every row fell back to
    now(), and because the tradebook is newest-first the matcher saw each SELL
    before its BUY -> zero round-trips -> every strategy reported 0 trades / Rs0.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _live_trade_rows(api_key, start_date):
    """Live broker tradebook. NOTE: broker trades carry no strategy tag, so the
    opener is None and per-strategy attribution is resolved by the caller from
    each strategy's own log symbols."""
    from services.tradebook_service import get_tradebook

    rows = []
    try:
        ok, resp, _ = get_tradebook(api_key=api_key)
        if not ok:
            return rows
        unparsed = 0
        for t in resp.get("data", []) or []:
            ts = _parse_broker_ts(
                t.get("timestamp") or t.get("trade_timestamp") or t.get("order_timestamp")
            )
            if ts is None:
                unparsed += 1
                continue
            if start_date is not None and ts.date() < start_date:
                continue
            rows.append({
                "ts": ts,
                "symbol": t.get("symbol", ""),
                "action": (t.get("action") or t.get("transaction_type") or "").upper(),
                "qty": int(float(t.get("quantity", 0) or 0)),
                "price": float(t.get("average_price", t.get("price", 0)) or 0),
                "strategy": t.get("strategy"),
                "orderid": str(t.get("orderid") or "") or None,
            })
        if unparsed:
            logger.warning(
                f"live tradebook: dropped {unparsed} trade(s) with unrecognised "
                f"timestamp format — add it to _TS_FORMATS (metrics will under-report)")
    except Exception as e:
        logger.warning(f"live tradebook fetch failed: {e}")
    return rows


def _live_open_positions(api_key, underlying):
    from services.positionbook_service import get_positionbook

    out = []
    try:
        ok, resp, _ = get_positionbook(api_key=api_key)
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

def _merge_with_archive(live_rows):
    """Archive today's broker fills, then return archive + live, de-duplicated.

    The broker tradebook is a same-session view; the archive is the only thing
    that remembers last week. Writing on every read is deliberate -- the UI
    polling metrics is what keeps the archive current, so history accumulates
    without a separate daemon. It is idempotent (unique on
    orderid+action+qty+price), so repeated reads add nothing.
    """
    try:
        from database.strategy_trades_db import archive_trades, fetch_trades
    except Exception as e:
        logger.warning(f"trade archive unavailable, history limited to today: {e}")
        return live_rows

    try:
        archive_trades([dict(r, source="live") for r in live_rows])
    except Exception as e:
        logger.warning(f"trade archive write failed: {e}")

    try:
        archived = fetch_trades()
    except Exception as e:
        logger.warning(f"trade archive read failed: {e}")
        return live_rows

    seen, merged = set(), []
    for r in list(archived) + list(live_rows):
        key = (str(r.get("orderid") or ""), (r.get("action") or "").upper(),
               int(r.get("qty") or 0), round(float(r.get("price") or 0), 4))
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    merged.sort(key=lambda r: r["ts"])
    return merged


def get_strategy_metrics(strategy_id, strategy_configs, user_id="nikhil",
                         period="week", api_key=None, trade_limit=50, logs_dir=None):
    """Return {performance, trades, positions, meta} for one strategy_id.

    strategy_configs: the live STRATEGY_CONFIGS dict (passed in to avoid import cycle).
    """
    if period not in _PERIODS:
        period = "week"
    display, underlying = _resolve_owner(strategy_id, strategy_configs)
    if not display:
        raise ValueError(f"Strategy not found: {strategy_id}")

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
        # The broker tradebook only serves the CURRENT session, so on its own
        # every period collapses to today and History renders empty. Archive
        # whatever it returns, then read the union back: today from the
        # broker, everything older from the local archive.
        rows = _merge_with_archive(_live_trade_rows(api_key, None))
        positions = _live_open_positions(api_key, underlying)

    round_trips, open_legs = _fifo_round_trips(rows)

    # Per-strategy ownership read from this strategy's OWN log files.
    # Precedence: order IDs (exact) -> symbols (approximate) -> opener tag.
    log_oids = _strategy_log_orderids(strategy_id, start_date, logs_dir)
    log_syms = _strategy_log_symbols(strategy_id, start_date, logs_dir)

    # 1. Filter to THIS strategy. Order ID is exact, so when the log yields IDs we
    #    use only those: symbol matching double-counts whenever two strategies
    #    trade the same contract on one day (routine — they all buy ATM on the
    #    same two indices). Symbols remain the fallback for logs predating order-id
    #    logging. The `opener is None` clause was dropped earlier: it fanned every
    #    untagged (i.e. every live) round-trip onto every same-underlying strategy.
    def _mine(rt):
        if not _owns(rt["symbol"], underlying):
            return False
        if log_oids:
            return rt.get("entry_oid") in log_oids or rt.get("exit_oid") in log_oids
        if rt["symbol"] in log_syms:
            return True
        return rt["opener"] is not None and rt["opener"] == display

    mine = [rt for rt in round_trips if _mine(rt)]

    # Open positions carry neither a strategy tag nor an order id in the
    # positionbook, so symbol is the only key. Gate unconditionally: an empty
    # allowlist means this strategy has no logged trade/hold, so it cannot own an
    # open leg — showing every same-underlying position there was the leak.
    positions = [p for p in positions if p["symbol"] in log_syms]

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
