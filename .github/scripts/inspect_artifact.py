"""
Gate 3: artifact inspection.

Gates 1 and 2 ask "is the recipe trustworthy." This one asks "did the thing
that came out of the oven actually match the recipe." It is the only gate
that can catch a trojaned upstream tarball behind an honest, unremarkable
PKGBUILD (T3 in the threat model) -- nothing static in Gate 1 sees inside a
tarball, and Gate 2's network-off build only proves the build didn't reach
out, not that what it packaged is clean.

Inputs: the package just built, and the version of the same package currently
published in the GitHub release. The release itself is the baseline -- there
is no separate file to keep in sync, and a package's second build always has
something to diff against, because the first build (if accepted) is what got
published.

A package with nothing currently published (a genuinely new package) has no
diff to run and is scored `review` outright: someone reads it once, by hand,
the same way `trust.json`'s bootstrap was a one-time manual acceptance.
"""

import argparse
import dataclasses
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BLOCK = "block"
REVIEW = "review"
INFO = "info"

_SYSTEMD_DIRS = ('usr/lib/systemd/system/', 'etc/systemd/system/', 'usr/lib/systemd/user/')
_HARD_BLOCK_DIRS = ('etc/profile.d/', 'etc/cron.', 'etc/sudoers.d/', 'usr/share/libalpm/hooks/')
_META_ENTRIES = {'.', '.PKGINFO', '.INSTALL', '.MTREE', '.BUILDINFO', '.CHANGELOG'}

_BASE_LIBS = frozenset({
    'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
    'libgcc_s.so.1', 'libstdc++.so.6', 'ld-linux-x86-64.so.2', 'libresolv.so.2',
    'libutil.so.1',
})

_DEPENDS_ARRAY_RE = re.compile(r'\bdepends(?:_\w+)?=\((.*?)\)', re.DOTALL)
_TOKEN_RE = re.compile(r'''['"]([^'"]+)['"]|(\S+)''')


@dataclasses.dataclass
class Finding:
    id: str
    severity: str
    detail: str


@dataclasses.dataclass
class Verdict:
    package: str
    verdict: str
    findings: list
    first_build: bool

    def to_dict(self):
        return {
            "package": self.package,
            "verdict": self.verdict,
            "first_build": self.first_build,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }


# --------------------------------------------------------------------------
# Package archive helpers
# --------------------------------------------------------------------------

def extract_package(pkg_path, dest_dir):
    subprocess.run(['tar', '--zstd', '-xf', pkg_path, '-C', dest_dir], check=True)


