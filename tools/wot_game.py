#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
wot_game.py -- find the installed game and read its paths.xml.

Standard library only, READ-ONLY: nothing here writes to the registry or to the
game folder.  Used by build.py and tools/extract_client_source.py.

Where the game folder comes from, in this order:
    1. --game-dir                       (explicit, taken as is)
    2. env WOT_GAME_DIR                 (explicit, taken as is; empty = unset)
    3. env WOTCOCKPIT_GAME_DIR          (explicit, the WoT Mod Cockpit variable)
    4. the Windows "installed programs" list (Uninstall keys in HKCU/HKLM, both
       64- and 32-bit views): publisher Lesta/Wargaming, or a display name with
       «Мир танков» / World of Tanks / Tanki -> InstallLocation
    5. typical folders on every fixed drive (see TYPICAL_FOLDERS)
Discovered candidates (4, 5) count only when they contain paths.xml; the ones
whose paths.xml mounts *.mtmod packages (the Lesta client) come first.

KEEP IN SYNC with WoT Mod Cockpit:
    src/WotModCockpit.Core/Game/GameDirectoryFinder.cs   (publishers, names,
                                                          typical folders)
    src/WotModCockpit.Core/Game/GameInstallLocator.cs    (paths.xml parsing)
so that the cockpit and the command line never disagree about the game folder,
the client version or the mods folders.

Run it directly to see what it finds:
    python tools/wot_game.py [--game-dir <path>]
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from typing import List, NamedTuple, Optional, Tuple

ENV_VARS = ("WOT_GAME_DIR", "WOTCOCKPIT_GAME_DIR")
PATHS_XML = "paths.xml"
MTMOD_MASK = "*.mtmod"

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
PUBLISHERS = ("Lesta", "Wargaming")
DISPLAY_NAMES = ("Мир танков", "World of Tanks", "Tanki")
TYPICAL_FOLDERS = (
    "Games\\World_of_Tanks_RU",
    "Games\\World_of_Tanks",
    "Games\\Tanki",
    "Games\\Мир танков",
    "Games\\WoT",
    "World_of_Tanks_RU",
    "World_of_Tanks",
    "Tanki",
    "Мир танков",
    "WoT",
)

SOURCE_CLI = "--game-dir"
SOURCE_REGISTRY = "registry"
SOURCE_TYPICAL = "typical folder"


class GameDir(NamedTuple):
    """A candidate game folder and where it came from."""

    path: str
    source: str      # "--game-dir", "env WOT_GAME_DIR", "registry", "typical folder"
    explicit: bool   # given by the user (flag or env), not discovered


