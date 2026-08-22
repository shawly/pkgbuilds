"""
Copy locally-built dependency archives into <pkgbuild_dir>/_deps so the build
container can pacman -U them before running makepkg.

The release holds every package ever published, including older versions of
packages that were rebuilt since. Picking an arbitrary archive per package name
silently builds against a stale dependency, so selection is explicit: the
version named in the target state wins, otherwise the newest one does.

Usage:
    resolve-deps.py <pkgbuild_dir> [--target-packages '{"pkg": "1.0-1"}']
"""

import argparse
import glob
import os
import shutil
import sys
import json

from pkg_utils import extract_pkginfo, vercmp
from pkgmeta import dep_name, get_pkg_info


def read_pkgbuild_deps(pkgbuild_dir):
    """Return the union of depends and makedepends, stripped of version specifiers."""
    info = get_pkg_info(os.path.join(pkgbuild_dir, 'PKGBUILD'))
    if info is None:
        print(f"Error reading metadata for {pkgbuild_dir}", file=sys.stderr)
        sys.exit(1)

    needed = set()
    for dep in info.get('depends', []) + info.get('makedepends', []):
        clean = dep_name(dep)
        if clean:
            needed.add(clean)
    return needed


def pick_archives(archives, target):
    """Map every package name and provide to the single archive that should be used."""
    best = {}

    for archive in archives:
        meta = extract_pkginfo(archive)
        if not meta:
            continue

        entry = {
            'path': archive,
            'name': meta['name'],
            'version': meta['version'],
            'provides': meta['provides'],
            'deps': meta['deps'],
        }

        current = best.get(entry['name'])
        if current is None or _preferred(entry, current, target.get(entry['name'])):
            best[entry['name']] = entry

    # Real package names take precedence over anything merely providing them.
    meta_map = dict(best)
    for entry in best.values():
        for provide in entry['provides']:
            meta_map.setdefault(provide, entry)

    return meta_map, best


def _preferred(candidate, current, wanted_version):
    """Should candidate replace current as the archive used for this package name?"""
    if wanted_version is not None:
        if candidate['version'] == wanted_version:
            return current['version'] != wanted_version
        if current['version'] == wanted_version:
            return False
    return vercmp(candidate['version'], current['version']) > 0


def resolve_and_copy_deps(pkgbuild_dir, target):
    if not os.path.exists(os.path.join(pkgbuild_dir, 'PKGBUILD')):
        print(f"No PKGBUILD found in {pkgbuild_dir}.", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {pkgbuild_dir}/PKGBUILD...")
    needed = read_pkgbuild_deps(pkgbuild_dir)
    if not needed:
        print("No dependencies found.")
        return

    print(f"Direct dependencies: {sorted(needed)}")

    repo_root = os.path.abspath('.')
    archives = glob.glob(os.path.join(repo_root, '*.pkg.tar.zst'))
    if not archives:
        print("No archives found in workspace.")
        return

    print(f"Scanning {len(archives)} archives in {repo_root}...")
    meta_map, best = pick_archives(archives, target)

    skipped = len(archives) - len(best)
    if skipped > 0:
        print(f"Ignoring {skipped} superseded archive(s).")

    # Resolve the transitive closure over the selected archives.
    queue = sorted(needed)
    resolved = {}
    checked = set()

    while queue:
        wanted = queue.pop(0)
        if wanted in checked:
            continue
        checked.add(wanted)

        entry = meta_map.get(wanted)
        if entry is None:
            continue

        if entry['name'] not in resolved:
            resolved[entry['name']] = entry
            print(f"  Found dependency: {wanted} -> {os.path.basename(entry['path'])}")
            queue.extend(entry['deps'])

    stale = False
    for name, entry in sorted(resolved.items()):
        wanted = target.get(name)
        if wanted is not None and entry['version'] != wanted:
            print(
                f"  WARNING: using {name} {entry['version']} but the repo targets {wanted}; "
                "that version was never published (its build likely failed).",
                file=sys.stderr,
            )
            stale = True

    if stale:
        print(
            "WARNING: building against stale local dependencies, this build may fail "
            "or produce a package linked against the wrong versions.",
            file=sys.stderr,
        )

    if not resolved:
        print("No local dependencies found to copy.")
        return

    deps_folder = os.path.join(pkgbuild_dir, '_deps')
    os.makedirs(deps_folder, exist_ok=True)
    print(f"Copying {len(resolved)} dependencies to {deps_folder}...")
    for entry in resolved.values():
        shutil.copy(entry['path'], deps_folder)


def main():
    parser = argparse.ArgumentParser(description="Copy local dependency archives into _deps")
    parser.add_argument('pkgbuild_dir')
    parser.add_argument('--target-packages', default='{}',
                        help="JSON string of {pkgname: version} representing the desired state")
    args = parser.parse_args()

    try:
        target = json.loads(args.target_packages or '{}')
    except json.JSONDecodeError as e:
        print(f"Invalid target_packages JSON: {e}", file=sys.stderr)
        sys.exit(1)

    resolve_and_copy_deps(args.pkgbuild_dir, target)


if __name__ == '__main__':
    main()
