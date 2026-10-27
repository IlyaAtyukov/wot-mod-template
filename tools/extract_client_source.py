#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
extract_client_source.py -- pull modules out of YOUR copy of the game's
scripts.pkg and make them readable.

Needs Python 3.10+.  Python 2.7 is NOT required: the client ships Python 2.7
bytecode, and uncompyle6 (running under Python 3) understands it.  uncompyle6
and xdis are optional, GPL-licensed tools that you install yourself into
.tools/venv_uncompyle (see README); this script only runs them as separate
processes.

THE OUTPUT IS DECOMPILED LESTA CLIENT CODE: keep it on your machine, do not
commit or publish it.  The default output folders live in .scratch/, which is
git-ignored.  The game folder is only ever READ.

Two ways to run it
------------------
--tree      the whole client as a source tree, the layout WoT Mod Cockpit and
            grep want:  .scratch/wot-src-<version>/{client,client_common,common}
            (no "scripts/" prefix).  Only real source goes into the tree;
            disassembly / strings of the modules uncompyle6 could not handle go
            to the sibling wot-src-<version>.extra/, and their names are listed
            in .cockpit-failed-modules.txt.  Modules of the extra .pkg files
            (battle modes and events) are listed in .cockpit-extra-modules.txt.
<pattern>   a few modules, into .scratch/client-src/:
                pyc/<path>          raw .pyc (always)
                src/<path>.py       decompiled source
                disasm/<path>.txt   bytecode listing (if decompiling failed)
                strings/<path>.txt  printable literals (last resort)

Pipeline per .pyc, first one that works wins: uncompyle6 -> pydisasm (xdis)
-> built-in strings dump.  uncompyle6 exits 0 even when it fails, so success
is judged by its "# okay decompiling" line and by real code in the output.

Examples
--------
  python tools/extract_client_source.py --tree
  python tools/extract_client_source.py gui/mods/__init__.pyc
  python tools/extract_client_source.py "gui/battle_control/avatar_getter*" --list
  python tools/extract_client_source.py Avatar.pyc --mode disasm
  python tools/extract_client_source.py "scripts/client/gui/battle_control/controllers/*.pyc" --limit 100
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO, "tools")
SCRATCH = os.path.join(REPO, ".scratch")
DEFAULT_OUT = os.path.join(SCRATCH, "client-src")
TREE_PREFIX = "wot-src-"
DEFAULT_VENV = os.path.join(REPO, ".tools", "venv_uncompyle")
REQUIREMENTS = os.path.join(TOOLS_DIR, "requirements-decompile.txt")

PY27_MAGIC = b"\x03\xf3\x0d\x0a"
BWXML_MAGIC = b"\x45\x4e\xa1\x62"  # packed BigWorld XML ("EN\xa1b")
PRINTABLE = re.compile(rb"[ -~]{5,}")

TREE_ROOTS = ("client", "client_common", "common")
FAILED_MODULES_FILE = ".cockpit-failed-modules.txt"   # same names as WoT Mod Cockpit
EXTRA_MODULES_FILE = ".cockpit-extra-modules.txt"

# What uncompyle6 prints (see WoT Mod Cockpit, Uncompyle6Decompiler.Interpret).
SUCCESS_MARKER = "# okay decompiling"
PARTIAL_MARKERS = (
    "# Deparsing stopped due to parse error",
    "Deparsing hit an internal grammar-rule bug",
    "--- This code section failed",
)
DUMP_START = "-- Stacks of completed symbols:"
DUMP_CONTEXT = "Instruction context:"
# First line of a partially decompiled file.  The prefix is shared with WoT Mod
# Cockpit, which skips such lines when it compares client versions.
PARTIAL_HEADER = "# ВНИМАНИЕ: декомпиляция частичная (uncompyle6: parse error), текст обрывается"

PROBE_BATCH = 20          # abort when this many .pyc in a row give no source at all
TOOL_TIMEOUT = 600        # seconds per file

