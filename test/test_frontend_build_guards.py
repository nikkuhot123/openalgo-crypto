#!/usr/bin/env python
"""Guards for the frontend build/deploy invariants.

2026-08-26: the Python Strategies page lost its lot-mode toggle, per-strategy
performance panel and armed-trade gauges. Nothing was deleted from the source --
the bundle being SERVED was upstream's.

Chain of events:
  1. Upstream commits its built `frontend/dist` to git and auto-builds it in CI.
     This fork untracked dist at 615d8d59c because it is a build artifact.
  2. `frontend/node_modules` on the VPS was from Jul 12 while package.json was
     from Aug 25 and had gained `openalgo-charts@1.6.0`. So `tsc -b` failed with
     TS2307 and `npm run build` could not run on the VPS at all.
  3. The 2.0.2.1 sync (merge 55c67c81a) hit 36 conflicted `frontend/dist/*`
     files and resolved them in UPSTREAM's favour -- "without needing npm on the
     VPS" (wiki/log.md:216-217).
  4. Result: `index.html` pointed at a 24,582-byte `PythonStrategyIndex` chunk
     with zero occurrences of `lot_mode`, while the fork's own 40,749-byte
     featured chunk sat orphaned and unreferenced on disk.

These tests pin the two things that made it silent and the source features that
must never quietly vanish from the TSX again.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GITATTRIBUTES = ROOT / ".gitattributes"
FRONTEND = ROOT / "frontend"
INDEX_TSX = FRONTEND / "src" / "pages" / "python-strategy" / "PythonStrategyIndex.tsx"
PANEL_TSX = FRONTEND / "src" / "components" / "python-strategy" / "StrategyStatusPanel.tsx"
API_TS = FRONTEND / "src" / "api" / "python-strategy.ts"


# ------------------------------------------------------- merge protection
def test_gitattributes_declares_the_rule():
    assert GITATTRIBUTES.exists(), ".gitattributes is missing"
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    rule = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#") and "frontend/dist" in ln
    ]
    assert rule, "no frontend/dist rule in .gitattributes"
    assert any("merge=ours" in ln for ln in rule), (
        "frontend/dist must be merge=ours, or upstream's prebuilt bundle wins again"
    )


def test_gitattributes_pattern_matches_nested_assets():
    """`frontend/dist/**` must cover the hashed chunks, not just the top level."""
    out = subprocess.run(
        ["git", "check-attr", "merge", "--",
         "frontend/dist/assets/index-DEADBEEF.js"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert "merge: ours" in out.stdout, (
        f"pattern does not reach nested assets: {out.stdout.strip()!r}"
    )


def test_ours_merge_driver_is_actually_configured():
    """The rule is INERT without this config.

    `ours` is NOT a built-in driver -- git ships only text, binary and union.
    An unresolvable driver does not warn; git silently falls back to the 3-way
    text merge and conflicts, which is exactly how upstream's bundle won last
    time. Config cannot be committed, so this asserts the clone is set up.
    """
    out = subprocess.run(
        ["git", "config", "--get", "merge.ours.driver"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.stdout.strip(), (
        "merge.ours.driver is not set, so `frontend/dist/** merge=ours` does "
        "NOTHING. Run: git config merge.ours.driver true"
    )


def test_gitattributes_does_not_claim_the_driver_is_builtin():
    """An earlier revision of this file asserted `ours` was built-in. It is not,
    and believing that is what made the guard inert."""
    text = GITATTRIBUTES.read_text(encoding="utf-8").lower()
    assert "is the built-in driver" not in text, "false built-in claim is back"
    assert "merge.ours.driver" in text, "the required config must be documented"


def test_gitattributes_documents_the_limits():
    """A guard trusted further than it works is worse than none."""
    text = GITATTRIBUTES.read_text(encoding="utf-8").lower()
    assert "untracked" in text, "the merge=ours limitation must stay documented"
    assert "npm ci" in text or "npm run build" in text, (
        "the rebuild requirement must stay documented next to the rule"
    )


# ------------------------------------------- the features that went dark
@pytest.mark.skipif(not INDEX_TSX.exists(), reason="frontend source not present")
def test_lot_mode_controls_exist_in_source():
    src = INDEX_TSX.read_text(encoding="utf-8")
    for needle in ("Manual Lots", "Auto Lots", "lot_mode"):
        assert needle in src, f"{needle!r} missing from PythonStrategyIndex.tsx"


@pytest.mark.skipif(not INDEX_TSX.exists(), reason="frontend source not present")
def test_max_lots_and_risk_inputs_exist_in_source():
    src = INDEX_TSX.read_text(encoding="utf-8")
    assert "max_lots_nifty" in src and "max_lots_sensex" in src, "max-lots inputs gone"
    assert "risk_pct_per_trade" in src, "risk %/trade input gone"


@pytest.mark.skipif(not PANEL_TSX.exists(), reason="frontend source not present")
def test_performance_and_armed_trade_panels_exist_in_source():
    src = PANEL_TSX.read_text(encoding="utf-8")
    for needle in ("Profit Factor", "Active Positions"):
        assert needle in src, f"{needle!r} missing from StrategyStatusPanel.tsx"


@pytest.mark.skipif(not API_TS.exists(), reason="frontend source not present")
def test_api_client_still_calls_the_write_and_metrics_endpoints():
    """The backend never regressed; the client is what must keep calling it."""
    src = API_TS.read_text(encoding="utf-8")
    assert "max-lots" in src, "client no longer POSTs lot settings"
    assert "/metrics" in src, "client no longer fetches per-strategy metrics"
    assert "/status" in src, "client no longer polls armed-trade status"


# --------------------------------------------------- build reproducibility
@pytest.mark.skipif(not (FRONTEND / "package.json").exists(), reason="no package.json")
def test_build_script_typechecks_before_bundling():
    """`tsc -b && vite build` is why the stale node_modules surfaced as a hard
    failure instead of a silently wrong bundle. Keep the typecheck."""
    import json

    pkg = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    build = pkg.get("scripts", {}).get("build", "")
    assert "vite build" in build, "build script no longer bundles"
    assert "tsc" in build, (
        "the typecheck gate was removed -- a missing dependency would then "
        "produce a quietly incomplete bundle instead of failing the build"
    )


@pytest.mark.skipif(not (FRONTEND / "package.json").exists(), reason="no package.json")
def test_openalgo_charts_is_pinned_not_floating():
    """The dependency whose absence broke the VPS build. An exact pin is what
    makes `npm ci` reproducible on a server that cannot be rebuilt casually."""
    import json

    pkg = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    ver = deps.get("openalgo-charts")
    if ver is None:
        pytest.skip("openalgo-charts no longer a dependency")
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver), (
        f"openalgo-charts should be exactly pinned, got {ver!r}"
    )


@pytest.mark.skipif(not (FRONTEND / "package-lock.json").exists(), reason="no lockfile")
def test_lockfile_is_committed_so_npm_ci_works():
    """`npm ci` is the only deterministic install; it requires the lockfile."""
    lock = FRONTEND / "package-lock.json"
    assert lock.stat().st_size > 1000, "package-lock.json looks truncated"


# ------------------------------------------- the guard that would have caught it
DIST = FRONTEND / "dist"
DIST_INDEX = DIST / "index.html"

# Strings that only exist in this fork's Python Strategies UI. Upstream's build
# has none of them, which is what made the swap invisible.
FORK_UI_MARKERS = (
    "Manual Lots",
    "Auto Lots",
    "lot_mode",
    "max_lots_nifty",
    "risk_pct_per_trade",
)


def _referenced_page_chunk():
    """Resolve index.html -> entry chunk -> PythonStrategyIndex chunk.

    Deliberately follows the REFERENCE chain rather than scanning assets/. The
    whole bug was a correct 40,749-byte chunk sitting orphaned on disk next to
    the 24,582-byte one that was actually served, so "some file under assets/
    contains the string" would have passed happily throughout the outage.
    """
    html = DIST_INDEX.read_text(encoding="utf-8", errors="ignore")
    entries = re.findall(r"assets/index-[A-Za-z0-9_-]+\.js", html)
    assert entries, "no entry chunk referenced by dist/index.html"
    entry = DIST / entries[0]
    assert entry.exists(), f"referenced entry missing from dist: {entries[0]}"
    body = entry.read_text(encoding="utf-8", errors="ignore")
    names = re.findall(r"PythonStrategyIndex-[A-Za-z0-9_-]+\.js", body)
    assert names, "entry chunk does not reference a PythonStrategyIndex chunk"
    chunk = DIST / "assets" / names[0]
    assert chunk.exists(), f"referenced page chunk missing from dist: {names[0]}"
    return chunk


@pytest.mark.skipif(not DIST_INDEX.exists(), reason="no built frontend present")
def test_referenced_bundle_contains_the_features():
    """The regression, expressed as an assertion.

    On 2026-08-26 the committed bundle was upstream's: the referenced chunk had
    ZERO occurrences of `lot_mode`. Source was fully intact, so every
    source-level test in this file passed green for the entire outage. This is
    the only test here that can tell a good build from a broken one.
    """
    chunk = _referenced_page_chunk()
    body = chunk.read_text(encoding="utf-8", errors="ignore")
    missing = [m for m in FORK_UI_MARKERS if m not in body]
    assert not missing, (
        f"the SERVED bundle ({chunk.name}, {chunk.stat().st_size} B) is missing "
        f"{missing} -- this is upstream's build, not ours. "
        f"Run: cd frontend && npm ci && npm run build"
    )


@pytest.mark.skipif(not DIST_INDEX.exists(), reason="no built frontend present")
def test_referenced_api_chunk_can_write_lot_settings():
    """The UI is useless if the client cannot reach the endpoints. Guards the
    same reference chain for the API chunk."""
    html = DIST_INDEX.read_text(encoding="utf-8", errors="ignore")
    entry = DIST / re.findall(r"assets/index-[A-Za-z0-9_-]+\.js", html)[0]
    body = entry.read_text(encoding="utf-8", errors="ignore")
    names = re.findall(r"python-strategy-[A-Za-z0-9_-]+\.js", body)
    if not names:
        pytest.skip("api client is inlined into another chunk in this build")
    api = DIST / "assets" / names[0]
    text = api.read_text(encoding="utf-8", errors="ignore")
    for needle in ("max-lots", "/metrics", "/status"):
        assert needle in text, f"served api chunk cannot call {needle}"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
