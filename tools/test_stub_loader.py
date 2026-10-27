#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
test_stub_loader.py -- exercise the mod entry-point stub's loader logic.

The stub (src/res/scripts/client/gui/mods/mod_hello_tanker.py) is written in the
py2/py3 intersection, so it can be executed here under Python 3 with fake
ResMgr / debug_utils / gui.mods modules and its behaviour checked for real:

    * ResMgr reads and package/module resolution
    * the PEP-302 fallback finder (scoping, package flag, sys.modules bookkeeping)
    * the two-step _load(): engine importer first, ResMgr fallback second
    * event dispatch, and that a raising handler never escapes to game.init()
      (an escaping exception reaches BigWorld.quit() and closes the client)
    * a missing mod body degrading to one logged error instead of a crash

Run:  python tools/test_stub_loader.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = os.path.join(REPO, "src", "res", "scripts", "client", "gui", "mods",
                    "mod_hello_tanker.py")

VFS = {
    "scripts/client/gui/mods/hello_tanker/__init__.py": "MARK = 'pkg'\n",
    # NOTE: deliberately WITHOUT a trailing newline and with CRLF + a bare CR --
    # Python 2.7's compile() rejects both, and load_module has to normalise them.
    "scripts/client/gui/mods/hello_tanker/main.py":
        "CALLS = []\r\n"
        "def init(): CALLS.append('init')\r"
        "def fini(): CALLS.append('fini')\n"
        "def onAccountShowGUI(ctx): CALLS.append(('gui', ctx))\n"
        "def boom(): raise ValueError('kaboom')\n"
        "def tail():\n"
        "    return 'no trailing newline after me'",
}


class _Section:
    def __init__(self, data):
        self.asBinary = data


class _ResMgr(types.ModuleType):
    def openSection(self, path):
        return _Section(VFS[path]) if path in VFS else None


class _Dbg(types.ModuleType):
    LINES: list = []

    def LOG_NOTE(self, *a):
        _Dbg.LINES.append(("NOTE",) + a)

    def LOG_ERROR(self, *a):
        _Dbg.LINES.append(("ERROR",) + a)

    def LOG_CURRENT_EXCEPTION(self):
        _Dbg.LINES.append(("EXC", repr(sys.exc_info()[1])))


def _install_fakes():
    sys.modules["ResMgr"] = _ResMgr("ResMgr")
    sys.modules["debug_utils"] = _Dbg("debug_utils")
    # gui/ and gui/mods/ already exist in the client (scripts.pkg)
    for name in ("gui", "gui.mods"):
        mod = types.ModuleType(name)
        mod.__path__ = ["scripts/client/" + name.replace(".", "/")]
        sys.modules[name] = mod
    sys.modules["gui"].mods = sys.modules["gui.mods"]


def _load_stub():
    stub = types.ModuleType("stub_under_test")
    stub.__file__ = STUB
    with open(STUB, encoding="utf-8") as fh:
        exec(compile(fh.read(), STUB, "exec"), stub.__dict__)  # noqa: S102

    # Python 3.12 no longer calls find_module() on meta path finders; add the
    # modern spelling so the full __import__ path can be exercised here.
    # The shipped client runs Python 2.7, where find_module() is the API.
    def find_spec(self, fullname, path=None, target=None):
        found = stub._locate(fullname)
        if found is None:
            return None
        if fullname != stub.PACKAGE and not fullname.startswith(stub.PACKAGE + "."):
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=found[2])

    stub._ResMgrFinder.find_spec = find_spec
    return stub


def _finders() -> int:
    return sum(1 for x in sys.meta_path if type(x).__name__ == "_ResMgrFinder")


def main() -> int:
    _install_fakes()
    stub = _load_stub()
    checks = 0

    # -- ResMgr reads / module resolution ----------------------------------- #
    assert stub._read("scripts/client/gui/mods/hello_tanker/main.py").startswith("CALLS")
    assert stub._read("nope/nope.py") is None
    assert stub._locate("gui.mods.hello_tanker")[2] is True
    assert stub._locate("gui.mods.hello_tanker.main")[2] is False
    assert stub._locate("gui.mods.hello_tanker.missing") is None
    finder = stub._ResMgrFinder()
    assert finder.find_module("gui.mods.hello_tanker.main") is finder
    assert finder.find_module("gui.mods.other") is None
    print("OK  ResMgr reads, package/module resolution, finder scoping")
    checks += 1

    # -- cold start: engine importer fails, fallback takes over -------------- #
    stub._load()
    assert stub._impl is not None and stub._impl.__name__ == stub.ENTRY
    assert any("fallback loader" in str(t) for t in _Dbg.LINES), _Dbg.LINES
    # the first attempt must be REPORTED, not swallowed -- otherwise "the engine
    # cannot import .py" is indistinguishable from "main.py has a SyntaxError"
    assert any("engine importer refused" in str(t) for t in _Dbg.LINES), _Dbg.LINES
    assert _finders() == 1
    # CRLF / bare CR / missing trailing newline all survived compile()
    assert stub._impl.tail() == "no trailing newline after me"
    # a package gets its OWN directory as __path__, not the package root
    pkg = sys.modules[stub.PACKAGE]
    assert pkg.__path__ == [stub.RES_DIR], pkg.__path__
    print("OK  cold start -> ResMgr fallback, first failure logged, newline "
          "normalisation, package __path__")
    checks += 1

    # -- event dispatch and exception containment --------------------------- #
    body = stub._impl
    stub.init()
    stub.onAccountShowGUI("ctx")
    stub.fini()
    assert body.CALLS == ["init", ("gui", "ctx"), "fini"], body.CALLS
    # fini() must take the finder back off sys.meta_path (symmetry with install)
    assert _finders() == 0, sys.meta_path
    stub.onConnected()            # handler absent -> silent no-op
    stub._call("boom", ())        # raising handler must NOT propagate
    assert any(t[0] == "EXC" for t in _Dbg.LINES)
    print("OK  event dispatch, fini() uninstalls the finder, exception containment")
    checks += 1

    # -- warm start: the module now imports directly (== engine importer) ---- #
    _Dbg.LINES.clear()
    stub._impl = None
    stub._loaded = False
    stub._load()
    assert stub._impl is body
    assert any("engine ResMgr importer" in str(t) for t in _Dbg.LINES), _Dbg.LINES
    stub._install_fallback()
    assert _finders() == 1
    print("OK  native path preferred; _install_fallback is idempotent")
    checks += 1

    # -- body missing entirely ---------------------------------------------- #
    VFS.clear()
    for name in ("gui.mods.hello_tanker.main", "gui.mods.hello_tanker"):
        sys.modules.pop(name, None)
    _Dbg.LINES.clear()
    stub._impl = None
    stub._loaded = False
    stub._load()
    assert stub._impl is None
    stub.init()
    stub.fini()
    stub.onAvatarBecomePlayer()   # every event must stay a safe no-op
    assert any(t[0] == "ERROR" for t in _Dbg.LINES), _Dbg.LINES
    print("OK  missing body -> one logged error, no crash")
    checks += 1

    print("\n%d check group(s) passed" % checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