KEEP_LOCAL = ("the output is decompiled Lesta client code: keep it on this machine, "
              "do not commit or publish it (.scratch/ is git-ignored)")
# Two PowerShell commands joined by "; " (Windows PowerShell 5.1 has no "&&"),
# with absolute paths so that they work from any folder.
INSTALL_HINT = ('python -m venv "%s"; & "%s" -m pip install -r "%s"'
                % (DEFAULT_VENV, os.path.join(DEFAULT_VENV, "Scripts", "python.exe"), REQUIREMENTS))

_print_lock = threading.Lock()


def info(msg: str) -> None:
    with _print_lock:
        print("[extract] %s" % msg, flush=True)


def warn(msg: str) -> None:
    with _print_lock:
        print("[extract] WARN %s" % msg, flush=True)


def die(msg: str, hint: str = "") -> "NoReturn":  # type: ignore[valid-type]
    sys.stdout.flush()
    print("[extract] ERROR %s" % msg, file=sys.stderr, flush=True)
    if hint:
        print("[extract]       hint: %s" % hint, file=sys.stderr, flush=True)
    sys.exit(1)


def _tool_module(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS_DIR, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# the decompiler: a Python interpreter that has uncompyle6 + xdis
# --------------------------------------------------------------------------- #


def _python_in(path: str) -> list[str]:
    """Interpreter candidates for a venv root, its Scripts/ dir or python.exe itself."""
    if os.path.isfile(path):
        return [path]
    out = []
    for rel in (("Scripts", "python.exe"), ("python.exe",), ("bin", "python"), ("python",)):
        cand = os.path.join(path, *rel)
        if os.path.isfile(cand):
            out.append(cand)
    return out


def find_decompiler(venv: str | None) -> str | None:
    """The first interpreter that can import what is run later.

    Interpreters, not the venv's uncompyle6.exe / pydisasm.exe: those launchers
    remember the absolute python.exe path of the moment the venv was created
    and silently fail after the template folder is moved or renamed.
    """
    candidates: list[str] = []
    if venv:
        found = _python_in(os.path.abspath(venv))
        if not found:
            die("--venv %s: no python.exe in it (pass the venv folder, its Scripts "
                "folder or the python.exe itself)" % venv)
        candidates.extend(found)
    candidates.extend(_python_in(DEFAULT_VENV))
    candidates.append(sys.executable)
    seen = set()
    for cand in candidates:
        key = os.path.normcase(os.path.abspath(cand))
        if key in seen:
            continue
        seen.add(key)
        try:
            probe = subprocess.run(
                [cand, "-c", "import uncompyle6.bin.uncompile, xdis"],
                capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return cand
    return None


class Runner:
    """Runs the decompiler / disassembler in child processes with UTF-8 pipes
    and a private TEMP (uncompyle6 leaves an empty py-dis-* folder per file)."""

    def __init__(self, python: str | None):
        self.python = python
        self.temp = tempfile.mkdtemp(prefix="wotmod-extract-")
        self.env = dict(os.environ)
        self.env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONWARNINGS": "ignore",
            "TEMP": self.temp, "TMP": self.temp, "TMPDIR": self.temp,
        })

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _run(self, module: str, pyc: str) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [self.python, "-m", module, pyc],
                capture_output=True, env=self.env, timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return -1, "", "timed out after %d s" % TOOL_TIMEOUT
        except OSError as exc:
            return -1, "", str(exc)
        # Bytes as they are, no newline translation: the tree then matches the one
        # WoT Mod Cockpit extracts itself byte for byte (it keeps the tool's output).
        return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def decompile(self, pyc: str) -> tuple[str, str, str]:
        """(status, text, why): status is "ok", "partial" or "failed"."""
        code, out, err = self._run("uncompyle6.bin.uncompile", pyc)
        return interpret(out, err, code)

    def disasm(self, pyc: str) -> tuple[bool, str]:
        code, out, err = self._run("xdis.bin.pydisasm", pyc)
        if code != 0 or not out.strip():
            return False, last_line(err or out) or "empty output, exit code %d" % code
        return True, out


