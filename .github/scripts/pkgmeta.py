"""
Read package metadata without executing the PKGBUILD.

Every AUR submodule ships a .SRCINFO: plain "key = value" text produced by
makepkg --printsrcinfo, with every array and substitution already expanded.
Parsing that instead of sourcing the PKGBUILD removes a code-execution
primitive from the metadata path, which runs on pull_request before anything
has audited the diff.

pkgver is the one field .SRCINFO cannot be trusted for. The analyze job
refreshes pkgver in the PKGBUILD (makepkg --nobuild) for VCS packages and does
not regenerate .SRCINFO, so versions are read from the PKGBUILD as literal text
and fall back to .SRCINFO per field. Only plain literal assignments at column 0
are accepted; anything containing a substitution or subshell is ignored, since
resolving it would mean running it.

Packages with no .SRCINFO still have to be sourced. That happens in a
network-isolated namespace where the kernel allows it.
"""

import json
import os
import re
import subprocess
import sys

# pkgver=1.2.3 / pkgrel="2" / epoch=1, at column 0 only. Indented assignments
# live inside pkgver()/package() and must not be picked up.
_SCALAR_ASSIGN = re.compile(r'^(pkgver|pkgrel|epoch)=(\S+)\s*(?:#.*)?$')

# Anything that would need a shell to resolve.
_NEEDS_SHELL = re.compile(r'[$`(){}\[\]]')

_UNSHARE_PREFIX = None


def dep_name(dep):
    """Strip the version specifier off a depends entry."""
    return re.split(r'[<>=]', dep, maxsplit=1)[0].strip()


def parse_srcinfo(srcinfo_path):
    """
    Parse a .SRCINFO into {pkgname, depends, makedepends, version_fields}.

    depends and makedepends are the union of the pkgbase section and every
    per-package section. Sourcing the PKGBUILD only ever saw the pkgbase
    arrays, so split packages that add dependencies in package_foo() were
    invisible to the dependency graph before.

    Returns None when the file is missing or has no pkgname at all.
    """
    if not os.path.exists(srcinfo_path):
        return None

    pkgnames = []
    depends = []
    makedepends = []
    version_fields = {}

    try:
        with open(srcinfo_path, encoding='utf-8', errors='replace') as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        print(f"Error reading {srcinfo_path}: {exc}", file=sys.stderr)
        return None

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if not value:
            continue

        if key == 'pkgname':
            pkgnames.append(value)
        elif key == 'depends':
            depends.append(value)
        elif key == 'makedepends':
            makedepends.append(value)
        elif key in ('pkgver', 'pkgrel', 'epoch') and key not in version_fields:
            # The pkgbase section comes first and these cannot be overridden
            # per package, so the first occurrence is the authoritative one.
            version_fields[key] = value

    if not pkgnames:
        return None

    return {
        'pkgname': pkgnames,
        'depends': sorted(set(depends)),
        'makedepends': sorted(set(makedepends)),
        'version_fields': version_fields,
    }


def read_pkgbuild_scalars(pkgbuild_path):
    """Pull literal pkgver/pkgrel/epoch out of a PKGBUILD as text."""
    fields = {}
    try:
        with open(pkgbuild_path, encoding='utf-8', errors='replace') as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        print(f"Error reading {pkgbuild_path}: {exc}", file=sys.stderr)
        return fields

    for raw in lines:
        match = _SCALAR_ASSIGN.match(raw.rstrip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not value or _NEEDS_SHELL.search(value):
            continue
        # Last assignment wins, same as bash.
        fields[key] = value

    return fields


def format_version(primary, fallback):
    """Assemble [epoch:]pkgver-pkgrel, preferring primary per field."""
    fields = {}
    for key in ('epoch', 'pkgver', 'pkgrel'):
        value = primary.get(key) or fallback.get(key)
        if value:
            fields[key] = value

    if 'pkgver' not in fields or 'pkgrel' not in fields:
        return ''

    epoch = f"{fields['epoch']}:" if 'epoch' in fields else ''
    return f"{epoch}{fields['pkgver']}-{fields['pkgrel']}"


def _unshare_prefix():
    """Command prefix that puts a child in an empty network namespace, if possible."""
    global _UNSHARE_PREFIX
    if _UNSHARE_PREFIX is not None:
        return _UNSHARE_PREFIX

    candidate = ['unshare', '--net', '--map-root-user']
    try:
        subprocess.run(
            candidate + ['true'],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        _UNSHARE_PREFIX = candidate
    except (OSError, subprocess.SubprocessError):
        print(
            "WARNING: unshare unavailable, sourcing PKGBUILD without network isolation",
            file=sys.stderr,
        )
        _UNSHARE_PREFIX = []

    return _UNSHARE_PREFIX


def source_pkgbuild(pkgbuild_path):
    """
    Last resort for packages with no .SRCINFO: source the PKGBUILD in a
    network-isolated namespace and emit the fields as JSON.

    This executes attacker-controlled code. It exists only because a PKGBUILD
    without a .SRCINFO gives us nothing else to read.
    """
    cwd = os.path.dirname(pkgbuild_path) or '.'
    script = f"""
    source "{os.path.basename(pkgbuild_path)}" || true

    jq -n \\
      --arg pkgname "${{pkgname[*]}}" \\
      --arg version "${{epoch:+${{epoch}}:}}${{pkgver}}-${{pkgrel}}" \\
      --arg depends "${{depends[*]}}" \\
      --arg makedepends "${{makedepends[*]}}" \\
      '{{
        pkgname: ($pkgname | split(" ") | map(select(length > 0))),
        version: $version,
        depends: ($depends | split(" ") | map(select(length > 0))),
        makedepends: ($makedepends | split(" ") | map(select(length > 0)))
      }}'
    """

    cmd = _unshare_prefix() + ['bash', '-c', script]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        print(f"Error executing bash script for {pkgbuild_path}: {exc}", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"Error parsing JSON from {pkgbuild_path}: {exc}", file=sys.stderr)
        return None


def get_pkg_info(pkgbuild_path):
    """
    Return {pkgname, version, depends, makedepends} for a PKGBUILD, without
    executing it when a .SRCINFO is available.
    """
    if not os.path.exists(pkgbuild_path):
        print(f"Skipping {pkgbuild_path}: File not found", file=sys.stderr)
        return None

    pkgdir = os.path.dirname(pkgbuild_path) or '.'
    info = parse_srcinfo(os.path.join(pkgdir, '.SRCINFO'))

    if info is None:
        print(
            f"{pkgbuild_path}: no usable .SRCINFO, falling back to sourcing it",
            file=sys.stderr,
        )
        return source_pkgbuild(pkgbuild_path)

    version_fields = info.pop('version_fields')
    info['version'] = format_version(read_pkgbuild_scalars(pkgbuild_path), version_fields)
    return info
