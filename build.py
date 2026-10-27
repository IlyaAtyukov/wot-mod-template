#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
build.py -- build / install pipeline for a "Mir Tankov" (Lesta) client mod.

Needs Python 3.10+ (`python build.py`).  Python 2.7 is optional: the entry
point is compiled to Python 2.7 bytecode by the bundled tools/py27c.py.

What it does by default (no flags):
    src/  ->  dist/_stage/  ->  dist/<id>_<version>.mtmod
Nothing is ever written into the game folder unless --install / --dev /
--uninstall is passed explicitly.  The game folder is found automatically
(see tools/wot_game.py) or given with --game-dir / WOT_GAME_DIR.

Engine requirements checked here (the client rejects or ignores the package
otherwise):
    * .mtmod is a plain ZIP where EVERY entry uses method Stored (0);
      Deflate -> "Mod package '%s' load error: compression not supported".
    * Entry names use forward slashes only.
    * Everything lives under the mount root "res/" (paths.xml mounts packages
      with root="res"); otherwise "mod is empty or dont have mount root".
    * The retail client imports only res/scripts/client/gui/mods/mod_*.pyc
      (Python 2.7 bytecode, magic 03 F3 0D 0A).  A .py entry point is picked
      up by development builds of the client only.

All console output is intentionally ASCII/English so it cannot be mangled by the
Windows console code page.  README.md (Russian) explains every message.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #

# build.py's own messages are ASCII/English on purpose, but it forwards the
# Russian output of tools/check_py2_syntax.py verbatim -- and that output is the
# one the user has to READ to fix a broken build.  Ask for UTF-8 so it does not
# turn into question marks on a cp866/cp1251 console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MIN_PYTHON = (3, 10)

REPO = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(REPO, "tools")
SRC_DIR = os.path.join(REPO, "src")
DIST_DIR = os.path.join(REPO, "dist")
STAGE_DIRNAME = "_stage"

# Used only when src/meta.xml is missing.  WoT Mod Cockpit rewrites this exact
# line (double quotes, literal value) when it creates a project from the
# template -- keep it a plain string literal.
FALLBACK_ARCHIVE_NAME = "ru.local.hello_tanker_1.0.0.mtmod"

PY27_ENV_VARS = ("WOT_PY27", "PYTHON27", "PYTHON2")
PY27_CANDIDATE_PATHS = (
    r"C:\Python27\python.exe",
    r"C:\Python27-x64\python.exe",
    r"C:\Program Files\Python27\python.exe",
    r"C:\Program Files (x86)\Python27\python.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python27", "python.exe"
    ),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python27-32", "python.exe"),
)

PY27_MAGIC = b"\x03\xf3\x0d\x0a"
# path inside the .mtmod, WITHOUT the "res/" mount root
MODS_VFS_DIR = "scripts/client/gui/mods"
DEV_MANIFEST = "dev_manifest.json"
PY27_DOWNLOAD_URL = "https://www.python.org/ftp/python/2.7.18/python-2.7.18.amd64.msi"
GAME_DIR_HINT = "pass --game-dir <folder with paths.xml> or set WOT_GAME_DIR"

# exit codes of tools/check_py2_syntax.py
CHECK_OK, CHECK_PROBLEMS, CHECK_NOTHING = 0, 1, 2

# --------------------------------------------------------------------------- #
# py2.7 compile helper (written to a temp dir and executed by python2.7)
# --------------------------------------------------------------------------- #

_PY27_COMPILER = r'''
# -*- coding: utf-8 -*-
# Executed by PYTHON 2.7 ONLY. Do not import anything from python 3.
# argv[1] is a JSON manifest: [[source, output, dfile], ...]
import json
import os
import sys
import py_compile

MAGIC = b'\x03\xf3\x0d\x0a'


def main(manifest_path):
    fh = open(manifest_path, 'rb')
    jobs = json.loads(fh.read().decode('utf-8'))
    fh.close()
    rc = 0
    for src, out, dfile in jobs:
        folder = os.path.dirname(out)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        try:
            # dfile is baked into co_filename -- it is what shows up in
            # python.log tracebacks, so make it the in-game VFS path.
            py_compile.compile(src, out, dfile, doraise=True)
        except Exception:
            sys.stderr.write('COMPILE-FAIL %s :: %s\n' % (src, sys.exc_info()[1]))
            rc = 1
            continue
        fh = open(out, 'rb')
        magic = fh.read(4)
        fh.close()
        if magic != MAGIC:
            sys.stderr.write('BAD-MAGIC %s (not python 2.7 bytecode)\n' % out)
            rc = 1
            continue
        sys.stdout.write('COMPILED %s  [dfile=%s]\n' % (out, dfile))
    return rc


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.stderr.write('usage: compile_py27.py <manifest.json>\n')
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
'''

# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

_WARNINGS: list[str] = []


def info(msg: str) -> None:
    print("[build] %s" % msg, flush=True)


def ok(msg: str) -> None:
    print("[build] OK   %s" % msg, flush=True)


def warn(msg: str) -> None:
    _WARNINGS.append(msg)
    print("[build] WARN %s" % msg, flush=True)


def error(msg: str) -> None:
    sys.stdout.flush()
    print("[build] ERROR %s" % msg, file=sys.stderr, flush=True)


def die(msg: str, hint: str = "") -> "NoReturn":  # type: ignore[valid-type]
    error(msg)
    if hint:
        print("[build]       hint: %s" % hint, file=sys.stderr, flush=True)
    sys.exit(1)


def artifact_message(path: str, size: int, entries: int) -> str:
    """Text of the success line.  WoT Mod Cockpit finds the artifact with
    ^\\[build\\]\\s+OK\\s+(?<path>.+?\\.mtmod)\\s*\\(  -- printed through ok()."""
    return "%s (%d bytes, %d entries, all Stored)" % (path, size, entries)


def install_message(dst: str, overwritten: bool) -> str:
    """Text of the --install line.  It must NOT look like the artifact line above
    (no "(" after ".mtmod"): the cockpit takes the LAST matching line as the
    built artifact."""
    return "installed %s%s" % (dst, " [overwritten]" if overwritten else "")


