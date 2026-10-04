#!/usr/bin/env python3
"""Read private workflow evidence without rerunning models or publication."""
import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location('evidence', Path(__file__).resolve().parents[1] / 'workflows/_shared/diagnostics.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def summary(output):
    keys = ('workflow', 'version', 'run_id', 'repository', 'result', 'status', 'failure_stage', 'stage',
            'reason_code', 'reason', 'error_type', 'pr', 'issue_number', 'execution_key', 'head_sha', 'base_sha',
            'deployed_sha', 'review_id', 'review_url', 'branch', 'commit_sha', 'comment_id', 'patch_path',
            'artifact_path', 'artifact_directory', 'evidence_directory', 'checks', 'tests', 'regression')
    keys += ('generation', 'execution_id', 'workflow_sha256', 'expected_policy_digest', 'expected_resource_digest')
    result = {k: output[k] for k in keys if k in output}
    if 'incidents' in output:
        counts = {}
        for row in output['incidents']:
            status = row.get('status', 'unknown')
            counts[status] = counts.get(status, 0) + 1
        result['incident_counts'] = counts
        result['incidents'] = output['incidents']
    if 'results' in output:
        result['results'] = output['results']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run')
    group.add_argument('--journal', type=Path)
    parser.add_argument('--root', type=Path, default=Path('/tmp/cao-workflow-results'))
    parser.add_argument('--verify-cao', action='store_true')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args(argv)
    if args.journal:
        saved = json.loads(evidence.diag_private(args.journal).read_text())
        outputs = list(saved.get('child_outputs', {}).values())
        if not outputs:
            raise ValueError('Journal has no retained workflow outputs; inspect CAO state and preserve evidence')
    else:
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', args.run) or args.run in ('.', '..'):
            raise ValueError('Invalid run ID')
        path = args.root / args.run / 'result.json'
        saved = json.loads(evidence.diag_private(path).read_text())
        if saved.get('schema_version') != 1 or saved.get('output', {}).get('run_id') != args.run:
            raise ValueError('Result identity mismatch')
        outputs = [saved['output']]
    rows = []
    for output in outputs:
        row = dict(output) if args.full else summary(output)
        if args.verify_cao:
            try:
                proc = subprocess.run(['cao', 'workflow', 'result', output['run_id'], '--json'],
                                      text=True, capture_output=True, timeout=30, check=False)
            except subprocess.TimeoutExpired:
                proc = subprocess.CompletedProcess([], 1)
                row['cao_error'] = 'timeout'
            if proc.returncode:
                row.update(cao_state='unresolved', publication_certainty='requires_reconciliation')
            else:
                value = json.loads(proc.stdout)
                if value.get('run_id') != output['run_id']:
                    raise ValueError('CAO result identity mismatch')
                row['cao_state'] = value['state']
            outcome = output.get('status', output.get('result'))
            row['execution_verified'] = (row['cao_state'] == 'completed'
                and outcome in ('completed', 'skipped', 'applied', 'processed', 'reconciled', 'dry-run', 'pushed', 'patch_ready', 'no_change')
                and not any(r.get('status') in ('blocked', 'failed', 'deferred', 'reconciling') for r in output.get('incidents', [])))
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit('Evidence unavailable: ' + str(exc))
