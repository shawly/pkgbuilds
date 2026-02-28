import os
import subprocess
import glob
import sys
import re
import shutil

from pkg_utils import extract_pkginfo


def resolve_and_copy_deps(pkgbuild_dir):
    # 1. Ensure we are in a directory with PKGBUILD
    if not os.path.exists(os.path.join(pkgbuild_dir, 'PKGBUILD')):
        print(f"No PKGBUILD found in {pkgbuild_dir}.", file=sys.stderr)
        sys.exit(1)

    # 2. Get dependencies from PKGBUILD
    print(f"Analyzing {pkgbuild_dir}/PKGBUILD...")
    cmd = [
        'bash', '-c',
        f'cd "{pkgbuild_dir}" && source PKGBUILD && echo "${{depends[@]}} ${{makedepends[@]}}"'
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw_deps = res.stdout.strip().split()
    except subprocess.CalledProcessError as e:
        print(f"Error reading PKGBUILD: {e}", file=sys.stderr)
        sys.exit(1)

    # Clean version specifiers (foo>=1.0 -> foo)
    needed = set()
    for d in raw_deps:
        clean = re.split('[<>=]', d)[0]
        if clean:
            needed.add(clean)

    if not needed:
        print("No dependencies found.")
        return

    print(f"Direct dependencies: {needed}")

    # 3. Scan available package archives (single pass)
    repo_root = os.path.abspath('.')
    archives = glob.glob(os.path.join(repo_root, '*.pkg.tar.zst'))

    if not archives:
        print("No archives found in workspace.")
        return

    print(f"Scanning {len(archives)} archives in {repo_root}...")

    # Build metadata map in a single pass over all archives
    # Maps: name/provides -> {path, deps}
    meta_map = {}

    for arc in archives:
        meta = extract_pkginfo(arc)
        if not meta:
            continue

        entry = {'path': arc, 'deps': meta['deps']}
        meta_map[meta['name']] = entry
        for prov in meta['provides']:
            if prov not in meta_map:
                meta_map[prov] = entry

    # 4. Resolve transitive closure of local dependencies
    queue = list(needed)
    resolved_files = set()
    checked = set()

    while queue:
        dep_name = queue.pop(0)
        if dep_name in checked:
            continue
        checked.add(dep_name)

        if dep_name in meta_map:
            entry = meta_map[dep_name]
            if entry['path'] not in resolved_files:
                resolved_files.add(entry['path'])
                print(f"  Found dependency: {dep_name} -> {os.path.basename(entry['path'])}")
                # Enqueue transitive dependencies
                queue.extend(entry['deps'])

    # 5. Copy resolved dependencies
    if resolved_files:
        deps_folder = os.path.join(pkgbuild_dir, '_deps')
        os.makedirs(deps_folder, exist_ok=True)
        print(f"Copying {len(resolved_files)} dependencies to {deps_folder}...")
        for f in resolved_files:
            shutil.copy(f, deps_folder)
    else:
        print("No local dependencies found to copy.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: resolve-deps.py <pkgbuild_dir>", file=sys.stderr)
        sys.exit(1)
    resolve_and_copy_deps(sys.argv[1])
