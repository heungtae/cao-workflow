import copy
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
shim = types.ModuleType('cao_workflow')
shim.step = lambda *a, **kw: None
shim.get_inputs = lambda: {}
shim.emit_output = lambda x: x
sys.modules.setdefault('cao_workflow', shim)
spec = importlib.util.spec_from_file_location('apply_workflow', ROOT / 'workflows/github-pr-apply/workflow.py')
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)
SHA, BASE = 'a' * 40, 'b' * 40


def policy():
    return {'review_authors': ['review-bot'], 'editable_paths': ['src/**', 'tests/**'],
            'context_paths': [], 'new_files': [], 'push_branches': ['fix/*'],
            'test_image': 'tests@sha256:' + '1' * 64,
            'test_commands': [['python3', '-m', 'unittest']],
            'git_author_name': 'Bot', 'git_author_email': 'bot@example.invalid'}


def pr(sha=SHA):
    return {'number': 1, 'state': 'open', 'draft': False,
            'head': {'sha': sha, 'ref': 'fix/one', 'repo': {'full_name': 'owner/repo'}},
            'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': 'owner/repo'}}}


def review():
    return {'id': 12, 'state': 'COMMENTED', 'commit_id': SHA, 'user': {'login': 'review-bot'},
            'body': apply.review_marker('owner/repo', 1, SHA) + f'\n<!-- cao-review-base {BASE} -->',
            'html_url': 'https://github.com/owner/repo/pull/1#pullrequestreview-12'}


def comment(cid=34):
    return {'id': cid, 'pull_request_review_id': 12, 'user': {'login': 'review-bot'},
            'commit_id': SHA, 'path': 'src/app.py', 'line': 1, 'side': 'RIGHT', 'body': 'fix value'}


def proposal():
    return {'edits': [{'path': 'src/app.py', 'old': 'value = 1', 'new': 'value = 2', 'comment_ids': [34]}],
            'outcomes': [{'comment_id': 34, 'status': 'addressed', 'reason': 'correct value'}]}


def init_source(path):
    (path / 'src').mkdir(parents=True)
    (path / 'src/app.py').write_text('value = 1\n')
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial']):
        subprocess.run(['git', '-C', str(path), *args], env=env, check=True, capture_output=True)


