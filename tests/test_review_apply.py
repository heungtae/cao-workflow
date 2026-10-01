import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_apply import SHA, BASE, pr, review, policy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import review_apply as chain


def state():
    return {'schema_version': 1, 'chain_id': '1' * 32, 'request': {
        'repository': 'owner/repo', 'pr_number': 1, 'head_sha': SHA, 'base_sha': BASE,
        'head_ref': 'fix/one', 'head_repository': 'owner/repo', 'policy_path': '/operator/policy.json',
        'apply_mode': 'patch', 'model': None, 'review_workspace': '/tmp/review', 'apply_workspace': '/tmp/apply',
        'versions': {'github-pr-review': 'v6', 'github-pr-apply': 'v1'}}}


def review_output(result='completed', count=1):
    return {'workflow': 'github-pr-review', 'version': 'v6', 'repository': 'owner/repo',
            'results': [{'pr': 1, 'head_sha': SHA, 'base_sha': BASE, 'result': result,
                         'findings': count, 'review_id': 12 if result == 'completed' else None,
                         'publish_result': 'url'}]}


def apply_output():
    return {'workflow': 'github-pr-apply', 'version': 'v1', 'repository': 'owner/repo',
            'pr': 1, 'head_sha': SHA, 'review_id': 12, 'apply_mode': 'patch', 'result': 'applied'}


