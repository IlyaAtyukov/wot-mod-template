#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
test_cockpit_contract.py -- guard what WoT Mod Cockpit relies on in this template.

The cockpit's "New mod..." wizard copies this template and rewrites it by plain
text replacement; its build panel runs build.py and parses the output.  None of
that is checked by the cockpit at the moment you edit the template, so an
innocent-looking edit here (single quotes instead of double, a renamed flag, a
token in a .toml file) breaks it silently.  This script fails instead.

Run:  python tools/test_cockpit_contract.py        (stdlib only, a few seconds)

It works in the template AND in a project created from it: everything is
derived from src/meta.xml and the entry point on disk.  The template-only checks
(exact template tokens, files the wizard cannot rewrite, publish guards) run
only while <id> in src/meta.xml is still the template's own id.

Keep in sync with WoT Mod Cockpit:
    src/WotModCockpit.Core/Scaffold/ProjectScaffolder.cs  (tokens, extensions, skipped dirs)
    src/WotModCockpit.Core/Scaffold/TemplateLocator.cs    (build.py + src/meta.xml)
    src/WotModCockpit.Core/Build/BuildRunner.cs           (flags, artifact regex)
    src/WotModCockpit.Core/Game/DeployProbe.cs            (dev_manifest.json "files")
    src/WotModCockpit.Core/Migration/*                    (.tools/venv_uncompyle, wot-src-<ver>)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODS = os.path.join(REPO, "src", "res", "scripts", "client", "gui", "mods")

# Written in pieces on purpose: the wizard replaces these tokens in every text
# file it copies, and in a new project this script must still recognise the
# template it came from.
TEMPLATE_ID = "ru.local." + "hello" + "_tanker"
TEMPLATE_NAME = "Hello" + " Tanker"
TEMPLATE_TAG = "Hello" + "Tanker"
TEMPLATE_PACKAGE = "hello" + "_tanker"
TEMPLATE_VERSION = "1.0.0"

# ProjectScaffolder.TextExtensions / SkipDirectories
TEXT_EXTENSIONS = {".py", ".pyi", ".json", ".xml", ".md", ".ps1", ".txt", ".cfg", ".ini",
                   ".yml", ".yaml", ".gitignore"}
SKIP_DIRS = {".git", ".scratch", ".tools", ".vs", ".idea", "dist", "__pycache__", "bin",
             "obj", "node_modules"}

# BuildRunner.ArtifactRegex (.NET (?<path>) == Python (?P<path>))
ARTIFACT_RE = re.compile(r"^\[build\]\s+OK\s+(?P<path>.+?\.mtmod)\s*\(")

# Flags BuildRunner passes (the value is a dummy).
BUILD_FLAGS = (
    ["--src", "x"], ["--dist", "x"], ["--game-dir", "x"], ["--install"], ["--dev"],
    ["--uninstall"], ["--list"], ["--clean"], ["--no-syntax-check"], ["--compile-all"],
    ["--keep-py"], ["--py27", "x"], ["--py2", "x"], ["--game-version", "1.0.0.0"],
)

# Machine paths that must never be published (built from pieces so that this
# file does not match itself).
AUTHOR_PATHS = re.compile(
    r"[dD]:[\\/]+[gG]ames[\\/]|/d/[gG]ames/|[cC]:[\\/]+[uU]sers[\\/]")


class ContractError(Exception):
    pass


def fail(msg: str) -> None:
    raise ContractError(msg)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


def dotnet_extension(name: str) -> str:
    """Path.GetExtension: from the last dot, so '.gitignore' -> '.gitignore'."""
    i = name.rfind(".")
    return name[i:] if 0 <= i < len(name) - 1 else ""


def project_files() -> list[str]:
    """Files git would publish (tracked + untracked, not ignored); without git,
    every file the wizard would copy."""
    try:
        out = subprocess.run(
            ["git", "-C", REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60)
        if out.returncode == 0:
            names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
            return sorted({os.path.join(REPO, n.replace("/", os.sep)) for n in names
                           if os.path.isfile(os.path.join(REPO, n))})
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        files.extend(os.path.join(root, n) for n in names if not n.endswith(".pyc"))
    return sorted(files)


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_identity() -> dict:
    for rel in ("build.py", os.path.join("src", "meta.xml")):
        if not os.path.isfile(os.path.join(REPO, rel)):
            fail("%s is missing -- TemplateLocator needs build.py AND src/meta.xml" % rel)
    root = ET.parse(os.path.join(REPO, "src", "meta.xml")).getroot()
    meta = {t: (root.findtext(t) or "").strip() for t in ("id", "version", "name")}
    for tag, value in meta.items():
        if not value:
            fail("src/meta.xml has no <%s>" % tag)
    if "<version>%s</version>" % meta["version"] not in read(os.path.join(REPO, "src", "meta.xml")):
        fail("src/meta.xml: write <version>...</version> on one line, the wizard replaces it as text")

    entries = sorted(n for n in os.listdir(MODS) if n.startswith("mod_") and n.endswith(".py"))
    if len(entries) != 1:
        fail("expected exactly one entry point mod_*.py in %s, found %s" % (MODS, entries))
    package = entries[0][len("mod_"):-len(".py")]
    for rel in ("__init__.py", "main.py"):
        if not os.path.isfile(os.path.join(MODS, package, rel)):
            fail("%s/%s is missing (the wizard checks for it after creating a project)" % (package, rel))

    main = read(os.path.join(MODS, package, "main.py"))
    m = re.search(r"^LOG_PREFIX = '\[(\w+)\]'$", main, re.M)
    if not m:
        fail("main.py: no line LOG_PREFIX = '[<Tag>]'")
    tag = m.group(1)
    for needle in ("MOD_ID = '%s'" % meta["id"], "MOD_NAME = '%s'" % meta["name"],
                   "logging.getLogger('%s')" % tag, "'%s', 'config.json'" % package):
        if needle not in main:
            fail("main.py: no %r" % needle)
    if not re.search(r"^_PATCH_MARKER = '_\w+_patched'$", main, re.M):
        fail("main.py: no line _PATCH_MARKER = '_<unique>_patched'")

    stub = read(os.path.join(MODS, entries[0]))
    for needle in ("MOD_ID = '%s'" % meta["id"],
                   "PACKAGE = 'gui.mods.%s'" % package,
                   "ENTRY = 'gui.mods.%s.main'" % package,
                   "RES_DIR = 'scripts/client/gui/mods/%s'" % package):
        if needle not in stub:
            fail("%s: no %r" % (entries[0], needle))
    if stub.count("'[%s/loader] '" % tag) != 2:
        fail("%s: the log prefix '[%s/loader] ' must appear in _note() and _error()" % (entries[0], tag))

    build = read(os.path.join(REPO, "build.py"))
    fallback = re.findall(r'^FALLBACK_ARCHIVE_NAME = "([^"\\]+\.mtmod)"$', build, re.M)
    if len(fallback) != 1:
        fail('build.py: need exactly one literal line FALLBACK_ARCHIVE_NAME = "<id>_<version>.mtmod" '
             "(double quotes) -- the wizard rewrites it as text")
    if not fallback[0].startswith(meta["id"] + "_"):
        fail("build.py: FALLBACK_ARCHIVE_NAME %s does not start with <id>_ %s" % (fallback[0], meta["id"]))

    loader_test = read(os.path.join(REPO, "tools", "test_stub_loader.py"))
    if '"%s"' % entries[0] not in loader_test or "check group(s) passed" not in loader_test:
        fail("tools/test_stub_loader.py must name %s and print 'check group(s) passed'" % entries[0])

    return {"meta": meta, "package": package, "tag": tag, "fallback": fallback[0]}


def check_template_tokens(project: dict) -> None:
    meta = project["meta"]
    expected = (TEMPLATE_ID, TEMPLATE_VERSION, TEMPLATE_NAME, TEMPLATE_PACKAGE, TEMPLATE_TAG)
    actual = (meta["id"], meta["version"], meta["name"], project["package"], project["tag"])
    if actual != expected:
        fail("template identity changed: %r != %r -- change it only together with the cockpit"
             % (actual, expected))
    if project["fallback"] != "%s_%s.mtmod" % (TEMPLATE_ID, TEMPLATE_VERSION):
        fail("build.py: FALLBACK_ARCHIVE_NAME must be %s_%s.mtmod" % (TEMPLATE_ID, TEMPLATE_VERSION))

    tokens = (TEMPLATE_ID, TEMPLATE_NAME, TEMPLATE_TAG, TEMPLATE_PACKAGE)
    problems = []
    for path in project_files():
        rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        for segment in rel.split("/"):
            if TEMPLATE_PACKAGE in segment and segment not in (TEMPLATE_PACKAGE, "mod_%s.py" % TEMPLATE_PACKAGE):
                problems.append("%s: the wizard renames only the folder %s/ and mod_%s.py"
                                % (rel, TEMPLATE_PACKAGE, TEMPLATE_PACKAGE))
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        text = data.decode("utf-8", "replace")
        if dotnet_extension(os.path.basename(path)).lower() not in TEXT_EXTENSIONS:
            hit = next((t for t in tokens if t in text), None)
            if hit:
                problems.append("%s contains %r, but the wizard copies this file type verbatim "
                                "(text extensions: %s)" % (rel, hit, " ".join(sorted(TEXT_EXTENSIONS))))
        m = AUTHOR_PATHS.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            problems.append("%s:%d: machine path %r -- use <папка игры> or C:\\Games\\Tanki"
                            % (rel, line, m.group(0)))
    if problems:
        fail("\n  ".join(problems))

    license_path = os.path.join(REPO, "LICENSE")
    if not os.path.isfile(license_path) or "BSD Zero Clause License" not in read(license_path):
        fail("LICENSE (0BSD) is missing")
    readme = read(os.path.join(REPO, "README.md"))
    for needle in ("0BSD", "Lesta", "WoT Mod Cockpit"):
        if needle not in readme:
            fail("README.md does not mention %s" % needle)


def check_build_cli() -> None:
    build = load("build_under_test", os.path.join(REPO, "build.py"))
    if not hasattr(build, "make_parser"):
        fail("build.py has no make_parser()")
    parser = build.make_parser()
    for flags in BUILD_FLAGS:
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(flags)
        except SystemExit:
            fail("build.py does not accept %s" % " ".join(flags))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        build.ok(build.artifact_message(r"C:\Games\x\dist\ru.example.x_1.0.0.mtmod", 123, 4))
        build.ok(build.install_message(r"C:\Games\Tanki\mods\1.0.0.0\ru.example.x_1.0.0.mtmod", True))
    artifact_line, install_line = out.getvalue().splitlines()
    m = ARTIFACT_RE.match(artifact_line)
    if not m or m.group("path") != r"C:\Games\x\dist\ru.example.x_1.0.0.mtmod":
        fail("the artifact line %r no longer matches the cockpit regex" % artifact_line)
    if ARTIFACT_RE.match(install_line):
        fail("the --install line %r looks like an artifact line to the cockpit" % install_line)

    tmp = tempfile.mkdtemp(prefix="contract-")
    try:
        build.write_dev_manifest(tmp, [r"C:\Games\Tanki\res_mods\1.0.0.0\x.py"],
                                 r"C:\Games\Tanki", r"C:\Games\Tanki\res_mods\1.0.0.0", "1.0.0.0")
        with open(os.path.join(tmp, build.DEV_MANIFEST), encoding="utf-8") as fh:
            data = json.load(fh)
        if build.DEV_MANIFEST != "dev_manifest.json" or not isinstance(data.get("files"), list):
            fail("dist/dev_manifest.json must stay {\"files\": [...]} (cockpit DeployProbe)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_syntax_checker() -> None:
    checker = os.path.join(REPO, "tools", "check_py2_syntax.py")
    tmp = tempfile.mkdtemp(prefix="contract-")
    try:
        good = os.path.join(tmp, "good")
        bad = os.path.join(tmp, "bad")
        empty = os.path.join(tmp, "empty")
        for folder in (good, bad, empty):
            os.makedirs(folder)
        with open(os.path.join(good, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        with open(os.path.join(bad, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = f'{1}'\n")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for folder, code in ((good, 0), (bad, 1), (empty, 2)):
            got = subprocess.run([sys.executable, checker, folder], capture_output=True,
                                 env=env, timeout=120).returncode
            if got != code:
                fail("check_py2_syntax.py exit code %d on %s, the contract says %d"
                     % (got, os.path.basename(folder), code))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_client_source_paths() -> None:
    extract = load("extract_under_test", os.path.join(REPO, "tools", "extract_client_source.py"))
    venv = os.path.join(REPO, ".tools", "venv_uncompyle")
    if os.path.normcase(extract.DEFAULT_VENV) != os.path.normcase(venv):
        fail("the decompiler venv must stay <template>/.tools/venv_uncompyle")
    if extract.TREE_PREFIX != "wot-src-" or os.path.normcase(extract.SCRATCH) != \
            os.path.normcase(os.path.join(REPO, ".scratch")):
        fail("client source trees must stay <template>/.scratch/wot-src-<version>")
    if (extract.FAILED_MODULES_FILE, extract.EXTRA_MODULES_FILE) != \
            (".cockpit-failed-modules.txt", ".cockpit-extra-modules.txt"):
        fail("module list file names must match the cockpit's ClientSourceTrees")
    if not extract.PARTIAL_HEADER.startswith("# ВНИМАНИЕ: декомпиляция частичная"):
        fail("the partial-decompile header prefix must match the cockpit's")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    groups = 0
    try:
        project = check_identity()
        print("OK  identity: %s %s, package %s, log tag %s"
              % (project["meta"]["id"], project["meta"]["version"], project["package"], project["tag"]))
        groups += 1
        if project["meta"]["id"] == TEMPLATE_ID:
            check_template_tokens(project)
            print("OK  template tokens, file types the wizard rewrites, no machine paths, LICENSE")
            groups += 1
        else:
            print("--  template-only checks skipped (this is a project created from the template)")
        check_build_cli()
        print("OK  build.py flags, artifact line, dev_manifest.json")
        groups += 1
        check_syntax_checker()
        print("OK  check_py2_syntax.py exit codes 0/1/2")
        groups += 1
        check_client_source_paths()
        print("OK  .tools/venv_uncompyle, .scratch/wot-src-<version>, module list files")
        groups += 1
    except ContractError as exc:
        print("FAIL %s" % exc)
        return 1
    print("\n%d check group(s) passed" % groups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
