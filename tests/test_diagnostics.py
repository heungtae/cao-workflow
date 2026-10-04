import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import incident_run
import results as viewer

shim = types.ModuleType('cao_workflow')
shim.step = lambda *a, **kw: None
shim.get_inputs = lambda: {}
shim.emit_output = lambda value: value
sys.modules.setdefault('cao_workflow', shim)


def load(name):
    spec = importlib.util.spec_from_file_location('diagnostic_' + name, ROOT / 'workflows' / name / 'workflow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fix = load('github-issue-fix')
review = load('github-pr-review')
apply = load('github-pr-apply')
monitor = load('mcp-exception-issue')


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        for module in (fix, review, apply, monitor):
            module._DIAG.clear()

    def test_result_identity_terminal_immutability_and_generation_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            root = fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            output = {'workflow': fix.WORKFLOW, 'repository': 'owner/repo', 'run_id': 'run1', 'status': 'blocked'}
            fix.diag_result(output)
            with self.assertRaises(ValueError):
                fix.diag_result(dict(output, status='pushed'))
            with self.assertRaises(ValueError):
                fix.diag_result(dict(output, run_id='another'))
            with patch.dict(os.environ, {'CAO_WORKFLOW_GENERATION': '1'}):
                fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
                fix.diag_result(dict(output, status='pushed'))
            self.assertEqual('blocked', json.loads((root / 'result.0.json').read_text())['output']['status'])
            self.assertEqual('pushed', json.loads((root / 'result.json').read_text())['output']['status'])
            self.assertEqual(0o600, (root / 'result.json').stat().st_mode & 0o777)

    def test_private_paths_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / 'bad'
            bad.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': str(bad)})
            alias = root / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': str(alias)})

    def test_failed_timeout_diagnostics_are_redacted_and_bounded(self):
        token = 'ghp_' + 'x' * 30
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            root = fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            error = subprocess.TimeoutExpired(['test'], 1, output=b'x' * 20000, stderr=('Bearer hidden ' + token + ' token=secret user@example.com').encode())
            with patch.object(fix.subprocess, 'run', side_effect=error):
                row = fix.diag_test_command(['test'], command_ref=['test'])
            saved = json.loads(Path(row['diagnostic_path']).read_text())
            self.assertTrue(saved['timeout'])
            self.assertTrue(saved['stdout_truncated'])
            self.assertLessEqual(len(saved['stdout']), 16384)
            self.assertNotIn(token, json.dumps(saved))
            self.assertNotIn('user@example.com', json.dumps(saved))
            self.assertEqual('[REDACTED]', fix.diag_clean({'token': 'opaque'})['token'])
            self.assertEqual(['test', '--token', '[REDACTED]'], fix.diag_clean(['test', '--token', 'opaque']))
            self.assertNotIn('opaque', fix.diag_clean('AWS_SECRET_ACCESS_KEY=opaque "token":"opaque"'))
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(list(range(1, len(events) + 1)), [r['seq'] for r in events])

    def test_incident_finish_saves_final_output_before_emission_and_coordinator_updates_preserve_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            path = Path(directory) / 'execution.json'
            inputs = {'repository': 'owner/repo', 'execution_state_path': str(path)}
            state = {'schema_version': 1, 'workflow': fix.WORKFLOW, 'execution_id': 'a' * 32,
                     'inputs': inputs, 'run_ids': ['run1']}
            fix.diag_atomic(path, state)
            fix.diag_init(inputs)
            def emit(output):
                saved = json.loads(path.read_text())
                self.assertEqual(output, saved['child_outputs']['run1'])
                self.assertTrue((Path(output['evidence_directory']) / 'result.json').exists())
            with patch.object(fix, 'emit_output', side_effect=emit):
                fix.finish(inputs, {'status': 'patch_ready'})
            incident_run.persist(state, path)
            self.assertIn('run1', json.loads(path.read_text())['child_outputs'])
            bare = {'run_id': 'run1', 'state': 'completed', 'steps': []}
            with patch.object(incident_run.review_apply, 'workflow_result', return_value=bare):
                result = incident_run.submit(state, path, Path('/deployed.py'))
            self.assertEqual('patch_ready', result['output']['status'])

    def test_missing_output_blocks_both_new_completion_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            state = {'workflow': fix.WORKFLOW, 'execution_id': 'a' * 32, 'run_ids': ['run1'],
                     'inputs': {'repository': 'owner/repo'}}
            bare = {'run_id': 'run1', 'state': 'completed', 'steps': []}
            with patch.object(incident_run.review_apply, 'workflow_result', return_value=bare):
                with self.assertRaisesRegex(ValueError, 'lacks verified'):
                    incident_run.submit(state, path, Path('/deployed.py'))
            with patch.object(incident_run.review_apply, 'workflow_result', side_effect=[None, bare]), patch.object(incident_run.review_apply, 'json_command', return_value={'run_id': 'run1'}), patch.object(incident_run.review_apply, 'command'):
                with self.assertRaisesRegex(ValueError, 'lacks verified'):
                    incident_run.submit(state, path, Path('/deployed.py'))

    def test_pending_comment_recovery_with_actual_retained_api_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            state = {'workflow': fix.WORKFLOW, 'execution_id': 'a' * 32, 'run_ids': ['run1'],
                     'inputs': {'repository': 'owner/repo', 'recovery_only': False}}
            output = {'workflow': fix.WORKFLOW, 'repository': 'owner/repo', 'run_id': 'run1', 'status': 'pushed_comment_pending'}
            state['child_outputs'] = {'run1': output}
            incident_run.persist(state, path)
            def lookup(rid, **kwargs):
                if rid == 'run1':
                    return {'run_id': rid, 'state': 'completed', 'steps': []}
                if kwargs.get('missing_ok'):
                    return None
                saved = json.loads(path.read_text())
                saved.setdefault('child_outputs', {})[rid] = dict(output, run_id=rid, status='pushed')
                incident_run.manage.atomic_json(path, saved)
                return {'run_id': rid, 'state': 'completed', 'steps': []}
            with patch.object(incident_run.review_apply, 'workflow_result', side_effect=lookup), patch.object(incident_run.review_apply, 'json_command', side_effect=lambda args: {'run_id': state['run_ids'][-1]}), patch.object(incident_run.review_apply, 'command'):
                result = incident_run.submit(state, path, Path('/deployed.py'))
            self.assertEqual('pushed', result['output']['status'])
            self.assertTrue(state['inputs']['recovery_only'])
            self.assertEqual(2, len(state['run_ids']))

    def test_multiline_groups_require_explicit_complete_same_revision_order(self):
        base = {'service': 'api', 'environment': 'prod', 'source_id': 'app', 'message': 'Exception', 'record_id': 'record1',
                'exception_group_id': 'g1', 'exception_group_size': 2, 'deployment_sha': 'a' * 40}
        first = dict(base, evidence_id='e1', exception_sequence=0)
        second = dict(base, evidence_id='e2', exception_sequence=1, message='at app.py:1')
        merged = monitor.group_exceptions([second, first])[0]
        self.assertEqual(['e1', 'e2'], merged['group_evidence_ids'])
        self.assertEqual('Exception\nat app.py:1', merged['stack_trace'])
        for rows in ([first], [first, first], [first, dict(second, deployment_sha='b' * 40)]):
            with self.assertRaises(monitor.Blocked):
                monitor.group_exceptions(rows)

    def test_ambiguous_review_post_never_posts_twice(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'review1'}):
            review.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            with patch.object(review, 'gh_json', return_value={'login': 'bot'}), patch.object(review, 'run_command', side_effect=RuntimeError('lost response')) as post:
                with self.assertRaises(RuntimeError):
                    review.publish('owner/repo', 1, 'a' * 40, 'body', [])
                with patch.object(review, 'pages', return_value=[]):
                    with self.assertRaisesRegex(RuntimeError, 'no new POST'):
                        review.publish('owner/repo', 1, 'a' * 40, 'body', [])
                self.assertEqual(1, post.call_count)

    def test_review_post_reconciles_exact_body_author_and_comment_membership(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'review1'}):
            root = review.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            payload = {'commit_id': 'a' * 40, 'body': 'body', 'event': 'COMMENT', 'comments': []}
            review.diag_atomic(root / 'review-1.publication.json', {'repository': 'owner/repo', 'publisher': 'bot', 'payload': payload, 'status': 'sending'})
            saved_review = {'id': 12, 'html_url': 'https://github.com/owner/repo/pull/1#pullrequestreview-12',
                            'commit_id': 'a' * 40, 'body': 'body', 'user': {'login': 'bot'}}
            with patch.object(review, 'pages', side_effect=[[saved_review], []]), patch.object(review, 'run_command') as post:
                result = review.publish('owner/repo', 1, 'a' * 40, 'body', [])
            self.assertEqual(12, result['review_id'])
            post.assert_not_called()

    def test_standalone_apply_reconciles_saved_output_with_retained_cao_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'old-run'}):
            apply.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            apply.diag_result({'workflow': apply.WORKFLOW, 'repository': 'owner/repo', 'run_id': 'old-run', 'result': 'applied'})
            with patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'new-run', 'CAO_API_BASE_URL': 'http://localhost:9889'}):
                apply.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
                response = types.SimpleNamespace(read=lambda size: json.dumps({'run_id': 'old-run', 'state': 'completed', 'steps': []}).encode())
                response.__enter__ = lambda s: s
                from contextlib import nullcontext
                with patch.object(apply, 'urlopen', return_value=nullcontext(response)):
                    result = apply.retained_result('old-run')
            self.assertEqual('applied', result['output']['result'])

    def test_viewer_reports_business_outcomes_without_model_or_write_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            fix.diag_result({'workflow': fix.WORKFLOW, 'run_id': 'run1', 'repository': 'owner/repo', 'status': 'blocked'})
            with patch('builtins.print') as printed:
                self.assertEqual(0, viewer.main(['--run', 'run1', '--root', directory]))
            self.assertEqual('blocked', json.loads(printed.call_args.args[0])[0]['status'])

    def test_viewer_verification_distinguishes_completed_blocked_and_cao_timeout(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            fix.diag_result({'workflow': fix.WORKFLOW, 'run_id': 'run1', 'repository': 'owner/repo', 'status': 'blocked'})
            response = subprocess.CompletedProcess([], 0, json.dumps({'run_id': 'run1', 'state': 'completed'}))
            with patch.object(viewer.subprocess, 'run', return_value=response), patch('builtins.print') as printed:
                viewer.main(['--run', 'run1', '--root', directory, '--verify-cao'])
            row = json.loads(printed.call_args.args[0])[0]
            self.assertFalse(row['execution_verified'])
            self.assertEqual('completed', row['cao_state'])
            with patch.object(viewer.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 30)), patch('builtins.print') as printed:
                viewer.main(['--run', 'run1', '--root', directory, '--verify-cao'])
            row = json.loads(printed.call_args.args[0])[0]
            self.assertFalse(row['execution_verified'])
            self.assertEqual('unresolved', row['cao_state'])
            self.assertEqual('timeout', row['cao_error'])

    def test_result_history_remains_immutable_if_current_pointer_is_missing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'run1'}):
            root = fix.diag_init({'repository': 'owner/repo', 'diagnostics_root': directory})
            output = {'workflow': fix.WORKFLOW, 'repository': 'owner/repo', 'run_id': 'run1', 'status': 'blocked'}
            fix.diag_result(output)
            (root / 'result.json').unlink()
            with self.assertRaisesRegex(ValueError, 'Generation result'):
                fix.diag_result(dict(output, status='pushed'))


class RegressionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CAO_TEST_IMAGE'), 'Set CAO_TEST_IMAGE for real Docker regression validation')
    def test_real_docker_before_after_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            (source / 'src').mkdir(parents=True)
            (source / 'tests').mkdir()
            (source / 'src/app.py').write_text('value = 1\n')
            (source / 'tests/test_app.py').write_text('from src.app import value\nassert value == 1\n')
            for argv in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'original']):
                subprocess.run(['git', '-C', str(source), *argv], check=True, capture_output=True)
            (source / 'src/app.py').write_text('value = 2\n')
            (source / 'tests/test_app.py').write_text('from src.app import value\nassert value == 2, "expected original defect"\n')
            policy = {'regression_test_paths': ['tests/**'],
                      'regression_commands': [['python3', '-B', '-c', 'import runpy; runpy.run_path("tests/test_app.py")']],
                      'regression_failure_pattern': 'AssertionError: expected original defect',
                      'test_image': os.environ['CAO_TEST_IMAGE']}
            rows = fix.verify_regression(source, work, policy, ['src/app.py', 'tests/test_app.py'])
            self.assertEqual(['expected_defect_detected', 'passed'], [r['result'] for r in rows])
            self.assertEqual([1, 0], [r['exit_code'] for r in rows])
            self.assertEqual('value = 2\n', (source / 'src/app.py').read_text())

    def test_incident_admission_uses_installed_cao_api_environment_contract(self):
        from contextlib import nullcontext
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile, config, journal = root / 'profile.md', root / 'config.toml', root / 'journal.json'
            fix.diag_atomic(journal, {'workflow': fix.WORKFLOW, 'inputs': {'repository': 'owner/repo'},
                                     'profile_path': str(profile), 'codex_config_path': str(config)})
            profile.write_text('codexProfile: cao_incident_readonly')
            config.write_text('sandbox_mode="read-only"\napproval_policy="never"\n[shell_environment_policy]\ninherit="none"\n')
            config.chmod(0o600)
            inputs = {'repository': 'owner/repo', 'execution_state_path': str(journal), 'expected_resource_digest': 'digest'}
            with patch.dict(os.environ, {'CAO_API_BASE_URL': 'http://localhost:9889'}, clear=True), patch.object(fix.os, 'getuid', return_value=os.getuid()), patch.object(fix, 'load_policy', return_value={'repository': 'owner/repo'}), patch.object(fix, 'verify_journal_inputs'), patch.object(fix, 'resource_digest', return_value='digest'), patch.object(fix, 'urlopen', return_value=nullcontext(io.StringIO('{"enabled":false}'))) as get:
                self.assertEqual('owner/repo', fix.admission(inputs, 'issue-fixer')['repository'])
            self.assertEqual('http://localhost:9889/settings/memory', get.call_args.args[0])

    def test_original_detects_defect_and_candidate_passes_with_same_new_test(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            (source / 'src').mkdir(parents=True)
            (source / 'tests').mkdir()
            (source / 'src/app.py').write_text('value = 1\n')
            (source / 'tests/test_app.py').write_text('from src.app import value\nassert value == 1\n')
            for argv in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'original']):
                subprocess.run(['git', '-C', str(source), *argv], check=True, capture_output=True)
            (source / 'src/app.py').write_text('value = 2\n')
            (source / 'tests/test_app.py').write_text('from src.app import value\nassert value == 2, "expected original defect"\n')
            argv = ['python3', '-B', '-c', 'import runpy; runpy.run_path("tests/test_app.py")']
            policy = {'regression_test_paths': ['tests/**'], 'regression_commands': [argv],
                      'regression_failure_pattern': 'AssertionError: expected original defect', 'test_image': 'sha256:' + '1' * 64}
            def local(source_dir, work_dir, policy, commands, phase, failure_pattern=None):
                proc = subprocess.run(commands[0], cwd=source_dir, text=True, capture_output=True)
                if failure_pattern:
                    self.assertNotEqual(0, proc.returncode)
                    self.assertIn('expected original defect', proc.stderr)
                else:
                    self.assertEqual(0, proc.returncode, proc.stderr)
                return [{'phase': phase, 'exit_code': proc.returncode}]
            with patch.object(fix, 'test_candidate', side_effect=local):
                rows = fix.verify_regression(source, work, policy, ['src/app.py', 'tests/test_app.py'])
            self.assertEqual(['original', 'regression'], [r['phase'] for r in rows])
            self.assertEqual('value = 2\n', (source / 'src/app.py').read_text())
            self.assertFalse((work / 'regression-baseline').exists())

    def test_original_failure_must_match_configured_defect_not_environment_or_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            source.mkdir()
            policy = {'test_image': 'sha256:' + '1' * 64}
            for row in ({'exit_code': 1, 'timeout': False, 'stdout': '', 'stderr': 'ModuleNotFoundError', 'result': 'failed'},
                        {'exit_code': None, 'timeout': True, 'stdout': '', 'stderr': 'AssertionError', 'result': 'failed'},
                        {'exit_code': 0, 'timeout': False, 'stdout': '', 'stderr': 'AssertionError', 'result': 'passed'}):
                with patch.object(fix, 'diag_test_command', return_value=row), patch.object(fix.subprocess, 'run'):
                    with self.assertRaises(fix.Blocked):
                        fix.test_candidate(source, work, policy, [['test']], 'original', 'AssertionError')

    def test_unresolved_or_missing_requirement_outcomes_block_all_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'app.py').write_text('value = 1')
            (source / 'test.py').write_text('assert 1')
            files = {'app.py': 'value = 1', 'test.py': 'assert 1'}
            proposal = {'decision': 'fix', 'reason': 'fix', 'regression_scenarios': ['value'],
                        'edits': [{'path': 'app.py', 'old': '1', 'new': '2'}, {'path': 'test.py', 'old': '1', 'new': '2'}]}
            policy = {'editable_paths': ['*'], 'regression_test_paths': ['test.py'], '_requirement_ids': ['title', 'body']}
            for outcomes in ([], [{'requirement_id': 'title', 'status': 'unresolved', 'reason': 'missing'}],
                             [{'requirement_id': 'title', 'status': 'addressed', 'reason': 'done', 'edit_paths': ['app.py'], 'test_paths': ['test.py']}]):
                with self.assertRaises(fix.Blocked):
                    fix.apply_proposal(source, files, dict(proposal, requirement_outcomes=outcomes), policy)
            self.assertEqual('value = 1', (source / 'app.py').read_text())


if __name__ == '__main__':
    unittest.main()
