"""
Gate 1: static, stateful, diff-scoped audit of AUR submodules.

Unlike a whole-tree linter sweep, this asks "did the trust relationship
change since the last commit we accepted," which is what actually matters
against a maintainer hijack or a slipped-in malicious diff. Traur is kept as
one signal among several rather than the whole gate, and it only runs on the
two PKGBUILD revisions that actually changed.

Every submodule's accepted state (last commit, maintainer, source hosts,
etc.) lives in trust.json. A package whose current commit already matches
its trust.json entry is a fast no-op: no AUR RPC call, no diff, no traur run.
A package with no entry at all is "first seen" and gets a review verdict
rather than a hard block, since there is nothing to compare against yet.

trust.json only advances (via --advance-trust) for packages that come back
`pass`. A `block` or `review` verdict leaves the old entry in place, so the
next run flags the same commit again instead of silently accepting it.
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

import aur_rpc
from pkgmeta import get_pkg_info, read_pkgbuild_scalars_from_text

BLOCK = "block"
REVIEW = "review"
INFO = "info"

DEFAULT_ALLOWED_HOSTS = frozenset({
    "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "gitlab.com", "sourceforge.net", "aur.archlinux.org",
})

# Old vote/popularity count must drop below this fraction of what trust.json
# recorded to be worth flagging. Arbitrary but conservative threshold.
_VOTE_DROP_RATIO = 0.5

_DANGEROUS_COMMANDS = re.compile(
    r'\b(curl|wget|nc|ncat)\b'
    r'|\bnpm\s+i(?:nstall)?\b'
    r'|\bpip\s+install\b'
    r'|\bcargo\s+install\b'
    r'|\bgo\s+install\b'
    r'|\bgit\s+clone\b'
    r'|bash\s*<\('
    r'|\|\s*sh\b'
    r'|\|\s*bash\b'
)
_DANGEROUS_ENCODING = re.compile(r'base64\s+-d|xxd\s+-r|\beval\b|\\x[0-9a-fA-F]{2}')
_URL_RE = re.compile(r'[a-z][a-z0-9+.\-]*://([^/\s\'")]+)')
_SOURCE_ARRAY_RE = re.compile(r'source[_a-zA-Z0-9]*=\((.*?)\)', re.DOTALL)


@dataclasses.dataclass
class Finding:
    id: str
    severity: str
    detail: str


@dataclasses.dataclass
class Verdict:
    package: str
    from_commit: str | None
    to_commit: str | None
    verdict: str
    findings: list
    changed: bool

    def to_dict(self):
        return {
            "package": self.package,
            "from": self.from_commit,
            "to": self.to_commit,
            "verdict": self.verdict,
            "changed": self.changed,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def run_git(args, cwd, check=True, timeout=60):
    result = subprocess.run(
        ['git'] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def submodule_paths(repo_root):
    """Parse .gitmodules for {name: relative_path}."""
    result = run_git(
        ['config', '-f', '.gitmodules', '--get-regexp', r'submodule\..*\.path'],
        cwd=repo_root, check=False,
    )
    paths = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(' ')
        name = key.split('.')[1]
        paths[name] = value.strip()
    return paths


def read_pkgbase(path):
    """The AUR pkgbase for a submodule, which is not always its folder name
    (e.g. the `steamtinkerlaunch` folder tracks AUR's `steamtinkerlaunch-git`)."""
    srcinfo = os.path.join(path, '.SRCINFO')
    if os.path.exists(srcinfo):
        with open(srcinfo, encoding='utf-8', errors='replace') as f:
            for line in f:
                key, sep, value = line.strip().partition('=')
                if sep and key.strip() == 'pkgbase':
                    return value.strip()
    return os.path.basename(path)


def submodule_url(repo_root, name):
    result = run_git(
        ['config', '-f', '.gitmodules', '--get', f'submodule.{name}.url'],
        cwd=repo_root, check=False,
    )
    return result.stdout.strip() or None


def head_commit(path):
    result = run_git(['rev-parse', 'HEAD'], cwd=path, check=False)
    return result.stdout.strip() or None


def is_shallow(path):
    return run_git(['rev-parse', '--is-shallow-repository'], cwd=path, check=False).stdout.strip() == 'true'


def ensure_commit(path, sha):
    """Make sure `sha` is present in `path` *with* the history connecting it to
    HEAD, not just as a loose object.

    A shallow submodule is the trap here. `git fetch origin <sha>` succeeds in
    a --depth=1 clone and grafts that one commit, so a naive "did the fetch
    work" test passes -- but the commits in between are still missing, and
    `merge-base --is-ancestor` then reports every ordinary fast-forward as a
    rewritten history. Unshallow first whenever the repo is shallow, even if
    the object is already there.
    """
    if not sha:
        return False
    if is_shallow(path):
        # --unshallow errors out on a complete repository, hence the guard.
        if run_git(['fetch', '--quiet', '--unshallow', 'origin'], cwd=path, check=False).returncode != 0:
            # Deliberately not falling back to `fetch origin <sha>` here: in a
            # shallow repo that succeeds and grafts the bare object, which is
            # worse than failing, because the caller then cannot tell a missing
            # middle from a rewritten history.
            return False
    if run_git(['cat-file', '-e', f'{sha}^{{commit}}'], cwd=path, check=False).returncode == 0:
        return True
    return run_git(['fetch', '--quiet', 'origin', sha], cwd=path, check=False).returncode == 0


def show_file(path, commit, filename):
    result = run_git(['show', f'{commit}:{filename}'], cwd=path, check=False)
    return result.stdout if result.returncode == 0 else None


def diff_files(path, old, new):
    result = run_git(['diff', '--name-only', old, new], cwd=path, check=False)
    return [l for l in result.stdout.splitlines() if l]


def diff_text(path, old, new, filename):
    result = run_git(['diff', old, new, '--', filename], cwd=path, check=False)
    return result.stdout


def commit_count(path, old, new):
    result = run_git(['rev-list', '--count', f'{old}..{new}'], cwd=path, check=False)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def is_ancestor(path, ancestor, descendant):
    return run_git(['merge-base', '--is-ancestor', ancestor, descendant], cwd=path, check=False).returncode == 0


def commit_author_email(path, commit):
    result = run_git(['show', '-s', '--format=%ae', commit], cwd=path, check=False)
    return result.stdout.strip() or None


def remote_head_sha(path):
    """The remote's HEAD sha, or None if it could not be fetched with enough
    history to be compared against. None means "unknown", never "bad"."""
    result = run_git(['ls-remote', 'origin', 'HEAD'], cwd=path, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    sha = result.stdout.split()[0]
    return sha if ensure_commit(path, sha) else None


# --------------------------------------------------------------------------
# PKGBUILD text helpers
# --------------------------------------------------------------------------

def added_lines(diff):
    return [l[1:] for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++')]


def extract_array_block(text, name):
    m = re.search(rf'{re.escape(name)}=\((.*?)\)', text, re.DOTALL)
    return m.group(1).strip() if m else None


def extract_function_body(text, name):
    m = re.search(rf'^{re.escape(name)}\(\)\s*\{{(.*?)^\}}', text, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else None


def extract_source_hosts(text):
    hosts = set()
    for block in _SOURCE_ARRAY_RE.finditer(text):
        for m in _URL_RE.finditer(block.group(1)):
            host = m.group(1).split('@')[-1].split(':')[0].lower()
            if '$' in host or '`' in host:
                continue  # unresolvable without a shell; skip rather than guess
            hosts.add(host)
    return hosts


def run_traur(pkgbuild_text, tmp_root):
    """True/False = traur's pass/fail rating, None = traur unavailable or errored."""
    if shutil.which('traur') is None:
        return None
    tmpdir = tempfile.mkdtemp(dir=tmp_root)
    try:
        pkgbuild_path = os.path.join(tmpdir, 'PKGBUILD')
        with open(pkgbuild_path, 'w', encoding='utf-8') as f:
            f.write(pkgbuild_text)
        result = subprocess.run(
            ['traur', 'scan', '--pkgbuild', pkgbuild_path],
            capture_output=True, text=True, timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"traur invocation failed: {exc}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_aur_provenance(aur_info, trust_entry):
    findings = []
    if aur_info is None:
        findings.append(Finding("aur.lookup_failed", REVIEW, "Could not retrieve AUR info for this package"))
        return findings

    maintainer = aur_info.get("Maintainer")
    trusted_maintainer = trust_entry.get("aur_maintainer")

    if maintainer is None:
        findings.append(Finding("aur.orphaned", BLOCK, "Package is orphaned on AUR (no maintainer)"))
    elif trusted_maintainer and maintainer != trusted_maintainer:
        findings.append(Finding(
            "aur.maintainer_changed", BLOCK,
            f"Maintainer changed: '{trusted_maintainer}' -> '{maintainer}'",
        ))
        first_submitted = aur_info.get("FirstSubmitted")
        last_modified = aur_info.get("LastModified")
        if first_submitted and last_modified and (last_modified - first_submitted) > 30 * 86400:
            findings.append(Finding(
                "aur.adopt_then_modify_shape", BLOCK,
                "Established package changed maintainer (the Atomic Arch adopt-then-modify shape)",
            ))

    comaintainers = set(aur_info.get("CoMaintainers") or [])
    trusted_comaintainers = set(trust_entry.get("aur_comaintainers") or [])
    new_comaintainers = comaintainers - trusted_comaintainers
    if new_comaintainers:
        findings.append(Finding(
            "aur.new_comaintainer", REVIEW,
            f"New co-maintainer(s): {', '.join(sorted(new_comaintainers))}",
        ))

    votes = aur_info.get("NumVotes")
    trusted_votes = trust_entry.get("aur_votes")
    if votes is not None and trusted_votes and votes < trusted_votes * _VOTE_DROP_RATIO:
        findings.append(Finding(
            "aur.votes_dropped", INFO,
            f"NumVotes dropped from {trusted_votes} to {votes} (possible listing manipulation)",
        ))

    return findings


def check_pkgbuild_diff(old_text, new_text, diff, changed_paths, trust_entry):
    findings = []

    def is_allowed(f):
        if f in ('PKGBUILD', '.SRCINFO', '.gitignore'):
            return True
        base = f.rsplit('/', 1)[-1]
        return base.endswith(('.install', '.desktop', '.patch'))

    unexpected = [f for f in changed_paths if not is_allowed(f)]
    if unexpected:
        findings.append(Finding(
            "diff.unexpected_files", REVIEW,
            f"Files changed beyond PKGBUILD/.SRCINFO/.install/.desktop/.patch: {', '.join(unexpected)}",
        ))

    install_changed = [f for f in changed_paths if f.endswith('.install')]
    if install_changed:
        findings.append(Finding(
            "diff.install_script_changed", BLOCK,
            f"Install scriptlet changed: {', '.join(install_changed)}",
        ))

    old_has_install = bool(re.search(r'^install=', old_text, re.MULTILINE))
    new_has_install = bool(re.search(r'^install=', new_text, re.MULTILINE))
    if new_has_install and not old_has_install:
        findings.append(Finding(
            "diff.install_line_added", BLOCK,
            "PKGBUILD gained an install= line where none existed before",
        ))

    added = added_lines(diff)
    added_blob = '\n'.join(added)

    m = _DANGEROUS_COMMANDS.search(added_blob)
    if m:
        findings.append(Finding(
            "diff.dangerous_command", BLOCK,
            f"New line contains a network-fetch/exec pattern: {m.group(0)!r}",
        ))

    m = _DANGEROUS_ENCODING.search(added_blob)
    if m:
        findings.append(Finding(
            "diff.obfuscation", BLOCK,
            f"New line contains an obfuscation/encoding pattern: {m.group(0)!r}",
        ))

    for line in added:
        if len(line) > 500:
            findings.append(Finding(
                "diff.long_line", BLOCK,
                "A new line over 500 characters was added (possible obfuscated payload)",
            ))
            break

    old_hosts = extract_source_hosts(old_text)
    new_hosts = extract_source_hosts(new_text)
    allowed_hosts = set(trust_entry.get("source_hosts") or []) | DEFAULT_ALLOWED_HOSTS
    unallowed_new_hosts = new_hosts - old_hosts - allowed_hosts
    if unallowed_new_hosts:
        findings.append(Finding(
            "diff.new_source_host", BLOCK,
            f"source=() gained host(s) not in the allowlist: {', '.join(sorted(unallowed_new_hosts))}",
        ))

    if re.search(r'source[_a-zA-Z0-9]*=\([^)]*http://', added_blob):
        findings.append(Finding("diff.insecure_source", BLOCK, "A source=() entry uses http:// instead of https://"))
    if re.search(r'source[_a-zA-Z0-9]*=\([^)]*://(?:\d{1,3}\.){3}\d{1,3}', added_blob):
        findings.append(Finding("diff.raw_ip_source", BLOCK, "A source=() entry points at a raw IP address"))

    old_scalars = read_pkgbuild_scalars_from_text(old_text)
    new_scalars = read_pkgbuild_scalars_from_text(new_text)
    pkgver_changed = old_scalars.get('pkgver') != new_scalars.get('pkgver')

    for sumname in ('sha256sums', 'b2sums'):
        old_sum = extract_array_block(old_text, sumname)
        new_sum = extract_array_block(new_text, sumname)
        if old_sum is not None and new_sum is not None and old_sum != new_sum and not pkgver_changed:
            findings.append(Finding(
                "diff.checksum_without_pkgver", BLOCK,
                f"{sumname} changed but pkgver did not (possible recut release or checksum laundering)",
            ))

    old_keys = extract_array_block(old_text, 'validpgpkeys')
    new_keys = extract_array_block(new_text, 'validpgpkeys')
    if old_keys != new_keys and (old_keys is not None or new_keys is not None):
        findings.append(Finding("diff.validpgpkeys_changed", BLOCK, "validpgpkeys changed"))

    old_pkgver_fn = extract_function_body(old_text, 'pkgver')
    new_pkgver_fn = extract_function_body(new_text, 'pkgver')
    if old_pkgver_fn != new_pkgver_fn and (old_pkgver_fn is not None or new_pkgver_fn is not None):
        findings.append(Finding("diff.pkgver_function_changed", REVIEW, "pkgver() function body changed"))

    for line in added:
        if re.search(r'(/etc/|/usr/lib/systemd|\$HOME\b|/root\b)', line):
            findings.append(Finding(
                "diff.write_outside_pkgdir", REVIEW,
                f"New line references a path outside $pkgdir/$srcdir: {line.strip()[:120]}",
            ))
            break

    if re.search(r"arch=\([^)]*'any'[^)]*\)", new_text) and not re.search(r"arch=\([^)]*'any'[^)]*\)", old_text):
        findings.append(Finding(
            "diff.arch_any_appeared", INFO,
            "arch=('any') appeared where the package was architecture-specific before",
        ))

    return findings


def check_traur_differential(old_text, new_text, tmp_root):
    old_ok = run_traur(old_text, tmp_root)
    new_ok = run_traur(new_text, tmp_root)
    if old_ok is None or new_ok is None:
        return []
    if old_ok and not new_ok:
        return [Finding("traur.regressed", REVIEW, "traur rating got worse across this diff")]
    if not old_ok and not new_ok:
        return [Finding("traur.still_bad", INFO, "traur already flagged this package before the diff; unchanged")]
    return []


# --------------------------------------------------------------------------
# Per-package orchestration
# --------------------------------------------------------------------------

def audit_package(pkg, path, repo_root, trust_entry, aur_info, tmp_root):
    findings = []

    pkgbase = read_pkgbase(path)
    expected_url = f"https://aur.archlinux.org/{pkgbase}.git"
    actual_url = submodule_url(repo_root, pkg)
    if actual_url and actual_url != expected_url:
        findings.append(Finding(
            "submodule.url_mismatch", BLOCK,
            f".gitmodules points {pkg} at '{actual_url}', expected '{expected_url}' (pkgbase from .SRCINFO)",
        ))

    new_commit = head_commit(path)
    if new_commit is None:
        findings.append(Finding("submodule.not_initialized", REVIEW, "Submodule has no checked-out HEAD"))
        return Verdict(pkg, None, None, REVIEW, findings, changed=False)

    old_commit = (trust_entry or {}).get("last_accepted_commit")
    if trust_entry is None:
        findings.append(Finding(
            "package.no_baseline", REVIEW,
            "No trust.json baseline for this package; treating as first-seen",
        ))

    changed = old_commit != new_commit

    if changed:
        findings.extend(check_aur_provenance(aur_info or {}, trust_entry or {}))

    if old_commit and changed:
        # Without the connecting history there is nothing to compare, and
        # claiming "history rewritten" on a shallow clone or a failed fetch
        # would be a false accusation. Say so instead; review still keeps the
        # PR out of auto-merge.
        history_available = ensure_commit(path, old_commit)
        if not history_available:
            findings.append(Finding(
                "commit.history_unavailable", REVIEW,
                f"Could not fetch enough of {pkg}'s history to compare {old_commit[:10]} "
                f"against {new_commit[:10]}",
            ))
        elif is_ancestor(path, old_commit, new_commit):
            count = commit_count(path, old_commit, new_commit)
            if count is not None and count > 3:
                findings.append(Finding(
                    "commit.large_range", REVIEW,
                    f"{count} commits between accepted and new HEAD (expected ~1 for a version bump)",
                ))

            new_author = commit_author_email(path, new_commit)
            trusted_author = (trust_entry or {}).get("last_accepted_author_email")
            if trusted_author and new_author and new_author != trusted_author:
                findings.append(Finding(
                    "commit.author_changed", REVIEW,
                    f"Commit author changed: '{trusted_author}' -> '{new_author}'",
                ))

            old_pkgbuild = show_file(path, old_commit, 'PKGBUILD') or ''
            new_pkgbuild = show_file(path, new_commit, 'PKGBUILD') or ''
            changed_paths = diff_files(path, old_commit, new_commit)
            diff = diff_text(path, old_commit, new_commit, 'PKGBUILD')
            findings.extend(check_pkgbuild_diff(old_pkgbuild, new_pkgbuild, diff, changed_paths, trust_entry or {}))
            findings.extend(check_traur_differential(old_pkgbuild, new_pkgbuild, tmp_root))
        else:
            findings.append(Finding(
                "commit.force_push", BLOCK,
                f"Previous accepted commit {old_commit[:10]} is not an ancestor of {new_commit[:10]} "
                "(history rewritten)",
            ))

        remote_head = remote_head_sha(path)
        if remote_head is None:
            findings.append(Finding(
                "commit.remote_unreachable", REVIEW,
                "Could not verify the new commit against the AUR git remote HEAD "
                "(network error, or the remote history could not be fetched)",
            ))
        elif not is_ancestor(path, new_commit, remote_head):
            findings.append(Finding(
                "commit.not_from_aur", BLOCK,
                f"New commit {new_commit[:10]} is not reachable from aur.archlinux.org HEAD ({remote_head[:10]})",
            ))

    severities = {f.severity for f in findings}
    if BLOCK in severities:
        verdict = BLOCK
    elif REVIEW in severities:
        verdict = REVIEW
    else:
        verdict = "pass"

    return Verdict(pkg, old_commit, new_commit, verdict, findings, changed=changed)


# --------------------------------------------------------------------------
# trust.json
# --------------------------------------------------------------------------

def load_trust(trust_path):
    if not os.path.exists(trust_path):
        return {
            "version": 1,
            "defaults": {
                "allowed_source_hosts": sorted(DEFAULT_ALLOWED_HOSTS),
                "network_in_build": False,
            },
            "packages": {},
        }
    with open(trust_path, encoding='utf-8') as f:
        return json.load(f)


def save_trust(trust_path, trust):
    with open(trust_path, 'w', encoding='utf-8') as f:
        json.dump(trust, f, indent=2, sort_keys=True)
        f.write('\n')


def build_trust_entry(pkg, path, aur_info):
    pkgbase = read_pkgbase(path)
    commit = head_commit(path)
    pkgbuild_path = os.path.join(path, 'PKGBUILD')
    text = ''
    if os.path.exists(pkgbuild_path):
        with open(pkgbuild_path, encoding='utf-8', errors='replace') as f:
            text = f.read()

    install_files = sorted(glob.glob(os.path.join(path, '*.install')))
    install_sha256 = None
    if install_files:
        with open(install_files[0], 'rb') as f:
            install_sha256 = hashlib.sha256(f.read()).hexdigest()

    validpgpkeys_block = extract_array_block(text, 'validpgpkeys')
    validpgpkeys = re.findall(r"'([0-9A-Fa-f]+)'", validpgpkeys_block) if validpgpkeys_block else []

    info = get_pkg_info(pkgbuild_path)
    version = info.get('version', '') if info else ''

    return {
        "aur_pkgbase": pkgbase,
        "aur_maintainer": (aur_info or {}).get("Maintainer"),
        "aur_comaintainers": sorted((aur_info or {}).get("CoMaintainers") or []),
        "aur_votes": (aur_info or {}).get("NumVotes"),
        "aur_popularity": (aur_info or {}).get("Popularity"),
        "last_accepted_commit": commit,
        "last_accepted_author_email": commit_author_email(path, commit) if commit else None,
        "last_accepted_pkgver": version,
        "source_hosts": sorted(extract_source_hosts(text)),
        "has_install_script": bool(install_files) or bool(re.search(r'^install=', text, re.MULTILINE)),
        "install_sha256": install_sha256,
        "validpgpkeys": validpgpkeys,
        "network_in_build": False,
        "vcs_package": pkg.endswith('-git'),
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def write_summary(verdicts, step_summary_path, comment_path):
    lines = ["## Gate 1 audit\n", "| Package | Verdict | Findings |", "|---|---|---|"]
    icon = {"pass": "pass", BLOCK: "BLOCK", REVIEW: "review"}
    for v in verdicts:
        if not v.changed and not v.findings:
            continue
        detail = "; ".join(f"**{f.severity}** {f.id}: {f.detail}" for f in v.findings) or "no findings"
        lines.append(f"| {v.package} | {icon.get(v.verdict, v.verdict)} | {detail} |")
    if len(lines) == 3:
        lines.append("| *(no submodule changed since its last accepted commit)* | | |")
    text = "\n".join(lines) + "\n"
    print(text)
    # $GITHUB_STEP_SUMMARY is shared across every step in the job, so append.
    if step_summary_path:
        with open(step_summary_path, 'a', encoding='utf-8') as f:
            f.write(text)
    # The sticky PR comment is this run's own file, so overwrite it fresh.
    if comment_path:
        with open(comment_path, 'w', encoding='utf-8') as f:
            f.write(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-root', default='.')
    parser.add_argument('--trust-file', default='.github/security/trust.json')
    parser.add_argument('--output-dir', default='.github/security/verdicts')
    parser.add_argument(
        '--comment-file', default='.github/security/audit-summary.md',
        help="Where to write the summary table for posting as a sticky PR comment",
    )
    parser.add_argument('--bootstrap', action='store_true', help="Seed trust.json from the current repo state")
    parser.add_argument(
        '--advance-trust', action='store_true',
        help="Advance trust.json to the new commit for every package whose verdict is 'pass'",
    )
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    trust_path = os.path.join(repo_root, args.trust_file)
    out_dir = os.path.join(repo_root, args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    trust = load_trust(trust_path)
    all_paths = submodule_paths(repo_root)
    packages = {
        name: os.path.join(repo_root, rel)
        for name, rel in all_paths.items()
        if os.path.exists(os.path.join(repo_root, rel, 'PKGBUILD'))
    }

    pkgbases = {pkg: read_pkgbase(path) for pkg, path in packages.items()}

    if args.bootstrap:
        aur_info = aur_rpc.get_info(sorted(set(pkgbases.values())))
        for pkg, path in sorted(packages.items()):
            trust['packages'][pkg] = build_trust_entry(pkg, path, aur_info.get(pkgbases[pkg]))
        trust.setdefault('defaults', {}).setdefault('allowed_source_hosts', sorted(DEFAULT_ALLOWED_HOSTS))
        trust['defaults'].setdefault('network_in_build', False)
        save_trust(trust_path, trust)
        print(f"Bootstrapped trust.json with {len(packages)} package(s).")
        return 0

    changed_pkgs = [
        pkg for pkg, path in packages.items()
        if (trust['packages'].get(pkg) or {}).get('last_accepted_commit') != head_commit(path)
    ]
    aur_info = aur_rpc.get_info(sorted({pkgbases[pkg] for pkg in changed_pkgs}))

    verdicts = []
    with tempfile.TemporaryDirectory() as tmp_root:
        for pkg, path in sorted(packages.items()):
            entry = trust['packages'].get(pkg)
            verdicts.append(audit_package(pkg, path, repo_root, entry, aur_info.get(pkgbases[pkg]), tmp_root))

    for v in verdicts:
        with open(os.path.join(out_dir, f"{v.package}.json"), 'w', encoding='utf-8') as f:
            json.dump(v.to_dict(), f, indent=2)

    write_summary(verdicts, os.environ.get('GITHUB_STEP_SUMMARY'), os.path.join(repo_root, args.comment_file))

    if args.advance_trust:
        advanced = 0
        for v in verdicts:
            if v.changed and v.verdict == "pass":
                trust['packages'][v.package] = build_trust_entry(
                    v.package, packages[v.package], aur_info.get(pkgbases[v.package]),
                )
                advanced += 1
        if advanced:
            save_trust(trust_path, trust)
            print(f"Advanced trust.json for {advanced} package(s).")

    blocked = [v.package for v in verdicts if v.verdict == BLOCK]
    reviewed = [v.package for v in verdicts if v.verdict == REVIEW]
    if blocked:
        print(f"::error::Audit BLOCKED: {', '.join(blocked)}", file=sys.stderr)
    if reviewed:
        print(f"::warning::Audit needs human review: {', '.join(reviewed)}", file=sys.stderr)

    return 1 if blocked else 0


if __name__ == '__main__':
    sys.exit(main())
