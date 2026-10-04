import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_apply import SHA, BASE, pr, policy, apply
from test_review_apply import state as chain_state
from test_workflow import workflow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automation as auto
import pr_resources
import review_apply as chain


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps({'schema_version': 1, 'repositories': {'owner/repo': policy()}}))
        self.policy.chmod(0o600)
        self.config = {'schema_version': 1, 'state_root': str(self.root / 'state'),
                       'sources': [{'id': 'prs', 'type': 'github_pull_requests', 'repository': 'owner/repo', 'interval_seconds': 1}],
                       'bindings': [{'id': 'review', 'source_id': 'prs', 'kind': 'pull_request_revision',
                                     'handler': 'pr_review_apply', 'policy_path': str(self.policy)}]}
        self.config_path = self.root / 'config.json'
        self.save_config()
        self.store = auto.Store(self.config['state_root'])
        self.addCleanup(self.store.close)
        self.resource_patch = patch.object(pr_resources, 'identity', return_value=[{'kind': 'workflows', 'name': 'github-pr-review', 'version': 'v7', 'sha256': 'review'},
                                                                                  {'kind': 'workflows', 'name': 'github-pr-apply', 'version': 'v2', 'sha256': 'apply'}])
        self.resource_patch.start()
        self.addCleanup(self.resource_patch.stop)
        for name, value in [('deployed_workflow', Path('/owned/deployed')), ('codex_review_profile_ok', True), ('codex_apply_profile_ok', True)]:
            patcher = patch.object(auto.manage, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def reset_poll(self):
        with self.store.db:
            value = self.store.source_state('prs')
            value['next_poll'] = 0
            self.store.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', ('prs', json.dumps(value)))

    def poll(self, rows=None, dry=False):
        self.reset_poll()
        with patch.object(auto, 'github_page', return_value=(200, {'etag': '"page"'}, [pr()] if rows is None else rows)):
            return auto.poll(self.config, self.store, dry_run=dry)

    def job(self):
        return self.store.job(self.store.db.execute('SELECT id FROM jobs ORDER BY created LIMIT 1').fetchone()[0])

    def test_initial_existing_pr_dedup_and_bounded_cache(self):
        row = dict(pr(), title='secret', body='secret')
        result = self.poll([row])
        self.assertEqual(1, len(result['proposed_jobs']))
        self.assertEqual([], self.poll([dict(row, title='changed')])['proposed_jobs'])
        raw = self.store.db.execute('SELECT state FROM sources').fetchone()[0]
        self.assertNotIn('secret', raw)
        self.assertEqual(0o600, (self.store.root / 'automation.sqlite3').stat().st_mode & 0o777)
        self.assertEqual(0o700, self.store.root.stat().st_mode & 0o777)

    def test_all_pages_and_mixed_304_use_cached_remaining_pages(self):
        hundred = [dict(pr(), number=n) for n in range(1, 101)]
        with patch.object(auto, 'github_page', side_effect=[(200, {'etag': 'one'}, hundred), (200, {'etag': 'two'}, [dict(pr(), number=101)])]):
            result = auto.poll(self.config, self.store)
        self.assertEqual(101, len(result['proposed_jobs']))
        self.reset_poll()
        changed = dict(pr('c' * 40), number=101)
        with patch.object(auto, 'github_page', side_effect=[(304, {}, None), (200, {'etag': 'new'}, [changed])]) as query:
            result = auto.poll(self.config, self.store)
        self.assertEqual(2, query.call_count)
        self.assertEqual('two', query.call_args.args[2])
        self.assertEqual(1, len(result['proposed_jobs']))

    def test_short_page_with_next_link_still_collects_remaining_page(self):
        with patch.object(auto, 'github_page', side_effect=[(200, {'link': '<https://api.github.com/page=2>; rel="next"'}, [pr()]),
                                                          (200, {}, [dict(pr(), number=2)])]) as query:
            result = auto.poll(self.config, self.store)
        self.assertEqual(2, query.call_count)
        self.assertEqual(2, len(result['proposed_jobs']))

    def test_second_page_failure_does_not_advance_checkpoint_or_lose_jobs(self):
        self.poll()
        self.reset_poll()
        old = self.store.source_state('prs')['pages']
        hundred = [dict(pr(), number=n) for n in range(1, 101)]
        with patch.object(auto, 'github_page', side_effect=[(200, {}, hundred), auto.CollectionError('rate_limit', 9999999999)]):
            result = auto.poll(self.config, self.store)
        checkpoint = self.store.source_state('prs')
        self.assertEqual(old, checkpoint['pages'])
        self.assertEqual(9999999999, checkpoint['next_poll'])
        self.assertEqual('error', result['sources']['prs'])
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_closure_draft_fork_and_nonpolicy_branches_excluded(self):
        self.poll()
        self.poll([])
        self.assertEqual('skipped', self.job()['state'])
        for mutation in ('draft', 'fork', 'branch'):
            value = pr()
            if mutation == 'draft': value['draft'] = True
            if mutation == 'fork': value['head']['repo']['full_name'] = 'other/repo'
            if mutation == 'branch': value['head']['ref'] = 'main'
            self.assertEqual([], self.poll([value])['proposed_jobs'])

    def test_policy_resource_and_base_changes_create_distinct_identity(self):
        first = self.poll()['proposed_jobs'][0]['job_id']
        data = json.loads(self.policy.read_text())
        data['repositories']['owner/repo']['editable_paths'].append('other/**')
        self.policy.write_text(json.dumps(data))
        second = self.poll()['proposed_jobs'][0]['job_id']
        self.assertNotEqual(first, second)
        changed = pr()
        changed['base']['sha'] = 'd' * 40
        third = self.poll([changed])['proposed_jobs'][0]['job_id']
        self.assertNotEqual(second, third)
        with patch.object(pr_resources, 'identity', return_value=['changed']):
            fourth = self.poll([changed])['proposed_jobs'][0]['job_id']
        self.assertNotEqual(third, fourth)

    def test_dry_run_does_not_create_or_modify_durable_state(self):
        self.poll()
        before = (self.store.root / 'automation.sqlite3').read_bytes()
        policies = list((self.store.root / 'policies').iterdir())
        with patch.object(auto, 'github_page', return_value=(200, {}, [pr('c' * 40)])):
            self.assertEqual(0, auto.main(['--config', str(self.config_path), 'poll', '--dry-run']))
        self.assertEqual(before, (self.store.root / 'automation.sqlite3').read_bytes())
        self.assertEqual(policies, list((self.store.root / 'policies').iterdir()))
        fresh = self.root / 'fresh-state'
        self.config['state_root'] = str(fresh)
        self.save_config()
        with patch.object(auto, 'github_page', return_value=(200, {}, [pr()])):
            auto.main(['--config', str(self.config_path), 'poll', '--dry-run'])
        self.assertFalse(fresh.exists())

    def test_worker_exclusion_and_atomic_attempt_assignment(self):
        self.poll()
        with auto.lock(self.store.root, 'worker.lock'):
            with self.assertRaises(BlockingIOError):
                with auto.lock(self.store.root, 'worker.lock'): pass
        with patch.object(chain, 'api', return_value=pr()), patch.object(pr_resources, 'guard_files', return_value={}), patch.object(chain, 'run_retained', return_value={'state': 'reconciling', 'reason': 'transport'}) as execute:
            auto.worker(str(self.config_path), self.store, once=True)
            attempt = dict(self.store.db.execute('SELECT * FROM attempts').fetchone())
            self.assertTrue(Path(attempt['journal']).exists())
            self.assertRegex(attempt['chain_id'], r'^[0-9a-f]{32}$')
            auto.worker(str(self.config_path), self.store, once=True)
        self.assertEqual(2, execute.call_count)
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0])
        self.assertEqual(attempt['chain_id'], chain.load_state(Path(attempt['journal']))['chain_id'])

    def test_atomic_claim_rejects_double_claim_and_second_global_execution(self):
        self.poll([pr(), dict(pr(), number=2)])
        ids = [r[0] for r in self.store.db.execute('SELECT id FROM jobs')]
        first = self.store.job(ids[0])
        auto.prepare(self.store, first)
        with self.assertRaises(ValueError): auto.prepare(self.store, first)
        with self.assertRaises(auto.sqlite3.IntegrityError): auto.prepare(self.store, self.store.job(ids[1]))
        self.assertEqual('queued', self.store.job(ids[1])['state'])
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0])

    def test_admission_supersedes_stale_or_changed_policy_resource_binding(self):
        self.poll()
        job = self.job()
        handler = auto.ReviewApply
        context = {'config': self.config}
        with patch.object(chain, 'api', return_value=pr('c' * 40)):
            self.assertEqual('superseded', handler.admit(job, context))
        with patch.object(chain, 'api', return_value=dict(pr(), draft=True)):
            self.assertEqual('skipped', handler.admit(job, context))
        with patch.object(chain, 'api', return_value=pr()), patch.object(pr_resources, 'identity', return_value=['new']):
            self.assertEqual('superseded', handler.admit(job, context))
        self.config['bindings'][0]['policy_path'] = '/different'
        self.assertEqual('superseded', handler.admit(job, context))

    def test_missing_running_journal_blocks_and_never_allocates_new_id(self):
        self.poll()
        job = self.job()
        attempt = auto.prepare(self.store, job)
        with self.store.db:
            self.store.db.execute("UPDATE attempts SET state='running'")
        result = auto.process_job(self.config, self.store, self.store.job(job['id']))
        self.assertEqual('blocked', result['state'])
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0])

    def test_crash_after_claim_before_launch_reuses_assigned_execution(self):
        self.poll()
        job = self.job()
        attempt = auto.prepare(self.store, job)
        with patch.object(pr_resources, 'guard_files', return_value={}), patch.object(chain, 'run_retained', return_value={'state': 'reconciling'}) as execute:
            auto.process_job(self.config, self.store, self.store.job(job['id']), str(self.config_path))
        self.assertEqual(attempt['chain_id'], execute.call_args.args[0]['chain_id'])
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0])

    def test_verified_output_suppresses_pending_exact_revision_only(self):
        self.poll()
        original = self.job()
        self.store.db.execute("UPDATE jobs SET state='running' WHERE id=?", (original['id'],))
        self.store.db.commit()
        output_sha = 'c' * 40
        follow = self.poll([pr(output_sha)])['proposed_jobs'][0]['job_id']
        self.store.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (original['id'],))
        self.store.db.commit()
        output = {'state': 'completed', 'execution': {'apply_result': {'apply_mode': 'push', 'result': 'applied', 'commit_sha': output_sha}}}
        with patch.object(chain, 'api', return_value=pr()), patch.object(pr_resources, 'guard_files', return_value={}), patch.object(chain, 'run_retained', return_value=output):
            auto.process_job(self.config, self.store, self.store.job(original['id']))
        self.assertEqual('superseded', self.store.job(follow)['state'])
        self.assertEqual([], self.poll([pr(output_sha)])['proposed_jobs'])
        self.assertEqual(1, len(self.poll([pr('d' * 40)])['proposed_jobs']))

    def test_forged_bot_or_commit_marker_has_no_skip_authority(self):
        value = dict(pr(), user={'login': 'bot'}, body='CAO-Apply-Key: forged')
        self.assertEqual(1, len(self.poll([value])['proposed_jobs']))

    def test_terminal_failure_never_automatically_creates_another_attempt(self):
        self.poll()
        with patch.object(chain, 'api', return_value=pr()), patch.object(pr_resources, 'guard_files', return_value={}), patch.object(chain, 'run_retained', return_value={'state': 'failed', 'execution': {'retry_safe': False}}):
            auto.worker(str(self.config_path), self.store, once=True)
            auto.worker(str(self.config_path), self.store, once=True)
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0])

    def test_retry_reconciles_previous_attempt_and_preserves_history(self):
        self.poll()
        with patch.object(chain, 'api', return_value=pr()), patch.object(pr_resources, 'guard_files', return_value={}), patch.object(chain, 'run_retained', return_value={'state': 'failed', 'execution': {'retry_safe': True}}):
            auto.worker(str(self.config_path), self.store, once=True)
            jid = self.job()['id']
            auto.retry(self.config, self.store, jid)
            auto.worker(str(self.config_path), self.store, once=True)
        rows = self.store.db.execute('SELECT * FROM attempts').fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual('failed', rows[0]['state'])
        self.assertNotEqual(rows[0]['chain_id'], rows[1]['chain_id'])

    def test_successful_last_page_rate_limit_defers_next_collection(self):
        with patch.object(auto, 'github_page', return_value=(200, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '9999999999'}, [pr()])):
            auto.poll(self.config, self.store)
        self.assertEqual(9999999999, self.store.source_state('prs')['next_poll'])

    def test_cached_304_without_a_page_blocks_collection(self):
        with patch.object(auto, 'github_page', return_value=(304, {}, None)):
            result = auto.poll(self.config, self.store)
        self.assertEqual('error', result['sources']['prs'])
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_configuration_forbids_commands_unowned_files_and_duplicate_triggers(self):
        self.config['sources'][0]['command'] = 'evil'
        self.save_config()
        with self.assertRaises(ValueError): auto.configuration(self.config_path)
        del self.config['sources'][0]['command']
        self.config['bindings'].append(dict(self.config['bindings'][0], id='other'))
        self.save_config()
        with self.assertRaises(ValueError): auto.configuration(self.config_path)
        self.config_path.chmod(0o644)
        with self.assertRaises(ValueError): auto.configuration(self.config_path)

    def test_rate_limits_retry_after_and_transient_backoff(self):
        for response, minimum in [('HTTP/2.0 429 Too Many Requests\nRetry-After: 120\n\n{}', 1000120),
                                  ('HTTP/2.0 403 Forbidden\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1000900\n\n{}', 1000900),
                                  ('HTTP/2.0 503 Unavailable\n\n{}', 1000060)]:
            with patch.object(auto.time, 'time', return_value=1000000), patch.object(auto.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, response, 'token-not-logged')):
                with self.assertRaises(auto.CollectionError) as raised: auto.github_page('owner/repo', 1)
            self.assertGreaterEqual(raised.exception.retry_at, minimum)
            self.assertNotIn('token', str(raised.exception))

    def test_generic_registered_source_handler_and_future_tick_contract(self):
        ticks = [1]
        class TickSource:
            @staticmethod
            def validate(source): pass
            @staticmethod
            def collect(context):
                source = context['source']
                return [{'source_id': source['id'], 'kind': 'tick', 'subject': 'monitor', 'revision': {'bucket': ticks[0]},
                         'observed_at': 1, 'data': {}}], {'health': 'ok', 'tick_observed': ticks[0]}
        class TickHandler:
            @staticmethod
            def validate(binding, config):
                if Path(binding['policy_path']).read_text() == 'standalone':
                    raise ValueError('Standalone automatic trigger already owns monitor')
            @staticmethod
            def freeze(event, binding, context):
                return {'event': event, 'binding': binding, 'policy_digest': 'p', 'resources': []}
            @staticmethod
            def admit(job, context): return 'running'
            @staticmethod
            def execute(job, context): return {'state': 'completed', 'journal_id': context['attempt']['chain_id']}
            @staticmethod
            def output_revision(job, result): return None
            @staticmethod
            def health(binding, context): return {'actual_ingestion_lag_seconds': 1000}
        self.config['sources'] = [{'id': 'ticks', 'type': 'test_ticks'}]
        self.config['bindings'] = [{'id': 'monitor', 'source_id': 'ticks', 'kind': 'tick', 'handler': 'test_monitor', 'policy_path': str(self.policy)}]
        self.save_config()
        with patch.dict(auto.SOURCES, test_ticks=TickSource), patch.dict(auto.HANDLERS, test_monitor=TickHandler):
            auto.configuration(self.config_path)
            first = auto.poll(self.config, self.store)['proposed_jobs'][0]['job_id']
            ticks[0] = 2
            auto.poll(self.config, self.store)
            self.assertEqual('superseded', self.store.job(first)['state'])
            report = auto.status(self.config, self.store)
            self.assertEqual('ok', report['sources']['ticks']['health'])
            self.assertEqual(1000, report['handler_health']['monitor']['actual_ingestion_lag_seconds'])
            auto.worker(str(self.config_path), self.store, once=True)
            self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM jobs WHERE state='completed'").fetchone()[0])
            self.policy.write_text('standalone')
            with self.assertRaises(ValueError): auto.configuration(self.config_path)


