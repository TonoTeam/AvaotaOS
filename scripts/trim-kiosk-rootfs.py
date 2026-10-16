#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
"""Trim build-time data from a staged kiosk root, preserving runtime packages."""
import argparse
import json
import os
from pathlib import Path


LICENSE_PREFIXES = ("copyright", "copying", "license", "licence", "notice")


def trim(root):
    root = root.resolve()
    release = root / "etc/avaota-release"
    if root == Path("/") or not release.is_file() or "DESKTOP=kiosk" not in release.read_text().splitlines():
        raise ValueError("Expected a staged kiosk root filesystem, never the build host")

    def tree(relative):
        path = root
        for component in Path(relative).parts:
            path /= component
            if path.is_symlink():
                raise ValueError("Refusing a symlinked cleanup root: " + relative)
        return path

    # Check every cleanup/configuration root before making any changes.
    paths = {name: tree(name) for name in (
        "var/lib/apt/lists", "var/cache/apt", "usr/share/man", "usr/share/info",
        "usr/share/doc", "usr/share/locale", "etc/apt/apt.conf.d", "etc/dpkg/dpkg.cfg.d",
    )}
    removed = {"files": 0, "bytes": 0}

    def remove(path):
        if path.is_symlink() or not path.is_dir():
            removed["files"] += 1
            removed["bytes"] += path.lstat().st_size
            path.unlink()
        else:
            for child in list(path.iterdir()):
                remove(child)
            path.rmdir()

    def clear_cache(path):
        for child in list(path.iterdir()):
            # Preserve apt's working directory ownership/modes, not downloads.
            if child.name in ("partial", "archives") and child.is_dir() and not child.is_symlink():
                clear_cache(child)
            else:
                remove(child)

    for relative in ("var/lib/apt/lists", "var/cache/apt", "usr/share/man", "usr/share/info"):
        path = paths[relative]
        if path.exists():
            if relative.startswith("var/"):
                clear_cache(path)
            else:
                for child in list(path.iterdir()):
                    remove(child)

    docs = paths["usr/share/doc"]
    if docs.exists():
        for directory, _, files in os.walk(docs, followlinks=False):
            for name in files:
                path = Path(directory) / name
                # Preserve doc aliases and every package's license/notice files.
                if not path.is_symlink() and not name.lower().startswith(LICENSE_PREFIXES):
                    remove(path)

    locales = paths["usr/share/locale"]
    if locales.exists():
        for child in list(locales.iterdir()):
            name = child.name
            keep = name in ("C", "C.UTF-8", "POSIX", "en", "zh") or name.startswith(("en_", "en@", "zh_", "zh@"))
            if (child.is_dir() or child.is_symlink()) and not keep:
                remove(child)

    filters = [
        "# Kiosk keeps runtime assets, English/Chinese UI translations and licenses.",
        "path-exclude=/usr/share/man/*", "path-exclude=/usr/share/info/*",
        "path-exclude=/usr/share/doc/*",
    ]
    for prefix in LICENSE_PREFIXES:
        pattern = "".join("[" + char.lower() + char.upper() + "]" for char in prefix)
        filters.append("path-include=/usr/share/doc/*/" + pattern + "*")
    filters += [
        "path-exclude=/usr/share/locale/*",
        "path-include=/usr/share/locale/locale.alias",
        "path-include=/usr/share/locale/C*", "path-include=/usr/share/locale/POSIX*",
        "path-include=/usr/share/locale/en", "path-include=/usr/share/locale/en_*",
        "path-include=/usr/share/locale/en@*", "path-include=/usr/share/locale/zh",
        "path-include=/usr/share/locale/zh_*", "path-include=/usr/share/locale/zh@*",
    ]
    paths["etc/dpkg/dpkg.cfg.d"].mkdir(parents=True, exist_ok=True)
    (paths["etc/dpkg/dpkg.cfg.d"] / "01-tono-kiosk-size").write_text("\n".join(filters) + "\n")
    paths["etc/apt/apt.conf.d"].mkdir(parents=True, exist_ok=True)
    # apt can resolve packages without these two regenerable binary caches.
    (paths["etc/apt/apt.conf.d"] / "01-tono-kiosk-size").write_text(
        'Dir::Cache::pkgcache "";\nDir::Cache::srcpkgcache "";\n')
    print(json.dumps({"kiosk_trim": removed}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    trim(parser.parse_args().root)
