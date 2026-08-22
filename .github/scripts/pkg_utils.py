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


_SEGMENT = re.compile(r"[0-9]+|[A-Za-z]+")
_LEADING_SEPARATORS = re.compile(r"^[^0-9A-Za-z]*")


def _rpmvercmp(a, b):
    """Compare two version segments using pacman's rpmvercmp algorithm."""
    if a == b:
        return 0

    while a and b:
        sep_a = _LEADING_SEPARATORS.match(a).end()
        sep_b = _LEADING_SEPARATORS.match(b).end()
        a = a[sep_a:]
        b = b[sep_b:]
        if not a or not b:
            break

        # differing separator runs decide the comparison on their own
        if sep_a != sep_b:
            return -1 if sep_a < sep_b else 1

        seg_a = _SEGMENT.match(a).group()
        seg_b = _SEGMENT.match(b).group()
        a = a[len(seg_a):]
        b = b[len(seg_b):]

        a_is_num = seg_a[0].isdigit()
        if a_is_num != seg_b[0].isdigit():
            # numeric segments always beat alphabetic ones
            return 1 if a_is_num else -1

        if a_is_num:
            seg_a = seg_a.lstrip("0")
            seg_b = seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1

        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1

    if not a and not b:
        return 0
    # a remaining alphabetic tail never beats an empty string
    if (not a and not b[0].isalpha()) or (a and a[0].isalpha()):
        return -1
    return 1


def _split_version(version):
    """Split 'epoch:pkgver-pkgrel' the way pacman's parseEVR does.

    An epoch is only recognised when everything before the ':' is digits;
    otherwise there is no epoch and the colon belongs to pkgver.
    """
    epoch = None
    head, sep, tail = version.partition(":")
    if sep and (head == "" or head.isdigit()):
        epoch = head or "0"
        version = tail

    pkgrel = None
    if "-" in version:
        version, pkgrel = version.rsplit("-", 1)

    return epoch, version, pkgrel


def vercmp(v1, v2):
    """Compare two full Arch package versions. Returns -1, 0 or 1."""
    if v1 == v2:
        return 0
    if v1 is None:
        return -1
    if v2 is None:
        return 1

    epoch1, ver1, rel1 = _split_version(v1)
    epoch2, ver2, rel2 = _split_version(v2)

    # an unset epoch is only ignored when the other side has none either
    if epoch1 is not None and epoch2 is not None:
        result = _rpmvercmp(epoch1, epoch2)
        if result:
            return result
    elif epoch1 is not None and epoch1.strip("0"):
        return 1
    elif epoch2 is not None and epoch2.strip("0"):
        return -1

    result = _rpmvercmp(ver1, ver2)
    if result:
        return result

    if rel1 is not None and rel2 is not None:
        return _rpmvercmp(rel1, rel2)
    return 0
