import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
shim = types.ModuleType('cao_workflow')
shim.step = lambda *a, **kw: None
shim.get_inputs = lambda: {}
shim.emit_output = lambda value: value
sys.modules.setdefault('cao_workflow', shim)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'workflows' / name / 'workflow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor, fix = load('mcp-exception-issue'), load('github-issue-fix')


def request():
    return {'contract_version': '1.0', 'service': 'api', 'environment': 'prod',
            'start': '2026-10-01T00:00:00Z', 'end': '2026-10-01T00:10:00Z',
            'source_ids': ['app'], 'limit': 500, 'filters': {}}


def record():
    return {'record_id': '1', 'source_id': 'app', 'service': 'api', 'environment': 'prod',
            'message': 'Exception', 'occurred_at': '2026-10-01T00:05:00Z', 'evidence_id': 'e1'}


def response():
    return {'contract_version': '1.0', 'query_id': 'query1', 'observed_at': '2026-10-01T00:12:00Z',
            'records': [record()], 'next_cursor': None,
            'coverage': {'start': request()['start'], 'end': request()['end'], 'complete': True, 'reason': None}}


def issue():
    return {'number': 1, 'state': 'open', 'title': 'Wrong sum', 'body': 'Return 2', 'user': {'login': 'human'}}


def proposal():
    return {'decision': 'fix', 'reason': 'Correct sum', 'regression_scenarios': ['sum is 2'],
            'edits': [{'path': 'src/app.py', 'old': 'value = 1', 'new': 'value = 2'},
                      {'path': 'tests/test_app.py', 'old': 'assert value == 1', 'new': 'assert value == 2'}]}


def init_source(path):
    for name, body in {'src/app.py': 'value = 1\n', 'tests/test_app.py': 'assert value == 1\n'}.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    for argv in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base']):
        subprocess.run(['git', '-C', str(path), *argv], check=True, capture_output=True)
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()