def strip_parser_dump(stdout: str) -> str:
    """Drop the parser debug dump uncompyle6 prints before the module text when a
    function does not parse: from "-- Stacks of completed symbols:" through the
    grammar rules to "Instruction context:" and the indented listing after it."""
    if DUMP_START not in stdout:
        return stdout
    kept = []
    state = 0  # 0 text, 1 grammar rules, 2 instruction listing
    for raw in stdout.split("\n"):
        line = raw.rstrip("\r")
        if line == DUMP_START:
            state = 1
            continue
        if state == 1:
            if line == DUMP_CONTEXT:
                state = 2
            continue
        if state == 2:
            if not line or line.startswith("->") or line[0].isspace():
                continue
            state = 0
        kept.append(raw)
    return "\n".join(kept)


def has_code(text: str) -> bool:
    return any(l.strip() and not l.strip().startswith("#") for l in text.split("\n"))


def last_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def interpret(stdout: str, stderr: str, code: int) -> tuple[str, str, str]:
    """Success only with exit 0, the success line, no stop marker and real code.
    A stop marker WITH code means uncompyle6 gave up on a nested function after
    printing the module: a usable partial result.  Anything else is a failure,
    even with exit code 0 (then stdout holds only the header comments)."""
    text = strip_parser_dump(stdout)
    stopped = next((m for m in PARTIAL_MARKERS if m in stderr or m in text), None)
    if code == 0 and stopped is None and SUCCESS_MARKER in text and has_code(text):
        return "ok", text, ""
    why = stopped or last_line(stderr) or last_line(stdout) or "empty output, exit code %d" % code
    if stopped is not None and has_code(text):
        return "partial", text, why
    return "failed", "", why


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def match_entries(names: list[str], pattern: str) -> list[str]:
    """Glob when the pattern has wildcards, case-insensitive substring otherwise."""
    pat = pattern.replace("\\", "/")
    low = pat.lower()
    if any(ch in pat for ch in "*?["):
        hits = [
            n
            for n in names
            if fnmatch.fnmatch(n.lower(), low)
            or fnmatch.fnmatch(n.lower(), "*/" + low.lstrip("/"))
            or fnmatch.fnmatch(os.path.basename(n).lower(), low)
        ]
    else:
        hits = [n for n in names if low in n.lower()]
    return sorted(hits)


