#!/usr/bin/env python3
"""Local PR polling: registered Sources/Handlers, durable dispatch and recovery."""
from __future__ import annotations

import argparse
import contextlib
import email.utils
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import manage
import pr_resources
import review_apply as chain

ACTIVE = ('queued', 'running', 'reconciling')
TERMINAL = ('completed', 'skipped', 'superseded', 'failed', 'blocked')
SOURCES = {}
HANDLERS = {}


def private_file(path):
    p = Path(path)
    if not p.is_absolute() or any(x.is_symlink() for x in (p, *p.parents)):
        raise ValueError('Use absolute paths without symlink components')
    info = p.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Configuration/state/policy files require ownership and mode 0600')
    return p


def configuration(path):
    data = json.loads(private_file(path).read_text())
    return validate_configuration(data)


def validate_configuration(data):
    if set(data) != {'schema_version', 'state_root', 'sources', 'bindings'} or data['schema_version'] != 1:
        raise ValueError('Invalid automation configuration schema')
    root = Path(data['state_root'])
    if not root.is_absolute() or any(x.is_symlink() for x in (root, *root.parents)):
        raise ValueError('State root must be absolute without symlinks')
    if root.resolve().is_relative_to(manage.ROOT):
        raise ValueError('Keep production state outside the checkout')
    seen, repositories = set(), set()
    for source in data['sources']:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source['id']) or source['id'] in seen or source['type'] not in SOURCES:
            raise ValueError('Invalid/duplicate/unregistered Source')
        seen.add(source['id'])
        SOURCES[source['type']].validate(source)
        if source['type'] == 'github_pull_requests':
            repository = source['repository'].lower()
            if repository in repositories:
                raise ValueError('Only one automatic PR Source per repository')
            repositories.add(repository)
    bindings, routes = set(), set()
    for binding in data['bindings']:
        if (set(binding) != {'id', 'source_id', 'kind', 'handler', 'policy_path'}
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', binding['id']) or binding['id'] in bindings
                or binding['source_id'] not in seen or binding['handler'] not in HANDLERS):
            raise ValueError('Invalid/duplicate/unregistered Binding')
        bindings.add(binding['id'])
        route = (binding['source_id'], binding['kind'])
        if route in routes:
            raise ValueError('Only one automatic Binding per Source/event kind')
        routes.add(route)
        HANDLERS[binding['handler']].validate(binding, data)
    return data


