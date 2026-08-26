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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GITATTRIBUTES = ROOT / ".gitattributes"
FRONTEND = ROOT / "frontend"
INDEX_TSX = FRONTEND / "src" / "pages" / "python-strategy" / "PythonStrategyIndex.tsx"
PANEL_TSX = FRONTEND / "src" / "components" / "python-strategy" / "StrategyStatusPanel.tsx"
API_TS = FRONTEND / "src" / "api" / "python-strategy.ts"


# ------------------------------------------------------- merge protection
def test_gitattributes_protects_the_built_frontend():
    """Without this, an upstream sync silently replaces our bundle again."""
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


def test_gitattributes_documents_the_limits():
    """A guard that is trusted further than it works is worse than none: this
    rule does nothing when dist is untracked here and tracked upstream."""
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


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
