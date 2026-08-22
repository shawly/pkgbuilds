"""
Reconcile release assets against the desired target state.

Given a target_packages map (pkgname -> version) and a directory of downloaded
.pkg.tar.zst files, determines which files are stale and should be deleted from
the release.

Assets are grouped per package name so that only one version of each survives:
  - package not in the target state at all -> delete every version of it
  - target version present                 -> keep it, delete the other versions
  - target version missing (build failed)   -> keep the newest published version
    so the package does not disappear from the repo, delete the rest

Outputs:
  - delete.txt: list of asset filenames to delete from the release
  - keep.txt: list of asset filenames that should stay

Usage:
    python3 cleanup_assets.py --target-packages '{"pkg": "1.0-1"}' --assets-dir .
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from functools import cmp_to_key

from pkg_utils import extract_pkginfo, vercmp


def main():
    parser = argparse.ArgumentParser(description="Reconcile release assets against target state")
    parser.add_argument("--target-packages", required=True,
                        help="JSON string of {pkgname: version} representing desired state")
    parser.add_argument("--assets-dir", default=".",
                        help="Directory containing downloaded .pkg.tar.zst files")
    args = parser.parse_args()

    try:
        target = json.loads(args.target_packages)
    except json.JSONDecodeError as e:
        print(f"Invalid target_packages JSON: {e}", file=sys.stderr)
        sys.exit(1)

    archives = glob.glob(os.path.join(args.assets_dir, "*.pkg.tar.zst"))

    if not archives:
        print("No .pkg.tar.zst files found in assets directory.", file=sys.stderr)
        # Write empty files so the workflow doesn't fail
        open("delete.txt", "w").close()
        open("keep.txt", "w").close()
        return

    to_delete = []
    to_keep = []

    def drop(filename, reason):
        print(f"  {filename}: {reason}, deleting", file=sys.stderr)
        to_delete.append(filename)
        to_delete.append(filename + ".sig")

    def retain(filename, reason):
        print(f"  {filename}: {reason}", file=sys.stderr)
        to_keep.append(filename)
        to_keep.append(filename + ".sig")

    by_name = defaultdict(list)

    for archive in archives:
        filename = os.path.basename(archive)
        info = extract_pkginfo(archive)
        if not info or not info.get("name"):
            drop(filename, "could not read metadata")
            continue
        by_name[info["name"]].append((filename, info["version"]))

    for name in sorted(by_name):
        versions = by_name[name]
        expected = target.get(name)

        if expected is None:
            # Package no longer exists in any PKGBUILD
            for filename, version in versions:
                drop(filename, f"'{name}' not in target state")
            continue

        matching = [f for f, v in versions if v == expected]
        if matching:
            keeper = matching[0]
            retain(keeper, f"'{name}' version '{expected}' OK")
            for filename, version in versions:
                if filename != keeper:
                    drop(filename, f"'{name}' version '{version}' superseded by '{expected}'")
            continue

        # Target version was never published: the build failed or has not run yet.
        # Keep the newest published version so the package stays in the repo.
        newest = max(versions, key=cmp_to_key(lambda a, b: vercmp(a[1], b[1])))
        print(
            f"  WARNING: '{name}' target version '{expected}' is not published, "
            f"keeping '{newest[1]}' instead.",
            file=sys.stderr,
        )
        retain(newest[0], f"'{name}' version '{newest[1]}' kept as last known good")
        for filename, version in versions:
            if filename != newest[0]:
                drop(filename, f"'{name}' version '{version}' older than '{newest[1]}'")

    with open("delete.txt", "w") as f:
        for item in to_delete:
            f.write(item + "\n")

    with open("keep.txt", "w") as f:
        for item in to_keep:
            f.write(item + "\n")

    print(f"\nSummary: {len(to_keep)} assets to keep, {len(to_delete)} assets to delete", file=sys.stderr)


if __name__ == "__main__":
    main()
