#!/usr/bin/env python3
"""External coordinator: two CAO runs, one pinned PR, durable resume/cancel state."""
from __future__ import annotations

import argparse
import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import manage


def command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and proc.returncode:
        raise RuntimeError(f"{args[0]} failed; inspect retained run IDs")
    return proc


def json_command(args: list[str]) -> dict:
    value = json.loads(command(args).stdout)
    if not isinstance(value, dict):
        raise ValueError("Command returned an invalid object")
    return value


def api(repository: str, path: str) -> dict:
    return json_command(['gh', 'api', f'repos/{repository}/{path}'])


def pages(repository: str, path: str) -> list[dict]:
    result = []
    for page in range(1, 4):
        value = json.loads(command(['gh', 'api', f'repos/{repository}/{path}?per_page=100&page={page}']).stdout)
        if not isinstance(value, list):
            raise ValueError("Invalid GitHub list")
        result.extend(value)
        if len(value) < 100:
            return result
    raise ValueError("GitHub result cap reached")


def private_root(path: str) -> Path:
    requested = Path(path)
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError('State directory must be absolute and not a symlink')
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = requested.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('State directory must have owner-only mode 0700')
    return requested.resolve()


def load_state(path: Path) -> dict:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Resume state must be owned by this user with mode 0600')
    state = json.loads(path.read_text())
    if state.get('schema_version') != 1 or not re.fullmatch(r'[0-9a-f]{32}', state.get('chain_id', '')):
        raise ValueError('Invalid resume state')
    for key, suffix in (('review_run_id', '-review'), ('apply_run_id', '-apply')):
        if state.get(key) and state[key] != state['chain_id'] + suffix:
            raise ValueError('Resume state references a foreign CAO run ID')
    return state


def check_snapshot(pr: dict, request: dict) -> None:
    if (pr.get('number') != request['pr_number'] or pr.get('state') != 'open' or pr.get('draft')
            or pr.get('base', {}).get('repo', {}).get('full_name', '').lower() != request['repository'].lower()
            or pr.get('head', {}).get('sha') != request['head_sha']
            or pr.get('head', {}).get('ref') != request['head_ref']
            or pr.get('head', {}).get('repo', {}).get('full_name') != request['head_repository']
            or pr.get('base', {}).get('sha') != request['base_sha']):
        raise ValueError('PR identity, state, base, or HEAD changed; start a new review')


def eligible_review(repository: str, number: int, sha: str, version: str, authors: list[str], rid: int | None, base_sha: str) -> dict:
    payload = {'repository': repository.lower(), 'pr': number, 'head_sha': sha,
               'workflow': 'github-pr-review', 'version': version}
    token = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode()
    marker = f'<!-- cao-review {token} -->'
    matches = [r for r in pages(repository, f'pulls/{number}/reviews')
               if marker in (r.get('body') or '') and r.get('user', {}).get('login') in authors
               and f'<!-- cao-review-base {base_sha} -->' in (r.get('body') or '')
               and r.get('commit_id') == sha and r.get('state') == 'COMMENTED']
    if len(matches) != 1 or (rid is not None and matches[0].get('id') != rid):
        raise ValueError('Expected exactly one eligible owned review for the pinned HEAD')
    review = matches[0]
    if type(review.get('id')) is not int or review['id'] <= 0:
        raise ValueError('Review ID must be a positive integer')
    return review


def workflow_result(run_id: str, *, missing_ok: bool = False) -> dict | None:
    response = command(['cao', 'workflow', 'result', run_id, '--json'], check=False)
    if response.returncode:
        # Transport failures never authorize a second submission.
        if missing_ok and 'unknown run' in response.stderr.lower():
            return None
        raise RuntimeError(f'Cannot resolve CAO run {run_id}; resume this state after recovery')
    value = json.loads(response.stdout)
    if value.get('run_id') != run_id:
        raise ValueError('CAO result run ID mismatch')
    return value


