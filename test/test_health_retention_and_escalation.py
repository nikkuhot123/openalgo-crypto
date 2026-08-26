#!/usr/bin/env python
"""Retention maintenance and alert escalation.

Two gaps found while diagnosing the 2026-08-25 outage. Neither caused it, but
both are why it lasted hours instead of minutes.

1. RETENTION WAS STARTUP-ONLY. `purge_old_metrics` ran once from
   `init_health_monitoring` and never again; `purge_old_data_logs` and
   `purge_old_traffic_logs` have the same shape. A process that stays up for
   weeks purges exactly once. Worse, DELETE moves pages to SQLite's free list
   and never shrinks the file, so db/health.db reached 1.38 GB while retention
   was nominally 7 days -- 202,863 rows, 23,816 of them already expired.

2. ALERTS REACHED NOBODY. The collector recorded
   `File descriptor count critical: 944` every ten seconds for four hours while
   the site was unreachable. The only consumer of HealthAlert is the health
   page, so the signal existed and the escalation did not.
"""
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

hm = pytest.importorskip("utils.health_monitor")


# --------------------------------------------------------------- escalation
@pytest.fixture(autouse=True)
def _reset_cooldowns():
    hm._last_escalated.clear()
    yield
    hm._last_escalated.clear()


@pytest.fixture
def notifier(monkeypatch):
    """Capture what would be pushed, without touching Telegram."""
    sent = []
    mod = types.ModuleType("services.telegram_alert_service")
    mod.send_health_alert = lambda msg: sent.append(msg)
    monkeypatch.setitem(sys.modules, "services.telegram_alert_service", mod)
    monkeypatch.setattr(hm, "HEALTH_ALERT_PUSH", True)
    monkeypatch.setattr(hm, "HEALTH_ALERT_COOLDOWN_SECS", 1800)
    return sent


def test_a_failing_metric_is_pushed(notifier):
    hm._escalate_failures({"fd": {"status": "fail", "count": 944}})
    assert len(notifier) == 1
    assert "fd" in notifier[0] and "944" in notifier[0]


def test_healthy_metrics_are_silent(notifier):
    hm._escalate_failures({"fd": {"status": "pass", "count": 40},
                           "threads": {"status": "warn", "count": 60}})
    assert notifier == [], "only 'fail' should page a human"


def test_cooldown_suppresses_the_repeat_storm(notifier):
    """The real incident logged this every 10s for four hours."""
    for _ in range(240):
        hm._escalate_failures({"fd": {"status": "fail", "count": 944}})
    assert len(notifier) == 1, f"cooldown leaked {len(notifier)} messages"


def test_cooldown_is_per_metric_not_global(notifier):
    hm._escalate_failures({"fd": {"status": "fail", "count": 944}})
    hm._escalate_failures({"threads": {"status": "fail", "count": 2888}})
    assert len(notifier) == 2, "a second, different failure must still get through"


def test_cooldown_expires(notifier, monkeypatch):
    monkeypatch.setattr(hm, "HEALTH_ALERT_COOLDOWN_SECS", 0)
    hm._escalate_failures({"fd": {"status": "fail", "count": 944}})
    hm._escalate_failures({"fd": {"status": "fail", "count": 950}})
    assert len(notifier) == 2


def test_push_can_be_disabled(notifier, monkeypatch):
    monkeypatch.setattr(hm, "HEALTH_ALERT_PUSH", False)
    hm._escalate_failures({"fd": {"status": "fail", "count": 944}})
    assert notifier == []


def test_a_broken_notifier_cannot_stop_collection(monkeypatch):
    """This runs inside the collector loop."""
    mod = types.ModuleType("services.telegram_alert_service")

    def boom(_msg):
        raise RuntimeError("telegram down")

    mod.send_health_alert = boom
    monkeypatch.setitem(sys.modules, "services.telegram_alert_service", mod)
    monkeypatch.setattr(hm, "HEALTH_ALERT_PUSH", True)
    hm._escalate_failures({"fd": {"status": "fail", "count": 944}})  # must not raise


def test_missing_metric_dicts_are_tolerated(notifier):
    hm._escalate_failures({"fd": None, "memory": {}})
    assert notifier == []


