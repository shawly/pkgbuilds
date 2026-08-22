"""
Phase 5: the auto-merge reaper for Dependabot's submodule-bump PRs.

Runs on `schedule`, not `pull_request`. That distinction is the whole point:
a workflow triggered by a `pull_request` event from Dependabot gets a
read-only GITHUB_TOKEN and no secrets no matter what `permissions:` it
declares, so it can label, approve, merge, or push nothing on the PR that
gate exists to act on. A `schedule` run executes in the base-branch context
with a normal token, which is the only place any of that write access
exists. That is also why this script -- not audit.yml or build-level.yml --
is the one applying the `security-hold`/`needs-human` labels: those jobs run
on `pull_request` and cannot write to a Dependabot PR even though the plan's
escape-hatch table describes the labels as something "the audit" sets.

Eligibility (every rule must hold, or the PR is left alone and explained):

1. Author is dependabot[bot] and the head ref starts with dependabot/.
2. The PR diff touches exactly one path, and it is a submodule listed in
   .gitmodules. Never .github/, config.json, key.gpg.enc, or .gitmodules
   itself.
3. PR age (from creation, not last push) is at least MERGE_DELAY_DAYS,
   unless the merge-now label is present (that label skips only this rule).
4. Every check on the head commit is success or neutral -- not pending, not
   failing, not skipped -- and checks matching each of "audit",
   "build-package", "inspect-artifact" are actually present. This catches a
   workflow that silently never triggered, which would otherwise look
   indistinguishable from "nothing to report".
5. The audit (Gate 1) and inspect (Gate 3) verdict artifacts for the head
   commit both say the literal string "pass" for this package. Rule 4 alone
   is not enough here: both gate scripts exit 0 on a `review` verdict (by
   design -- a review finding must not fail PR builds outright), so a check
   can be green while the verdict inside it is not "pass". This rule is what
   actually catches that case; it is stricter than the plan's table implies
   was needed, and deliberately so.
6. No security-hold, needs-human, or do-not-merge label.
7. mergeStateStatus is CLEAN.

On merge: approve (best-effort -- a repo that doesn't require review approval
will just no-op here) then squash-merge with branch deletion. After every PR
in a run has been handled, if anything merged, pull master, update the
submodule(s) that changed, and run audit_submodule.py --advance-trust once,
pushing the result as a follow-up commit. That follow-up push is a second
push to master (the merge itself was the first) and will cancel-and-restart
the build that the merge triggered, by build-level.yml's own concurrency
group -- harmless (trust.json carries no build input), just a documented
inefficiency, not a correctness bug.
"""

import argparse
import dataclasses
import datetime
import glob
import json
import os
import subprocess
import sys
import tempfile

import audit_submodule

REQUIRED_CHECK_SUBSTRINGS = ("audit", "build-package", "inspect-artifact")
BLOCKING_LABELS = frozenset({"security-hold", "needs-human", "do-not-merge"})
PROTECTED_PATH_PREFIXES = (".github/",)
PROTECTED_PATHS_EXACT = frozenset({"config.json", "key.gpg.enc", ".gitmodules"})


@dataclasses.dataclass
class Decision:
    eligible: bool
    reasons: list
    target_label: str | None
    package: str | None


# --------------------------------------------------------------------------
# Pure logic -- no subprocess calls below this point in the file except where
# explicitly noted. This is what the Docker test harness exercises directly.
# --------------------------------------------------------------------------

def touches_exactly_one_submodule(changed_paths, submodule_names):
    """Returns (package_name_or_None, error_message_or_None)."""
    if len(changed_paths) != 1:
        return None, f"PR touches {len(changed_paths)} path(s), expected exactly 1"
    path = changed_paths[0]
    if path in PROTECTED_PATHS_EXACT or any(path.startswith(p) for p in PROTECTED_PATH_PREFIXES):
        return None, f"changed path '{path}' is a protected path, never auto-merged"
    if path not in submodule_names:
        return None, f"changed path '{path}' is not a tracked submodule"
    return path, None


def days_since(iso_ts, now):
    dt = datetime.datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    return (now - dt).total_seconds() / 86400


def parse_checks(rollup):
    """(failing_names, missing_categories) from a gh statusCheckRollup list."""
    failing = []
    seen = {cat: False for cat in REQUIRED_CHECK_SUBSTRINGS}
    for item in rollup or []:
        name = f"{item.get('name', '')} {item.get('workflowName', '')}".lower()
        state = (item.get('conclusion') or item.get('state') or '').upper()
        for cat in seen:
            if cat in name:
                seen[cat] = True
        if state not in ('SUCCESS', 'NEUTRAL'):
            failing.append(item.get('name') or '?')
    missing = [cat for cat, found in seen.items() if not found]
    return failing, missing


