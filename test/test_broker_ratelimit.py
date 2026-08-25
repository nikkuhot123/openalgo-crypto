#!/usr/bin/env python
"""The rate limiter that prevents the 2026-08-25 outage.

Flattrade caps 120 requests/min per USER. Steady state was 23-75 strategy
calls/min, but each fans out to one or more broker calls, so peaks hit 133-136
and the broker began rejecting everything -- including the quotes needed to
manage two open MIS positions, which had to be closed by hand.

The prior handling RETRIED with exponential backoff, spending more of an
exhausted quota: 344 rate-limit errors in 16 minutes, and its `time.sleep` inside
the single eventlet worker grew the request queue until the API stopped
answering. These tests pin the opposite behaviour: throttle before the call, and
on a breach stop spending rather than retry.
"""
import importlib
import time
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "broker" / "flattrade" / "api"
       / "data.py").read_text(encoding="utf-8")


@pytest.fixture
def rl(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_RATE_LIMIT_DIR", str(tmp_path))
    monkeypatch.setenv("BROKER_RATE_LIMIT_PER_MIN", "5")
    monkeypatch.setenv("BROKER_RATE_LIMIT_MAX_WAIT", "1")
    import utils.broker_ratelimit as m
    importlib.reload(m)
    return m


def test_allows_up_to_the_cap(rl):
    assert all(rl.acquire("t") for _ in range(5))


def test_refuses_beyond_the_cap_instead_of_queueing_forever(rl):
    for _ in range(5):
        rl.acquire("t")
    t0 = time.time()
    assert rl.acquire("t") is False, "spent more than the cap"
    # must give up near MAX_WAIT, not pin the caller for a whole minute
    assert time.time() - t0 < 3.0


def test_breach_burns_the_remaining_window(rl):
    assert rl.acquire("t") is True
    rl.penalise("t")
    assert rl.acquire("t") is False, "still spending after a broker breach"


def test_budget_is_shared_across_names_not_leaked(rl):
    for _ in range(5):
        rl.acquire("shared")
    assert rl.acquire("shared") is False
    # a different broker has its own budget
    assert rl.acquire("other") is True


def test_state_survives_a_fresh_process(rl, tmp_path):
    """The quota is per ACCOUNT but callers live in different processes (gunicorn
    worker and the websocket_proxy subprocess). A per-process limiter would let
    each spend the full budget."""
    for _ in range(5):
        rl.acquire("acct")
    import utils.broker_ratelimit as m
    importlib.reload(m)          # simulates a second process
    assert m.acquire("acct") is False, "budget was not shared across processes"


def test_window_resets_so_it_cannot_wedge(rl, tmp_path):
    import json
    for _ in range(5):
        rl.acquire("roll")
    p = tmp_path / "ratelimit" / "roll.json"
    d = json.loads(p.read_text())
    d["window"] = d["window"] - 120        # pretend two minutes passed
    p.write_text(json.dumps(d))
    assert rl.acquire("roll") is True, "limiter wedged after the window rolled"


# ---------------------------------------------------- wiring in the broker
def test_all_three_flattrade_paths_charge_the_bucket():
    """get_api_response, the sync quote fan-out and the async fan-out all reach
    Flattrade directly; any unthrottled path spends the quota invisibly."""
    assert SRC.count('_rl.acquire("flattrade")') == 3


def test_no_retry_into_an_exhausted_window_remains():
    assert "retry_count + 1" not in SRC, "recursive rate-limit retry still present"
    assert SRC.count("_rl.penalise") == 3


def test_throttle_happens_before_the_request_is_sent():
    for marker in ("client.request(method, url", "httpx.post(url", "await client.post(url"):
        i = SRC.index(marker)
        before = SRC[:i]
        assert before.rindex('_rl.acquire("flattrade")') < i, \
            f"call at {marker!r} is sent before the throttle"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


def test_shipped_limiter_is_not_above_the_broker_ceiling():
    """The shipped cap was 190/min against a real Flattrade ceiling of 120 --
    the limiter permitted the exact breach it existed to prevent."""
    import broker.flattrade.api.data as d
    assert d.FLATTRADE_MAX_PER_MINUTE <= 120, "limiter allows more than the broker"


def test_both_limiters_share_one_number():
    """Two limiters with different caps would double-wait and disagree."""
    import utils.broker_ratelimit as m
    import broker.flattrade.api.data as d
    assert d.FLATTRADE_MAX_PER_MINUTE == m.DEFAULT_LIMIT