class ChainTests(unittest.TestCase):
    def run_chain(self, outputs, *, changed=False, count=1, force=False):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'state.json'
            current = pr('c' * 40) if changed else pr()
            def invoke(name, inputs, saved, path, deployed):
                if name == 'github-pr-review':
                    self.assertEqual(force, inputs.get('force_review', False))
                if name == 'github-pr-apply':
                    self.assertEqual(12, inputs['review_id'])
                    self.assertEqual(SHA, inputs['head_sha'])
                    self.assertEqual(BASE, inputs['base_sha'])
                    self.assertEqual(count, inputs['expected_findings'])
                output = outputs.pop(0)
                if isinstance(output, Exception):
                    raise output
                return output
            with patch.object(chain, 'api', return_value=current), patch.object(chain, 'execute_stage', side_effect=invoke) as execute, patch.object(chain, 'eligible_review', return_value=review()), patch.object(chain, 'pages', return_value=[{'id': 34}] * count):
                saved = state()
                saved['request']['force_review'] = force
                result = chain.orchestrate(saved, journal, policy(), {'github-pr-review': Path('/review'), 'github-pr-apply': Path('/apply')})
                return result, execute.call_count

    def test_review_completed_starts_exact_apply_and_zero_findings_skips(self):
        result, calls = self.run_chain([review_output(), apply_output()])
        self.assertEqual('applied', result['result'])
        self.assertEqual(2, calls)
        result, calls = self.run_chain([review_output(), apply_output()], force=True)
        self.assertEqual('applied', result['result'])
        result, calls = self.run_chain([review_output(count=0)], count=0)
        self.assertEqual('skipped', result['result'])
        self.assertEqual(1, calls)

    def test_review_failure_mismatch_and_stale_head_never_start_apply(self):
        for output in (RuntimeError('review failed'), review_output(count=2), dict(review_output(), results=[])):
            with self.subTest(output=output), self.assertRaises((ValueError, RuntimeError)):
                self.run_chain([output])
        with self.assertRaises(ValueError):
            self.run_chain([review_output(), apply_output()], changed=True)

    def test_skipped_review_is_resolved_to_owned_review_id(self):
        result, calls = self.run_chain([review_output(result='skipped'), apply_output()])
        self.assertEqual(12, result['review_id'])
        self.assertEqual(2, calls)

    def test_ambiguous_or_wrong_author_reviews_are_not_selected(self):
        for rows in ([review(), review()], [dict(review(), user={'login': 'attacker'})], [dict(review(), commit_id='c' * 40)]):
            with self.subTest(rows=rows), patch.object(chain, 'pages', return_value=rows), self.assertRaises(ValueError):
                chain.eligible_review('owner/repo', 1, SHA, 'v6', ['review-bot'], None, BASE)

    def test_changed_base_or_branch_invalidates_snapshot(self):
        for field in ('base', 'head'):
            snapshot = pr()
            if field == 'base':
                snapshot['base']['sha'] = 'c' * 40
            else:
                snapshot['head']['ref'] = 'other'
            with self.assertRaises(ValueError):
                chain.check_snapshot(snapshot, state()['request'])

    def test_stage_records_id_before_submission_and_checks_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, journal = state(), Path(directory) / 'state.json'
            rid = saved['chain_id'] + '-review'
            output = dict(review_output(), run_id=rid)
            def submit(args):
                durable = json.loads(journal.read_text())
                self.assertEqual(rid, durable['active_run_id'])
                self.assertIn(rid, args)
                return {'run_id': rid}
            with patch.object(chain, 'json_command', side_effect=submit), patch.object(chain, 'command'), patch.object(chain, 'workflow_result', return_value={'run_id': rid, 'state': 'completed', 'output': output}):
                self.assertEqual(output, chain.execute_stage('github-pr-review', {'repository': 'owner/repo'}, saved, journal, Path('/review')))
            self.assertIsNone(json.loads(journal.read_text())['active_run_id'])
            with patch.object(chain, 'command'), patch.object(chain, 'workflow_result', return_value={'run_id': rid, 'state': 'failed'}), patch.object(chain, 'json_command') as submit:
                with self.assertRaises(RuntimeError):
                    chain.execute_stage('github-pr-review', {'repository': 'owner/repo'}, saved, journal, Path('/review'))
                submit.assert_not_called()

    def test_resume_never_submits_a_new_id_or_restarts_an_existing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, journal = state(), Path(directory) / 'state.json'
            rid = saved['chain_id'] + '-apply'
            saved['apply_run_id'] = rid
            result = {'run_id': rid, 'state': 'completed', 'output': dict(apply_output(), run_id=rid)}
            with patch.object(chain, 'workflow_result', return_value=result), patch.object(chain, 'command'), patch.object(chain, 'json_command') as submit:
                chain.execute_stage('github-pr-apply', {'repository': 'owner/repo'}, saved, journal, Path('/apply'))
                submit.assert_not_called()
            with patch.object(chain, 'workflow_result', side_effect=[None, result]), patch.object(chain, 'command'), patch.object(chain, 'json_command', return_value={'run_id': rid}) as submit:
                chain.execute_stage('github-pr-apply', {'repository': 'owner/repo'}, saved, journal, Path('/apply'))
                self.assertEqual(1, submit.call_count)
                self.assertIn(rid, submit.call_args.args[0])

    def test_unknown_transport_failure_cannot_authorize_resubmission(self):
        response = subprocess.CompletedProcess([], 1, '', 'could not reach cao-server')
        with patch.object(chain, 'command', return_value=response), self.assertRaises(RuntimeError):
            chain.workflow_result('run', missing_ok=True)

    def test_repository_policy_is_checked_before_reading_the_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'policy.json'
            config.write_text(json.dumps({'schema_version': 1, 'repositories': {'owner/repo': policy()}}))
            config.chmod(0o600)
            argv = ['review_apply.py', '--repository', 'forbidden/repo', '--pr', '1', '--policy', str(config),
                    '--state-root', str(Path(directory) / 'state')]
            with patch.object(sys, 'argv', argv), patch.object(chain, 'api') as api, self.assertRaises(ValueError):
                chain.main()
            api.assert_not_called()

    def test_cancel_forwards_only_active_id_and_observes_child_state(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, journal = state(), Path(directory) / 'state.json'
            saved['active_run_id'] = saved['chain_id'] + '-review'
            with patch.object(chain, 'command') as command, patch.object(chain, 'workflow_result', return_value={'state': 'cancelled'}):
                chain.cancel_active(saved, journal)
                command.assert_called_once_with(['cao', 'workflow', 'cancel', saved['active_run_id']], check=False)
            self.assertEqual('cancelled', json.loads(journal.read_text())['child_state'])


if __name__ == '__main__':
    unittest.main()