def target_label_for(audit_verdict, inspect_verdict):
    if audit_verdict == audit_submodule.BLOCK or inspect_verdict == 'block':
        return 'security-hold'
    if audit_verdict == audit_submodule.REVIEW or inspect_verdict == 'review':
        return 'needs-human'
    return None


def evaluate(pr, changed_paths, submodule_names, now, merge_delay_days, audit_verdict, inspect_verdict):
    reasons = []
    author = (pr.get('author') or {}).get('login', '')
    head_ref = pr.get('headRefName', '') or ''
    if author != 'dependabot[bot]' or not head_ref.startswith('dependabot/'):
        reasons.append(f"not a Dependabot submodule-bump PR (author={author!r}, head={head_ref!r})")

    package, err = touches_exactly_one_submodule(changed_paths, submodule_names)
    if err:
        reasons.append(err)

    label_names = {l.get('name') for l in (pr.get('labels') or [])}
    target_label = target_label_for(audit_verdict, inspect_verdict)

    blocking = label_names & BLOCKING_LABELS
    if blocking:
        reasons.append(f"blocking label(s) present: {', '.join(sorted(blocking))}")

    age_days = days_since(pr['createdAt'], now)
    if age_days < merge_delay_days and 'merge-now' not in label_names:
        reasons.append(f"only {age_days:.1f}d old, needs {merge_delay_days}d (no merge-now label)")

    failing, missing = parse_checks(pr.get('statusCheckRollup'))
    if failing:
        reasons.append(f"non-passing check(s): {', '.join(failing)}")
    if missing:
        reasons.append(f"required check category missing (workflow may not have triggered): {', '.join(missing)}")

    if audit_verdict != 'pass':
        reasons.append(f"audit (Gate 1) verdict is {audit_verdict!r}, not 'pass'")
    if inspect_verdict != 'pass':
        reasons.append(f"inspect (Gate 3) verdict is {inspect_verdict!r}, not 'pass'")

    mergeable_state = (pr.get('mergeStateStatus') or '').upper()
    if mergeable_state != 'CLEAN':
        reasons.append(f"mergeStateStatus is {mergeable_state!r}, not 'CLEAN'")

    return Decision(eligible=not reasons, reasons=reasons, target_label=target_label, package=package)


# --------------------------------------------------------------------------
# I/O: gh CLI / git plumbing
# --------------------------------------------------------------------------

def run(args, **kwargs):
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    return subprocess.run(args, **kwargs)


def gh_json(args):
    result = run(['gh'] + args)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def repo_slug():
    env = os.environ.get('GITHUB_REPOSITORY')
    if env:
        return env
    return run(['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner']).stdout.strip()


def list_dependabot_prs():
    return gh_json([
        'pr', 'list', '--state', 'open', '--json',
        'number,author,headRefName,headRefOid,createdAt,labels,mergeStateStatus,statusCheckRollup',
    ])


def changed_paths_for(pr_number):
    result = run(['gh', 'api', f'repos/{repo_slug()}/pulls/{pr_number}/files',
                  '--paginate', '--jq', '.[].filename'])
    if result.returncode != 0:
        raise RuntimeError(f"could not list changed files for PR #{pr_number}: {result.stderr.strip()}")
    return [l for l in result.stdout.splitlines() if l]


def find_run_id(sha):
    runs = gh_json(['run', 'list', '--commit', sha, '--json', 'databaseId,name', '-L', '20'])
    for r in runs:
        if r.get('name') == 'Build Packages':
            return r['databaseId']
    return None


def list_artifact_names(run_id):
    result = run(['gh', 'api', f'repos/{repo_slug()}/actions/runs/{run_id}/artifacts',
                  '--paginate', '-f', 'per_page=100', '--jq', '.artifacts[].name'])
    if result.returncode != 0:
        return []
    return [l for l in result.stdout.splitlines() if l]


def download_artifact_json(run_id, artifact_name, json_filename=None):
    """Downloads a single-file artifact and returns its parsed JSON, or None
    if the artifact doesn't exist / can't be fetched (fails closed by the
    caller, since a missing verdict is treated as "not pass")."""
    with tempfile.TemporaryDirectory() as d:
        result = run(['gh', 'run', 'download', str(run_id), '-n', artifact_name, '-D', d])
        if result.returncode != 0:
            return None
        if json_filename:
            path = os.path.join(d, json_filename)
            files = [path] if os.path.exists(path) else []
        else:
            files = glob.glob(os.path.join(d, '*.json'))
        if not files:
            return None
        with open(files[0], encoding='utf-8') as f:
            return json.load(f)