class ExecutionGateTests(unittest.TestCase):
    def test_dependency_identity_ignores_unrelated_manifest_entries(self):
        original = auto.manage.load_manifest()
        changed = copy.deepcopy(original)
        changed['workflows'].append({'name': 'unrelated', 'version': 'v20', 'source': 'unrelated.py', 'agents': ['unrelated-agent']})
        changed['agents'].append({'name': 'unrelated-agent', 'source': 'unrelated.md'})
        with patch.object(pr_resources.manage, 'digest', side_effect=lambda path: 'hash:' + str(path)), patch.object(pr_resources.manage, 'load_manifest', return_value=original):
            first = pr_resources.identity()
        with patch.object(pr_resources.manage, 'digest', side_effect=lambda path: 'hash:' + str(path)), patch.object(pr_resources.manage, 'load_manifest', return_value=changed):
            second = pr_resources.identity()
        self.assertEqual(first, second)
        changed['agents'][0]['source'] = 'changed.md'
        with patch.object(pr_resources.manage, 'digest', side_effect=lambda path: 'hash:' + str(path)), patch.object(pr_resources.manage, 'load_manifest', return_value=changed):
            self.assertNotEqual(first, pr_resources.identity())

    def journal(self, root):
        saved = chain_state()
        resource = root / 'workflow.py'
        resource.write_text('frozen code')
        private = root / 'policy.json'
        private.write_text(json.dumps({'schema_version': 1, 'repositories': {'owner/repo': policy()}}))
        private.chmod(0o600)
        current = root / 'current.json'
        current.write_bytes(private.read_bytes()); current.chmod(0o600)
        saved['request'].update(policy_path=str(private), authorized_policy_path=str(current),
                                policy_digest=auto.manage.digest(private), resources=[], resource_digest=pr_resources.fingerprint([]),
                                guard_files={str(resource): auto.manage.digest(resource)})
        path = root / 'journal.json'
        auto.manage.atomic_json(path, saved)
        inputs = {'repository': 'owner/repo', 'pr_number': 1, 'expected_head_sha': SHA, 'expected_base_sha': BASE,
                  'execution_state_path': str(path), 'expected_policy_digest': saved['request']['policy_digest'],
                  'expected_resource_digest': saved['request']['resource_digest'], 'authorized_policy_path': str(current)}
        return saved, path, inputs, resource, current

    def test_child_admission_and_prepublication_reject_policy_resource_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            with patch.object(workflow, '__file__', str(resource)), patch.dict(os.environ, CAO_WORKFLOW_RUN_ID=saved['chain_id'] + '-review'):
                workflow.automation_guard(inputs)
                current.write_text('{}')
                with self.assertRaises(ValueError): workflow.automation_guard(inputs)
                current.write_bytes(Path(saved['request']['policy_path']).read_bytes())
                resource.write_text('changed')
                with self.assertRaises(ValueError): workflow.automation_guard(inputs)

    def test_child_rejects_journal_run_or_expectation_swapping(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            with patch.object(workflow, '__file__', str(resource)), patch.dict(os.environ, CAO_WORKFLOW_RUN_ID='foreign-review'):
                with self.assertRaises(ValueError): workflow.automation_guard(inputs)
            with self.assertRaises(ValueError): workflow.automation_guard({'expected_policy_digest': 'p'})

    def test_running_binding_change_blocks_child_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved, path, inputs, resource, current = self.journal(root)
            binding = {'id': 'b', 'source_id': 's', 'kind': 'pull_request_revision', 'handler': 'pr_review_apply', 'policy_path': str(current)}
            config = {'state_root': str(root), 'bindings': [binding], 'sources': [{'id': 's', 'type': 'github_pull_requests', 'repository': 'owner/repo'}]}
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
            saved['request'].update(automation_config_path=str(config_path), automation_state_root=str(root), binding=binding)
            auto.manage.atomic_json(path, saved)
            with patch.object(workflow, '__file__', str(resource)), patch.dict(os.environ, CAO_WORKFLOW_RUN_ID=saved['chain_id'] + '-review'):
                workflow.automation_guard(inputs)
                config['bindings'] = []
                config_path.write_text(json.dumps(config))
                with self.assertRaises(ValueError): workflow.automation_guard(inputs)

    def test_expectations_forwarded_to_both_children(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            from test_review_apply import review_output, apply_output
            from test_apply import review
            outputs = [review_output(), apply_output()]
            def execute(name, values, *rest):
                self.assertEqual(str(path), values['execution_state_path'])
                self.assertEqual(inputs['expected_policy_digest'], values['expected_policy_digest'])
                self.assertEqual(inputs['expected_resource_digest'], values['expected_resource_digest'])
                return outputs.pop(0)
            with patch.object(chain, 'api', return_value=pr()), patch.object(chain, 'execute_stage', side_effect=execute), patch.object(chain, 'eligible_review', return_value=review()), patch.object(chain, 'pages', return_value=[{}]):
                chain.orchestrate(saved, path, policy(), {'github-pr-review': Path('/review'), 'github-pr-apply': Path('/apply')})

    def test_completed_apply_reconciles_even_when_configuration_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            saved['apply_run_id'] = saved['chain_id'] + '-apply'
            output = {'workflow': 'github-pr-apply', 'version': 'v2', 'repository': 'owner/repo', 'pr': 1,
                      'head_sha': SHA, 'review_id': 12, 'apply_mode': 'patch', 'run_id': saved['apply_run_id'], 'result': 'applied'}
            with patch.object(chain, 'workflow_result', return_value={'state': 'completed', 'output': output}), patch.object(chain, 'preflight', side_effect=AssertionError('must reconcile first')):
                self.assertEqual('completed', chain.run_retained(saved, path)['state'])

    def test_crash_after_push_reconciles_frozen_publication_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            saved['request']['apply_mode'] = 'push'
            saved['apply_run_id'] = saved['chain_id'] + '-apply'
            saved['apply_publication'] = {'workflow': 'github-pr-apply', 'version': 'v2', 'repository': 'owner/repo', 'pr': 1,
                                         'head_sha': SHA, 'review_id': 12, 'apply_mode': 'push', 'run_id': saved['apply_run_id'],
                                         'result': 'applied', 'commit_sha': 'c' * 40, 'apply_key': 'key'}
            auto.manage.atomic_json(path, saved)
            commit = {'sha': 'c' * 40, 'parents': [{'sha': SHA}], 'commit': {'message': 'CAO-Apply-Run: ' + saved['apply_run_id'] + '\nCAO-Apply-Key: key\nCAO-Review-ID: 12'}}
            with patch.object(chain, 'workflow_result', return_value={'state': 'failed'}), patch.object(chain, 'api', side_effect=[pr('c' * 40), commit]), patch.object(chain, 'preflight', side_effect=AssertionError('new writes forbidden')):
                result = chain.run_retained(saved, path)
            self.assertEqual('completed', result['state'])
            self.assertTrue(result['execution']['recovered_push'])

    def test_transport_failure_keeps_same_id_reconciling(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            saved['apply_run_id'] = saved['chain_id'] + '-apply'
            with patch.object(chain, 'workflow_result', side_effect=RuntimeError('transport')), patch.object(chain, 'preflight') as preflight:
                self.assertEqual('reconciling', chain.run_retained(saved, path)['state'])
            preflight.assert_not_called()

    def test_cao_result_without_run_output_uses_private_terminal_output(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            rid = saved['chain_id'] + '-review'
            output = {'workflow': 'github-pr-review', 'run_id': rid, 'repository': 'owner/repo', 'version': 'v7', 'results': []}
            with patch.dict(os.environ, CAO_WORKFLOW_RUN_ID=rid):
                workflow.automation_record_output(inputs, output)
            with patch.object(chain, 'workflow_result', return_value={'run_id': rid, 'state': 'completed', 'steps': []}):
                actual = chain.retained_stage_result(rid, saved, path)
            self.assertEqual(output, actual['output'])
            self.assertEqual(output, saved['child_outputs'][rid])
            with patch.dict(os.environ, CAO_WORKFLOW_RUN_ID=rid), self.assertRaises(ValueError):
                workflow.automation_record_output(inputs, dict(output, results=[{'forged': True}]))

    def test_stale_coordinator_write_preserves_child_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            rid = saved['chain_id'] + '-review'
            output = {'workflow': 'github-pr-review', 'run_id': rid, 'repository': 'owner/repo', 'version': 'v7', 'results': []}
            with patch.dict(os.environ, CAO_WORKFLOW_RUN_ID=rid):
                workflow.automation_record_output(inputs, output)
            saved['active_run_id'] = None
            chain.persist_state(path, saved)
            self.assertEqual(output, chain.load_state(path)['child_outputs'][rid])

    def test_cao_completed_without_retained_output_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            saved, path, inputs, resource, current = self.journal(Path(directory))
            saved['review_run_id'] = saved['chain_id'] + '-review'
            auto.manage.atomic_json(path, saved)
            with patch.object(chain, 'workflow_result', return_value={'run_id': saved['review_run_id'], 'state': 'completed', 'steps': []}), patch.object(chain, 'preflight') as preflight:
                self.assertEqual('blocked', chain.run_retained(saved, path)['state'])
                preflight.assert_not_called()

    def test_launcher_forwards_all_frozen_chain_arguments(self):
        argv = ['github-pr-review', '--repository', 'owner/repo', '--pr', '1', '--policy', '/operator/policy.json', '--apply',
                '--state-root', '/operator/chains', '--chain-id', '1' * 32, '--expected-head-sha', SHA,
                '--expected-base-sha', BASE, '--expected-policy-digest', '2' * 64, '--expected-resource-digest', '3' * 64]
        with patch.object(auto.manage.os, 'execv', side_effect=SystemExit) as execute, self.assertRaises(SystemExit):
            auto.manage.run(argv)
        actual = execute.call_args.args[1]
        for flag in ('--state-root', '--chain-id', '--expected-head-sha', '--expected-base-sha', '--expected-policy-digest', '--expected-resource-digest'):
            self.assertIn(flag, actual)


if __name__ == '__main__':
    unittest.main()