def _tool_module(name: str):
    """Import tools/<name>.py (cached).  Raises on any problem."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(TOOLS_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# --------------------------------------------------------------------------- #
# python 2.7 discovery
# --------------------------------------------------------------------------- #


def _probe_py27(cmd: list[str]) -> bool:
    try:
        out = subprocess.run(
            cmd + ["-c", 'import sys; sys.stdout.write("%d.%d" % sys.version_info[:2])'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return out.returncode == 0 and out.stdout.strip() == "2.7"


def find_python27(explicit: str | None) -> list[str] | None:
    """Return the argv prefix of a working python 2.7, or None."""
    candidates: list[list[str]] = []

    if explicit:
        candidates.append([explicit])
    for var in PY27_ENV_VARS:
        val = os.environ.get(var)
        if val:
            candidates.append([val])
    launcher = shutil.which("py")
    if launcher:
        candidates.append([launcher, "-2.7"])
    for path in PY27_CANDIDATE_PATHS:
        if path and os.path.isfile(path):
            candidates.append([path])
    for name in ("python2.7", "python2"):
        found = shutil.which(name)
        if found:
            candidates.append([found])

    for cand in candidates:
        if _probe_py27(cand):
            return cand
    if explicit:
        die(
            "the interpreter given via --py2/env is not a working Python 2.7: %s" % explicit,
            "check the path, or drop the flag to build without compilation",
        )
    return None


# --------------------------------------------------------------------------- #
# source tree
# --------------------------------------------------------------------------- #


META_TAGS = ("id", "version", "name", "description")


def parse_meta_xml(raw: str) -> tuple[dict[str, str], str | None]:
    """(fields, error).  A real XML parse first, a regex only as a fallback.

    meta.xml files in the wild use self-closing tags (`<description />`), which
    a regex alone would silently miss.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except Exception as exc:
        meta = {}
        for tag in META_TAGS:
            m = re.search(r"<%s\s*>(.*?)</%s\s*>" % (tag, tag), raw, re.S)
            if m:
                meta[tag] = m.group(1).strip()
        return meta, "meta.xml is not well-formed XML (%s)" % exc

    meta = {}
    for tag in META_TAGS:
        node = root.find(tag)
        if node is not None:
            meta[tag] = (node.text or "").strip()
    if root.tag != "root":
        return meta, "meta.xml root element is <%s>, mods use <root>" % root.tag
    return meta, None