def execute_stage(name: str, inputs: dict, state: dict, journal: Path, deployed: Path) -> dict:
    key = 'review_run_id' if name == 'github-pr-review' else 'apply_run_id'
    if not state.get(key):
        state[key] = state['chain_id'] + ('-review' if key == 'review_run_id' else '-apply')
        state['active_run_id'] = state[key]
        manage.atomic_json(journal, state)
        args = ['cao', 'workflow', 'run', str(deployed), '--detach', '--json', '--run-id', state[key]]
        for field, value in inputs.items():
            args += ['--input', f'{field}={value}']
        submitted = json_command(args)
        if submitted.get('run_id') != state[key]:
            raise ValueError('Submitted run ID differs from durable state')
    else:
        # A failed submission may never have reached the server. Query first and
        # resubmit the SAME explicit ID only on the CLI's confirmed unknown-run error.
        prior = workflow_result(state[key], missing_ok=True)
        if prior is None:
            args = ['cao', 'workflow', 'run', str(deployed), '--detach', '--json', '--run-id', state[key]]
            for field, value in inputs.items():
                args += ['--input', f'{field}={value}']
            if json_command(args).get('run_id') != state[key]:
                raise ValueError('Resubmission run ID mismatch')
    state['active_run_id'] = state[key]
    manage.atomic_json(journal, state)
    command(['cao', 'workflow', 'wait', state[key], '--json'], check=False)
    result = workflow_result(state[key])
    if result.get('state') != 'completed' or not isinstance(result.get('output'), dict):
        raise RuntimeError(f'{name} did not complete; inspect {state[key]}')
    output = result['output']
    if (output.get('workflow') != name or output.get('repository', '').lower() != inputs['repository'].lower()
            or output.get('run_id') != state[key]):
        raise ValueError('Workflow output identity mismatch')
    state['active_run_id'] = None
    manage.atomic_json(journal, state)
    return output