class IncidentContractTests(unittest.TestCase):
    def test_fingerprint_preserves_failure_location_but_removes_request_identity(self):
        first = dict(record(), stack_trace='File "src/app.py", line 10\nException request request-one', request_id='request-one')
        same = dict(first, request_id='request-two', stack_trace=first['stack_trace'].replace('request-one', 'request-two'))
        different = dict(first, stack_trace=first['stack_trace'].replace('line 10', 'line 20'))
        identity = monitor.fingerprint('owner/repo', first, 'a' * 40, {})
        self.assertEqual(identity, monitor.fingerprint('owner/repo', same, 'a' * 40, {}))
        self.assertNotEqual(identity, monitor.fingerprint('owner/repo', different, 'a' * 40, {}))

    def test_journal_rejects_manual_issue_scope_or_retry_override(self):
        frozen = {'repository': 'owner/repo', 'issue_number': 1}
        monitor.verify_journal_inputs(frozen, {'inputs': frozen})
        for extra in ({'issue_number': 2}, {'operator_retry_reason': 'not recorded'}):
            with self.assertRaises(monitor.Blocked):
                monitor.verify_journal_inputs(dict(frozen, **extra), {'inputs': frozen})

    def test_installed_mcp_sdk_client_fixtures(self):
        cao = shutil.which('cao')
        if not cao:
            self.skipTest('CAO environment unavailable')
        executable = Path(cao).resolve().parent / 'python'
        result = subprocess.run([str(executable), '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_mcp_sdk.py', '-v'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('skipped', result.stderr)
    def test_timezone_and_half_open_scope(self):
        monitor.validate_page(response(), request())
        for changes in ({'occurred_at': request()['end']}, {'source_id': 'other'}, {'environment': 'dev'}, {'occurred_at': '2026-10-01T00:05:00'}):
            bad = response()
            bad['records'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises((monitor.Blocked, ValueError)):
                monitor.validate_page(bad, request())

    def test_incomplete_and_missing_coverage_are_rejected(self):
        for change in ({'complete': False, 'reason': 'retention gap'}, {'end': '2026-10-01T00:09:00Z'}, {'reason': 'truncated'}):
            bad = response()
            bad['coverage'].update(change)
            with self.assertRaises(monitor.Blocked):
                monitor.validate_page(bad, request())

    def test_filters_and_contract_version_fail_closed(self):
        req = dict(request(), filters={'trace_id': 'trace'})
        with self.assertRaises(monitor.Blocked):
            monitor.validate_page(response(), req)
        bad = response()
        bad['contract_version'] = '2.0'
        with self.assertRaises(monitor.Blocked):
            monitor.validate_page(bad, request())

    def test_mcp_transport_is_only_evidence_source_and_redacts(self):
        policy = {'mcp_connections': {'provider': {'id': 'p'}}, 'redact_patterns': [r'user@example.com']}
        scope = {'connection': 'provider', 'service': 'api', 'environment': 'prod', 'source_ids': ['app']}
        async def fake(*args):
            row = record()
            row['message'] = 'ghp_' + 'x' * 30 + ' user@example.com'
            return [row]
        with patch.object(monitor, 'collect_async', fake):
            rows = monitor.collect(scope, policy, 'context', monitor.instant(request()['start']), monitor.instant(request()['end']))
        self.assertEqual('[REDACTED] [REDACTED]', rows[0]['message'])
        with self.assertRaises(monitor.Blocked):
            monitor.collect(scope, policy, 'context', monitor.instant(request()['start']), monitor.instant(request()['end']), {'query': 'raw DSL'})

    def test_revision_requires_authoritative_unambiguous_identity(self):
        with patch.object(monitor, 'resolve_sha', return_value='a' * 40):
            self.assertEqual('a' * 40, monitor.revision(dict(record(), deployment_sha='a' * 40), {}, {'repository': 'owner/repo'}))
            with self.assertRaises(monitor.Blocked):
                monitor.revision(record(), {}, {'repository': 'owner/repo'})

    def test_triage_cannot_invent_evidence_or_source(self):
        result = {'decision': 'code_change_required', 'summary': 'Fix', 'cause': 'cause', 'fix_direction': 'change',
                  'verification': ['test'], 'evidence_ids': ['e1'], 'source_locations': [{'path': 'a.py', 'line': 1}]}
        self.assertEqual('code_change_required', monitor.triage_gate(result, [record()], {'a.py': 'value=1'}))
        result['evidence_ids'] = ['invented']
        with self.assertRaises(monitor.Blocked):
            monitor.triage_gate(result, [record()], {'a.py': 'value=1'})

    def test_carrier_is_private_shell_inert_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            def fake_step(*args, **kwargs):
                self.assertRegex(args[2], r'^: CAO_INCIDENT_INPUT_[0-9a-f]{32}$')
                self.assertEqual('manual', kwargs['recovery'])
                files = list((work / 'inputs').glob('*.json'))
                self.assertEqual(1, len(files))
                self.assertEqual(0o600, files[0].stat().st_mode & 0o777)
                return types.SimpleNamespace(output='{"decision":"non_code"}')
            with patch.object(monitor, 'step', side_effect=fake_step):
                monitor.model(work, 'exception-triager', {'message': '$(never run)'}, 'triage')
            self.assertEqual([], list((work / 'inputs').iterdir()))

    def test_private_paths_and_lock_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'policy.json'
            file.write_text('{}')
            with self.assertRaises(monitor.Blocked):
                monitor.private(file)
            file.chmod(0o600)
            self.assertEqual(file, monitor.private(file))
            with monitor.locked(root / 'locks', ['repo', 1]):
                with self.assertRaises(monitor.Blocked):
                    with monitor.locked(root / 'locks', ['repo', 1]):
                        pass


class PublicationTests(unittest.TestCase):
    def test_issue_body_has_bounded_mcp_provenance_and_pinned_source(self):
        row = record()
        row['provenance'] = {'server_id': 'provider', 'tool': 'logs_search_context', 'query': request(), 'query_id': 'query1'}
        decision = {'summary': 'Fix', 'cause': 'Observed cause', 'fix_direction': 'Correct source', 'verification': ['Regression'],
                    'evidence_ids': ['e1'], 'source_locations': [{'path': 'src/app.py', 'line': 1}]}
        payload = monitor.issue_payload(row, decision, 'a' * 40, 'b' * 64, 'owner/repo', 'prod', [row])
        self.assertIn('https://github.com/owner/repo/blob/' + 'a' * 40 + '/src/app.py#L1', payload['body'])
        self.assertIn('logs_search_context', payload['body'])
        self.assertIn(request()['start'], payload['body'])

    def test_monitor_queue_checkpoint_and_dry_run_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = {'service': 'api', 'environment': 'prod', 'source_paths': ['src/app.py']}
            policy = {'repository': 'owner/repo', 'state_root': directory, 'workspace_root': str(root / 'workspace'),
                      'monitors': {'prod': scope}}
            inputs = {'repository': 'owner/repo', 'monitor_id': 'prod', 'publish_mode': 'dry-run'}
            with patch.object(monitor, 'admission', return_value=policy), patch.object(monitor, 'collect', return_value=[record()]), patch.object(monitor, 'analyze', return_value=('non_code', {'summary': 'Expected exception'}, 'a' * 40, [record()])):
                self.assertEqual('dry-run', monitor.process(inputs)['status'])
                self.assertFalse((root / 'monitor.sqlite3').exists())
                inputs['publish_mode'] = 'issue'
                self.assertEqual('non_code', monitor.process(inputs)['incidents'][0]['status'])
                db = monitor.store(root)
                checkpoint = db.execute('SELECT cutoff FROM checkpoints').fetchone()[0]
                self.assertEqual('non_code', db.execute('SELECT status FROM incidents').fetchone()[0])
                self.assertEqual([], monitor.process(inputs)['incidents'])
                with patch.object(monitor, 'collect', side_effect=monitor.Blocked('Incomplete coverage')):
                    before = db.execute('SELECT cutoff FROM checkpoints').fetchone()[0]
                    with self.assertRaises(monitor.Blocked):
                        monitor.process(inputs)
                    self.assertEqual(before, db.execute('SELECT cutoff FROM checkpoints').fetchone()[0])
                self.assertTrue(checkpoint)
                db.close()

    def test_terminal_incident_is_retained_and_only_explicit_retry_processes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = {'repository': 'owner/repo', 'state_root': directory, 'workspace_root': str(root / 'workspace'),
                      'monitors': {'prod': {'service': 'api', 'environment': 'prod'}}}
            inputs = {'repository': 'owner/repo', 'monitor_id': 'prod'}
            with patch.object(monitor, 'admission', return_value=policy), patch.object(monitor, 'collect', return_value=[record()]), patch.object(monitor, 'analyze', side_effect=monitor.Blocked('Missing revision')) as analyze:
                self.assertEqual('blocked', monitor.process(inputs)['incidents'][0]['status'])
                self.assertEqual([], monitor.process(inputs)['incidents'])
                self.assertEqual(1, analyze.call_count)
                inputs.update(retry_incident_id='e1', operator_retry_reason='Corrected deployment mapping')
                result = monitor.process(inputs)['incidents'][0]
                self.assertEqual('blocked', result['status'])
                db = monitor.store(root)
                saved = json.loads(db.execute('SELECT result FROM incidents').fetchone()[0])
                self.assertEqual('Corrected deployment mapping', saved['retry_history'][0]['reason'])
                self.assertEqual(2, analyze.call_count)
                db.close()

    def test_shared_fingerprint_reuses_issue_across_monitors_and_creates_closed_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = {'repository': 'owner/repo', 'state_root': directory}
            db = monitor.store(Path(directory))
            remote = []
            payload = {'title': 'Fix', 'body': 'Evidence', 'occurred_at': '2026-10-01T00:05:00Z'}
            def api(repo, path, policy, method='GET', payload=None):
                if method == 'POST':
                    row = dict(payload, number=len(remote)+1, html_url='https://github.com/owner/repo/issues/' + str(len(remote)+1), state='open', user={'login': 'bot'})
                    remote.append(row)
                    return row
                return remote[int(path.split('/')[-1])-1]
            with patch.object(monitor, 'api', side_effect=api), patch.object(monitor, 'pages', side_effect=lambda *a: remote), patch.object(monitor, 'publisher_identity', return_value='bot'):
                first = monitor.publication(db, policy, 'fp', payload)
                self.assertEqual('issue_created', first['status'])
                second = monitor.publication(db, policy, 'fp', payload)
                self.assertEqual(first['issue_number'], second['issue_number'])
                remote[0].update(state='closed', closed_at='2026-10-01T01:00:00Z')
                self.assertEqual(1, monitor.publication(db, policy, 'fp', payload)['issue_number'])
                later = dict(payload, occurred_at='2026-10-01T02:00:00Z')
                self.assertEqual(2, monitor.publication(db, policy, 'fp', later)['issue_number'])
            db.close()

    def test_lost_issue_post_response_never_reposts(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = {'repository': 'owner/repo', 'state_root': directory}
            db = monitor.store(Path(directory))
            payload = {'title': 'Fix', 'body': 'Evidence', 'occurred_at': '2026-10-01T00:05:00Z'}
            with patch.object(monitor, 'pages', return_value=[]), patch.object(monitor, 'publisher_identity', return_value='bot'), patch.object(monitor, 'api', side_effect=TimeoutError) as write:
                with self.assertRaises(TimeoutError):
                    monitor.publication(db, policy, 'fp', payload)
                self.assertEqual('reconciling', monitor.publication(db, policy, 'fp', payload)['status'])
                self.assertEqual(1, write.call_count)
            db.close()

    def test_result_comment_exclusion_requires_retained_id_author_body_marker(self):
        body = 'Result\n<!-- cao-issue-fix key -->'
        retained = [{'comment_id': 7, 'publisher': 'bot', 'body_digest': fix.digest(body), 'marker': '<!-- cao-issue-fix key -->'}]
        comments = [{'id': 7, 'body': body, 'user': {'login': 'bot'}}]
        with patch.object(fix, 'api', return_value=issue()), patch.object(fix, 'pages', return_value=comments):
            _, _, original, excluded = fix.issue_snapshot('owner/repo', 1, {}, retained)
            self.assertEqual([7], excluded)
            comments[0]['body'] += ' edited'
            _, _, changed, excluded = fix.issue_snapshot('owner/repo', 1, {}, retained)
            self.assertNotEqual(original, changed)
            self.assertEqual([], excluded)

    def test_comment_ambiguous_post_retains_push_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {'issue_number': 1, 'branch': 'cao/issue-1-key', 'commit_sha': 'a' * 40, 'execution_key': 'key', 'publisher': 'bot'}
            path = Path(directory) / 'state.json'
            fix.atomic(path, state)
            with patch.object(fix, 'branch_sha', return_value='a' * 40), patch.object(fix, 'pages', return_value=[]), patch.object(fix, 'api', side_effect=TimeoutError) as write:
                with self.assertRaises(TimeoutError):
                    fix.reconcile_comment(state, path, {'repository': 'owner/repo'})
                self.assertEqual('pushed_comment_pending', fix.reconcile_comment(state, path, {'repository': 'owner/repo'})['status'])
                self.assertEqual(1, write.call_count)


class FixTests(unittest.TestCase):
    def test_incident_filters_cannot_override_time_or_service(self):
        specification = {'title': 'Incident', 'body': 'logs', 'comments': []}
        policy = {'issue_incidents': {'1': {'service': 'api', 'environment': 'prod', 'occurred_at': '2026-10-01T00:05:00Z',
                                          'filters': {'occurred_at': '2020-01-01T00:00:00Z'}}},
                  'monitors': {'prod': {'service': 'api', 'environment': 'prod'}}}
        with self.assertRaises(fix.Blocked), patch.object(fix, 'collect') as collect:
            fix.incident_context(issue(), specification, policy, Path('/tmp'), {})
        collect.assert_not_called()

    def test_push_flow_checks_issue_base_parent_and_reuses_verified_result(self):
        self._publication_flow()

    def test_changed_issue_base_test_failure_and_competing_branch_prevent_push(self):
        for failure in ('issue', 'base', 'tests', 'branch'):
            with self.subTest(failure=failure):
                self._publication_flow(failure)

    def _publication_flow(self, failure=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = {'repository': 'owner/repo', 'state_root': directory, 'workspace_root': str(root / 'workspace'),
                      'source_paths': ['src/app.py', 'tests/test_app.py'], 'editable_paths': ['src/**', 'tests/**'],
                      'regression_test_paths': ['tests/**'], 'allow_code_issues': True,
                      'git_author_name': 'Bot', 'git_author_email': 'bot@example.invalid'}
            inputs = {'repository': 'owner/repo', 'issue_number': 1, 'apply_mode': 'push',
                      'execution_state_path': str(root / 'execution.json'), 'expected_policy_digest': 'p', 'expected_resource_digest': 'r'}
            template = root / 'template'
            template.mkdir()
            base = init_source(template)
            remote = {'sha': None, 'pushes': 0, 'comments': []}
            real_git = fix.git
            def checkout(repo, sha, work, policy):
                source = work / 'source'
                shutil.copytree(template, source)
                return source
            def git(source, *argv, **kwargs):
                if argv[0] == 'push':
                    self.assertEqual('--force-with-lease=refs/heads/' + argv[1].split('refs/heads/')[1], argv[1])
                    self.assertTrue(argv[1].endswith(':'))
                    remote['sha'] = argv[-1].split(':')[0]
                    self.assertEqual(base, real_git(source, 'rev-parse', 'HEAD^').strip())
                    remote['pushes'] += 1
                    return ''
                return real_git(source, *argv, **kwargs)
            def api(repo, path, policy, method='GET', payload=None):
                if method == 'POST':
                    row = {'id': 77, 'body': payload['body'], 'user': {'login': 'bot'}}
                    remote['comments'].append(row)
                    return row
                return {'default_branch': 'main'}
            snapshots = [(issue(), {'title': 'Wrong sum', 'body': 'Return 2', 'comments': []}, 'snapshot', [])] * 3
            if failure == 'issue':
                snapshots[1] = (issue(), {}, 'changed', [])
            refs = [base, 'b' * 40] if failure == 'base' else None
            with patch.object(fix, 'admission', return_value=policy), patch.object(fix, 'issue_snapshot', side_effect=snapshots), patch.object(fix, 'api', side_effect=api), patch.object(fix, 'resolve_sha', side_effect=refs, return_value=base), patch.object(fix, 'validate_fix_policy'), patch.object(fix, 'checkout', side_effect=checkout), patch.object(fix, 'git', side_effect=git), patch.object(fix, 'model', return_value=proposal()) as model, patch.object(fix, 'test_candidate', side_effect=fix.Blocked('test failure') if failure == 'tests' else None, return_value=[{'result': 'passed'}]), patch.object(fix, 'branch_sha', side_effect=lambda *a: 'c' * 40 if failure == 'branch' else remote['sha']), patch.object(fix, 'pages', side_effect=lambda *a: remote['comments']), patch.object(fix, 'command', wraps=fix.command) as command:
                # The authenticated identity GET is the only command intercepted.
                real_command = command._mock_wraps
                command.side_effect = lambda argv, **kwargs: '{"login":"bot"}' if argv == ['gh', 'api', 'user'] else real_command(argv, **kwargs)
                if failure:
                    with self.assertRaises(fix.Blocked):
                        fix.process(inputs)
                    self.assertEqual(0, remote['pushes'])
                    self.assertEqual([], remote['comments'])
                else:
                    result = fix.process(inputs)
                    self.assertEqual('pushed', result['status'])
                    self.assertEqual(1, remote['pushes'])
                    self.assertEqual('pushed', fix.process(inputs)['status'])
                    self.assertEqual(1, model.call_count)
                    self.assertEqual(1, remote['pushes'])
                    self.assertEqual(1, len(remote['comments']))

    def test_all_edits_validated_before_mutation_and_regression_required(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            init_source(source)
            policy = {'editable_paths': ['src/**', 'tests/**'], 'regression_test_paths': ['tests/**']}
            files = {name: (source / name).read_text() for name in ['src/app.py', 'tests/test_app.py']}
            invalid = proposal()
            invalid['edits'][1]['old'] = 'missing'
            with self.assertRaises(fix.Blocked):
                fix.apply_proposal(source, files, invalid, policy)
            self.assertEqual(files['src/app.py'], (source / 'src/app.py').read_text())
            without_test = proposal()
            without_test['edits'] = without_test['edits'][:1]
            with self.assertRaises(fix.Blocked):
                fix.apply_proposal(source, files, without_test, policy)
            self.assertEqual(2, len(fix.apply_proposal(source, files, proposal(), policy)))

    def test_symlink_traversal_env_and_unsupported_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            policy = {'editable_paths': ['**']}
            (source / 'alias').symlink_to('/tmp')
            for name in ('../secret', '.git/config', '.env', 'alias/a.py', '/absolute'):
                with self.subTest(name=name), self.assertRaises(fix.Blocked):
                    fix.editable(source, name, policy)

    def test_test_worker_has_no_credentials_git_network_or_candidate_access(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            source.mkdir()
            init_source(source)
            policy = {'test_commands': [['python3', '-m', 'unittest']], 'test_image': 'sha256:' + '1' * 64}
            def command(argv, **kwargs):
                self.assertIn('--network=none', argv)
                self.assertIn('--pull=never', argv)
                mount = argv[argv.index('--mount') + 1]
                self.assertIn('test-source', mount)
                self.assertFalse((work / 'test-source/.git').exists())
                self.assertNotIn('GH_TOKEN', ' '.join(argv))
                self.assertNotIn('env', kwargs)
                (work / 'test-source/src/app.py').write_text('tampered')
                return ''
            with patch.object(fix, 'command', side_effect=command), patch.object(fix.subprocess, 'run'):
                self.assertEqual('passed', fix.test_candidate(source, work, policy)[0]['result'])
            self.assertEqual('value = 1\n', (source / 'src/app.py').read_text())

    def test_human_issue_patch_flow_has_no_monitor_or_github_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            policy = {'repository': 'owner/repo', 'state_root': directory, 'workspace_root': str(workspace),
                      'source_paths': ['src/app.py', 'tests/test_app.py'], 'editable_paths': ['src/**', 'tests/**'],
                      'regression_test_paths': ['tests/**'], 'allow_code_issues': True}
            inputs = {'repository': 'owner/repo', 'issue_number': 1, 'apply_mode': 'patch',
                      'execution_state_path': str(root / 'execution.json'), 'expected_policy_digest': 'p', 'expected_resource_digest': 'r'}
            def checkout(repo, sha, work, policy):
                source = work / 'source'
                source.mkdir()
                init_source(source)
                return source
            with patch.object(fix, 'admission', return_value=policy), patch.object(fix, 'issue_snapshot', return_value=(issue(), {'title': 'Wrong sum', 'body': 'Return 2', 'comments': []}, 'snapshot', [])), patch.object(fix, 'api', return_value={'default_branch': 'main'}) as api, patch.object(fix, 'resolve_sha', return_value='a' * 40), patch.object(fix, 'validate_fix_policy'), patch.object(fix, 'checkout', side_effect=checkout), patch.object(fix, 'model', return_value=proposal()), patch.object(fix, 'test_candidate', return_value=[{'result': 'passed'}]), patch.object(fix, 'collect') as collect:
                result = fix.process(inputs)
                self.assertEqual('patch_ready', result['status'])
                self.assertTrue((Path(result['artifact_path']) / 'candidate.patch').exists())
                collect.assert_not_called()
                self.assertEqual(1, api.call_count)
                self.assertEqual('patch_ready', fix.process(inputs)['status'])


if __name__ == '__main__':
    unittest.main()
