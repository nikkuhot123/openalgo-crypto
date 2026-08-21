#!/usr/bin/env python
"""Cross-strategy lock interoperability.

Four strategies share log/strategies/locks with TWO body conventions:

    pipe  "owner|iso|pid"                    POV, Judas, Renko
    JSON  {"strategy":..,"ts":..,"pid":..}   PDH-PDL EMA

Parsing only the pipe form made a JSON lock read as
owner='{"strategy": "PDH-PML EMA"...' with ts='', and _lock_is_stale() treats an
unparseable timestamp as STALE -- so POV and Renko were silently RECLAIMING
PDH's LIVE locks and could open a contract PDH already held. Reproduced against
PDH's exact on-disk body before fixing.

Judas had the mirror-image defect: its reader compared the raw first field to its
own name, so it never stole a foreign lock -- but it had NO staleness check at
all, so a single leaked lock blocked that contract forever. POV found 9 orphaned
.lock files this way.
"""
import importlib
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kw: types.SimpleNamespace(**kw)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")


def load(name, **env):
    os.environ.update({k: str(v) for k, v in env.items()})
    sys.modules.pop(name, None)
    return importlib.import_module(name)


POV = load("pov_wall_squeeze_strategy", UNDERLYING="SENSEX")
JUD = load("judas_swing_strategy", UNDERLYING="SENSEX")
RNK = load("renko_engine_strategy", UNDERLYING="SENSEX", DRY_RUN="false",
           STRATEGY_NAME="Renko Engine (SENSEX)")

SYM = "SENSEX20AUG2677600CE"


def pdh_body(when=None, pid=999999):
    """PDH-PDL's exact on-disk format."""
    ts = (when or datetime.now()).isoformat()
    return '{"strategy": "PDH-PML EMA", "pid": %d, "ts": "%s"}' % (pid, ts)



PDH = load("prior_levels_ema_strategy", UNDERLYING="SENSEX", DRY_RUN="false")
ALL = [("POV", POV), ("Judas", JUD), ("Renko", RNK), ("PDH", PDH)]



@pytest.mark.parametrize("name,mod", ALL)
def test_reads_pdh_json_format(name, mod, tmp_path):
    f = tmp_path / f"{SYM}.lock"
    f.write_text(pdh_body())
    owner, ts, pid = mod._read_lock(f)
    assert owner == "PDH-PML EMA", f"{name} misparsed PDH's body"
    assert pid == 999999
    datetime.fromisoformat(ts)


@pytest.mark.parametrize("name,mod", ALL)
def test_reads_own_pipe_format(name, mod, tmp_path):
    f = tmp_path / f"{SYM}.lock"
    f.write_text(f"POV Wall-Squeeze|{datetime.now().isoformat()}|4242")
    owner, ts, pid = mod._read_lock(f)
    assert owner == "POV Wall-Squeeze"
    assert pid == 4242


@pytest.mark.parametrize("name,mod", ALL)
def test_a_live_pdh_lock_is_not_stale(name, mod, tmp_path):
    """The actual bug: a live PDH lock was being classified stale."""
    f = tmp_path / f"{SYM}.lock"
    f.write_text(pdh_body(pid=os.getpid()))
    owner, ts, pid = mod._read_lock(f)
    assert mod._lock_is_stale(ts, pid) is False, f"{name} still calls it stale"


@pytest.mark.parametrize("name,mod", ALL)
def test_does_not_steal_a_live_pdh_lock(name, mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "LOCKS_DIR", tmp_path)
    (tmp_path / f"{SYM}.lock").write_text(pdh_body(pid=os.getpid()))
    got = (mod.acquire_symbol_lock(SYM) if mod is RNK
           else mod.acquire_symbol_lock(SYM, "Some Other Strategy"))
    assert got is False, f"{name} stole PDH's live lock"
    # and it must not have overwritten the file
    assert "PDH-PML EMA" in (tmp_path / f"{SYM}.lock").read_text()


@pytest.mark.parametrize("name,mod", ALL)
def test_reclaims_a_previous_session_pdh_lock(name, mod, tmp_path, monkeypatch):
    """Respecting the format must not mean wedging on yesterday's lock."""
    monkeypatch.setattr(mod, "LOCKS_DIR", tmp_path)
    old = datetime.now() - timedelta(days=3)
    (tmp_path / f"{SYM}.lock").write_text(pdh_body(when=old))
    got = (mod.acquire_symbol_lock(SYM) if mod is RNK
           else mod.acquire_symbol_lock(SYM, "Mine"))
    assert got is True, f"{name} wedged on a stale lock"


@pytest.mark.parametrize("name,mod", ALL)
def test_unreadable_body_stands_aside(name, mod, tmp_path, monkeypatch):
    """Claiming a lock we cannot parse is how PDH's got stolen."""
    monkeypatch.setattr(mod, "LOCKS_DIR", tmp_path)
    (tmp_path / f"{SYM}.lock").write_text("{not valid json")
    got = (mod.acquire_symbol_lock(SYM) if mod is RNK
           else mod.acquire_symbol_lock(SYM, "Mine"))
    assert got is False, f"{name} claimed an unreadable lock"