def _fs(path: str) -> str:
    """Path usable beyond MAX_PATH on Windows: deep client modules (gui/impl/gen/...)
    under a deep output folder exceed 260 characters, and Windows long paths are
    off by default.  The \\\\?\\ prefix lifts the limit without that setting."""
    if sys.platform != "win32":
        return path
    full = os.path.abspath(path)
    if len(full) < 240 or full.startswith("\\\\?\\"):
        return path
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def save(path: str, data: bytes) -> None:
    path = _fs(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def extract_strings(data: bytes) -> str:
    """Unique printable literals -- the last-resort view of a .pyc."""
    found = sorted({m.decode("ascii", "replace") for m in PRINTABLE.findall(data)})
    header = (
        "# printable strings only (%d unique literal(s)).\n"
        "# For real source install the decompiler (README):\n"
        "#   %s\n\n" % (len(found), INSTALL_HINT)
    )
    return header + "\n".join(found) + "\n"


def short_version(version: str) -> str:
    """1.46.0.0 -> 1.46, 1.45.1.0 -> 1.45.1 (what WoT Mod Cockpit expects)."""
    parts = version.strip().split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def dotted_module(entry: str) -> str:
    """scripts/client/gui/X.pyc -> gui.X ; scripts/common/items/__init__.pyc -> items."""
    parts = entry[:-4].split("/")
    try:
        at = parts.index("scripts")
    except ValueError:
        return ""
    parts = parts[at + 2:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def tree_entry(name: str) -> bool:
    """scripts/{client,client_common,common}/....pyc"""
    if not name.lower().endswith(".pyc") or not name.startswith("scripts/") or ".." in name:
        return False
    rest = name[len("scripts/"):]
    return "/" in rest and rest.split("/", 1)[0] in TREE_ROOTS


def extra_modules(packages_dir: str) -> list[str]:
    """Modules of the other *.pkg files of the client (battle modes, events)."""
    modules = set()
    try:
        names = sorted(os.listdir(packages_dir))
    except OSError:
        return []
    for name in names:
        if not name.lower().endswith(".pkg") or name.lower() == "scripts.pkg":
            continue
        try:
            with zipfile.ZipFile(os.path.join(packages_dir, name)) as zf:
                entries = zf.namelist()
        except (OSError, zipfile.BadZipFile, ValueError):
            continue
        for entry in entries:
            entry = entry.replace("\\", "/")
            if not entry.lower().endswith(".pyc"):
                continue
            parts = entry.split("/")
            if "scripts" not in parts:
                continue
            at = parts.index("scripts")
            if at + 2 >= len(parts) or parts[at + 1] not in TREE_ROOTS:
                continue
            module = dotted_module("/".join(parts[at:]))
            if module:
                modules.add(module)
    return sorted(modules)


def _inside(path: str, folder: str) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    f = os.path.normcase(os.path.abspath(folder))
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# the game: scripts.pkg and the client version
# --------------------------------------------------------------------------- #


def resolve_package(args) -> tuple[str, str | None]:
    """(path to scripts.pkg, client version or None).  Computed when needed,
    never at import time."""
    wot_game = _tool_module("wot_game")
    if args.pkg:
        pkg = os.path.abspath(args.pkg)
        game_root = os.path.dirname(os.path.dirname(os.path.dirname(pkg)))
    else:
        chosen, candidates = wot_game.resolve_game_dir(args.game_dir)
        if chosen is None:
            die("game folder not found",
                "pass --game-dir <folder with paths.xml>, --pkg <path to scripts.pkg>, "
                "or set WOT_GAME_DIR")
        info("game dir: %s (from %s)" % (chosen.path, chosen.source))
        others = [c.path for c in candidates if c.path != chosen.path]
        if others:
            info("other game folders found: %s (pick one with --game-dir)" % ", ".join(others))
        game_root = chosen.path
        pkg = os.path.join(game_root, "res", "packages", "scripts.pkg")
    version = args.game_version
    if not version:
        layout, _problem = wot_game.read_paths_xml(game_root)
        version = layout.version if layout is not None else None
    return pkg, version


# --------------------------------------------------------------------------- #
# one .pyc
# --------------------------------------------------------------------------- #


class Job:
    __slots__ = ("entry", "pyc", "data")

    def __init__(self, entry: str, pyc: str, data: bytes):
        self.entry = entry
        self.pyc = pyc
        self.data = data


class Result:
    __slots__ = ("job", "kind", "text", "why")

    def __init__(self, job: Job, kind: str, text: str = "", why: str = ""):
        self.job = job
        self.kind = kind    # decompiled / partial / disasm / strings / failed
        self.text = text
        self.why = why

    @property
    def has_source(self) -> bool:
        return self.kind in ("decompiled", "partial")


def process(job: Job, runner: Runner, mode: str) -> Result:
    """Turn one .pyc into source / listing / strings.  Never raises."""
    try:
        if mode in ("auto", "decompile", "tree") and runner.python:
            status, text, why = runner.decompile(job.pyc)
            # The trailer names the .pyc it read -- a temp path of this machine.
            # Name the package entry instead: stable between runs, nothing local.
            text = text.replace(job.pyc, job.entry)
            if status == "ok":
                return Result(job, "decompiled", text)
            if status == "partial":
                return Result(job, "partial", PARTIAL_HEADER + "\n" + text, why)
            if mode == "decompile":
                return Result(job, "failed", "", why)
            first_why = why
        else:
            first_why = ""
        if mode in ("auto", "disasm", "tree") and runner.python:
            good, text = runner.disasm(job.pyc)
            if good:
                return Result(job, "disasm", text, first_why)
            if mode == "disasm":
                return Result(job, "failed", "", text)
        return Result(job, "strings", extract_strings(job.data), first_why)
    except Exception as exc:  # one broken file must not stop the other thousands
        return Result(job, "failed", "", "%s: %s" % (type(exc).__name__, exc))


def run_jobs(jobs: list[Job], runner: Runner, mode: str, workers: int, on_result) -> None:
    """Run in parallel; the first PROBE_BATCH .pyc go first, and if none of them
    gives source while a decompiler is present, stop instead of turning the
    whole client into strings dumps (a broken uncompyle6 install)."""
    probe_jobs = jobs[:PROBE_BATCH]
    rest = jobs[PROBE_BATCH:]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        probe_results = list(pool.map(lambda j: process(j, runner, mode), probe_jobs))
        for res in probe_results:
            on_result(res)
        wants_source = runner.python and mode in ("auto", "decompile", "tree")
        if wants_source and len(probe_jobs) >= PROBE_BATCH and not any(r.has_source for r in probe_results):
            why = next((r.why for r in probe_results if r.why), "no explanation")
            raise DecompilerBroken(why)
        for res in pool.map(lambda j: process(j, runner, mode), rest):
            on_result(res)


class DecompilerBroken(Exception):
    pass


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_client_source.py",
        description="Extract and decompile modules of YOUR game's scripts.pkg. "
        "Reads the game folder only, never writes to it. The output is decompiled "
        "Lesta client code: keep it on your machine, do not commit or publish it "
        "(.scratch/ is git-ignored).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples" + __doc__.split("Examples", 1)[-1] if __doc__ else "",
    )
    parser.add_argument("pattern", nargs="?", default=None,
                        help="path fragment or glob inside scripts.pkg "
                             "(with --tree: a filter, default scripts/**/*.pyc)")
    parser.add_argument("--tree", action="store_true",
                        help="the whole client as .scratch/wot-src-<version>/{client,client_common,common}")
    parser.add_argument("--pkg", default=None,
                        help="path to scripts.pkg (default: <game>/res/packages/scripts.pkg)")
    parser.add_argument("--game-dir", default=None,
                        help="game folder (default: WOT_GAME_DIR, WOTCOCKPIT_GAME_DIR, then auto-detect)")
    parser.add_argument("--game-version", default=None,
                        help="client version for the tree name (default: read from <game>/paths.xml)")
    parser.add_argument("--out", default=None,
                        help="output folder (default: .scratch/client-src, with --tree "
                             ".scratch/wot-src-<version>)")
    parser.add_argument(
        "--mode",
        choices=("auto", "decompile", "disasm", "strings", "raw"),
        default="auto",
        help="auto = decompile, fall back to disasm, then strings (default); ignored with --tree",
    )
    parser.add_argument("--list", action="store_true", help="only list matches, extract nothing")
    parser.add_argument("--limit", type=int, default=50,
                        help="max files to process, 0 = no limit (default 50; --tree has no limit)")
    parser.add_argument("--venv", default=None,
                        help="venv with uncompyle6 + xdis: its folder, Scripts folder or python.exe "
                             "(default: .tools/venv_uncompyle, then this Python)")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2),
                        help="files decompiled in parallel (default: CPU count - 2)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.version_info < (3, 10):
        die("extract_client_source.py needs Python 3.10+ (found %d.%d)" % sys.version_info[:2])
    parser = make_parser()
    args = parser.parse_args(argv)
    if not args.tree and not args.pattern:
        parser.error("give a pattern, or --tree for the whole client")
    pattern = args.pattern or "scripts/**/*.pyc"
    workers = max(1, args.jobs)

    pkg_path, version = resolve_package(args)
    if not os.path.isfile(pkg_path):
        die("package not found: %s" % pkg_path,
            "pass --game-dir <folder with paths.xml> or --pkg <path to scripts.pkg>")
    try:
        pkg = zipfile.ZipFile(pkg_path)
    except (zipfile.BadZipFile, OSError) as exc:
        die("%s is not a readable zip archive (%s)" % (pkg_path, exc))

    with pkg:
        names = pkg.namelist()
        hits = match_entries(names, pattern)
        if args.tree:
            hits = [n for n in hits if tree_entry(n)]
        if not hits:
            die("no entry matches %r among %d files in %s"
                % (pattern, len(names), os.path.basename(pkg_path)),
                "try a fragment like 'gui/mods/__init__.pyc' or a glob like '*avatar_getter*'")
        info("%d match(es) for %r (of %d entries)" % (len(hits), pattern, len(names)))
        if args.list:
            for name in hits:
                print("  %9d  %s" % (pkg.getinfo(name).file_size, name))
            return 0
        if not args.tree and args.limit > 0 and len(hits) > args.limit:
            info("limiting to the first %d (raise with --limit, 0 = no limit)" % args.limit)
            hits = hits[: args.limit]

        if args.tree:
            return extract_tree(args, pkg, pkg_path, hits, version, workers)
        return extract_files(args, pkg, hits, workers)