def fetch_audit_verdict(run_id, package):
    doc = download_artifact_json(run_id, 'audit-verdicts', json_filename=f'{package}.json')
    return doc.get('verdict') if doc else None


def fetch_inspect_verdict(run_id, package):
    name = next(
        (n for n in list_artifact_names(run_id) if n.startswith('inspect-l') and n.endswith(f'-{package}')),
        None,
    )
    if name is None:
        return None
    doc = download_artifact_json(run_id, name)
    return doc.get('verdict') if doc else None


def sync_label(pr_number, target_label, dry_run):
    if not target_label:
        return
    print(f"PR #{pr_number}: ensuring label '{target_label}'")
    if dry_run:
        return
    run(['gh', 'pr', 'edit', str(pr_number), '--add-label', target_label])


def approve_and_merge(pr_number, dry_run):
    if dry_run:
        print(f"PR #{pr_number}: [dry-run] would approve + squash-merge")
        return
    run(['gh', 'pr', 'review', str(pr_number), '--approve'])
    result = run(['gh', 'pr', 'merge', str(pr_number), '--squash', '--delete-branch'])
    if result.returncode != 0:
        raise RuntimeError(f"merge failed for PR #{pr_number}: {result.stderr.strip()}")


def advance_trust_after_merge(packages, dry_run):
    print(f"Advancing trust.json baseline for: {', '.join(packages)}")
    if dry_run:
        return
    run(['git', 'pull', '--ff-only', 'origin', 'master'], capture_output=False)
    run(['git', 'submodule', 'update', '--init', '--recursive'], capture_output=False)
    subprocess.run(['python3', '.github/scripts/audit_submodule.py', '--advance-trust'])
    status = run(['git', 'status', '--porcelain', '.github/security/trust.json'])
    if not status.stdout.strip():
        print("trust.json unchanged after --advance-trust; nothing to push.")
        return
    run(['git', 'config', 'user.name', 'github-actions[bot]'], capture_output=False)
    run(['git', 'config', 'user.email', 'github-actions[bot]@users.noreply.github.com'], capture_output=False)
    run(['git', 'add', '.github/security/trust.json'], capture_output=False)
    run(['git', 'commit', '-m', f"chore(security): advance trust baseline for {', '.join(packages)}"],
        capture_output=False)
    run(['git', 'push', 'origin', 'HEAD:master'], capture_output=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                         help="Evaluate and print decisions but never label, approve, merge, or push")
    args = parser.parse_args()

    if os.environ.get('AUTOMERGE_ENABLED', 'true').strip().lower() == 'false':
        print("AUTOMERGE_ENABLED=false; skipping this run entirely.")
        return 0

    merge_delay_days = int(os.environ.get('MERGE_DELAY_DAYS', '3'))
    now = datetime.datetime.now(datetime.timezone.utc)
    submodule_names = set(audit_submodule.submodule_paths('.').keys())

    merged = []
    for pr in list_dependabot_prs():
        number = pr['number']
        try:
            changed = changed_paths_for(number)
        except RuntimeError as exc:
            print(f"PR #{number}: {exc}; skipping this run", file=sys.stderr)
            continue

        package, _ = touches_exactly_one_submodule(changed, submodule_names)
        audit_verdict = inspect_verdict = None
        if package is not None:
            run_id = find_run_id(pr['headRefOid'])
            if run_id is not None:
                audit_verdict = fetch_audit_verdict(run_id, package)
                inspect_verdict = fetch_inspect_verdict(run_id, package)

        decision = evaluate(pr, changed, submodule_names, now, merge_delay_days, audit_verdict, inspect_verdict)
        sync_label(number, decision.target_label, args.dry_run)

        if decision.eligible:
            print(f"PR #{number} ({decision.package}): ELIGIBLE, merging")
            try:
                approve_and_merge(number, args.dry_run)
                merged.append(decision.package)
            except RuntimeError as exc:
                print(f"::error::{exc}", file=sys.stderr)
        else:
            print(f"PR #{number}: not eligible -- {'; '.join(decision.reasons)}")

    if merged:
        advance_trust_after_merge(merged, args.dry_run)
    else:
        print("Nothing merged this run.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
