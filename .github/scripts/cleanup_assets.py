"""
Reconcile release assets against the desired target state.

Given a target_packages map (pkgname -> version) and a directory of downloaded
.pkg.tar.zst files, determines which files are stale (wrong version or unknown
package) and should be deleted from the release.

Outputs:
  - delete.txt: list of asset filenames to delete from the release
  - keep.txt: list of asset filenames that match the target state

Usage:
    python3 cleanup_assets.py --target-packages '{"pkg": "1.0-1"}' --assets-dir .
"""

import argparse
import glob
import json
import os
import sys

from pkg_utils import extract_pkginfo


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

    for archive in archives:
        filename = os.path.basename(archive)
        sig_filename = filename + ".sig"
        info = extract_pkginfo(archive)
        name = info["name"] if info else None
        version = info["version"] if info else None

        if name is None:
            print(f"  Could not read metadata from {filename}, marking for deletion", file=sys.stderr)
            to_delete.append(filename)
            to_delete.append(sig_filename)
            continue

        expected_version = target.get(name)

        if expected_version is None:
            # Package no longer exists in any PKGBUILD — delete it
            print(f"  {filename}: '{name}' not in target state, deleting", file=sys.stderr)
            to_delete.append(filename)
            to_delete.append(sig_filename)
        elif version != expected_version:
            # Wrong version — old build, delete it
            print(f"  {filename}: '{name}' version '{version}' != expected '{expected_version}', deleting", file=sys.stderr)
            to_delete.append(filename)
            to_delete.append(sig_filename)
        else:
            # Matches target state — keep
            print(f"  {filename}: '{name}' version '{version}' OK", file=sys.stderr)
            to_keep.append(filename)
            to_keep.append(sig_filename)

    with open("delete.txt", "w") as f:
        for item in to_delete:
            f.write(item + "\n")

    with open("keep.txt", "w") as f:
        for item in to_keep:
            f.write(item + "\n")

    print(f"\nSummary: {len(to_keep)} assets to keep, {len(to_delete)} assets to delete", file=sys.stderr)


if __name__ == "__main__":
    main()