@contextlib.contextmanager
def lock(root, name):
    path = root / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Invalid automation lock')
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Store:
    def __init__(self, root, *, readonly=False, memory=False):
        self.root = Path(root)
        if self.root.exists():
            chain.private_root(str(self.root))
        if not memory and not readonly:
            chain.private_root(str(self.root))
        path = self.root / 'automation.sqlite3'
        if path.exists():
            private_file(path)
        for suffix in ('-journal', '-wal', '-shm'):
            if Path(str(path) + suffix).exists():
                private_file(str(path) + suffix)
        if memory:
            self.db = sqlite3.connect(':memory:')
            if path.exists():
                with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
                    source.backup(self.db)
        elif readonly:
            self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        else:
            # Create the file privately before SQLite can create a broader-mode file.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                private_file(path)
                fd = None
            if fd is not None:
                os.close(fd)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, subject TEXT NOT NULL,
                    snapshot TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, reason TEXT, attempt INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    job_id TEXT NOT NULL, number INTEGER NOT NULL, chain_id TEXT NOT NULL UNIQUE,
                    journal TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(job_id, number));
                CREATE TABLE IF NOT EXISTS handled(
                    identity TEXT PRIMARY KEY, evidence TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution
                    ON jobs ((1)) WHERE state IN ('running','reconciling');
            ''')

    def close(self):
        self.db.close()

    def source_state(self, sid):
        row = self.db.execute('SELECT state FROM sources WHERE id=?', (sid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def job(self, jid):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise ValueError('Unknown job ID')
        value = dict(row)
        value['snapshot'] = json.loads(value['snapshot'])
        return value

    def outcome(self, jid, state, reason='', result=None):
        if state not in (*ACTIVE, *TERMINAL):
            raise ValueError('Invalid Handler result state')
        with self.db:
            self.db.execute('UPDATE jobs SET state=?,reason=?,updated=? WHERE id=?', (state, reason, time.time(), jid))
            self.db.execute('UPDATE attempts SET state=?,result=? WHERE job_id=? AND number=(SELECT attempt FROM jobs WHERE id=?)',
                            (state, json.dumps(result) if result else None, jid, jid))


class CollectionError(RuntimeError):
    def __init__(self, reason, retry_at):
        super().__init__(reason)
        self.retry_at = retry_at


def github_page(repository, page, etag=None):
    args = ['gh', 'api', '--include', '--method', 'GET',
            f'repos/{repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page={page}',
            '-H', 'Accept: application/vnd.github+json']
    if etag:
        if not isinstance(etag, str) or len(etag) > 512 or any(c in etag for c in '\r\n'):
            raise ValueError('Invalid cached ETag')
        args += ['-H', 'If-None-Match: ' + etag]
    result = subprocess.run(args, text=True, capture_output=True, timeout=60, check=False)
    output = result.stdout.replace('\r\n', '\n')
    if len(output) > 16_000_000:
        raise CollectionError('response_limit', time.time() + 180)
    header, separator, body = output.partition('\n\n')
    lines = header.splitlines()
    match = re.match(r'^HTTP/[\d.]+ (\d{3})', lines[0] if lines else '')
    if not match or not separator:
        raise CollectionError('github_transport', time.time() + 60)
    status = int(match[1])
    headers = {k.lower().strip(): v.strip() for k, v in (line.split(':', 1) for line in lines[1:] if ':' in line)}
    if status not in (200, 304):
        delay = time.time() + 60
        try:
            if 'retry-after' in headers:
                value = headers['retry-after']
                delay = time.time() + int(value) if value.isdigit() else email.utils.parsedate_to_datetime(value).timestamp()
            if status in (403, 429) and headers.get('x-ratelimit-remaining') == '0':
                delay = max(delay, float(headers.get('x-ratelimit-reset', delay)))
        except (ValueError, TypeError, OverflowError):
            pass
        raise CollectionError('github_http_' + str(status), max(time.time() + 1, delay))
    if result.returncode and status != 304:
        raise CollectionError('github_transport', time.time() + 60)
    try:
        rows = json.loads(body) if status == 200 else None
    except ValueError:
        raise CollectionError('github_invalid_json', time.time() + 60) from None
    return status, headers, rows


class PullRequests:
    @staticmethod
    def validate(source):
        if (set(source) - {'id', 'type', 'repository', 'interval_seconds', 'max_pages'}
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source.get('repository', ''))
                or '..' in source['repository'] or type(source.get('interval_seconds', 180)) is not int
                or source.get('interval_seconds', 180) < 1 or type(source.get('max_pages', 100)) is not int
                or not 1 <= source.get('max_pages', 100) <= 1000):
            raise ValueError('Invalid PR Source options')

    @staticmethod
    def collect(context):
        source, checkpoint = context['source'], context['checkpoint']
        cache, fresh, events = checkpoint.get('pages', {}), {}, []
        rate_reset = 0
        for page in range(1, source.get('max_pages', 100) + 1):
            old = cache.get(str(page), {})
            status, headers, rows = github_page(source['repository'], page, old.get('etag'))
            if headers.get('x-ratelimit-remaining') == '0':
                try:
                    rate_reset = max(rate_reset, float(headers.get('x-ratelimit-reset', 0)))
                except ValueError:
                    pass
            if status == 304:
                if not old:
                    raise CollectionError('uncached_304', time.time() + 60)
                entry = dict(old)
            else:
                if not isinstance(rows, list) or len(rows) > 100:
                    raise CollectionError('invalid_pr_page', time.time() + 60)
                # Persist bounded identity metadata only, never PR bodies/comments.
                normalized = []
                for pr in rows:
                    try:
                        item = {k: pr[k] for k in ('number', 'state', 'draft')}
                        item.update(head_sha=pr['head']['sha'], base_sha=pr['base']['sha'],
                                    head_ref=pr['head']['ref'], head_repository=(pr['head'].get('repo') or {}).get('full_name', ''),
                                    base_repository=(pr['base'].get('repo') or {}).get('full_name', ''))
                        if (type(item['number']) is not int or item['number'] <= 0
                                or not all(re.fullmatch(r'[0-9a-f]{40}', item[k]) for k in ('head_sha', 'base_sha'))
                                or not isinstance(item['head_ref'], str) or len(item['head_ref']) > 1024):
                            raise ValueError()
                        normalized.append(item)
                    except (KeyError, TypeError, ValueError):
                        raise CollectionError('invalid_pr_identity', time.time() + 60) from None
                more = 'rel="next"' in headers['link'] if 'link' in headers else len(rows) == 100
                entry = {'etag': headers.get('etag'), 'rows': normalized, 'more': more}
            fresh[str(page)] = entry
            for pr in entry['rows']:
                events.append({'source_id': source['id'], 'kind': 'pull_request_revision',
                               'subject': source['repository'].lower() + '#' + str(pr['number']),
                               'revision': {'head_sha': pr['head_sha'], 'base_sha': pr['base_sha']},
                               'observed_at': time.time(), 'data': dict(pr, repository=source['repository'].lower())})
            if not entry['more']:
                return events, {'pages': fresh, 'last_success': time.time(), 'next_poll': max(rate_reset, time.time() + source.get('interval_seconds', 180)),
                                'failures': 0, 'health': 'ok'}
        raise CollectionError('page_limit_incomplete', time.time() + 180)


def job_identity(snapshot, revision=None):
    event = snapshot['event']
    return pr_resources.fingerprint({'source_id': event['source_id'], 'kind': event['kind'], 'subject': event['subject'],
                                     'revision': revision or event['revision'], 'binding': snapshot['binding'],
                                     'policy_digest': snapshot['policy_digest'], 'resources': snapshot['resources']})


class ReviewApply:
    @staticmethod
    def validate(binding, config):
        if binding['kind'] != 'pull_request_revision':
            raise ValueError('PR Handler requires pull_request_revision')
        source = next(s for s in config['sources'] if s['id'] == binding['source_id'])
        path = Path(binding['policy_path'])
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Binding policy requires an absolute nonsymlink path')

    @staticmethod
    def freeze(event, binding, context):
        pr = event['data']
        policy = chain.operator_policy(binding['policy_path'], pr['repository'])
        if (pr['state'] != 'open' or pr['draft'] or pr['head_repository'].lower() != pr['repository']
                or pr['base_repository'].lower() != pr['repository']
                or not any(fnmatch.fnmatchcase(pr['head_ref'], pattern) for pattern in policy.get('push_branches', []))):
            return None
        resources = pr_resources.identity()
        raw = private_file(binding['policy_path']).read_bytes()
        if json.loads(raw).get('repositories', {}).get(pr['repository']) != policy:
            raise ValueError('Policy changed during collection')
        snapshot = {'event': event, 'binding': binding, 'policy_digest': hashlib.sha256(raw).hexdigest(),
                    'resources': resources, 'resource_digest': pr_resources.fingerprint(resources)}
        if not context['dry_run']:
            root = chain.private_root(str(context['root'] / 'policies'))
            path = root / (snapshot['policy_digest'] + '.json')
            if path.exists():
                if private_file(path).read_bytes() != raw:
                    raise ValueError('Frozen policy conflict')
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            snapshot['policy_snapshot'] = str(path)
        return snapshot

    @staticmethod
    def invalidated(snapshot, events, context):
        binding = next((b for b in context['config']['bindings'] if b['id'] == snapshot['binding']['id']), None)
        if (binding != snapshot['binding'] or manage.digest(private_file(binding['policy_path'])) != snapshot['policy_digest']
                or pr_resources.fingerprint(pr_resources.identity()) != snapshot['resource_digest']):
            return 'superseded'
        if not any(e['subject'] == snapshot['event']['subject'] for e in events):
            return 'skipped'
        eligible = any(job_identity(s) == job_identity(snapshot) for s in context.get('snapshots', []))
        same_revision = any(e['subject'] == snapshot['event']['subject'] and e['revision'] == snapshot['event']['revision'] for e in events)
        return 'skipped' if same_revision and not eligible else 'superseded'

    @staticmethod
    def admit(job, context):
        frozen = job['snapshot']
        binding = next((b for b in context['config']['bindings'] if b['id'] == frozen['binding']['id']), None)
        source = next((s for s in context['config']['sources'] if s['id'] == frozen['event']['source_id']), None)
        if not binding or binding != frozen['binding'] or not source or source['repository'].lower() != frozen['event']['data']['repository']:
            return 'superseded'
        pr = frozen['event']['data']
        current = chain.api(pr['repository'], f"pulls/{pr['number']}")
        if (current.get('state') != 'open' or current.get('draft')
                or (current.get('head', {}).get('repo') or {}).get('full_name', '').lower() != pr['repository']
                or (current.get('base', {}).get('repo') or {}).get('full_name', '').lower() != pr['repository']):
            return 'skipped'
        policy = chain.operator_policy(binding['policy_path'], pr['repository'])
        if not any(fnmatch.fnmatchcase(current['head']['ref'], p) for p in policy.get('push_branches', [])):
            return 'skipped'
        if (current['head']['sha'] != frozen['event']['revision']['head_sha']
                or current['base']['sha'] != frozen['event']['revision']['base_sha']
                or manage.digest(private_file(binding['policy_path'])) != frozen['policy_digest']
                or pr_resources.fingerprint(pr_resources.identity()) != frozen['resource_digest']):
            return 'superseded'
        for name in pr_resources.NAMES:
            manage.deployed_workflow(name)
        if not manage.codex_review_profile_ok() or not manage.codex_apply_profile_ok():
            raise ValueError('Named read-only Codex profiles are unavailable')
        return 'running'

    @staticmethod
    def execute(job, context):
        attempt = context['attempt']
        frozen, root = job['snapshot'], context['root']
        journal = Path(attempt['journal'])
        if (not re.fullmatch(r'[0-9a-f]{32}', attempt['chain_id'])
                or journal != (root / 'chains' / (attempt['chain_id'] + '.json')).resolve()):
            return {'state': 'blocked', 'reason': 'attempt_location_mismatch'}
        if journal.exists():
            state = chain.load_state(journal)
        elif attempt['state'] == 'prepared':
            pr = frozen['event']['data']
            request = {'repository': pr['repository'], 'pr_number': pr['number'],
                       'head_sha': pr['head_sha'], 'base_sha': pr['base_sha'], 'head_ref': pr['head_ref'],
                       'head_repository': pr['head_repository'], 'policy_path': frozen['policy_snapshot'],
                       'authorized_policy_path': frozen['binding']['policy_path'], 'policy_digest': frozen['policy_digest'],
                       'resources': frozen['resources'], 'resource_digest': frozen['resource_digest'],
                       'versions': {r['name']: r['version'] for r in frozen['resources'] if r['kind'] == 'workflows'},
                       'apply_mode': 'push', 'review_workspace': str(root / 'review-workspace'),
                       'apply_workspace': str(root / 'apply-workspace'), 'model': None, 'force_review': True}
            # Paths are trusted local dependencies; no input PR content may supply paths.
            request['guard_files'] = pr_resources.guard_files()
            request.update(binding=frozen['binding'], automation_config_path=context.get('config_path'),
                           automation_state_root=str(root))
            state = {'schema_version': 1, 'chain_id': attempt['chain_id'], 'request': request, 'result': 'running'}
            manage.atomic_json(journal, state)
        else:
            return {'state': 'blocked', 'reason': 'missing_execution_journal'}
        pr = frozen['event']['data']
        expected = {'repository': pr['repository'], 'pr_number': pr['number'], 'head_sha': pr['head_sha'],
                    'base_sha': pr['base_sha'], 'policy_path': frozen['policy_snapshot'],
                    'authorized_policy_path': frozen['binding']['policy_path'], 'policy_digest': frozen['policy_digest'],
                    'resource_digest': frozen['resource_digest'], 'resources': frozen['resources'], 'apply_mode': 'push'}
        if state['chain_id'] != attempt['chain_id'] or any(state['request'].get(k) != v for k, v in expected.items()):
            return {'state': 'blocked', 'reason': 'journal_identity_mismatch'}
        with context['store'].db:
            context['store'].db.execute("UPDATE attempts SET state='running' WHERE job_id=? AND number=?", (job['id'], attempt['number']))
        result = chain.run_retained(state, journal)
        if result['state'] == 'completed' and state['request']['apply_mode'] == 'push':
            output = result.get('execution', {}).get('apply_result', {})
            if output.get('result') == 'applied' and not output.get('commit_sha'):
                return {'state': 'blocked', 'reason': 'push_result_missing_commit', 'execution': result.get('execution', {})}
        return result

    @staticmethod
    def output_revision(job, result):
        state = result.get('execution', {})
        applied = state.get('apply_result', {})
        if applied.get('commit_sha') and applied.get('apply_mode') == 'push':
            return {'head_sha': applied['commit_sha'], 'base_sha': job['snapshot']['event']['revision']['base_sha']}
        return None


SOURCES['github_pull_requests'] = PullRequests
HANDLERS['pr_review_apply'] = ReviewApply


def poll(config, store, *, dry_run=False):
    proposed = []
    context = {'root': store.root, 'config': config, 'store': store, 'dry_run': dry_run}
    for source in config['sources']:
        prior = store.source_state(source['id'])
        if prior.get('next_poll', 0) > time.time() and not dry_run:
            continue
        try:
            events, checkpoint = SOURCES[source['type']].collect(dict(context, source=source, checkpoint=prior))
            snapshots = []
            for event in events:
                for binding in config['bindings']:
                    if binding['source_id'] == source['id'] and binding['kind'] == event['kind']:
                        snapshot = HANDLERS[binding['handler']].freeze(event, binding, context)
                        if snapshot:
                            snapshots.append(snapshot)
            # Checkpoint, page cache, invalidation, and job insertion commit together.
            with store.db:
                new_ids = {job_identity(s) for s in snapshots}
                for old in store.db.execute("SELECT id,snapshot FROM jobs WHERE source_id=? AND state='queued'", (source['id'],)).fetchall():
                    if old['id'] not in new_ids:
                        frozen = json.loads(old['snapshot'])
                        handler = HANDLERS[frozen['binding']['handler']]
                        outcome = handler.invalidated(frozen, events, dict(context, snapshots=snapshots)) if hasattr(handler, 'invalidated') else 'superseded'
                        store.db.execute("UPDATE jobs SET state=?,reason='collection_changed',updated=? WHERE id=?", (outcome, time.time(), old['id']))
                for snapshot in snapshots:
                    jid = job_identity(snapshot)
                    if store.db.execute('SELECT 1 FROM handled WHERE identity=?', (jid,)).fetchone():
                        continue
                    inserted = store.db.execute('INSERT OR IGNORE INTO jobs(id,source_id,subject,snapshot,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                                                (jid, source['id'], snapshot['event']['subject'], json.dumps(snapshot), 'queued', time.time(), time.time()))
                    if inserted.rowcount:
                        proposed.append({'job_id': jid, 'subject': snapshot['event']['subject'], 'revision': snapshot['event']['revision']})
                store.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (source['id'], json.dumps(checkpoint)))
        except Exception as exc:
            failures = prior.get('failures', 0) + 1
            state = dict(prior, health='error', last_attempt=time.time(), failures=failures,
                         error=str(exc) if isinstance(exc, CollectionError) else type(exc).__name__,
                         next_poll=max(getattr(exc, 'retry_at', 0), time.time() + min(3600, 30 * 2 ** min(failures, 7))))
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (source['id'], json.dumps(state)))
    return {'proposed_jobs': proposed, 'dry_run': dry_run,
            'sources': {s['id']: store.source_state(s['id'])['health'] for s in config['sources']}}


def prepare(store, job):
    number = job['attempt'] + 1
    cid = secrets.token_hex(16)
    root = chain.private_root(str(store.root / 'chains'))
    journal = str(root / (cid + '.json'))
    with store.db:
        claimed = store.db.execute("UPDATE jobs SET state='running',attempt=?,updated=? WHERE id=? AND state='queued'", (number, time.time(), job['id']))
        if claimed.rowcount != 1:
            raise ValueError('Job was already claimed')
        store.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL)', (job['id'], number, cid, journal, 'prepared'))
    return dict(store.db.execute('SELECT * FROM attempts WHERE job_id=? AND number=?', (job['id'], number)).fetchone())


def process_job(config, store, job, config_path=None):
    context = {'config': config, 'root': store.root, 'store': store, 'dry_run': False, 'config_path': config_path}
    handler = HANDLERS[job['snapshot']['binding']['handler']]
    if job['state'] == 'queued':
        try:
            admission = handler.admit(job, context)
        except Exception as exc:
            # A collection/transport problem does not authorize an execution.
            return {'state': 'queued', 'reason': type(exc).__name__}
        if admission != 'running':
            store.outcome(job['id'], admission, 'admission_changed')
            return {'state': admission}
        attempt = prepare(store, job)
    else:
        row = store.db.execute('SELECT * FROM attempts WHERE job_id=? AND number=?', (job['id'], job['attempt'])).fetchone()
        if not row:
            store.outcome(job['id'], 'blocked', 'missing_attempt')
            return {'state': 'blocked'}
        attempt = dict(row)
    try:
        result = handler.execute(job, dict(context, attempt=attempt))
    except Exception as exc:
        result = {'state': 'reconciling', 'reason': type(exc).__name__}
    revision = handler.output_revision(job, result) if result['state'] in ('completed', 'skipped') else None
    # Recording handled output and outcome is one transaction, including pending jobs.
    with store.db:
        if revision:
            jid = job_identity(job['snapshot'], revision)
            store.db.execute('INSERT OR REPLACE INTO handled VALUES(?,?)', (jid, json.dumps({'job_id': job['id'], 'attempt': attempt['number'], 'result': result})))
            store.db.execute("UPDATE jobs SET state='superseded',reason='verified_output',updated=? WHERE id=? AND state='queued'", (time.time(), jid))
        store.db.execute('UPDATE jobs SET state=?,reason=?,updated=? WHERE id=?', (result['state'], result.get('reason', ''), time.time(), job['id']))
        store.db.execute('UPDATE attempts SET state=?,result=? WHERE job_id=? AND number=?',
                         (result['state'], json.dumps(result), job['id'], attempt['number']))
    return result


def worker(config_path, store, *, once=False):
    with lock(store.root, 'worker.lock'):
        while True:
            config = configuration(config_path)
            row = store.db.execute("SELECT id FROM jobs WHERE state IN ('running','reconciling') ORDER BY created LIMIT 1").fetchone()
            if not row:
                row = store.db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                result = process_job(config, store, store.job(row['id']), str(Path(config_path).resolve()))
                print(json.dumps({'job_id': row['id'], 'state': result['state'], 'reason': result.get('reason', '')}), flush=True)
                if once:
                    return
                if result['state'] in ('queued', 'reconciling'):
                    time.sleep(5)
            elif once:
                return
            else:
                time.sleep(5)


def retry(config, store, jid, config_path=None):
    with lock(store.root, 'worker.lock'):
        job = store.job(jid)
        if job['state'] not in ('failed', 'blocked'):
            raise ValueError('Retry requires a failed/blocked job')
        handler = HANDLERS[job['snapshot']['binding']['handler']]
        if job['attempt']:
            # Reconcile using the same journal/IDs before authorizing another attempt.
            result = process_job(config, store, job, config_path)
            if result['state'] not in ('failed',):
                return result
            execution = result.get('execution', {})
            if not execution.get('retry_safe'):
                raise ValueError('Previous publication outcome is unresolved; manual reconciliation required')
        admission = handler.admit(job, {'config': config, 'store': store, 'root': store.root})
        if admission != 'running':
            store.outcome(jid, admission, 'retry_admission_changed')
            return {'state': admission}
        with store.db:
            store.db.execute("UPDATE jobs SET state='queued',reason='operator_retry',updated=? WHERE id=?", (time.time(), jid))
        return {'state': 'queued'}


def status(config, store):
    sources = {}
    for source in config['sources']:
        state = store.source_state(source['id'])
        sources[source['id']] = {k: v for k, v in state.items() if k != 'pages'}
        sources[source['id']].setdefault('health', 'never_polled')
        sources[source['id']]['collection_overdue'] = time.time() > state.get('next_poll', 0) + source.get('interval_seconds', 180)
    jobs = [dict(row) for row in store.db.execute('SELECT id,subject,state,reason,created,updated,attempt FROM jobs ORDER BY created')]
    attempts = []
    for row in store.db.execute('SELECT * FROM attempts ORDER BY job_id,number'):
        value = dict(row)
        result = json.loads(value.pop('result') or '{}')
        execution = result.get('execution', {})
        journal = Path(value['journal'])
        if journal.exists():
            try:
                execution = chain.load_state(journal)
            except (ValueError, OSError):
                value['journal_health'] = 'invalid'
        value['run_ids'] = [execution[k] for k in ('review_run_id', 'apply_run_id') if execution.get(k)]
        value['outcome'] = execution.get('result')
        attempts.append(value)
    context = {'config': config, 'store': store, 'root': store.root}
    health = {b['id']: HANDLERS[b['handler']].health(b, context)
              for b in config['bindings'] if hasattr(HANDLERS[b['handler']], 'health')}
    return {'sources': sources, 'handler_health': health, 'jobs': jobs, 'attempts': attempts}


def doctor(config):
    manage.prerequisites(runtime=True)
    if chain.command(['gh', 'auth', 'status'], check=False).returncode:
        raise ValueError('GitHub service-account authentication unavailable')
    rows = json.loads(chain.command(['cao', 'workflow', 'list', '--json']).stdout)
    indexed = {row['name']: row for row in rows}
    for binding in config['bindings']:
        if binding['handler'] == 'pr_review_apply':
            for name in pr_resources.NAMES:
                deployed = manage.deployed_workflow(name)
                if name not in indexed or indexed[name].get('source_path') != str(deployed.resolve()):
                    raise ValueError('CAO server must index the same owned deployment paths')
            if not manage.codex_review_profile_ok() or not manage.codex_apply_profile_ok():
                raise ValueError('Named read-only Codex profiles unavailable')
            source = next(s for s in config['sources'] if s['id'] == binding['source_id'])
            request = {'repository': source['repository'], 'policy_path': binding['policy_path'], 'apply_mode': 'patch',
                       'review_workspace': str(Path(config['state_root']) / 'review-workspace'),
                       'apply_workspace': str(Path(config['state_root']) / 'apply-workspace')}
            policy, _ = chain.preflight(request)
            if not policy.get('push_branches') or any(not isinstance(policy.get(k), str) or not policy[k]
                    or any(c in policy[k] for c in '\r\n\x00') for k in ('git_author_name', 'git_author_email')):
                raise ValueError('Configure authorized push branches and Git author')
    chain.private_root(config['state_root'])
    return {'status': 'ok'}


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('poll').add_argument('--dry-run', action='store_true')
    commands.add_parser('worker').add_argument('--once', action='store_true')
    commands.add_parser('status').add_argument('--json', action='store_true')
    commands.add_parser('retry').add_argument('job_id')
    args = parser.parse_args(argv)
    if os.getuid() == 0:
        raise ValueError('Use a non-root service account')
    config = configuration(args.config)
    if args.command == 'doctor':
        result = doctor(config)
    else:
        dry = args.command == 'poll' and args.dry_run
        root = Path(config['state_root'])
        if args.command == 'status' and not (root / 'automation.sqlite3').exists():
            result = {'sources': {s['id']: {'health': 'never_polled', 'collection_overdue': True} for s in config['sources']},
                      'handler_health': {}, 'jobs': [], 'attempts': []}
        else:
            store = Store(root, memory=dry, readonly=args.command == 'status')
            try:
                if args.command == 'poll':
                    if dry:
                        result = poll(config, store, dry_run=True)
                    else:
                        with lock(root, 'poll.lock'):
                            result = poll(config, store)
                elif args.command == 'worker':
                    worker(args.config, store, once=args.once)
                    return 0
                elif args.command == 'retry':
                    result = retry(config, store, args.job_id, str(Path(args.config).resolve()))
                else:
                    result = status(config, store)
            finally:
                store.close()
    print(json.dumps(result, sort_keys=True))
    return 1 if args.command == 'poll' and 'error' in result['sources'].values() else 0


def main(argv=None):
    previous = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(previous)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Only types/metadata: no credential-bearing subprocess output or PR text.
        print('Automation failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