# ---------------------------------------------------------------- retention
def _make_db(path, mb):
    """A SQLite file with `mb` of deleted rows -- pages on the free list."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE junk (id INTEGER PRIMARY KEY, blob TEXT)")
    payload = "x" * 4000
    rows = int((mb * 1024 * 1024) / 4000)
    conn.executemany("INSERT INTO junk (blob) VALUES (?)", [(payload,) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM junk")
    conn.commit()
    conn.close()


def test_vacuum_reclaims_freed_pages(tmp_path, monkeypatch):
    """DELETE alone never shrinks the file -- the 1.38 GB mechanism."""
    db = tmp_path / "db"
    db.mkdir()
    target = db / "health.db"
    _make_db(target, mb=3)
    before = target.stat().st_size
    assert before > 2 * 1024 * 1024, "fixture did not grow the file"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hm, "HEALTH_VACUUM_MIN_MB", 1)
    hm._vacuum_if_oversized("health.db")

    after = target.stat().st_size
    assert after < before / 2, f"VACUUM did not reclaim: {before} -> {after}"


def test_vacuum_skips_a_large_but_BUSY_file(tmp_path, monkeypatch):
    """The refinement: health.db was still 471 MB of LIVE rows after the
    reclaim. Triggering on total size would VACUUM every pass, block readers
    for ~10s, and recover nothing. Only the free list justifies a rewrite."""
    db = tmp_path / "db"
    db.mkdir()
    target = db / "health.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE live (id INTEGER PRIMARY KEY, blob TEXT)")
    payload = "x" * 4000
    conn.executemany("INSERT INTO live (blob) VALUES (?)", [(payload,) for _ in range(800)])
    conn.commit()
    conn.close()  # nothing deleted -> empty free list
    before = target.stat().st_size

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hm, "HEALTH_VACUUM_MIN_MB", 1)
    hm._vacuum_if_oversized("health.db")

    assert target.stat().st_size == before, "VACUUMed a busy file with nothing to reclaim"


def test_vacuum_skips_when_little_is_reclaimable(tmp_path, monkeypatch):
    db = tmp_path / "db"
    db.mkdir()
    target = db / "health.db"
    _make_db(target, mb=2)
    before = target.stat().st_size

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hm, "HEALTH_VACUUM_MIN_MB", 4096)
    hm._vacuum_if_oversized("health.db")

    assert target.stat().st_size == before, "threshold not respected"


def test_vacuum_refuses_when_disk_is_tight(tmp_path, monkeypatch):
    """VACUUM writes a full copy; doing it on a nearly-full disk is how you
    turn a large file into an outage."""
    import shutil

    db = tmp_path / "db"
    db.mkdir()
    target = db / "health.db"
    _make_db(target, mb=3)
    before = target.stat().st_size

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hm, "HEALTH_VACUUM_MIN_MB", 1)
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: types.SimpleNamespace(total=0, used=0, free=1024),
    )
    hm._vacuum_if_oversized("health.db")

    assert target.stat().st_size == before, "must skip VACUUM, not attempt it"


def test_vacuum_on_a_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hm._vacuum_if_oversized("does_not_exist.db")  # must not raise


def test_one_failing_purge_does_not_strand_the_others(monkeypatch):
    """Three DBs are purged; a failure in one must not skip the rest."""
    calls = []
    monkeypatch.setattr(hm, "purge_old_metrics", lambda **_k: (_ for _ in ()).throw(RuntimeError("locked")))
    monkeypatch.setattr(hm, "_purge_latency", lambda: calls.append("latency") or 5)
    monkeypatch.setattr(hm, "_purge_traffic", lambda: calls.append("traffic") or 7)
    monkeypatch.setattr(hm, "_vacuum_if_oversized", lambda _n: calls.append(f"vacuum:{_n}"))

    hm._run_retention_maintenance()

    assert "latency" in calls and "traffic" in calls, "a raising purge stranded the others"
    assert any(c.startswith("vacuum:") for c in calls), "vacuum pass was skipped"


def test_maintenance_covers_all_three_rolling_dbs(monkeypatch):
    vacuumed = []
    monkeypatch.setattr(hm, "purge_old_metrics", lambda **_k: 0)
    monkeypatch.setattr(hm, "_purge_latency", lambda: 0)
    monkeypatch.setattr(hm, "_purge_traffic", lambda: 0)
    monkeypatch.setattr(hm, "_vacuum_if_oversized", lambda n: vacuumed.append(n))

    hm._run_retention_maintenance()

    assert set(vacuumed) == {"health.db", "latency.db", "logs.db"}


def test_maintenance_interval_is_not_every_sample():
    """Running VACUUM on a 10s cadence would be its own outage."""
    assert hm.HEALTH_MAINTENANCE_SECS >= 3600
    assert hm.HEALTH_MAINTENANCE_SECS > hm.HEALTH_SAMPLE_INTERVAL * 100


def test_collector_loop_schedules_maintenance(monkeypatch):
    """The bug was that this only ever ran at startup."""
    src = (ROOT / "utils" / "health_monitor.py").read_text(encoding="utf-8")
    loop = src[src.index("def _collector_loop"):src.index("def start_health_collector")]
    assert "_run_retention_maintenance()" in loop, "retention still startup-only"
    assert "HEALTH_MAINTENANCE_SECS" in loop, "no interval gate on maintenance"


def test_escalation_is_wired_into_collect_metrics():
    src = (ROOT / "utils" / "health_monitor.py").read_text(encoding="utf-8")
    body = src[src.index("def collect_metrics"):src.index("def _collector_loop")]
    assert "_escalate_failures(" in body, "collector does not escalate anything"


def test_helper_exists_in_the_alert_service():
    src = (ROOT / "services" / "telegram_alert_service.py").read_text(encoding="utf-8")
    assert "def send_health_alert(" in src
    # must reuse the existing broadcast path, not open a second one
    helper = src[src.index("def send_health_alert("):]
    assert "send_broadcast_alert" in helper


def test_detail_blobs_are_only_stored_when_something_is_wrong():
    """85% of every health row was detail JSON written on healthy samples."""
    src = (ROOT / "database" / "health_db.py").read_text(encoding="utf-8")
    body = src[src.index("def log_metrics("):src.index("def get_current_metrics")]
    td = body[body.index("thread_details="):]
    assert 'get("status") != "pass"' in td[:400], "thread_details still unconditional"
    pd = body[body.index("process_details="):]
    assert 'overall_status != "pass"' in pd[:300], "process_details still unconditional"


def test_scalar_counts_are_still_always_recorded():
    """Dropping the blobs must not blind the graphs."""
    src = (ROOT / "database" / "health_db.py").read_text(encoding="utf-8")
    body = src[src.index("def log_metrics("):src.index("def get_current_metrics")]
    for field in ("thread_count=", "stuck_threads=", "thread_status="):
        line = body[body.index(field):body.index(field) + 160]
        assert "!= \"pass\"" not in line, f"{field} must be recorded on every sample"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