def cancel_active(state: dict, journal: Path) -> None:
    rid = state.get('active_run_id')
    state['result'] = 'cancelled'
    if rid:
        command(['cao', 'workflow', 'cancel', rid], check=False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                result = workflow_result(rid)
                state['child_state'] = result.get('state')
                if state['child_state'] in ('completed', 'failed', 'cancelled'):
                    break
            except Exception:
                state['child_state'] = 'unknown'
                break
            time.sleep(1)
        if rid == state.get('apply_run_id'):
            # Covers a worker killed before its finally block could remove the container.
            workers = command(['docker', 'ps', '-aq', '--filter', 'label=cao-workflow=github-pr-apply',
                               '--filter', f'label=cao.apply.run_id={rid}'], check=False)
            if workers.returncode == 0:
                for cid in workers.stdout.split():
                    if re.fullmatch(r'[0-9a-f]{12,64}', cid):
                        command(['docker', 'rm', '-f', cid], check=False)
    manage.atomic_json(journal, state)


def operator_policy(policy_path: str, repository: str) -> dict:
    path = Path(policy_path)
    info = path.lstat()
    if not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Operator policy must be an absolute owner-only 0600 file')
    data = json.loads(path.read_text())
    policy = data.get('repositories', {}).get(repository.lower())
    if data.get('schema_version') != 1 or not isinstance(policy, dict) or not policy.get('review_authors'):
        raise ValueError('Repository review authors must be configured before review')
    return policy


def preflight(request: dict) -> tuple[dict, dict[str, Path]]:
    if os.getuid() == 0:
        raise ValueError('Run the chain as a non-root service user')
    manage.prerequisites(runtime=True)
    if not manage.codex_review_profile_ok() or not manage.codex_apply_profile_ok():
        raise ValueError('Configure both named read-only Codex profiles before running the chain')
    deployed = {name: manage.deployed_workflow(name) for name in ('github-pr-review', 'github-pr-apply')}
    path = Path(request['policy_path'])
    policy = operator_policy(request['policy_path'], request['repository'])
    if any(path.resolve().is_relative_to(Path(request[key]).resolve()) for key in ('review_workspace', 'apply_workspace')):
        raise ValueError('Keep operator policy outside both model workspaces')
    for key in ('review_authors', 'editable_paths'):
        if not isinstance(policy.get(key), list) or not policy[key] or any(not isinstance(x, str) or not x for x in policy[key]):
            raise ValueError('Configure valid review authors and editable paths before review')
    commands = policy.get('test_commands')
    if not isinstance(commands, list) or not commands or len(commands) > 10 or any(not isinstance(c, list) or not c or any(not isinstance(a, str) or not a or '\x00' in a for a in c) for c in commands):
        raise ValueError('Configure editable paths and isolated test commands before review')
    for key in ('context_paths', 'new_files', 'push_branches'):
        if not isinstance(policy.get(key, []), list) or any(not isinstance(x, str) or not x for x in policy.get(key, [])):
            raise ValueError('Invalid policy file/branch list')
    if request['apply_mode'] == 'push':
        if (request['head_repository'].lower() != request['repository'].lower()
                or not any(fnmatch.fnmatchcase(request['head_ref'], p) for p in policy.get('push_branches', []))
                or not policy.get('git_author_name') or not policy.get('git_author_email')):
            raise ValueError('Push requires a same-repository allowlisted branch and configured author')
    image = policy.get('test_image', '')
    if not isinstance(image, str) or not re.fullmatch(r'(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}', image):
        raise ValueError('Configure a digest-pinned test image')
    # Avoid spending a review run before discovering unavailable test isolation.
    command(['docker', 'image', 'inspect', image])
    fingerprint = manage.digest(path)
    versions = {w['name']: w['version'] for w in manage.load_manifest()['workflows']}
    if request.get('policy_digest') and request['policy_digest'] != fingerprint:
        raise ValueError('Policy changed since this chain started')
    if request.get('versions') and request['versions'] != versions:
        raise ValueError('Workflow versions changed; start a new chain')
    request.update(policy_digest=fingerprint, versions=versions)
    return policy, deployed


def orchestrate(state: dict, journal: Path, policy: dict, deployed: dict) -> dict:
    request = state['request']
    repo, number, sha = request['repository'], request['pr_number'], request['head_sha']
    # On resume of an already submitted apply, let its result/reconciliation own
    # the changed HEAD. Before the first submission require the original snapshot.
    if not state.get('apply_run_id'):
        check_snapshot(api(repo, f'pulls/{number}'), request)
    review_inputs = {'repository': repo, 'pr_number': number, 'expected_head_sha': sha,
                     'expected_base_sha': request['base_sha'],
                     'publish_mode': 'review', 'workspace_root': request['review_workspace']}
    if request.get('model'):
        review_inputs['model'] = request['model']
    reviewed = execute_stage('github-pr-review', review_inputs, state, journal, deployed['github-pr-review'])
    rows = reviewed.get('results')
    if (reviewed.get('version') != request['versions']['github-pr-review'] or not isinstance(rows, list)
            or len(rows) != 1 or rows[0].get('pr') != number or rows[0].get('head_sha') != sha
            or rows[0].get('base_sha') != request['base_sha']
            or rows[0].get('result') not in ('completed', 'skipped')):
        raise ValueError('Review output does not identify exactly the pinned PR/HEAD')
    row = rows[0]
    if row.get('publish_result') == 'dry-run':
        raise ValueError('Dry-run review cannot start apply')
    review = eligible_review(repo, number, sha, reviewed['version'], policy['review_authors'], row.get('review_id'), request['base_sha'])
    rid = review['id']
    count = len(pages(repo, f'pulls/{number}/reviews/{rid}/comments'))
    if row.get('result') == 'completed' and row.get('findings') != count:
        raise ValueError('Published comment count differs from the review findings')
    if count == 0:
        state.update(result='skipped', reason='no findings', review_id=rid)
        manage.atomic_json(journal, state)
        return state
    if not state.get('apply_run_id'):
        check_snapshot(api(repo, f'pulls/{number}'), request)
    apply_inputs = {'repository': repo, 'pr_number': number, 'head_sha': sha, 'review_id': rid,
                    'base_sha': request['base_sha'],
                    'expected_findings': count, 'policy_path': request['policy_path'],
                    'apply_mode': request['apply_mode'], 'workspace_root': request['apply_workspace']}
    if request.get('model'):
        apply_inputs['model'] = request['model']
    applied = execute_stage('github-pr-apply', apply_inputs, state, journal, deployed['github-pr-apply'])
    if (applied.get('version') != request['versions']['github-pr-apply'] or applied.get('pr') != number
            or applied.get('head_sha') != sha or applied.get('review_id') != rid
            or applied.get('apply_mode') != request['apply_mode']
            or applied.get('result') not in ('applied', 'partial', 'skipped')):
        raise ValueError('Apply output identity/status mismatch')
    state.update(result=applied['result'], review_id=rid, apply_result=applied)
    manage.atomic_json(journal, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository')
    parser.add_argument('--pr', type=int)
    parser.add_argument('--policy')
    parser.add_argument('--apply-mode', choices=('patch', 'push'), default='patch')
    parser.add_argument('--model')
    parser.add_argument('--state-root', default='/tmp/cao-pr-review-apply')
    parser.add_argument('--review-workspace', default='/tmp/cao-pr-review')
    parser.add_argument('--apply-workspace', default='/tmp/cao-pr-apply')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    root = private_root(args.state_root)
    if args.resume:
        if any((args.repository, args.pr, args.policy, args.model)) or args.apply_mode != 'patch':
            raise ValueError('Resume uses the retained request; do not override its inputs')
        journal, state = args.resume.resolve(), load_state(args.resume)
        if journal.parent != root:
            raise ValueError('Resume state must be in the selected state root')
    else:
        if not args.repository or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository) or '..' in args.repository or not args.pr or args.pr <= 0 or not args.policy:
            raise ValueError('Provide --repository owner/repo --pr N --policy /absolute/file')
        operator_policy(args.policy, args.repository)
        pr = api(args.repository, f'pulls/{args.pr}')
        request = {'repository': args.repository, 'pr_number': args.pr, 'policy_path': args.policy,
                   'apply_mode': args.apply_mode, 'model': args.model, 'head_sha': pr['head']['sha'],
                   'head_ref': pr['head']['ref'], 'head_repository': pr['head']['repo']['full_name'],
                   'base_sha': pr['base']['sha'], 'review_workspace': args.review_workspace,
                   'apply_workspace': args.apply_workspace}
        check_snapshot(pr, request)
        chain_id = secrets.token_hex(16)
        state = {'schema_version': 1, 'chain_id': chain_id, 'request': request, 'result': 'running'}
        journal = root / f'{chain_id}.json'
    policy, deployed = preflight(state['request'])
    lock_key = hashlib.sha256(f"{state['request']['repository'].lower()}#{state['request']['pr_number']}".encode()).hexdigest()
    fd = os.open(root / f'{lock_key}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Invalid chain lock ownership')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manage.atomic_json(journal, state)
        print(json.dumps({'state_path': str(journal), 'chain_id': state['chain_id']}), flush=True)
        def interrupt(signum, frame):
            raise InterruptedError('Chain interrupted')
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        try:
            result = orchestrate(state, journal, policy, deployed)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['result'] in ('applied', 'skipped') else 1
        except InterruptedError:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            cancel_active(state, journal)
            raise
        except Exception:
            state.update(result='failed')
            manage.atomic_json(journal, state)
            raise


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Chain failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