def list_entries(pkg_path):
    """[(mode_str, path)] for every entry, symlink targets and trailing
    slashes stripped."""
    result = subprocess.run(
        ['tar', '--zstd', '-tvf', pkg_path], capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        mode = parts[0]
        path = parts[5].split(' -> ')[0].rstrip('/')
        if path.startswith('./'):
            path = path[2:]
        if not path or path == '.':
            continue
        entries.append((mode, path))
    return entries


def has_setuid(mode):
    return bool(mode) and len(mode) > 3 and mode[3] in ('s', 'S')


def has_setgid(mode):
    return bool(mode) and len(mode) > 6 and mode[6] in ('s', 'S')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def read_pkginfo(extracted_dir):
    """.PKGINFO is the same plain key=value text .SRCINFO is, produced by
    makepkg rather than hand-written. `depend` repeats; everything else is
    first-occurrence-wins."""
    path = os.path.join(extracted_dir, '.PKGINFO')
    if not os.path.exists(path):
        return {}
    fields = {}
    depends = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key == 'depend':
                depends.append(value)
            elif key not in fields:
                fields[key] = value
    fields['depend'] = depends
    return fields


def dep_base_name(dep):
    return re.split(r'[<>=]', dep, maxsplit=1)[0].strip()


def parse_pkgbuild_depends(text):
    """Every literal depends=()/depends_$arch=() entry, base name only.
    Approximate: array parsing via regex, same tradeoff as audit_submodule.py."""
    deps = set()
    for m in _DEPENDS_ARRAY_RE.finditer(text):
        for quoted, bare in _TOKEN_RE.findall(m.group(1)):
            value = quoted or bare
            if value:
                deps.add(dep_base_name(value))
    return deps


def prefix_of(path):
    parts = path.split('/')
    return '/'.join(parts[:3]) if len(parts) > 1 else parts[0]


def is_elf(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x7fELF'
    except OSError:
        return False


def elf_needed(path):
    try:
        result = subprocess.run(
            ['readelf', '-d', path], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    needed = []
    for line in result.stdout.splitlines():
        m = re.search(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', line)
        if m:
            needed.append(m.group(1))
    return needed


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_install_script(old_dir, new_dir):
    findings = []
    old_install = os.path.join(old_dir, '.INSTALL')
    new_install = os.path.join(new_dir, '.INSTALL')
    old_exists = os.path.exists(old_install)
    new_exists = os.path.exists(new_install)
    if new_exists and not old_exists:
        findings.append(Finding(
            "artifact.install_added", BLOCK,
            "This build adds an .INSTALL scriptlet where none existed before",
        ))
    elif old_exists and new_exists and sha256_file(old_install) != sha256_file(new_install):
        findings.append(Finding(
            "artifact.install_changed", BLOCK,
            "The .INSTALL scriptlet content changed",
        ))
    return findings


def check_new_paths(added_paths):
    findings = []
    systemd_hits = sorted(p for p in added_paths if p.startswith(_SYSTEMD_DIRS))
    if systemd_hits:
        findings.append(Finding(
            "artifact.new_systemd_unit", REVIEW,
            f"New file(s) under a systemd unit directory: {', '.join(systemd_hits)}",
        ))
    hard_hits = sorted(p for p in added_paths if p.startswith(_HARD_BLOCK_DIRS) or p == 'etc/crontab')
    if hard_hits:
        findings.append(Finding(
            "artifact.new_privileged_path", BLOCK,
            f"New file(s) in a privileged auto-run location: {', '.join(hard_hits)}",
        ))
    return findings


def check_outside_prefix(added_paths, old_paths):
    if not old_paths:
        return []
    old_prefixes = {prefix_of(p) for p in old_paths}
    outside = sorted(p for p in added_paths if prefix_of(p) not in old_prefixes)
    if not outside:
        return []
    shown = ', '.join(outside[:10]) + ('...' if len(outside) > 10 else '')
    return [Finding(
        "artifact.new_path_prefix", REVIEW,
        f"New file(s) outside any prefix the previous version used: {shown}",
    )]


def check_setuid(new_modes, old_modes, first_build):
    findings = []
    severity = REVIEW if first_build else BLOCK
    for path, mode in new_modes.items():
        old_mode = old_modes.get(path)
        if has_setuid(mode) and not has_setuid(old_mode):
            findings.append(Finding("artifact.new_setuid", severity, f"{path} gained the setuid bit"))
        if has_setgid(mode) and not has_setgid(old_mode):
            findings.append(Finding("artifact.new_setgid", severity, f"{path} gained the setgid bit"))
    return findings


def check_size_growth(old_pkginfo, new_pkginfo):
    try:
        old_size = int(old_pkginfo.get('size', 0))
        new_size = int(new_pkginfo.get('size', 0))
    except ValueError:
        return []
    if old_size <= 0:
        return []
    pkgver_changed = old_pkginfo.get('pkgver') != new_pkginfo.get('pkgver')
    if new_size > old_size * 3 and not pkgver_changed:
        return [Finding(
            "artifact.size_spike", REVIEW,
            f"Installed size grew from {old_size} to {new_size} bytes (>3x) with no pkgver change",
        )]
    return []


def check_depends(old_pkginfo, new_pkginfo, pkgbuild_text):
    old_deps = set(old_pkginfo.get('depend', []))
    new_deps = set(new_pkginfo.get('depend', []))
    gained = new_deps - old_deps
    if not gained:
        return []
    expected = parse_pkgbuild_depends(pkgbuild_text)
    unexplained = []
    for dep in sorted(gained):
        base = dep_base_name(dep)
        if '.so' in base or base in expected:
            continue  # soname deps are auto-added by makepkg from ELF scanning
        unexplained.append(dep)
    if not unexplained:
        return []
    return [Finding(
        "artifact.unexplained_depend", REVIEW,
        f"New runtime depend(s) not present in PKGBUILD's depends=(): {', '.join(unexplained)}",
    )]


def check_elf_closure(extract_dir, new_pkginfo):
    provided_sonames = {
        dep_base_name(d) for d in new_pkginfo.get('depend', []) if '.so' in dep_base_name(d)
    }
    unresolved = set()
    for root, _, files in os.walk(extract_dir):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or not is_elf(path):
                continue
            for lib in elf_needed(path):
                if lib in _BASE_LIBS or lib in provided_sonames:
                    continue
                unresolved.add(lib)
    if not unresolved:
        return []
    return [Finding(
        "artifact.unresolved_needed", INFO,
        f"ELF NEEDED librar(y/ies) not covered by depends/provides: {', '.join(sorted(unresolved))}",
    )]


def run_clamav(extract_dir):
    if shutil.which('clamscan') is None:
        return [Finding("artifact.clamav_unavailable", REVIEW, "clamscan not installed; skipped")]
    try:
        result = subprocess.run(
            ['clamscan', '-r', '--infected', '--no-summary', extract_dir],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.SubprocessError as exc:
        return [Finding("artifact.clamav_error", REVIEW, f"clamscan failed to run: {exc}")]
    return [
        Finding("artifact.clamav_hit", BLOCK, line.strip())
        for line in result.stdout.splitlines() if line.endswith('FOUND')
    ]


def run_yara(extract_dir, rules_dir):
    rule_files = sorted(glob.glob(os.path.join(rules_dir, '*.yar')))
    if not rule_files:
        return []
    if shutil.which('yara') is None:
        return [Finding("artifact.yara_unavailable", REVIEW, "yara not installed; skipped")]
    findings = []
    for rule_file in rule_files:
        try:
            result = subprocess.run(
                ['yara', '-r', rule_file, extract_dir],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.SubprocessError as exc:
            findings.append(Finding("artifact.yara_error", REVIEW, f"yara failed on {rule_file}: {exc}"))
            continue
        for line in result.stdout.splitlines():
            if line.strip():
                findings.append(Finding("artifact.yara_hit", BLOCK, line.strip()))
    return findings


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def inspect_package(package, new_pkg_path, old_pkg_path, pkgbuild_text, yara_rules_dir,
                     skip_clamav, skip_yara):
    findings = []
    first_build = not (old_pkg_path and os.path.exists(old_pkg_path))

    with tempfile.TemporaryDirectory() as tmp:
        new_dir = os.path.join(tmp, 'new')
        os.makedirs(new_dir)
        extract_package(new_pkg_path, new_dir)
        new_entries = list_entries(new_pkg_path)
        new_pkginfo = read_pkginfo(new_dir)

        old_dir = None
        old_entries = []
        old_pkginfo = {}
        if not first_build:
            old_dir = os.path.join(tmp, 'old')
            os.makedirs(old_dir)
            extract_package(old_pkg_path, old_dir)
            old_entries = list_entries(old_pkg_path)
            old_pkginfo = read_pkginfo(old_dir)

        new_paths = {p for _, p in new_entries if p not in _META_ENTRIES}
        old_paths = {p for _, p in old_entries if p not in _META_ENTRIES}
        added_paths = new_paths - old_paths
        new_modes = dict(reversed([(p, m) for m, p in new_entries]))
        old_modes = dict(reversed([(p, m) for m, p in old_entries]))

        if first_build:
            findings.append(Finding(
                "artifact.no_baseline", REVIEW,
                "No previously published version to diff against; treating as first build",
            ))
            if os.path.exists(os.path.join(new_dir, '.INSTALL')):
                findings.append(Finding(
                    "artifact.first_install_script", INFO,
                    "First build ships an .INSTALL scriptlet; read it in full before approving",
                ))
        else:
            findings.extend(check_install_script(old_dir, new_dir))
            findings.extend(check_new_paths(added_paths))
            findings.extend(check_outside_prefix(added_paths, old_paths))
            findings.extend(check_size_growth(old_pkginfo, new_pkginfo))
            findings.extend(check_depends(old_pkginfo, new_pkginfo, pkgbuild_text))

        findings.extend(check_setuid(new_modes, old_modes, first_build))
        # Hard-block dirs and setuid apply to the whole tree, not just what's
        # new, on a first build too -- added_paths == new_paths there anyway.
        if first_build:
            findings.extend(check_new_paths(new_paths))

        if not skip_clamav:
            findings.extend(run_clamav(new_dir))
        if not skip_yara:
            findings.extend(run_yara(new_dir, yara_rules_dir))

        findings.extend(check_elf_closure(new_dir, new_pkginfo))

    severities = {f.severity for f in findings}
    if BLOCK in severities:
        verdict = BLOCK
    elif REVIEW in severities:
        verdict = REVIEW
    else:
        verdict = "pass"

    return Verdict(package, verdict, findings, first_build)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--new', required=True, help="Path to the freshly built .pkg.tar.zst")
    parser.add_argument('--old', help="Path to the currently published .pkg.tar.zst, if any")
    parser.add_argument('--pkgbuild', required=True, help="Path to the PKGBUILD used for this build")
    parser.add_argument('--output', required=True, help="Where to write this package's verdict.json")
    parser.add_argument('--yara-rules-dir', default='.github/security/yara')
    parser.add_argument('--skip-clamav', action='store_true')
    parser.add_argument('--skip-yara', action='store_true')
    args = parser.parse_args()

    pkgbuild_text = ''
    if os.path.exists(args.pkgbuild):
        with open(args.pkgbuild, encoding='utf-8', errors='replace') as f:
            pkgbuild_text = f.read()

    verdict = inspect_package(
        args.package, args.new, args.old, pkgbuild_text,
        args.yara_rules_dir, args.skip_clamav, args.skip_yara,
    )

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(verdict.to_dict(), f, indent=2)

    lines = [f"### Gate 3 inspect: {args.package} -- {verdict.verdict}", ""]
    for finding in verdict.findings:
        lines.append(f"- **{finding.severity}** {finding.id}: {finding.detail}")
    if not verdict.findings:
        lines.append("- no findings")
    text = "\n".join(lines) + "\n"
    print(text)

    step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary:
        with open(step_summary, 'a', encoding='utf-8') as f:
            f.write(text)

    if verdict.verdict == BLOCK:
        print(f"::error::Gate 3 BLOCKED {args.package}", file=sys.stderr)
    elif verdict.verdict == REVIEW:
        print(f"::warning::Gate 3 needs human review: {args.package}", file=sys.stderr)

    return 1 if verdict.verdict == BLOCK else 0


if __name__ == '__main__':
    sys.exit(main())