class GameLayout(NamedTuple):
    """What paths.xml says about one install."""

    root: str
    version: str         # e.g. 1.46.0.0 -- the last segment of the mods line
    mods_dir: str        # <root>/mods/<version>      (packages)
    res_mods_dir: str    # <root>/res_mods/<version>  (loose files, loaded first)
    mods_mask: str       # mask of the mods line: '*.mtmod' (Lesta), '*.wotmod' (WG), '' = none
    python_log: str

    @property
    def loads_mtmod(self) -> bool:
        return self.mods_mask.lower() == MTMOD_MASK


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip spaces and quotes; an empty value counts as unset."""
    if value is None:
        return None
    value = value.strip().strip('"').strip()
    return value or None


def normalize(path: str) -> str:
    """Absolute path without a trailing separator (except for a drive root)."""
    full = os.path.abspath(os.path.expanduser(path))
    drive_root = os.path.splitdrive(full)[0] + os.sep
    if len(full) > len(drive_root):
        full = full.rstrip("\\/")
    return full


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(normalize(a)) == os.path.normcase(normalize(b))


def looks_like_game(path: str) -> bool:
    try:
        return os.path.isfile(os.path.join(path, PATHS_XML))
    except (OSError, ValueError):
        return False


def looks_like_version(segment: str) -> bool:
    """Digits and dots only, at least one of each (1.46.0.0)."""
    if not segment:
        return False
    has_dot = has_digit = False
    for ch in segment:
        if ch == ".":
            has_dot = True
        elif "0" <= ch <= "9":
            has_digit = True
        else:
            return False
    return has_dot and has_digit


def _segments(raw: str) -> List[str]:
    return [s for s in raw.replace("\\", "/").split("/") if s and s != "."]


# --------------------------------------------------------------------------- #
# paths.xml
# --------------------------------------------------------------------------- #


def read_paths_xml(root: str) -> Tuple[Optional[GameLayout], str]:
    """Parse <root>/paths.xml.  Returns (layout, "") or (None, problem).

    Mirrors PathsXmlGameInstallLocator: every <Path> element in file order; the
    mods line is the first one with mask="*.mtmod" or parent folder 'mods', the
    res_mods line is the first one with parent folder 'res_mods'; the last
    segment must look like a version.  If the two lines disagree, the version
    of the mods line wins, but each folder is taken from its own line.
    """
    root = normalize(root)
    path = os.path.join(root, PATHS_XML)
    if not os.path.isfile(path):
        return None, "no paths.xml in %s" % root
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError, ValueError) as exc:
        return None, "cannot read %s (%s)" % (path, exc)

    mods_raw = mods_ver = res_raw = res_ver = None
    mods_mask = ""
    for el in tree.getroot().iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag.rsplit("}", 1)[-1].lower() != "path":
            continue
        raw = (el.text or "").strip()
        segs = _segments(raw)
        if len(segs) < 2 or not looks_like_version(segs[-1]):
            continue
        folder = segs[-2].lower()
        mask = (el.get("mask") or "").strip()
        if mods_raw is None and (mask.lower() == MTMOD_MASK or folder == "mods"):
            mods_raw, mods_ver, mods_mask = raw, segs[-1], mask
            continue
        if res_raw is None and folder == "res_mods":
            res_raw, res_ver = raw, segs[-1]

    version = mods_ver or res_ver
    if not version:
        return None, ("%s has no ./mods/<version> or ./res_mods/<version> line -- "
                      "cannot tell the client version" % path)

    def resolve(raw: Optional[str], default_folder: str) -> str:
        if raw is None:
            return os.path.join(root, default_folder, version)
        if os.path.isabs(raw):
            return normalize(raw)
        return normalize(os.path.join(root, *_segments(raw)))

    layout = GameLayout(
        root=root,
        version=version,
        mods_dir=resolve(mods_raw, "mods"),
        res_mods_dir=resolve(res_raw, "res_mods"),
        mods_mask=mods_mask,
        python_log=os.path.join(root, "python.log"),
    )
    return layout, ""


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def explicit_game_dir(cli_value: Optional[str]) -> Optional[GameDir]:
    """--game-dir, then WOT_GAME_DIR, then WOTCOCKPIT_GAME_DIR (taken as is)."""
    value = _clean(cli_value)
    if value:
        return GameDir(normalize(value), SOURCE_CLI, True)
    for var in ENV_VARS:
        value = _clean(os.environ.get(var))
        if value:
            return GameDir(normalize(value), "env " + var, True)
    return None


def _registry_locations() -> List[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg  # noqa: WPS433 (Windows only)
    except ImportError:
        return []
    found: List[str] = []
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive in hives:
        for view in views:
            try:
                uninstall = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with uninstall:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(uninstall, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        app = winreg.OpenKey(uninstall, name, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
                    with app:
                        publisher = _reg_str(winreg, app, "Publisher")
                        display = _reg_str(winreg, app, "DisplayName")
                        location = _reg_str(winreg, app, "InstallLocation")
                    if not location:
                        continue
                    if _contains_any(publisher, PUBLISHERS) or _contains_any(display, DISPLAY_NAMES):
                        found.append(location)
    return found


def _reg_str(winreg, key, name: str) -> str:
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return value if isinstance(value, str) else ""


def _contains_any(text: str, needles) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _fixed_drive_roots() -> List[str]:
    if sys.platform != "win32":
        return []
    try:
        import ctypes  # noqa: WPS433
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
    except Exception:
        return []
    roots = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = "%s:\\" % chr(ord("A") + i)
        try:
            if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                roots.append(root)
        except Exception:
            continue
    return roots


def _typical_locations() -> List[str]:
    out = []
    for root in _fixed_drive_roots():
        for folder in TYPICAL_FOLDERS:
            out.append(os.path.join(root, folder))
    return out


def discover_game_dirs() -> List[GameDir]:
    """Registry, then typical folders.  Only folders with paths.xml, no duplicates,
    the ones that mount *.mtmod packages first (stable otherwise)."""
    seen = set()
    found: List[GameDir] = []
    for source, paths in ((SOURCE_REGISTRY, _registry_locations()),
                          (SOURCE_TYPICAL, _typical_locations())):
        for raw in paths:
            raw = _clean(raw)
            if not raw:
                continue
            try:
                full = normalize(raw)
            except (OSError, ValueError):
                continue
            key = os.path.normcase(full)
            if key in seen or not looks_like_game(full):
                continue
            seen.add(key)
            found.append(GameDir(full, source, False))

    def rank(item: GameDir) -> int:
        layout, _problem = read_paths_xml(item.path)
        return 0 if layout is not None and layout.loads_mtmod else 1

    found.sort(key=rank)  # list.sort is stable
    return found


def find_game_dirs(cli_value: Optional[str] = None) -> List[GameDir]:
    """Every candidate in priority order: the explicit one (if any) first."""
    out: List[GameDir] = []
    explicit = explicit_game_dir(cli_value)
    if explicit is not None:
        out.append(explicit)
    for item in discover_game_dirs():
        if explicit is None or not same_path(item.path, explicit.path):
            out.append(item)
    return out


def resolve_game_dir(cli_value: Optional[str] = None) -> Tuple[Optional[GameDir], List[GameDir]]:
    """(chosen, discovered candidates).

    An explicit folder (flag or env) wins and is taken as is, even without
    paths.xml -- the caller reports that, it never silently falls back to a
    different install.  Otherwise the best discovered candidate is chosen and
    the full list is returned so that a writing action can refuse to guess.
    """
    explicit = explicit_game_dir(cli_value)
    if explicit is not None:
        return explicit, [explicit]
    found = discover_game_dirs()
    return (found[0] if found else None), found


def _main(argv=None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        prog="wot_game.py",
        description="Show which game folder build.py / extract_client_source.py would use. "
                    "Read-only.")
    parser.add_argument("--game-dir", default=None, help="explicit game folder")
    args = parser.parse_args(argv)

    chosen, candidates = resolve_game_dir(args.game_dir)
    if chosen is None:
        print("game dir: not found (pass --game-dir <path> or set WOT_GAME_DIR)")
        return 1
    print("game dir: %s (from %s)" % (chosen.path, chosen.source))
    layout, problem = read_paths_xml(chosen.path)
    if layout is None:
        print("  %s" % problem)
    else:
        print("  client version: %s" % layout.version)
        print("  mods:     %s (mask %s)" % (layout.mods_dir, layout.mods_mask or "-"))
        print("  res_mods: %s" % layout.res_mods_dir)
    others = [c for c in candidates if not same_path(c.path, chosen.path)]
    for other in others:
        print("other candidate: %s (from %s)" % (other.path, other.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
