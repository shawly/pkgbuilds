"""
Shared utilities for parsing Arch Linux package archives.
"""

import io
import re
import sys
import tarfile

import zstandard


def extract_pkginfo(archive_path):
    """
    Extract metadata from a .pkg.tar.zst archive by reading its .PKGINFO.

    Returns a dict with keys: 'name', 'version', 'provides', 'deps'
    or None on failure.
    """
    try:
        with open(archive_path, "rb") as fh:
            dctx = zstandard.ZstdDecompressor()
            with dctx.stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    for member in tar:
                        if member.name == ".PKGINFO":
                            f = tar.extractfile(member)
                            if f:
                                content = f.read().decode("utf-8", errors="ignore")
                                return _parse_pkginfo(content)
    except Exception as e:
        print(f"Failed to read {archive_path}: {e}", file=sys.stderr)

    return None


def _parse_pkginfo(content):
    """Parse .PKGINFO content into a metadata dict."""
    name = None
    version = None
    provides = []
    deps = []

    for line in content.splitlines():
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
