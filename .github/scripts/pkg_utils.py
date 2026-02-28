"""
Shared utilities for parsing Arch Linux package archives.
"""

import re
import subprocess
import sys


def extract_pkginfo(archive_path):
    """
    Extract metadata from a .pkg.tar.zst archive by reading its .PKGINFO.

    Returns a dict with keys: 'name', 'version', 'provides', 'deps'
    or None on failure.
    """
    cmd = ["tar", "--use-compress-program=zstd", "-xOf", archive_path, ".PKGINFO"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return None
    except Exception as e:
        print(f"Failed to read {archive_path}: {e}", file=sys.stderr)
        return None

    name = None
    version = None
    provides = []
    deps = []

    for line in result.stdout.splitlines():
        if line.startswith("pkgname = "):
            name = line.split(" = ", 1)[1].strip()
        elif line.startswith("pkgver = "):
            version = line.split(" = ", 1)[1].strip()
        elif line.startswith("depend = "):
            raw_dep = line.split(" = ", 1)[1].strip()
            deps.append(re.split("[<>=]", raw_dep)[0])
        elif line.startswith("provides = "):
            raw_prov = line.split(" = ", 1)[1].strip()
            provides.append(re.split("[<>=]", raw_prov)[0])

    if not name:
        return None

    return {"name": name, "version": version, "provides": provides, "deps": deps}
