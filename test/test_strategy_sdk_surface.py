#!/usr/bin/env python
"""Every `client.<method>(...)` in a strategy must exist on the real SDK.

2026-08-07: prior_levels_ema_strategy called `client.quote(...)`. The openalgo
SDK only has `quotes()`. Every call raised AttributeError, so fetch_option_ltp
returned None for every leg and the strategy could not price -- therefore could
not enter -- a single trade from deployment onward. It ran green for days:
polling, logging levels, computing bias, entering nothing.

The unit tests did not catch it because the fake broker in
test_prior_levels_ema_runloop.py defined `def quote(self, ...)` -- written
against the implementation instead of the SDK, so it encoded the typo.

A mock can be made to answer any name. The real class cannot. This test walks
the AST of each strategy and checks the attribute against `openalgo.api`.
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIRS = [ROOT / "strategies" / "examples", ROOT / "strategies" / "scripts"]

# Names bound to something other than the SDK client in some strategies.
IGNORE_ATTRS = {"__class__", "__dict__"}


def _sdk_surface():
    # The repo root directory is itself named `openalgo` and ships an empty
    # __init__.py, so under pytest a plain `import openalgo` resolves to the
    # REPO, not the installed SDK, and this guard would silently pass on a
    # module with no `api` at all. Force site-packages to win.
    import sysconfig

    saved_path, saved_mod = sys.path[:], sys.modules.pop("openalgo", None)
    for key in ("purelib", "platlib"):
        sys.path.insert(0, sysconfig.get_paths()[key])
    try:
        from openalgo import api

        return {m for m in dir(api) if not m.startswith("_")}
    except ImportError as e:
        pytest.fail(f"openalgo SDK import failed, guard cannot run: {e!r}")
    finally:
        sys.path[:] = saved_path
        sys.modules.pop("openalgo", None)
        if saved_mod is not None:
            sys.modules["openalgo"] = saved_mod


def _client_calls(path):
    """Yield (lineno, attr) for every `client.<attr>(...)` call in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "client"
            and fn.attr not in IGNORE_ATTRS
        ):
            yield node.lineno, fn.attr


def _strategy_files():
    seen = {}
    for d in STRATEGY_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            seen.setdefault(f.name, f)  # examples/ wins; scripts/ is the mirror
    return sorted(seen.values())


@pytest.mark.parametrize("path", _strategy_files(), ids=lambda p: p.name)
def test_client_calls_exist_on_sdk(path):
    surface = _sdk_surface()
    bad = [(ln, a) for ln, a in _client_calls(path) if a not in surface]
    assert not bad, (
        f"{path.name} calls SDK methods that do not exist: "
        + ", ".join(f"line {ln}: client.{a}()" for ln, a in bad)
        + f"\nAvailable: {', '.join(sorted(surface))}"
    )


def test_guard_detects_a_known_bad_call(tmp_path):
    """The check must actually fail on the 2026-08-07 bug, not vacuously pass."""
    f = tmp_path / "bogus_strategy.py"
    f.write_text("resp = client.quote(symbol='X', exchange='NFO')\n", encoding="utf-8")
    bad = [(ln, a) for ln, a in _client_calls(f) if a not in _sdk_surface()]
    assert bad == [(1, "quote")]


def test_guard_accepts_the_corrected_call(tmp_path):
    f = tmp_path / "ok_strategy.py"
    f.write_text("resp = client.quotes(symbol='X', exchange='NFO')\n", encoding="utf-8")
    assert [(ln, a) for ln, a in _client_calls(f) if a not in _sdk_surface()] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