@pytest.mark.parametrize("name,mod", ALL)
def test_writes_a_pid_so_siblings_can_expire_it(name, mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "LOCKS_DIR", tmp_path)
    if mod is RNK:
        mod.acquire_symbol_lock(SYM)
    else:
        mod.acquire_symbol_lock(SYM, "Mine")
    owner, ts, pid = mod._read_lock(tmp_path / f"{SYM}.lock")
    assert pid == os.getpid(), f"{name} wrote no usable pid"
    datetime.fromisoformat(ts)


def test_judas_now_expires_a_leaked_lock(tmp_path, monkeypatch):
    """Judas had NO staleness check: one leaked lock blocked that contract
    forever. POV found 9 orphaned files this way."""
    monkeypatch.setattr(JUD, "LOCKS_DIR", tmp_path)
    dead = datetime.now() - timedelta(days=2)
    (tmp_path / f"{SYM}.lock").write_text(f"Judas Swing|{dead.isoformat()}|999999")
    assert JUD.acquire_symbol_lock(SYM, "Judas Swing") is True


def test_judas_still_respects_a_live_foreign_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(JUD, "LOCKS_DIR", tmp_path)
    (tmp_path / f"{SYM}.lock").write_text(
        f"POV Wall-Squeeze|{datetime.now().isoformat()}|{os.getpid()}")
    assert JUD.acquire_symbol_lock(SYM, "Judas Swing") is False


def test_all_three_agree_on_the_written_format(tmp_path, monkeypatch):
    """Whatever one writes, the others must read identically -- otherwise the
    shared directory is decorative."""
    bodies = {}
    for name, mod in ALL:
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setattr(mod, "LOCKS_DIR", d)
        if mod is RNK:
            mod.acquire_symbol_lock(SYM)
        else:
            mod.acquire_symbol_lock(SYM, f"{name} Strategy")
        bodies[name] = (d / f"{SYM}.lock").read_text()
    for writer, body in bodies.items():
        f = tmp_path / "probe.lock"
        f.write_text(body)
        for reader, mod in ALL:
            owner, ts, pid = mod._read_lock(f)
            assert owner and pid == os.getpid(), \
                f"{reader} could not read {writer}'s lock"
            datetime.fromisoformat(ts)




def test_pdh_direction_blocks_pov_opposite(tmp_path, monkeypatch):
    """If PDH holds CE overnight, POV must not open PE next morning."""
    monkeypatch.setattr(PDH, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(POV, "LOCKS_DIR", tmp_path)
    # PDH takes CE
    assert PDH.acquire_direction_lock("SENSEX", "CE", "PDH-PML EMA (SENSEX)") is True
    # POV tries to take PE -> must be blocked
    assert POV.acquire_direction_lock("SENSEX", "PE", "POV Wall-Squeeze") is False
    # POV tries to take CE -> permitted (aligned direction)
    assert POV.acquire_direction_lock("SENSEX", "CE", "POV Wall-Squeeze") is True


def test_pov_direction_blocks_pdh_opposite(tmp_path, monkeypatch):
    """If POV holds PE at 15:04, PDH must not buy CE at 15:05."""
    monkeypatch.setattr(PDH, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(POV, "LOCKS_DIR", tmp_path)
    # POV takes PE
    assert POV.acquire_direction_lock("SENSEX", "PE", "POV Wall-Squeeze") is True
    # PDH tries to take CE -> must be blocked
    assert PDH.acquire_direction_lock("SENSEX", "CE", "PDH-PML EMA (SENSEX)") is False
    # PDH tries to take PE -> permitted
    assert PDH.acquire_direction_lock("SENSEX", "PE", "PDH-PML EMA (SENSEX)") is True


def test_pdh_direction_release_unblocks_others(tmp_path, monkeypatch):
    monkeypatch.setattr(PDH, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(POV, "LOCKS_DIR", tmp_path)
    PDH.acquire_direction_lock("SENSEX", "CE", "PDH-PML EMA (SENSEX)")
    assert POV.acquire_direction_lock("SENSEX", "PE", "POV Wall-Squeeze") is False
    PDH.release_direction_lock("SENSEX", "PDH-PML EMA (SENSEX)", "CE")
    assert POV.acquire_direction_lock("SENSEX", "PE", "POV Wall-Squeeze") is True


def test_all_four_agree_on_symbol_lock(tmp_path, monkeypatch):
    """One strategy takes a contract, all three others must stand aside."""
    for holder_name, holder_mod in ALL:
        d = tmp_path / f"test_{holder_name}"
        d.mkdir(exist_ok=True)
        for _, mod in ALL:
            monkeypatch.setattr(mod, "LOCKS_DIR", d)
        
        # Holder acquires
        if holder_mod is RNK:
            assert holder_mod.acquire_symbol_lock(SYM) is True
        else:
            assert holder_mod.acquire_symbol_lock(SYM, f"{holder_name} Strategy") is True
            
        # Everyone else must be blocked
        for other_name, other_mod in ALL:
            if other_name == holder_name:
                continue
            if other_mod is RNK:
                assert other_mod.acquire_symbol_lock(SYM) is False, f"{other_name} took lock from {holder_name}"
            else:
                assert other_mod.acquire_symbol_lock(SYM, f"{other_name} Strategy") is False, f"{other_name} took lock from {holder_name}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