def _decompiler_for(args, needed: bool) -> str | None:
    python = find_decompiler(args.venv)
    if python:
        info("decompiler: %s -m uncompyle6.bin.uncompile" % python)
    elif needed:
        die("uncompyle6 + xdis are not installed (they are optional, GPL-licensed, and "
            "never shipped with the template)", INSTALL_HINT)
    else:
        info("uncompyle6 + xdis not found -- falling back to strings (%s)" % INSTALL_HINT)
    return python


def _warn_if_outside_scratch(path: str) -> None:
    if not _inside(path, SCRATCH):
        warn("%s is outside %s (the git-ignored work folder): make sure it is never "
             "committed or published" % (path, SCRATCH))


def extract_files(args, pkg: zipfile.ZipFile, hits: list[str], workers: int) -> int:
    out_root = os.path.abspath(args.out or DEFAULT_OUT)
    _warn_if_outside_scratch(out_root)
    mode = args.mode
    python = None
    if mode in ("auto", "decompile", "disasm"):
        python = _decompiler_for(args, needed=mode in ("decompile", "disasm"))
    stats = dict.fromkeys(("decompiled", "partial", "disasm", "strings", "raw", "failed"), 0)

    jobs: list[Job] = []
    for name in hits:
        data = pkg.read(name)
        raw_path = os.path.join(out_root, "pyc", name.replace("/", os.sep))
        try:
            save(raw_path, data)
        except OSError as exc:
            info("%s: cannot write %s (%s)%s" % (name, raw_path, exc, _long_path_hint(exc.filename or raw_path)))
            stats["failed"] += 1
            continue
        if not name.lower().endswith(".pyc"):
            kind = "packed BigWorld XML (binary)" if data[:4] == BWXML_MAGIC else "raw"
            info("%s -> pyc/%s  [%s]" % (name, name, kind))
            stats["raw"] += 1
            continue
        if data[:4] != PY27_MAGIC:
            info("%s: unexpected magic %s (not python 2.7)" % (name, data[:4].hex()))
        if mode == "raw":
            stats["raw"] += 1
            continue
        jobs.append(Job(name, raw_path, data))

    def on_result(res: Result) -> None:
        name = res.job.entry
        try:
            if res.kind in ("decompiled", "partial"):
                save(os.path.join(out_root, "src", name[:-1].replace("/", os.sep)),
                     res.text.encode("utf-8", "replace"))
                info("%s -> src/%s%s" % (name, name[:-1],
                                         "  [PARTIAL: %s]" % res.why if res.kind == "partial" else ""))
            elif res.kind == "disasm":
                save(os.path.join(out_root, "disasm", name.replace("/", os.sep) + ".txt"),
                     res.text.encode("utf-8", "replace"))
                info("%s -> disasm/%s.txt%s" % (name, name, "  (decompile failed: %s)" % res.why if res.why else ""))
            elif res.kind == "strings":
                save(os.path.join(out_root, "strings", name.replace("/", os.sep) + ".txt"),
                     res.text.encode("utf-8", "replace"))
                info("%s -> strings/%s.txt" % (name, name))
            else:
                info("%s: failed (%s)" % (name, res.why))
            stats[res.kind] += 1
        except OSError as exc:
            info("%s: cannot write the result (%s)%s" % (name, exc, _long_path_hint(exc.filename or out_root)))
            stats["failed"] += 1

    runner = Runner(python)
    try:
        run_jobs(jobs, runner, mode, workers, on_result)
    except DecompilerBroken as exc:
        die("the first %d files gave no source at all -- uncompyle6 looks broken: %s"
            % (PROBE_BATCH, exc), "check: " + INSTALL_HINT)
    finally:
        runner.close()

    info("done: %d decompiled, %d partial, %d disassembled, %d strings-only, %d raw, %d failed"
         % (stats["decompiled"], stats["partial"], stats["disasm"], stats["strings"],
            stats["raw"], stats["failed"]))
    info("output root: %s" % out_root)
    info(KEEP_LOCAL)
    return 1 if stats["failed"] else 0


