#!/usr/bin/env python
"""Every scoped_session must be in the cleanup registry.

2026-08-25 outage. `database/strategy_trades_db.py` defined a `scoped_session`
but was never added to `SCOPED_SESSION_MODULES`, so `remove_all_scoped_sessions()`
never released it -- not even on the request path, because `teardown_appcontext`
delegates to exactly that list.

It binds to `sqlite:///db/openalgo.db` with `NullPool`, so every checkout opens a
fresh connection: 2 descriptors (the db and its `-wal`). Every strategy-metrics
request leaked both, permanently. The worker died holding:

    337  fds on db/openalgo.db
    296  fds on db/openalgo.db-wal
    294  sockets
    ---
    944  against the systemd default LimitNOFILE of 1024

Past the ceiling, every DB open failed with "unable to open database file",
requests could not complete, 2888 greenlets blocked forever holding their
descriptors, and the eventlet hub stopped accepting. systemd still reported the
unit `active` while the site was unreachable -- there was nothing to crash.

A reviewer cannot catch an omission from a 25-entry list by reading it, so this
test derives the expected set from the filesystem instead of restating it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "utils" / "db_sessions.py"
DB_DIR = ROOT / "database"

# `name = scoped_session(` at module level
_DEFN = re.compile(r"^(\w+)\s*=\s*scoped_session\s*\(", re.M)
# ("database.foo", "db_session")
_ENTRY = re.compile(r"\(\s*\"([\w.]+)\"\s*,\s*\"(\w+)\"\s*\)")


def _defined():
    """(module, attr) for every module-level scoped_session under database/."""
    out = set()
    for path in sorted(DB_DIR.glob("*.py")):
        for m in _DEFN.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            out.add((f"database.{path.stem}", m.group(1)))
    return out


def _registered():
    text = REGISTRY.read_text(encoding="utf-8")
    body = text[text.index("SCOPED_SESSION_MODULES"):text.index("def remove_all_scoped_sessions")]
    return set(_ENTRY.findall(body))


def test_every_scoped_session_is_registered():
    """The failure that took the site down: a session nobody releases."""
    missing = _defined() - _registered()
    assert not missing, (
        "scoped_session(s) absent from SCOPED_SESSION_MODULES -- each one leaks "
        "a connection per greenlet on every code path, including requests: "
        + ", ".join(f"{m}.{a}" for m, a in sorted(missing))
    )


def test_registry_has_no_stale_entries():
    """A renamed or deleted module must not linger: `sys.modules.get` returns
    None and the entry silently stops protecting anything."""
    stale = {
        (m, a) for m, a in _registered()
        if m.startswith("database.") and (m, a) not in _defined()
    }
    assert not stale, (
        "registered but no longer defined: "
        + ", ".join(f"{m}.{a}" for m, a in sorted(stale))
    )


def test_strategy_trades_db_specifically():
    """Pin the exact regression. It binds to openalgo.db with NullPool, so an
    unreleased session costs two descriptors, not one."""
    assert ("database.strategy_trades_db", "db_session") in _registered()
    src = (DB_DIR / "strategy_trades_db.py").read_text(encoding="utf-8")
    assert "NullPool" in src, "if pooling changed, re-derive the FD cost above"


def test_removal_helper_never_raises_on_a_missing_module():
    """It runs in `finally` blocks; an exception here would mask the real error
    and strand the remaining sessions."""
    import sys

    from utils.db_sessions import remove_all_scoped_sessions

    for mod, _ in _registered():
        sys.modules.pop(mod, None)
    remove_all_scoped_sessions()  # must be a no-op, not an ImportError


@pytest.mark.parametrize("mod,attr", sorted(_defined()))
def test_each_defined_session_is_individually_covered(mod, attr):
    """Parametrised so a failure names the offending module directly."""
    assert (mod, attr) in _registered(), f"{mod}.{attr} leaks: add it to the registry"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