def read_meta(src_dir: str) -> dict[str, str]:
    """Parse src/meta.xml. Returns {} when absent (meta.xml is optional)."""
    path = os.path.join(src_dir, "meta.xml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read().decode("utf-8-sig", "replace")
    except Exception as exc:
        warn("meta.xml unreadable (%s), falling back to default archive name" % exc)
        return {}
    meta, problem = parse_meta_xml(raw)
    if problem:
        warn("%s -- parsed with the regex fallback; fix it, mod managers read this file"
             % problem)
    return meta


def _safe_archive_name(name: str) -> str:
    """The archive name reaches os.path.join() against the game folder.

    <id> comes straight out of meta.xml (or --name), so it must never be able to
    carry a path: `<id>../../../x</id>` would make --uninstall delete outside the
    mods folder.  Strip every separator and refuse to walk up.
    """
    cleaned = os.path.basename(name.replace("\\", "/").rstrip("/"))
    cleaned = cleaned.strip()
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        warn("archive name %r contains a path -- using %s instead"
             % (name, FALLBACK_ARCHIVE_NAME))
        return FALLBACK_ARCHIVE_NAME
    if cleaned != name:
        warn("archive name %r contains a path separator -- stripped to %r "
             "(check <id> in meta.xml)" % (name, cleaned))
    return cleaned


def archive_name(meta: dict[str, str], override: str | None) -> str:
    if override:
        raw = override if override.endswith(".mtmod") else override + ".mtmod"
        return _safe_archive_name(raw)
    mod_id = meta.get("id")
    version = meta.get("version")
    if mod_id and version:
        return _safe_archive_name("%s_%s.mtmod" % (mod_id, version))
    if mod_id:
        return _safe_archive_name("%s.mtmod" % mod_id)
    return FALLBACK_ARCHIVE_NAME


SYNTAX_SKIP_MARKER = "WARN:lib2to3-unavailable"
_PY3_SCAN_BROKEN = False


def run_py2_syntax_check(targets: list[str]) -> None:
    """Validate every .py in `targets` against Python 2.7 syntax.

    tools/check_py2_syntax.py runs under Python 3 and does THREE passes:
      * ast.parse + a blacklist of nodes that do not exist in 2.7
        (f-strings, annotations, keyword-only args, `raise X from Y`,
        `{**d}`, starred assignment targets, walrus, async, print(a, b), ...);
      * lib2to3 (extra, and only when that module still exists);
      * textual heuristics (pathlib, typing, argument-less super()).

    The ast pass is the one that matters: lib2to3's `python_grammar` is a
    COMBINED 2/3 grammar and accepts py3-only code, so it can never be the gate
    on its own.  This is the ONLY syntax check the nested modules of the mod
    body get -- they ship as plain .py and are compiled inside the game -- so a
    failure here is fatal, never a warning.

    Contract with the checker (strict, no substring guessing):
        exit 0 -> clean;  exit 1 -> problems;  exit 2 -> nothing to check;
        anything else -> the checker itself crashed.
        A first stderr line "WARN:lib2to3-unavailable" means only the optional
        lib2to3 pass was skipped (Python 3.13 removed the module).
    """
    checker = os.path.join(TOOLS_DIR, "check_py2_syntax.py")
    if not os.path.isfile(checker):
        warn("tools/check_py2_syntax.py is missing -- the mod body is NOT syntax checked")
        return
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"  # the checker prints Russian
    proc = subprocess.run(
        [sys.executable, checker] + list(targets),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )
    combined = proc.stdout + proc.stderr
    degraded = SYNTAX_SKIP_MARKER in proc.stderr
    if proc.returncode == CHECK_OK:
        ok("python 2.7 syntax check passed (tools/check_py2_syntax.py)")
        if degraded:
            warn(
                "lib2to3 is unavailable on this interpreter (removed in 3.13); only the "
                "ast-based pass ran. It is the important one, but the grammar pass is off."
            )
        return
    sys.stdout.flush()
    for line in combined.splitlines():
        if line.strip():
            print("[build]      %s" % line, file=sys.stderr, flush=True)
    if proc.returncode == CHECK_NOTHING:
        die("tools/check_py2_syntax.py found no .py files to check in: %s"
            % ", ".join(targets))
    if proc.returncode != CHECK_PROBLEMS:
        die(
            "tools/check_py2_syntax.py crashed (exit code %d, see its output above)"
            % proc.returncode,
            "this is a bug in the checker, not a verdict on your code. "
            "--no-syntax-check builds anyway (the ast gate in the staging step stays on).",
        )
    die(
        "the sources are not valid Python 2.7 (see tools/check_py2_syntax.py output above)",
        "the client runs Python 2.7: no f-strings, no annotations, print is a statement. "
        "Skip this gate with --no-syntax-check if you believe it is wrong.",
    )


def _py3_only_scan(path: str) -> list[str]:
    """Second, independent gate applied to every .py that goes into the package.

    run_py2_syntax_check() already covers src/, but it can be turned off with
    --no-syntax-check and it looks at the SOURCE tree; this one looks at what is
    actually being shipped and cannot be disabled.  It needs no lib2to3.
    """
    global _PY3_SCAN_BROKEN
    try:
        check_py2_syntax = _tool_module("check_py2_syntax")
    except Exception as exc:
        if not _PY3_SCAN_BROKEN:
            _PY3_SCAN_BROKEN = True
            warn("tools/check_py2_syntax.py is not importable (%s) -- the staged "
                 ".py files are NOT checked for python 3 syntax" % exc)
        return []
    try:
        source = check_py2_syntax.read_source(path)
    except Exception as exc:
        warn("cannot read %s for the syntax scan (%s)" % (_short(path), exc))
        return []
    try:
        problems, _parsed = check_py2_syntax.py3_only_problems(source, path)
    except Exception as exc:
        warn("syntax scan crashed on %s (%s)" % (_short(path), exc))
        return []
    return problems


def check_source_tree(src_dir: str) -> None:
    if not os.path.isdir(src_dir):
        die(
            "source folder not found: %s" % src_dir,
            "expected layout: src/meta.xml and src/res/scripts/client/gui/mods/mod_*.py",
        )
    res_dir = os.path.join(src_dir, "res")
    if not os.path.isdir(res_dir):
        die(
            "mount root missing: %s" % res_dir,
            'everything must live under src/res/ (paths.xml mounts .mtmod with root="res")',
        )
    if not os.path.isfile(os.path.join(src_dir, "meta.xml")):
        warn("src/meta.xml is missing (optional for the engine, but recommended)")

    mods_dir = os.path.join(src_dir, "res", "scripts", "client", "gui", "mods")
    if not os.path.isdir(mods_dir):
        die(
            "entry point folder missing: %s" % mods_dir,
            "the loader only scans scripts/client/gui/mods",
        )
    children = sorted(os.listdir(mods_dir))
    entries = [
        n for n in children
        if n.endswith((".py", ".pyc")) and os.path.isfile(os.path.join(mods_dir, n))
    ]
    packages = [n for n in children
                if os.path.isdir(os.path.join(mods_dir, n)) and n != "__pycache__"]
    if not entries:
        die(
            "no entry point found in %s" % mods_dir,
            "the file must be named mod_<something>.py and be a DIRECT child of "
            "scripts/client/gui/mods (subfolders are ordinary modules, not entry points)",
        )
    for name in entries:
        stem = name.rsplit(".", 1)[0]
        if not name.startswith("mod_"):
            warn("%s does not start with 'mod_' -- the loader will ignore it" % name)
        if name != name.lower():
            warn(
                "%s contains upper case; the loader lowercases file names before "
                "importing them -- rename it to all-lowercase" % name
            )
        if ".pyc" in stem:
            warn(
                "%s has '.pyc' inside the stem; the loader strips '.pyc' from the "
                "name and will mangle the module name" % name
            )
    info("entry points found: %s" % ", ".join(entries))
    if packages:
        info("mod packages (stay plain .py): %s" % ", ".join(packages))
    for pkg in packages:
        if not os.path.isfile(os.path.join(mods_dir, pkg, "__init__.py")):
            warn(
                "%s/ has no __init__.py -- neither the engine importer nor the "
                "stub fallback will treat it as a package" % pkg
            )


# --------------------------------------------------------------------------- #
# staging + compilation
# --------------------------------------------------------------------------- #

# The retail client imports exactly one kind of file: a DIRECT child of
# scripts/client/gui/mods named mod_*.pyc (Python 2.7 bytecode).  Everything
# below that -- e.g. scripts/client/gui/mods/<package>/main.py -- is a normal
# module and may stay plain .py: the stub tries the engine's own importer first
# and falls back to its own ResMgr-based loader (see the stub's header).


def _rel_posix(full: str, root: str) -> str:
    return os.path.relpath(full, root).replace(os.sep, "/")


def is_entry_point(rel: str) -> bool:
    """True for res/scripts/client/gui/mods/mod_<x>.py (direct child only)."""
    prefix = "res/" + MODS_VFS_DIR + "/"
    if not rel.startswith(prefix):
        return False
    tail = rel[len(prefix):]
    return "/" not in tail and tail.startswith("mod_") and tail.endswith(".py")


def vfs_path(rel: str) -> str:
    """res/scripts/... -> scripts/...  (what the engine sees, and what we bake
    into co_filename so python.log tracebacks point at a real in-game path)."""
    return rel[4:] if rel.startswith("res/") else rel


def _compile_builtin(jobs: list[tuple[str, str, str]]) -> list[str]:
    """Compile with tools/py27c.py. Returns [] on success, else error strings."""
    try:
        py27c = _tool_module("py27c")
    except Exception as exc:
        return ["tools/py27c.py is not importable: %s" % exc]

    errors: list[str] = []
    for src, out, dfile in jobs:
        try:
            blob = py27c.compile_file(src, out, dfile=dfile, mtime=int(time.time()))
        except py27c.Py27CompileError as exc:
            errors.append("%s: %s" % (_short(src), exc))
            continue
        except SyntaxError as exc:
            # py27c parses with the Python 3 ast, so VALID Python 2.7 that is not
            # valid Python 3 lands here.  That is the author's syntax choice, not
            # a bug in py27c -- say which one.
            errors.append(
                "%s:%s: %s -- py27c reads the source with the Python 3 parser, so the "
                "stub must sit in the py2 AND py3 intersection (no 123L, no "
                "`except E, e:`, no print statement, no backticks, no 0777, no <>, "
                "no ur''). Rewrite that line, or compile with a real Python 2.7 "
                "(--py2 <path>)." % (_short(src), getattr(exc, "lineno", "?"), exc.msg)
            )
            continue
        except Exception as exc:  # a real bug in py27c -- say so loudly
            errors.append("%s: py27c internal error: %r" % (_short(src), exc))
            continue
        if blob[:4] != PY27_MAGIC:
            errors.append("%s: py27c produced a wrong magic number" % _short(src))
            continue
        info("compiled (py27c) %s  ->  %s  [dfile=%s]"
             % (_short(src), os.path.basename(out), dfile))
    return errors


def _compile_external(jobs: list[tuple[str, str, str]], py27: list[str]) -> list[str]:
    """Compile with a real Python 2.7 interpreter (py_compile).

    The helper and its job list live in a private temp folder, never in dist/:
    --dist may point anywhere, and two builds must not share these files.
    """
    with tempfile.TemporaryDirectory(prefix="wotmod-py27-") as tmp:
        manifest = os.path.join(tmp, "jobs.json")
        helper = os.path.join(tmp, "compile_py27.py")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump([list(job) for job in jobs], fh)
        with open(helper, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_PY27_COMPILER)
        proc = subprocess.run(py27 + [helper, manifest], capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("COMPILED"):
            info("compiled (python2.7) %s" % line[9:])
    if proc.returncode != 0 or proc.stderr.strip():
        return [line for line in proc.stderr.splitlines() if line.strip()] or \
               ["python 2.7 exited with code %d" % proc.returncode]
    return []


def _short(path: str) -> str:
    try:
        return os.path.relpath(path, REPO).replace(os.sep, "/")
    except ValueError:
        return path


def compile_entry_points(jobs, py27, backend: str) -> str:
    """Turn every mod_*.py into mod_*.pyc. Never returns without a .pyc.

    backend: "auto" | "builtin" | "py27"
    Returns the backend that actually produced the bytecode.
    """
    if not jobs:
        return "none"

    if backend == "py27":
        if not py27:
            die(
                "--compiler py27 was requested but no working Python 2.7 was found",
                "pass --py2 <path to python.exe>, set WOT_PY27, or use the default "
                "--compiler auto (which uses the built-in tools/py27c.py)",
            )
        errors = _compile_external(jobs, py27)
        if errors:
            for line in errors:
                error(line)
            die("python 2.7 could not compile the entry point(s)")
        return "python2.7"

    if backend == "builtin":
        errors = _compile_builtin(jobs)
        if errors:
            for line in errors:
                error(line)
            die(
                "tools/py27c.py could not compile the entry point(s)",
                "py27c only understands a restricted subset (see its header). Move the "
                "complex code into a plain .py module next to the stub, or install "
                "Python 2.7 and re-run with --compiler py27:\n"
                "[build]             " + PY27_DOWNLOAD_URL,
            )
        return "py27c"

    # auto: the built-in compiler first (deterministic, no external dependency),
    # then a real Python 2.7 if one is around and py27c refused the source.
    errors = _compile_builtin(jobs)
    if not errors:
        return "py27c"
    for line in errors:
        warn("py27c: %s" % line)
    if py27:
        info("retrying with the external Python 2.7 (%s)" % " ".join(py27))
        errors = _compile_external(jobs, py27)
        if not errors:
            return "python2.7"
        for line in errors:
            error(line)
    die(
        "no compiler could turn the entry point into Python 2.7 bytecode",
        "either simplify the mod_*.py stub so tools/py27c.py accepts it (the template "
        "keeps the heavy code in plain .py modules for exactly this reason), or install "
        "Python 2.7 and re-run with --py2 <path>:\n"
        "[build]             " + PY27_DOWNLOAD_URL,
    )


def stage(src_dir: str, stage_dir: str, py27, backend: str,
          keep_py: bool, compile_all: bool) -> dict:
    """Copy src -> stage and compile the entry point(s) to Python 2.7 bytecode.

    Returns a small report dict; dies (never returns silently) if the staged tree
    has no loadable mod_*.pyc.
    """
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir)

    entry_jobs: list[tuple[str, str, str]] = []
    extra_jobs: list[tuple[str, str, str]] = []
    copied = 0
    plain_py: list[str] = []
    py3_hits: list[str] = []

    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = _rel_posix(full, src_dir)
            dst = os.path.join(stage_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            # Hard gate on EVERY python file that goes into the package -- the
            # nested modules ship as plain .py and are compiled inside the game,
            # so this is their last chance to be checked.
            if name.endswith(".py"):
                py3_hits.extend(_py3_only_scan(full))

            if is_entry_point(rel):
                entry_jobs.append((full, dst + "c", vfs_path(rel)))
                if keep_py:
                    shutil.copy2(full, dst)
                    copied += 1
                continue

            if name.endswith(".py") and compile_all:
                extra_jobs.append((full, dst + "c", vfs_path(rel)))
                if keep_py:
                    shutil.copy2(full, dst)
                    copied += 1
                continue

            shutil.copy2(full, dst)
            copied += 1
            if name.endswith(".py"):
                plain_py.append(rel)

    if py3_hits:
        for line in py3_hits[:20]:
            error(line)
        die(
            "python 3 only syntax in the files that are about to be packaged",
            "the client is Python 2.7. The nested modules are compiled INSIDE the "
            "game, so this would only surface as one ERROR line in python.log.",
        )

    if not entry_jobs:
        die(
            "no entry point found: %s/res/%s/mod_*.py" % (src_dir, MODS_VFS_DIR),
            "the loader only imports DIRECT children of scripts/client/gui/mods "
            "whose name matches mod_*.pyc",
        )

    info("staged %d file(s) verbatim" % copied)
    used = compile_entry_points(entry_jobs, py27, backend)
    if extra_jobs:
        # --compile-all: these are ordinary modules, so a real 2.7 is preferred.
        errors = _compile_external(extra_jobs, py27) if py27 else _compile_builtin(extra_jobs)
        if errors:
            for line in errors:
                error(line)
            die("--compile-all failed on the nested modules",
                "drop --compile-all: the shipped stub loads plain .py modules out "
                "of the .mtmod")

    # --- hard gate: the staged tree MUST contain valid 2.7 bytecode ---------- #
    staged_mods = os.path.join(stage_dir, "res", *MODS_VFS_DIR.split("/"))
    pycs = sorted(
        n for n in os.listdir(staged_mods)
        if n.startswith("mod_") and n.endswith(".pyc")
    ) if os.path.isdir(staged_mods) else []
    if not pycs:
        die("the staging tree has no mod_*.pyc -- refusing to build a dead package")
    for name in pycs:
        with open(os.path.join(staged_mods, name), "rb") as fh:
            magic = fh.read(4)
        if magic != PY27_MAGIC:
            die(
                "%s is not Python 2.7 bytecode (magic %s, expected 03 f3 0d 0a)"
                % (name, magic.hex(" ")),
                "never compile the entry point with python 3 -- the client rejects it",
            )
    ok("entry point(s) compiled by %s: %s" % (used, ", ".join(pycs)))
    if plain_py:
        info("shipped as plain .py (loaded by the engine importer / stub fallback): %s"
             % ", ".join(plain_py))
    return {"backend": used, "pycs": pycs, "plain_py": plain_py}


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #


def _zip_dt(path: str) -> tuple[int, int, int, int, int, int]:
    stamp = time.localtime(os.path.getmtime(path))[:6]
    if stamp[0] < 1980:  # ZIP cannot store anything older
        stamp = time.localtime()[:6]
    return stamp  # type: ignore[return-value]


def build_archive(stage_dir: str, out_path: str, dir_entries: bool = True) -> None:
    files: list[tuple[str, str]] = []
    for root, dirs, names in os.walk(stage_dir):
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            full = os.path.join(root, name)
            arc = os.path.relpath(full, stage_dir).replace(os.sep, "/")
            files.append((arc, full))
    if not files:
        die("staging folder is empty: %s" % stage_dir)

    # meta.xml first, then the res/ tree -- the usual layout of mod packages
    files.sort(key=lambda pair: (pair[0] != "meta.xml", pair[0].lower()))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    seen_dirs: set[str] = set()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for arc, full in files:
            if dir_entries:
                parts = arc.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    folder = "/".join(parts[:i]) + "/"
                    if folder in seen_dirs:
                        continue
                    seen_dirs.add(folder)
                    zi = zipfile.ZipInfo(folder, date_time=_zip_dt(full))
                    zi.compress_type = zipfile.ZIP_STORED
                    zi.external_attr = (0o40755 << 16) | 0x10  # dir bit
                    zf.writestr(zi, b"")
            zi = zipfile.ZipInfo(arc, date_time=_zip_dt(full))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            with open(full, "rb") as fh:
                zf.writestr(zi, fh.read())


def verify_archive(path: str) -> tuple[list[str], list[str]]:
    """Re-open the built package and assert every engine requirement.

    Returns (fatal, soft):
      fatal -- the engine would reject or ignore the package outright;
      soft  -- it mounts, but something is off (e.g. no compiled entry point).
    """
    problems: list[str] = []
    soft: list[str] = []
    with zipfile.ZipFile(path) as zf:
        if zf.testzip() is not None:
            problems.append("CRC error inside the archive")
        infos = zf.infolist()
        names = [i.filename for i in infos]

        bad_method = [i.filename for i in infos if i.compress_type != zipfile.ZIP_STORED]
        if bad_method:
            problems.append(
                "these entries are NOT Stored (engine: 'compression not supported'): %s"
                % ", ".join(bad_method[:5])
            )
        bad_sep = [n for n in names if "\\" in n]
        if bad_sep:
            problems.append("backslashes in entry names: %s" % ", ".join(bad_sep[:5]))
        if not any(n.startswith("res/") for n in names):
            problems.append("no 'res/' mount root (engine: 'mod is empty or dont have mount root')")
        # The loader lists DIRECT children of scripts/client/gui/mods, lowercases
        # them and keeps only mod_*.pyc -- so that is exactly what we check for.
        prefix = "res/" + MODS_VFS_DIR + "/"
        direct = [n[len(prefix):] for n in names
                  if n.lower().startswith(prefix) and "/" not in n[len(prefix):]]
        entry_pycs = [n for n in direct if n.startswith("mod_") and n.endswith(".pyc")]
        entry_pys = [n for n in direct if n.startswith("mod_") and n.endswith(".py")]
        if not entry_pycs:
            if entry_pys:
                problems.append(
                    "entry point is .py only (%s) -- the retail client imports mod_*.pyc "
                    "exclusively (a .py entry point is read by development builds of the "
                    "client only), so this package would mount and do nothing"
                    % ", ".join(entry_pys)
                )
            else:
                problems.append("no entry point %s%s/mod_*.pyc" % ("res/", MODS_VFS_DIR))
        else:
            for name in entry_pycs:
                with zf.open(prefix + name) as fh:
                    if fh.read(4) != PY27_MAGIC:
                        problems.append(
                            "%s is not Python 2.7 bytecode (magic must be 03 f3 0d 0a)" % name
                        )
            bad_case = [n for n in entry_pycs if n != n.lower()]
            if bad_case:
                problems.append(
                    "entry point name is not lowercase (%s); the loader lowercases the "
                    "name before importing it and the import will fail"
                    % ", ".join(bad_case)
                )
        if "meta.xml" in names:
            if names[0] != "meta.xml":
                soft.append("meta.xml is not the first entry (cosmetic; mods usually put it first)")
            try:
                raw = zf.read("meta.xml").decode("utf-8-sig", "replace")
            except Exception as exc:
                soft.append("meta.xml unreadable inside the archive (%s)" % exc)
            else:
                fields, problem = parse_meta_xml(raw)
                if problem:
                    soft.append(problem + " -- mod managers parse this file, fix it")
                missing = [t for t in ("id", "version", "name") if not fields.get(t)]
                if missing:
                    soft.append("meta.xml has no %s" % "/".join("<%s>" % t for t in missing))
        stray = [
            n
            for n in names
            if not n.startswith("res/") and n != "meta.xml" and not n.endswith("/")
        ]
        if stray:
            problems.append(
                "files outside res/ that are not meta.xml (invisible to the game): %s"
                % ", ".join(stray[:5])
            )
    return problems, soft


def print_listing(path: str) -> None:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        print("[build] archive listing (%s):" % os.path.basename(path))
        print("[build]   %9s  %-6s  %s" % ("Length", "Method", "Name"))
        for i in infos:
            method = "Stored" if i.compress_type == zipfile.ZIP_STORED else "DEFLATE!"
            print("[build]   %9d  %-6s  %s" % (i.file_size, method, i.filename))
        total = sum(i.file_size for i in infos)
        print("[build]   %9d  %-6s  %d entries" % (total, "", len(infos)))


# --------------------------------------------------------------------------- #
# the game folder (read-only unless an explicit flag was given)
# --------------------------------------------------------------------------- #


def _looks_like_version(segment: str) -> bool:
    return bool(re.fullmatch(r"[0-9.]*[0-9][0-9.]*", segment or "")) and "." in segment


def _norm(path: str) -> str:
    """Comparable form of a path: absolute, symlinks resolved, case folded."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_inside(path: str, folder: str) -> bool:
    """True when `path` really lives under `folder` (no .. escape, no symlink
    trick, no case mismatch: C:\\Games\\Tanki and c:\\games\\tanki are the
    same folder)."""
    try:
        p = _norm(path)
        f = _norm(folder)
        return p != f and os.path.commonpath([p, f]) == f
    except (ValueError, OSError):
        return False


class Game:
    """The game folder this run talks to, and what its paths.xml says."""

    def __init__(self) -> None:
        self.root: str | None = None
        self.source = ""
        self.explicit = False
        self.candidates: list = []          # wot_game.GameDir
        self.layout = None                  # wot_game.GameLayout | None
        self.problem = ""
        self.version: str | None = None
        self.version_source = ""
        self.mods_dir: str | None = None
        self.res_mods_dir: str | None = None

    @property
    def paths_xml(self) -> str:
        return os.path.join(self.root or "<game>", "paths.xml")

    @property
    def python_log(self) -> str:
        return os.path.join(self.root or "<game>", "python.log")

    @property
    def readable(self) -> bool:
        """Enough is known to look at the installed mods (conflict scan)."""
        return bool(self.root and self.mods_dir and self.res_mods_dir)


def locate_game(cli_value: str | None, version_override: str | None) -> Game:
    """Find the game folder and read paths.xml.  Never writes, never dies."""
    game = Game()
    try:
        wot_game = _tool_module("wot_game")
    except Exception as exc:
        warn("tools/wot_game.py is not importable (%s) -- the game folder is unknown" % exc)
        return game

    chosen, candidates = wot_game.resolve_game_dir(cli_value)
    game.candidates = candidates
    if chosen is None:
        info("game dir: not found (%s)" % GAME_DIR_HINT)
        return game
    game.root, game.source, game.explicit = chosen.path, chosen.source, chosen.explicit
    info("game dir: %s (from %s)" % (game.root, game.source))
    if not game.explicit and len(candidates) > 1:
        info("other game folders found: %s -- writing actions will ask you to pick "
             "one with --game-dir" % ", ".join(c.path for c in candidates[1:]))

    layout, problem = wot_game.read_paths_xml(game.root)
    game.layout, game.problem = layout, problem
    if version_override:
        game.version, game.version_source = version_override, "--game-version"
        game.mods_dir = os.path.join(game.root, "mods", version_override)
        game.res_mods_dir = os.path.join(game.root, "res_mods", version_override)
    elif layout is not None:
        game.version, game.version_source = layout.version, "paths.xml"
        game.mods_dir, game.res_mods_dir = layout.mods_dir, layout.res_mods_dir

    if layout is None:
        info(problem)
    if game.version:
        info("client version: %s (from %s)" % (game.version, game.version_source))
    return game


def require_game_for_write(game: Game, action: str, need_mtmod: bool) -> None:
    """Die unless `action` can safely write into exactly one known game folder."""
    if game.root is None:
        die("%s needs the game folder, and none was found" % action, GAME_DIR_HINT)
    if not game.explicit and len(game.candidates) > 1:
        error("%s: more than one game folder was found, refusing to guess:" % action)
        for cand in game.candidates:
            print("[build]        %s (from %s)" % (cand.path, cand.source), file=sys.stderr)
        die("pick the game folder explicitly", GAME_DIR_HINT)
    if not os.path.isdir(game.root):
        die("game folder not found: %s" % game.root, GAME_DIR_HINT)
    if not os.path.isfile(game.paths_xml):
        die("%s does not look like a game folder (no paths.xml)" % game.root, GAME_DIR_HINT)
    if not game.version or not game.mods_dir or not game.res_mods_dir:
        die("cannot read the client version: %s" % (game.problem or game.paths_xml),
            "pass --game-version <version>, e.g. the name of the newest <game>\\mods\\<version> folder")
    if need_mtmod:
        if game.layout is None:
            warn("cannot check that this client loads .mtmod packages (%s)" % game.problem)
        elif not game.layout.loads_mtmod:
            die(
                "this looks like a client that loads %s, not .mtmod (paths.xml mods line: "
                "mask=%r) -- %s" % (game.layout.mods_mask or "no packages", game.layout.mods_mask,
                                    game.paths_xml),
                "the template builds .mtmod packages for the Mir Tankov (Lesta) client; "
                + GAME_DIR_HINT,
            )


def scan_conflicts(game: Game, our_names: list[str], our_archive: str) -> None:
    """Read-only scan of the installed .mtmod packages for colliding res/ paths."""
    mods_root = game.mods_dir
    if not mods_root or not os.path.isdir(mods_root):
        return
    ours = {n.lower() for n in our_names if n.startswith("res/") and not n.endswith("/")}
    if not ours:
        return
    hits: list[str] = []
    for root, _dirs, files in os.walk(mods_root):
        for name in files:
            if not name.lower().endswith(".mtmod"):
                continue
            if name.lower() == our_archive.lower():
                continue
            full = os.path.join(root, name)
            try:
                with zipfile.ZipFile(full) as zf:
                    clash = ours & {n.lower() for n in zf.namelist()}
            except Exception:
                continue
            for path in sorted(clash):
                hits.append("%s <-> %s" % (name, path))
    res_mods = game.res_mods_dir or ""
    for rel in sorted(ours):
        loose = os.path.join(res_mods, rel[4:].replace("/", os.sep))
        if res_mods and os.path.isfile(loose):
            hits.append("res_mods override <-> %s" % rel)
    if hits:
        warn("path conflicts with already installed mods (only ONE of them will load):")
        for line in hits[:10]:
            print("[build]      %s" % line)


# --------------------------------------------------------------------------- #
# dev_manifest.json -- the list of loose files --dev put into res_mods
# --------------------------------------------------------------------------- #
#
# {"files": [<absolute paths>], "game_dir": ..., "res_mods_dir": ..., "version": ...}
# "files" is read by WoT Mod Cockpit (DeployProbe) -- keep that key and its shape.
# The other keys say where the files went, so --uninstall still cleans up after
# a client update or with a differently spelled --game-dir.


def _manifest_path(dist_dir: str) -> str:
    return os.path.join(dist_dir, DEV_MANIFEST)


def read_dev_manifest(dist_dir: str) -> dict | None:
    """The manifest as a dict with a "files" list, None when there is none.
    Dies on an unreadable manifest: it is the only record of what is in the game."""
    path = _manifest_path(dist_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        die("%s is unreadable (%s) -- it lists the files --dev put into the game"
            % (path, exc),
            "fix or delete it by hand; build.py does not touch the game without it")
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        die("%s has no \"files\" list" % path,
            "fix or delete it by hand; build.py does not touch the game without it")
    return data


def write_dev_manifest(dist_dir: str, files: list[str], game_dir: str | None,
                       res_mods_dir: str | None, version: str | None) -> None:
    os.makedirs(dist_dir, exist_ok=True)
    data: dict = {"files": files}
    if game_dir and res_mods_dir:
        data.update({"game_dir": game_dir, "res_mods_dir": res_mods_dir, "version": version})
    path = _manifest_path(dist_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def manifest_fence(data: dict, game: Game | None) -> tuple[str | None, str]:
    """(folder, why_not): the only folder the manifest's files may be deleted from.

    A manifest that records where it deployed to is fenced by that folder, but
    only if it really is <recorded game>\\res_mods\\<version> of a game folder.
    An old manifest ({"files": [...]} only) is fenced by the current game.
    """
    recorded = data.get("res_mods_dir")
    recorded_game = data.get("game_dir")
    if isinstance(recorded, str) and isinstance(recorded_game, str) and recorded and recorded_game:
        res_mods_parent = os.path.dirname(os.path.abspath(recorded))
        valid = (
            _looks_like_version(os.path.basename(os.path.abspath(recorded)))
            and os.path.basename(res_mods_parent).lower() == "res_mods"
            and _norm(os.path.dirname(res_mods_parent)) == _norm(recorded_game)
            and os.path.isfile(os.path.join(recorded_game, "paths.xml"))
        )
        if valid:
            return os.path.abspath(recorded), ""
        return None, ("%s records %s, which is not <game>\\res_mods\\<version> of an "
                      "existing game folder" % (DEV_MANIFEST, recorded))
    if game is not None and game.res_mods_dir:
        return game.res_mods_dir, ""
    return None, "%s does not say which game it belongs to" % DEV_MANIFEST


# --------------------------------------------------------------------------- #
# install / dev / uninstall  (the only code paths that WRITE into the game)
# --------------------------------------------------------------------------- #


def do_install(archive: str, game: Game) -> None:
    target_dir = game.mods_dir or ""
    if not os.path.isdir(target_dir):
        die(
            "mods folder not found: %s" % target_dir,
            "paths.xml points there, but the folder does not exist -- start the client "
            "once, or check --game-version",
        )
    dst = os.path.join(target_dir, os.path.basename(archive))
    existed = os.path.exists(dst)
    shutil.copy2(archive, dst)
    ok(install_message(dst, existed))
    info("restart the client completely, then look for '%s' in %s"
         % (os.path.basename(archive).lower(), game.python_log))


def do_dev_deploy(stage_dir: str, game: Game, dist_dir: str) -> None:
    src_res = os.path.join(stage_dir, "res")
    if not os.path.isdir(src_res):
        die("nothing staged to deploy: %s" % src_res)
    target = game.res_mods_dir or ""
    if not os.path.isdir(target):
        die(
            "res_mods folder not found: %s" % target,
            "paths.xml points there, but the folder does not exist -- start the client "
            "once, or check --game-version",
        )

    plan: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(src_res):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            plan.append((full, os.path.join(target, os.path.relpath(full, src_res))))
    planned = {_norm(dst) for _src, dst in plan}

    # 1. files of the previous --dev run that this run no longer ships (a renamed
    #    or deleted module) -- left alone they would keep loading from res_mods.
    old = read_dev_manifest(dist_dir)
    if old is not None:
        fence, why = manifest_fence(old, game)
        failed: list[str] = []
        for path in old["files"]:
            if not isinstance(path, str) or _norm(path) in planned or not os.path.lexists(path):
                continue
            if fence is None or not _is_inside(path, fence):
                error("previous --dev file outside %s, not deleting: %s"
                      % (fence or "<unknown>", path))
                if why:
                    error(why)
                failed.append(path)
                continue
            try:
                os.remove(path)
                info("removed stale %s" % path)
            except OSError as exc:
                error("cannot remove %s (%s)" % (path, exc))
                failed.append(path)
        if failed:
            die("%d file(s) of the previous --dev run are still in the game (see above); "
                "%s is left as it was" % (len(failed), _manifest_path(dist_dir)),
                "close the client and retry, or run --uninstall")

    # 2. record the plan BEFORE copying: a crash halfway still leaves a full list
    write_dev_manifest(dist_dir, [dst for _src, dst in plan], game.root, target, game.version)

    # 3. copy
    for src, dst in plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print("[build]      -> %s" % dst)
    ok("%d file(s) deployed into res_mods (loose, no archive)" % len(plan))
    info("res_mods has priority over every .mtmod -- remember to run --uninstall later")


def do_uninstall(archive_name_: str, game: Game, dist_dir: str) -> None:
    """Remove what --install / --dev put into the game folder -- and NOTHING else.

    Both deletions are fenced: the package may only be removed from the mods
    folder of paths.xml, the dev files only from the res_mods folder recorded in
    dev_manifest.json.  The manifest is a file on disk and the archive name comes
    from meta.xml, so neither is trusted to stay inside those folders on its own.
    The manifest is deleted only when nothing it lists is left in the game.
    """
    removed = 0
    problems = 0
    refused = 0

    mods_dir = game.mods_dir or ""
    pkg = os.path.join(mods_dir, _safe_archive_name(archive_name_))
    if mods_dir and _is_inside(pkg, mods_dir) and os.path.isfile(pkg):
        try:
            os.remove(pkg)
            removed += 1
            ok("removed %s" % pkg)
        except OSError as exc:
            error("cannot remove %s (%s)" % (pkg, exc))
            problems += 1

    manifest = _manifest_path(dist_dir)
    data = read_dev_manifest(dist_dir)
    if data is not None:
        fence, why = manifest_fence(data, game)
        if fence is None:
            warn(why)
        leftovers: list[str] = []
        for path in data["files"]:
            if not isinstance(path, str) or not os.path.lexists(path):
                continue
            if fence is None or not _is_inside(path, fence):
                warn("dev_manifest entry outside %s -- refusing to delete: %s"
                     % (fence or "<unknown>", path))
                leftovers.append(path)
                refused += 1
                continue
            try:
                os.remove(path)
                removed += 1
                ok("removed %s" % path)
            except OSError as exc:
                error("cannot remove %s (%s)" % (path, exc))
                leftovers.append(path)
        if leftovers:
            write_dev_manifest(dist_dir, leftovers, data.get("game_dir"),
                               data.get("res_mods_dir"), data.get("version"))
            problems += len(leftovers)
        else:
            os.remove(manifest)
    if removed == 0 and problems == 0:
        info("nothing to remove (no installed package, no dev manifest)")
    if problems:
        die("%d file(s) could not be removed; %s now lists only the ones still in the game"
            % (problems, manifest),
            "files outside the game's res_mods folder are never deleted -- if they are "
            "not this mod's, delete the manifest by hand" if refused else
            "close the client and run --uninstall again")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Build a .mtmod package for the Mir Tankov (Lesta) client. "
        "By default nothing is written into the game folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="game folder: --game-dir, else env WOT_GAME_DIR, else env WOTCOCKPIT_GAME_DIR,\n"
        "else auto-detect (installed programs list, typical folders). Run\n"
        "python tools/wot_game.py to see what is detected.\n\n"
        "examples:\n"
        "  python build.py\n"
        "  python build.py --compiler py27 --py2 C:\\Python27\\python.exe\n"
        "  python build.py --install          # copy the .mtmod into the game\n"
        "  python build.py --dev              # loose files into res_mods (fast iteration)\n"
        "  python build.py --uninstall        # undo both\n"
        "  python build.py --install --game-dir C:\\Games\\Tanki\n",
    )
    parser.add_argument("--src", default=SRC_DIR, help="source folder (default: src/ next to build.py)")
    parser.add_argument("--dist", default=DIST_DIR, help="output folder (default: dist/ next to build.py)")
    parser.add_argument(
        "--compiler",
        choices=("auto", "builtin", "py27"),
        default=os.environ.get("WOT_MOD_COMPILER", "auto"),
        help="how to produce mod_*.pyc: 'builtin' = tools/py27c.py (no Python 2.7 "
             "needed), 'py27' = an external Python 2.7, 'auto' = builtin first and "
             "fall back to Python 2.7 if the stub is too complex (default: auto)",
    )
    parser.add_argument("--py2", "--py27", dest="py2", default=None,
                        help="path to python 2.7 (env: WOT_PY27) -- optional, only "
                             "needed for --compiler py27 / --compile-all")
    parser.add_argument("--no-py2", action="store_true",
                        help="ignore any external Python 2.7 (same as --compiler builtin). "
                             "The entry point is STILL compiled to .pyc -- by "
                             "tools/py27c.py. There is no way to ship a .py entry point.")
    parser.add_argument("--compile-all", action="store_true",
                        help="also compile the nested .py modules to .pyc "
                             "(not required: the shipped stub loads plain .py out of "
                             "the .mtmod)")
    parser.add_argument(
        "--no-syntax-check",
        action="store_true",
        help="skip the tools/check_py2_syntax.py grammar gate",
    )
    parser.add_argument("--keep-py", action="store_true", help="ship the .py sources next to the .pyc")
    parser.add_argument("--name", default=None, help="override the output archive name")
    parser.add_argument("--game-dir", default=None,
                        help="game folder, the one with paths.xml (default: WOT_GAME_DIR, "
                             "WOTCOCKPIT_GAME_DIR, then auto-detect)")
    parser.add_argument("--game-version", default=None,
                        help="client version (default: read from <game>/paths.xml)")
    parser.add_argument("--no-dir-entries", action="store_true", help="omit ZIP folder entries")
    parser.add_argument("--list", action="store_true", help="print the archive listing")
    parser.add_argument("--clean", action="store_true",
                        help="wipe dist/ before building (dev_manifest.json is kept)")
    parser.add_argument("--install", action="store_true", help="WRITES: copy .mtmod into <game>/mods/<ver>/")
    parser.add_argument("--dev", action="store_true", help="WRITES: loose files into <game>/res_mods/<ver>/")
    parser.add_argument("--uninstall", action="store_true", help="WRITES: remove what --install/--dev put there")
    return parser


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        die("build.py needs Python %d.%d+ (found %d.%d: %s)"
            % (MIN_PYTHON + tuple(sys.version_info[:2]) + (sys.executable,)),
            "run it with a newer Python, e.g. py -3.12 build.py")


def main(argv: list[str] | None = None) -> int:
    check_python_version()
    args = make_parser().parse_args(argv)

    src_dir = os.path.abspath(args.src)
    dist_dir = os.path.abspath(args.dist)
    stage_dir = os.path.join(dist_dir, STAGE_DIRNAME)

    if args.clean and os.path.isdir(dist_dir):
        # dev_manifest.json survives --clean: it is the only list of files that
        # --dev copied into <game>/res_mods/<ver>/, and --uninstall has nothing
        # else to go by.  res_mods shadows every .mtmod, so losing it would leave
        # a loose copy in the game that silently wins over every later build.
        manifest_path = _manifest_path(dist_dir)
        kept_manifest = None
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "rb") as fh:
                    kept_manifest = fh.read()
            except OSError as exc:
                die("could not preserve %s across --clean: %s" % (DEV_MANIFEST, exc),
                    "run --uninstall first, or build without --clean")
        shutil.rmtree(dist_dir)
        info("dist cleaned")
        if kept_manifest is not None:
            os.makedirs(dist_dir, exist_ok=True)
            with open(manifest_path, "wb") as fh:
                fh.write(kept_manifest)
            info("kept %s -- --uninstall still knows what to remove from res_mods" % DEV_MANIFEST)

    meta = read_meta(src_dir) if os.path.isdir(src_dir) else {}
    out_name = archive_name(meta, args.name)

    game = locate_game(args.game_dir, args.game_version)

    if args.uninstall:
        require_game_for_write(game, "--uninstall", need_mtmod=False)
        do_uninstall(out_name, game, dist_dir)
        _summary()
        return 0
    # Fail before building, not after: an install that cannot happen should not
    # look like a half-successful run.
    if args.install:
        require_game_for_write(game, "--install", need_mtmod=True)
    if args.dev:
        require_game_for_write(game, "--dev", need_mtmod=True)

    # ---- validate + stage ------------------------------------------------- #
    check_source_tree(src_dir)
    if not args.no_syntax_check:
        targets = [src_dir]
        # examples/ never ships, but it is copy-paste material for the user --
        # a py3-ism there would be handed straight to the next mod author.
        examples_dir = os.path.join(REPO, "examples")
        if os.path.isdir(examples_dir):
            targets.append(examples_dir)
        run_py2_syntax_check(targets)
    if meta:
        info("meta.xml: id=%s version=%s name=%s" % (meta.get("id", "?"), meta.get("version", "?"), meta.get("name", "?")))
    else:
        info("meta.xml: absent -- using default archive name")

    backend = "builtin" if args.no_py2 else args.compiler
    py27 = None if backend == "builtin" else find_python27(args.py2)
    if py27:
        info("external python 2.7: %s" % " ".join(py27))
    elif backend != "py27":
        info("external python 2.7: not found -- using the built-in tools/py27c.py")
    if args.compile_all and not py27:
        warn("--compile-all without an external Python 2.7: tools/py27c.py will have to "
             "accept every module, which it usually cannot")
    report = stage(src_dir, stage_dir, py27, backend, args.keep_py, args.compile_all)

    # ---- dev deploy ------------------------------------------------------- #
    if args.dev:
        do_dev_deploy(stage_dir, game, dist_dir)
        _summary()
        return 0

    # ---- archive ---------------------------------------------------------- #
    out_path = os.path.join(dist_dir, out_name)
    build_archive(stage_dir, out_path, dir_entries=not args.no_dir_entries)
    fatal, soft = verify_archive(out_path)
    with zipfile.ZipFile(out_path) as zf:
        names = zf.namelist()
    if fatal:
        for problem in fatal:
            error(problem)
        die("the built package violates the engine requirements (see above)")
    for problem in soft:
        warn(problem)

    ok(artifact_message(out_path, os.path.getsize(out_path), len(names)))
    if args.list:
        print_listing(out_path)
    info("entry point bytecode produced by: %s" % report["backend"])

    if game.readable:
        try:
            scan_conflicts(game, names, out_name)
        except Exception as exc:  # never let a read-only scan break the build
            info("conflict scan skipped (%s)" % exc)
    else:
        info("conflict scan skipped (no readable game folder)")

    if args.install:
        do_install(out_path, game)
    elif game.mods_dir:
        info("not installed (pass --install to copy into %s)" % game.mods_dir)
    elif game.root:
        info("not installed (--install needs a game folder with a readable paths.xml)")
    else:
        info("not installed (no game folder known: %s, then --install)" % GAME_DIR_HINT)

    _summary()
    return 0


def _summary() -> None:
    if _WARNINGS:
        print("[build] finished with %d warning(s)" % len(_WARNINGS))
    else:
        print("[build] finished clean")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