def _long_path_hint(path) -> str:
    if sys.platform == "win32" and len(os.path.abspath(str(path))) > 200:
        return (" -- long path: enable Windows long paths (LongPathsEnabled) or pass a "
                "shorter --out")
    return ""


def extract_tree(args, pkg: zipfile.ZipFile, pkg_path: str, hits: list[str],
                 version: str | None, workers: int) -> int:
    if not version:
        die("cannot tell the client version for the tree name",
            "pass --game-version <version>, e.g. 1.46.0.0")
    tree_dir = os.path.abspath(args.out or os.path.join(SCRATCH, TREE_PREFIX + short_version(version)))
    extra_dir = tree_dir + ".extra"
    for path in (tree_dir, extra_dir):
        if os.path.exists(path):
            die("%s already exists" % path,
                "delete it (and %s) to extract again, or pass --out <new folder>"
                % os.path.basename(extra_dir))
    _warn_if_outside_scratch(tree_dir)
    python = _decompiler_for(args, needed=True)

    stamp = uuid.uuid4().hex[:8]
    staging = tree_dir + ".partial-" + stamp
    staging_extra = extra_dir + ".partial-" + stamp
    # .pyc copies go flat and numbered into a SHORT temp folder: Python without
    # Windows long paths cannot open files beyond 260 characters, and the deep
    # gui/impl/gen/... modules next to a deep target would exceed that.
    pyc_root = tempfile.mkdtemp(prefix="wotmod-pyc-")
    info("client %s: %d modules -> %s" % (version, len(hits), tree_dir))
    info(KEEP_LOCAL)

    stats = dict.fromkeys(("decompiled", "partial", "disasm", "strings", "failed"), 0)
    failed_modules: list[str] = []
    first_error = [""]
    done = [0]
    started = time.time()
    lock = threading.Lock()

    def on_result(res: Result) -> None:
        entry = res.job.entry
        rel = entry[len("scripts/"):]
        try:
            if res.has_source:
                save(os.path.join(staging, rel[:-1].replace("/", os.sep)),
                     res.text.encode("utf-8", "replace"))
            else:
                folder = "disasm" if res.kind == "disasm" else "strings"
                if res.kind in ("disasm", "strings"):
                    save(os.path.join(staging_extra, folder, rel.replace("/", os.sep) + ".txt"),
                         res.text.encode("utf-8", "replace"))
                with lock:
                    failed_modules.append(dotted_module(entry))
            kind = res.kind
        except OSError as exc:
            kind = "failed"
            res.why = "cannot write (%s)%s" % (exc, _long_path_hint(exc.filename or staging))
            with lock:
                failed_modules.append(dotted_module(entry))
        with lock:
            stats[kind] += 1
            if kind != "decompiled" and res.why and not first_error[0]:
                first_error[0] = "%s: %s" % (entry, res.why)
            done[0] += 1
            n = done[0]
        if n % 250 == 0 or n == len(hits):
            info("%d/%d (%d decompiled, %d partial, %d without source) %.0f s"
                 % (n, len(hits), stats["decompiled"], stats["partial"],
                    stats["disasm"] + stats["strings"] + stats["failed"], time.time() - started))

    runner = Runner(python)
    try:
        jobs = []
        for i, entry in enumerate(hits):
            data = pkg.read(entry)
            pyc = os.path.join(pyc_root, "%05d.pyc" % i)
            with open(pyc, "wb") as fh:
                fh.write(data)
            jobs.append(Job(entry, pyc, data))
        os.makedirs(staging)
        run_jobs(jobs, runner, "tree", workers, on_result)
        if stats["decompiled"] == 0:
            raise DecompilerBroken(first_error[0] or "no explanation")

        with open(os.path.join(staging, EXTRA_MODULES_FILE), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# Modules of the extra .pkg files of client %s "
                     "(extract_client_source.py --tree).\n" % version)
            for module in extra_modules(os.path.dirname(pkg_path)):
                fh.write(module + "\n")
        modules = sorted({m for m in failed_modules if m})
        if modules:
            with open(os.path.join(staging, FAILED_MODULES_FILE), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# Modules of client %s that uncompyle6 could not decompile "
                         "(they exist in the game, the tree has no source).\n" % version)
                for module in modules:
                    fh.write(module + "\n")
    except DecompilerBroken as exc:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(staging_extra, ignore_errors=True)
        die("no module decompiled -- uncompyle6 looks broken, the tree was not created (%s)" % exc,
            "check: " + INSTALL_HINT)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(staging_extra, ignore_errors=True)
        raise
    finally:
        runner.close()
        shutil.rmtree(pyc_root, ignore_errors=True)

    _move_into_place(staging, tree_dir)
    if os.path.isdir(staging_extra):
        _move_into_place(staging_extra, extra_dir)

    info("done: %d decompiled, %d partial (kept, marked in line 1), %d without source "
         "(%d disassembled, %d strings, %d failed)"
         % (stats["decompiled"], stats["partial"],
            stats["disasm"] + stats["strings"] + stats["failed"],
            stats["disasm"], stats["strings"], stats["failed"]))
    if first_error[0]:
        info("first problem: %s" % first_error[0])
    info("tree: %s" % tree_dir)
    if os.path.isdir(extra_dir):
        info("disassembly / strings of the rest: %s" % extra_dir)
    info("WoT Mod Cockpit: pick this folder with the folder button of the API browser")
    info(KEEP_LOCAL)
    return 0


def _move_into_place(src: str, dst: str) -> None:
    """Rename with a few retries: an antivirus or indexer scanning thousands of new
    files keeps Windows from renaming the folder for a moment."""
    for delay in (0.1, 0.25, 0.5, 1.0, 2.0, None):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if delay is None:
                die("the tree is ready but could not be renamed: %s -> %s (%s)" % (src, dst, exc),
                    "close what holds files in it and rename the folder by hand")
            time.sleep(delay)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
