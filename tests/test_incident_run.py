import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import incident_run as coordinator


def state():
    return {'workflow': 'github-issue-fix', 'execution_id': 'a' * 32, 'run_ids': ['a' * 32 + '-run'],
            'inputs': {'repository': 'owner/repo', 'issue_number': 1, 'recovery_only': False}}


def result(saved, status='pushed'):
    return {'run_id': saved['run_ids'][-1], 'state': 'completed',
            'output': {'workflow': saved['workflow'], 'repository': 'owner/repo', 'run_id': saved['run_ids'][-1], 'status': status}}


class CoordinatorTests(unittest.TestCase):
    def test_only_confirmed_unknown_run_resubmits_same_recorded_id(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            saved = state()
            with patch.object(coordinator.review_apply, 'workflow_result', side_effect=[None, result(saved)]), patch.object(coordinator.review_apply, 'json_command', return_value={'run_id': saved['run_ids'][0]}) as submit, patch.object(coordinator.review_apply, 'command'):
                coordinator.submit(saved, journal, Path('/deployed.py'))
            self.assertIn(saved['run_ids'][0], submit.call_args.args[0])
            self.assertEqual(1, len(saved['run_ids']))

    def test_transport_failure_never_submits_or_allocates_id(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = state()
            with patch.object(coordinator.review_apply, 'workflow_result', side_effect=RuntimeError('unavailable')), patch.object(coordinator.review_apply, 'json_command') as submit:
                with self.assertRaises(RuntimeError):
                    coordinator.submit(saved, Path(directory) / 'journal.json', Path('/deployed.py'))
                submit.assert_not_called()
                self.assertEqual(1, len(saved['run_ids']))

    def test_completed_pending_push_records_recovery_only_child_before_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            saved = state()
            pending = result(saved, 'pushed_comment_pending')
            def submit(args):
                recorded = json.loads(journal.read_text())
                self.assertTrue(recorded['inputs']['recovery_only'])
                self.assertIn(recorded['run_ids'][-1], args)
                return {'run_id': recorded['run_ids'][-1]}
            def lookup(rid, **kwargs):
                if rid.endswith('-run'):
                    return pending
                if kwargs.get('missing_ok'):
                    return None
                return result(saved)
            with patch.object(coordinator.review_apply, 'workflow_result', side_effect=lookup), patch.object(coordinator.review_apply, 'json_command', side_effect=submit), patch.object(coordinator.review_apply, 'command'):
                coordinator.submit(saved, journal, Path('/deployed.py'))
            self.assertEqual(2, len(saved['run_ids']))
            self.assertTrue(saved['inputs']['recovery_only'])

    def test_terminal_failed_run_never_restarts_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(coordinator.review_apply, 'workflow_result', return_value={'state': 'failed'}), patch.object(coordinator.review_apply, 'json_command') as submit:
                with self.assertRaises(ValueError):
                    coordinator.submit(state(), Path(directory) / 'journal.json', Path('/deployed.py'))
                submit.assert_not_called()