class ApplyTests(unittest.TestCase):
    def test_repository_metadata_endpoint_has_no_trailing_slash(self):
        with patch.object(apply, 'run', return_value='{"default_branch":"main"}') as run:
            self.assertEqual('main', apply.api('owner/repo', '')['default_branch'])
        self.assertEqual(['gh', 'api', 'repos/owner/repo'], run.call_args.args[0])

    def test_review_rejects_forged_author_marker_and_comment(self):
        files = [{'filename': 'src/app.py', 'patch': '@@ -1 +1 @@\n-old\n+value = 1\n'}]
        with patch.object(apply, 'api', return_value=review()), patch.object(apply, 'pages', side_effect=[[comment()], [comment()], files]):
            self.assertEqual(34, apply.validate_review('owner/repo', 1, SHA, 12, policy(), 1, BASE)[1][0]['id'])
        for key, value in [('commit_id', 'c' * 40), ('body', 'fake'), ('user', {'login': 'attacker'})]:
            bad = dict(review(), **{key: value})
            with self.subTest(key=key), patch.object(apply, 'api', return_value=bad), patch.object(apply, 'pages') as pages:
                with self.assertRaises(ValueError):
                    apply.validate_review('owner/repo', 1, SHA, 12, policy(), base_sha=BASE)
                pages.assert_not_called()
        for key, value in [('in_reply_to_id', 1), ('line', 2), ('id', True), ('user', {'login': 'attacker'}), ('pull_request_review_id', 13)]:
            with self.subTest(comment_key=key), patch.object(apply, 'api', return_value=review()), patch.object(apply, 'pages', side_effect=[[dict(comment(), **{key: value})], [dict(comment(), **{key: value})], files]):
                with self.assertRaises(ValueError):
                    apply.validate_review('owner/repo', 1, SHA, 12, policy(), 1, BASE)

    def test_review_comment_ids_join_modern_locations_and_fail_on_missing_evidence(self):
        files = [{'filename': 'src/app.py', 'patch': '@@ -1 +1 @@\n-old\n+value = 1\n'}]
        legacy = {k: v for k, v in comment().items() if k not in ('line', 'side')}
        with patch.object(apply, 'api', return_value=review()), patch.object(apply, 'pages', side_effect=[[legacy], [comment(), comment(999)], files]):
            self.assertEqual(34, apply.validate_review('owner/repo', 1, SHA, 12, policy(), 1, BASE)[1][0]['id'])
        for modern in ([], [comment(), comment()], [comment(999)]):
            with patch.object(apply, 'api', return_value=review()), patch.object(apply, 'pages', side_effect=[[legacy], modern]), self.assertRaises(ValueError):
                apply.validate_review('owner/repo', 1, SHA, 12, policy(), 1, BASE)

    def test_ambiguous_or_unsupported_edits_do_not_mutate_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            init_source(source)
            files = {'src/app.py': 'value = 1\n', 'tests/new.py': None}
            first = {'path': 'tests/new.py', 'old': '', 'new': 'pass\n', 'comment_ids': [34]}
            bad = {'path': 'src/app.py', 'old': 'missing', 'new': 'new', 'comment_ids': [34]}
            p = dict(proposal(), edits=[first, bad])
            with self.assertRaises(ValueError):
                apply.apply_edits(source, files, [comment()], p, policy())
            self.assertFalse((source / 'tests/new.py').exists())
            self.assertEqual('value = 1\n', (source / 'src/app.py').read_text())
            p = proposal()
            p['outcomes'][0]['comment_id'] = 999
            with self.assertRaises(ValueError):
                apply.apply_edits(source, files, [comment()], p, policy())

    def test_path_traversal_credentials_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'src').mkdir()
            (source / 'src/link').symlink_to('/tmp')
            for name in ('../escape', '/tmp/escape', 'src/../../escape', 'src/.git/config', 'src/.env', 'src/link/escape', 'src//file'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    apply.safe_path(source, name, policy())

    def test_only_complete_supported_outcomes_can_be_marked_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            init_source(source)
            files = {'src/app.py': 'value = 1\n'}
            for change in ({'edits': []}, {'outcomes': []}, {'outcomes': proposal()['outcomes'] * 2}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    apply.apply_edits(source, files, [comment()], dict(proposal(), **change), policy())

    def test_model_only_gets_shell_inert_carrier_and_file_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def complete(provider, role, carrier, **opts):
                self.assertEqual('pr-review-applier', role)
                self.assertRegex(carrier, r'^: CAO_APPLY_INPUT_[0-9a-f]{32}$')
                task = json.loads((root / 'inputs' / (carrier.split()[1] + '.json')).read_text())
                self.assertEqual('value = 1\n', task['files']['src/app.py'])
                self.assertEqual('manual', opts['recovery'])
                self.assertEqual(str(root), opts['working_directory'])
                return types.SimpleNamespace(output=json.dumps(proposal()))
            with patch.object(apply, 'step', side_effect=complete):
                self.assertEqual(proposal(), apply.propose(root, {'src/app.py': 'value = 1\n'}, [comment()], None))
            self.assertEqual([], list((root / 'inputs').iterdir()))
            with patch.object(apply, 'step') as step:
                with self.assertRaises(ValueError):
                    apply.propose(root, {'src/app.py': 'ghp_' + 'a' * 30}, [comment()], None)
                step.assert_not_called()

    def test_tests_have_no_network_credentials_git_or_access_to_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            init_source(source)
            (source / '.env').write_text('token=secret')
            def isolated(args, **kwargs):
                self.assertIn('--network=none', args)
                self.assertIn('--read-only', args)
                self.assertIn('--cap-drop=ALL', args)
                self.assertIn('--security-opt=no-new-privileges', args)
                self.assertIn('--pull=never', args)
                self.assertTrue(kwargs['quiet'])
                self.assertNotIn('GH_TOKEN', ' '.join(args))
                self.assertFalse((work / 'test-source/.git').exists())
                self.assertFalse((work / 'test-source/.env').exists())
                (work / 'test-source/src/app.py').write_text('test mutated this copy')
                return ''
            with patch.object(apply, 'run', side_effect=isolated), patch.object(apply.subprocess, 'run') as cleanup:
                self.assertEqual('passed', apply.test_candidate(source, work, policy())[0]['result'])
                self.assertEqual('docker', cleanup.call_args.args[0][0])
            self.assertEqual('value = 1\n', (source / 'src/app.py').read_text())
            self.assertFalse((work / 'test-source').exists())

    def test_policy_is_owned_and_pinned_and_workspace_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps({'schema_version': 1, 'repositories': {'owner/repo': policy()}}))
            path.chmod(0o600)
            self.assertEqual(policy(), apply.load_policy(str(path), 'owner/repo'))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                apply.load_policy(str(path), 'owner/repo')
            root = Path(directory) / 'private'
            root.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                apply.private_directory(root)

    def test_pr_lock_prevents_two_apply_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with apply.lock_pr(root, 'owner/repo', 1):
                with self.assertRaises(RuntimeError):
                    apply.lock_pr(root, 'OWNER/REPO', 1)

    def test_push_gate_blocks_forks_protected_and_default_branches(self):
        for mutate in ('fork', 'protected', 'default', 'base'):
            snapshot = pr()
            info = {'name': 'fix/one', 'protected': False}
            repo = {'default_branch': 'main'}
            if mutate == 'fork':
                snapshot['head']['repo']['full_name'] = 'attacker/repo'
            elif mutate == 'protected':
                info['protected'] = True
            elif mutate == 'default':
                repo['default_branch'] = 'fix/one'
            else:
                snapshot['head']['ref'] = 'main'
            with self.subTest(mutate=mutate), patch.object(apply, 'api', side_effect=[info, repo]), self.assertRaises(ValueError):
                apply.push_gate('owner/repo', snapshot, policy())

    def test_process_retains_verified_patch_and_removes_checkout(self):
        self.process_fixture()

    def test_partial_application_is_never_pushed(self):
        self.process_fixture(partial=True)

    def test_changed_head_and_test_failure_preserve_patch_without_push(self):
        self.process_fixture(failure='head')
        self.process_fixture(failure='tests')

    def process_fixture(self, failure=None, partial=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'apply'
            root.mkdir(mode=0o700)
            work = root / 'candidate-fixture'
            source = work / 'source'
            init_source(source)
            original = root / 'original'
            init_source(original)
            inputs = {'repository': 'owner/repo', 'pr_number': 1, 'head_sha': SHA, 'base_sha': BASE,
                      'review_id': 12, 'policy_path': str(Path(directory) / 'policy'), 'workspace_root': str(root)}
            candidate = proposal()
            if partial:
                inputs['apply_mode'] = 'push'
                candidate['outcomes'].append({'comment_id': 35, 'status': 'unaddressed', 'reason': 'insufficient context'})
            def api(repo, endpoint):
                if endpoint == 'pulls/1':
                    if failure == 'head' and (work / 'candidate.patch').exists():
                        return pr('c' * 40)
                    return pr()
                if endpoint == 'pulls/1/reviews/12':
                    return review()
                if endpoint == 'branches/fix%2Fone':
                    return {'name': 'fix/one', 'protected': False}
                if endpoint == '':
                    return {'default_branch': 'main'}
                raise AssertionError(endpoint)
            def pages(repo, endpoint, *args):
                return ([comment(), comment(35)] if partial else [comment()]) if 'comments' in endpoint else [{'filename': 'src/app.py', 'patch': '@@ -1 +1 @@\n-old\n+value = 1\n'}]
            with patch.object(apply, 'load_policy', return_value=policy()), patch.object(apply, 'api', side_effect=api), patch.object(apply, 'pages', side_effect=pages), patch.object(apply, 'checkout', return_value=work), patch.object(apply, 'propose', return_value=candidate), patch.object(apply, 'test_candidate', side_effect=RuntimeError('failed') if failure == 'tests' else None, return_value=[{'result': 'passed'}]), patch.object(apply, 'publish') as publish:
                if failure:
                    with self.assertRaises((ValueError, RuntimeError)):
                        apply.process(inputs)
                else:
                    output = apply.process(inputs)
                    self.assertEqual('partial' if partial else 'applied', output['result'])
                    self.assertEqual(['src/app.py'], output['changed_files'])
                publish.assert_not_called()
            self.assertFalse(source.exists())
            patchfile = work / 'candidate.patch'
            subprocess.run(['git', '-C', str(original), 'apply', '--check', str(patchfile)], check=True, capture_output=True)
            status = json.loads((work / 'result.json').read_text())
            self.assertEqual('failed' if failure else ('partial' if partial else 'applied'), status['result'])

    def test_publish_commits_verified_patch_and_uses_only_normal_push(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            init_source(source)
            original_git = apply.git
            sha = original_git(source, 'rev-parse', 'HEAD').strip()
            snapshot = pr(sha)
            (source / 'src/app.py').write_text('value = 2\n')
            original_git(source, 'add', '--all')
            inputs = {'pr_number': 1, 'head_sha': sha, 'review_id': 12}
            remote = [sha]
            pushes = []
            def git(path, *args):
                if 'ls-remote' in args:
                    return remote[0] + '\trefs/heads/fix/one\n'
                if 'push' in args:
                    pushes.append(args)
                    remote[0] = original_git(path, 'rev-parse', 'HEAD').strip()
                    return ''
                return original_git(path, *args)
            def api(repo, endpoint):
                if endpoint == 'pulls/1':
                    return snapshot
                if endpoint == 'branches/fix%2Fone':
                    return {'name': 'fix/one', 'protected': False}
                if endpoint == '':
                    return {'default_branch': 'main'}
                raise AssertionError(endpoint)
            with patch.dict(os.environ, {'CAO_WORKFLOW_RUN_ID': 'test-apply'}), patch.object(apply, 'git', side_effect=git), patch.object(apply, 'api', side_effect=api):
                committed = apply.publish('owner/repo', snapshot, source, work, inputs, policy(), 'f' * 64)
            self.assertNotEqual(sha, committed)
            self.assertEqual(committed, remote[0])
            self.assertEqual(1, len(pushes))
            self.assertNotIn('--force', pushes[0])
            self.assertIn(f'--force-with-lease=refs/heads/fix/one:{sha}', pushes[0])
            self.assertEqual('HEAD:refs/heads/fix/one', pushes[0][-1])
            message = original_git(source, 'log', '-1', '--format=%B')
            self.assertIn('CAO-Review-ID: 12', message)
            self.assertIn('CAO-Original-Head: ' + sha, message)
            self.assertIn('CAO-Apply-Run: test-apply', message)

    def test_reconcile_already_pushed_does_not_read_outdated_comment_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'apply'
            root.mkdir(mode=0o700)
            inputs = {'repository': 'owner/repo', 'pr_number': 1, 'head_sha': SHA, 'base_sha': BASE,
                      'review_id': 12, 'policy_path': str(Path(directory) / 'policy'), 'workspace_root': str(root), 'apply_mode': 'push'}
            key = apply.apply_key(inputs, policy())
            commit = {'parents': [{'sha': SHA}], 'commit': {'message': f'fixed\nCAO-Apply-Key: {key}\nCAO-Review-ID: 12\nCAO-Apply-Run: prior-apply'}}
            prior = {'run_id': 'prior-apply', 'state': 'completed', 'output': {
                'run_id': 'prior-apply', 'workflow': apply.WORKFLOW, 'version': apply.VERSION,
                'result': 'applied', 'apply_mode': 'push', 'apply_key': key, 'head_sha': SHA,
                'review_id': 12, 'pr': 1, 'repository': 'owner/repo', 'commit_sha': 'c' * 40}}
            with patch.object(apply, 'retained_result', return_value=prior), patch.object(apply, 'load_policy', return_value=policy()), patch.object(apply, 'api', side_effect=[pr('c' * 40), review(), commit]), patch.object(apply, 'pages') as pages, patch.object(apply, 'checkout') as checkout:
                self.assertEqual('skipped', apply.process(inputs)['result'])
                pages.assert_not_called()
                checkout.assert_not_called()

    def test_forged_commit_trailer_cannot_skip_apply_without_retained_success(self):
        key = 'f' * 64
        commit = {'parents': [{'sha': SHA}], 'commit': {'message': f'fixed\nCAO-Apply-Key: {key}\nCAO-Review-ID: 12\nCAO-Apply-Run: fake'}}
        with patch.object(apply, 'api', return_value=commit), patch.object(apply, 'retained_result', return_value={'run_id': 'fake', 'state': 'failed'}), self.assertRaises(ValueError):
            apply.reconciled('owner/repo', pr('c' * 40), SHA, 12, key)


if __name__ == '__main__':
    unittest.main()
