#!/usr/bin/env python3
"""Durable submission coordinator for independently executed incident workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

import manage
import review_apply

NAMES = {'mcp-exception-issue': 'exception-triager', 'github-issue-fix': 'issue-fixer'}


def resource_identity(name, deployed):
    home = manage.cao_home()
    profile = manage.profile_context(home, NAMES[name])
    config = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'cao_incident_readonly.config.toml'
    import tomllib
    data = tomllib.loads(config.read_text())
    if (data.get('sandbox_mode') != 'read-only' or data.get('approval_policy') != 'never'
            or data.get('shell_environment_policy', {}).get('inherit') != 'none'):
        raise ValueError('Configure cao_incident_readonly: read-only, approval never, environment inherit none')
    values = [manage.digest(p) for p in (deployed, profile, config)]
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return fingerprint, profile, config


def submit(state, path, deployed):
    rid = state['run_ids'][-1]
    prior = review_apply.workflow_result(rid, missing_ok=True)
    if prior is None:
        args = ['cao', 'workflow', 'run', str(deployed), '--detach', '--json', '--run-id', rid]
        for key, value in state['inputs'].items():
            args += ['--input', key + '=' + (str(value).lower() if isinstance(value, bool) else str(value))]
        result = review_apply.json_command(args)
        if result.get('run_id') != rid:
            raise ValueError('Submitted run ID mismatch')
    elif prior.get('state') in ('failed', 'cancelled'):
        raise ValueError('Terminal failed/cancelled run: inspect CAO and explicitly resolve its manual recovery before resume')
    elif prior.get('state') == 'completed':
        output = prior.get('output') or {}
        if output.get('status') not in ('reconciling', 'pushed_comment_pending'):
            state['result'] = prior
            manage.atomic_json(path, state)
            return prior
        if state['inputs'].get('recovery_only'):
            state['result'] = prior
            manage.atomic_json(path, state)
            return prior
        state['inputs']['recovery_only'] = True
        state['run_ids'].append(state['execution_id'] + '-recovery-' + str(len(state['run_ids'])))
        manage.atomic_json(path, state)
        return submit(state, path, deployed)
    review_apply.command(['cao', 'workflow', 'wait', rid, '--json'], check=False)
    result = review_apply.workflow_result(rid)
    output = result.get('output')
    if result.get('state') == 'completed' and (not isinstance(output, dict) or output.get('workflow') != state['workflow']
                                              or output.get('run_id') != rid or output.get('repository') != state['inputs']['repository']):
        raise ValueError('CAO output identity mismatch')
    state['result'] = result
    manage.atomic_json(path, state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('workflow', choices=NAMES)
    parser.add_argument('--repository')
    parser.add_argument('--monitor', dest='monitor_id')
    parser.add_argument('--issue', dest='issue_number', type=int)
    parser.add_argument('--policy', dest='policy_path')
    parser.add_argument('--from', dest='since')
    parser.add_argument('--until')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply-mode', choices=('push', 'patch'))
    parser.add_argument('--state-root')
    parser.add_argument('--resume')
    parser.add_argument('--retry-reason', help='Explicit corrective change/new-evidence explanation for a terminal attempt')
    parser.add_argument('--retry-incident', help='Monitor evidence ID to retry after operator correction')
    args = parser.parse_args(argv)
    manage.prerequisites(runtime=True)
    deployed = manage.deployed_workflow(args.workflow)
    fingerprint, profile, config = resource_identity(args.workflow, deployed)
    if args.resume:
        if any((args.repository, args.monitor_id, args.issue_number, args.policy_path, args.since, args.until,
                args.dry_run, args.apply_mode, args.state_root, args.retry_reason, args.retry_incident)):
            raise ValueError('Resume rejects input overrides')
        path = Path(args.resume)
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError('Journal symlink components forbidden')
        review_apply.private_root(str(path.parent))
        # Existing helper checks ownership/schema for PR chains, so validate this journal explicitly.
        info = path.lstat()
        import stat
        if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Invalid execution journal')
        state = json.loads(path.read_text())
        if (state.get('schema_version') != 1 or state.get('workflow') != args.workflow
                or state.get('inputs', {}).get('execution_state_path') != str(path)
                or path.stem != state.get('execution_id')):
            raise ValueError('Invalid journal identity')
        # Changed dependencies block new writes inside the workflow, but never erase a
        # recorded remote outcome. A verified current deployment can reconcile reads.
    else:
        if not args.repository or not args.policy_path:
            raise ValueError('--repository and --policy are required')
        inputs = {'repository': args.repository.lower(), 'policy_path': args.policy_path}
        if any(parent.is_symlink() for parent in [Path(args.policy_path), *Path(args.policy_path).parents]):
            raise ValueError('Policy symlink components forbidden')
        if args.retry_reason:
            if not 1 <= len(args.retry_reason.strip()) <= 1000:
                raise ValueError('Retry requires a bounded corrective-change explanation')
            inputs['operator_retry_reason'] = args.retry_reason.strip()
        data = review_apply.operator_policy(args.policy_path, inputs['repository'])
        if args.workflow == 'mcp-exception-issue':
            if not args.monitor_id or args.issue_number or args.apply_mode or bool(args.since) != bool(args.until):
                raise ValueError('Monitor requires --monitor and paired --from/--until; fix flags forbidden')
            inputs.update(monitor_id=args.monitor_id, publish_mode='dry-run' if args.dry_run else 'issue')
            if bool(args.retry_incident) != bool(args.retry_reason) or (args.retry_incident and args.dry_run):
                raise ValueError('Monitor retry requires --retry-incident and --retry-reason; dry-run cannot retry')
            if args.retry_incident:
                inputs['retry_incident_id'] = args.retry_incident
            if args.since:
                inputs.update(since=args.since, until=args.until)
        else:
            if not args.issue_number or args.issue_number <= 0 or any((args.monitor_id, args.since, args.until, args.dry_run, args.retry_incident)):
                raise ValueError('Fix requires a positive --issue; monitor flags forbidden')
            inputs.update(issue_number=args.issue_number, apply_mode=args.apply_mode or 'push')
        root = review_apply.private_root(args.state_root or data['state_root'])
        if any(parent.is_symlink() for parent in Path(args.state_root or data['state_root']).parents):
            raise ValueError('State symlink components forbidden')
        directory = review_apply.private_root(str(root / 'executions'))
        execution = secrets.token_hex(16)
        path = directory / (execution + '.json')
        inputs.update(execution_state_path=str(path), expected_policy_digest=manage.digest(Path(args.policy_path)),
                      expected_resource_digest=fingerprint, recovery_only=False)
        state = {'schema_version': 1, 'workflow': args.workflow, 'execution_id': execution,
                 'inputs': inputs, 'run_ids': [execution + '-run'], 'profile_path': str(profile),
                 'codex_config_path': str(config), 'reconciliation_policy': dict(data, repository=inputs['repository'])}
        manage.atomic_json(path, state)
    print('Execution journal: ' + str(path), flush=True)
    result = submit(state, path, deployed)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('state') == 'completed' and (result.get('output') or {}).get('status') not in ('failed', 'blocked') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
