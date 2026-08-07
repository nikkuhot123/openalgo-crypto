"""Persistent per-strategy fill archive.

WHY THIS EXISTS
    Strategy Performance & History read `_live_trade_rows()`, which calls the
    broker tradebook. That endpoint only ever serves the CURRENT session, so
    in live mode every period -- week, month, all -- collapsed to today and
    the UI showed no history. (Analyzer mode was unaffected: sandbox trades
    live in sandbox.db, which persists.)

    This table is the missing persistent store. Fills are archived as they are
    observed, so history survives the session, the broker's retention policy
    and app restarts.

ATTRIBUTION
    Broker fills carry no strategy tag. Two sources supply one:
      - `strategy` recorded at archive time by the caller (order_logs
        backfill knows it exactly, from the orderstatus request payload)
      - NULL, when the fill came from a plain tradebook snapshot; the metrics
        service then attributes it the way it always has, from each
        strategy's own log symbols/orderids.

IDEMPOTENCE
    (orderid, action, qty, price) is UNIQUE, so re-archiving the same
    tradebook -- which happens on every metrics read -- is a no-op. Rows are
    never updated once written: a fill is a historical fact.

Follows the repo's one-engine-per-module pattern.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    or_,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

# Same split as every other database module here: NullPool for SQLite,
# real pooling for PostgreSQL (pool_size is invalid with NullPool).
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class StrategyTrade(Base):
    __tablename__ = "strategy_trades"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, nullable=False, index=True)   # exchange fill time (IST, naive)
    orderid = Column(String(64), nullable=False, index=True)
    strategy = Column(String(128), nullable=True, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    exchange = Column(String(16), nullable=True)
    action = Column(String(8), nullable=False)          # BUY / SELL
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)               # average fill price
    source = Column(String(16), nullable=False, default="live")  # live | sandbox | backfill
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("orderid", "action", "qty", "price", name="uq_strategy_trade_fill"),
    )


def init_db():
    Base.metadata.create_all(bind=engine)


def archive_trades(rows):
    """Insert fills, skipping ones already present. Returns the count added.

    `rows` are dicts with ts / orderid / symbol / action / qty / price and
    optional strategy / exchange / source. Anything unparseable is skipped
    rather than raised: archiving must never break the caller that is only
    trying to render a metrics page.
    """
    if not rows:
        return 0
    added = 0
    # The same fill can appear several times in one batch: orderstatus is
    # polled repeatedly, so a completed order is logged on every poll. The
    # DB check below only sees COMMITTED rows, so in-batch repeats would hit
    # the unique constraint and roll the whole archive write back.
    batch_keys = set()
    try:
        for r in rows:
            oid = str(r.get("orderid") or "").strip()
            ts = r.get("ts")
            if not oid or ts is None:
                continue
            try:
                qty = int(float(r.get("qty") or 0))
                price = round(float(r.get("price") or 0), 4)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0:
                continue
            action = str(r.get("action") or "").upper()
            key = (oid, action, qty, price)
            if key in batch_keys:
                continue
            batch_keys.add(key)
            exists = (
                db_session.query(StrategyTrade.id)
                .filter(
                    StrategyTrade.orderid == oid,
                    StrategyTrade.action == action,
                    StrategyTrade.qty == qty,
                    StrategyTrade.price == price,
                )
                .first()
            )
            if exists:
                continue
            db_session.add(
                StrategyTrade(
                    ts=ts,
                    orderid=oid,
                    strategy=(r.get("strategy") or None),
                    symbol=str(r.get("symbol") or ""),
                    exchange=(r.get("exchange") or None),
                    action=action,
                    qty=qty,
                    price=price,
                    source=str(r.get("source") or "live"),
                )
            )
            added += 1
        if added:
            db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.warning(f"archive_trades failed: {e}")
        return 0
    return added


def fetch_trades(start_date=None, end_date=None, strategy=None):
    """Archived fills as metrics-shaped dicts, oldest first."""
    try:
        q = db_session.query(StrategyTrade)
        if start_date is not None:
            q = q.filter(StrategyTrade.ts >= datetime.combine(start_date, datetime.min.time()))
        if end_date is not None:
            q = q.filter(StrategyTrade.ts <= datetime.combine(end_date, datetime.max.time()))
        if strategy:
            q = q.filter(or_(StrategyTrade.strategy == strategy, StrategyTrade.strategy.is_(None)))
        return [
            {
                "ts": t.ts,
                "symbol": t.symbol,
                "action": t.action,
                "qty": t.qty,
                "price": t.price,
                "strategy": t.strategy,
                "orderid": t.orderid,
            }
            for t in q.order_by(StrategyTrade.ts.asc()).all()
        ]
    except Exception as e:
        logger.warning(f"fetch_trades failed: {e}")
        return []


def archive_span():
    """(earliest, latest, count) -- useful for telling the user how far back
    history actually goes."""
    try:
        n = db_session.query(StrategyTrade).count()
        if not n:
            return None, None, 0
        lo = db_session.query(StrategyTrade.ts).order_by(StrategyTrade.ts.asc()).first()[0]
        hi = db_session.query(StrategyTrade.ts).order_by(StrategyTrade.ts.desc()).first()[0]
        return lo, hi, n
    except Exception as e:
        logger.warning(f"archive_span failed: {e}")
        return None, None, 0


init_db()
