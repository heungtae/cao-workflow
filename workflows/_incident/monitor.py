WORKFLOW = 'mcp-exception-issue'
VERSION = 'v1'
INPUTS = {
    'repository': {'type': 'string', 'required': True},
    'monitor_id': {'type': 'string', 'required': True},
    'policy_path': {'type': 'string', 'required': True},
    'since': {'type': 'string', 'required': False},
    'until': {'type': 'string', 'required': False},
    'publish_mode': {'type': 'string', 'default': 'issue'},
    'execution_state_path': {'type': 'string', 'required': True},
    'expected_policy_digest': {'type': 'string', 'required': True},
    'expected_resource_digest': {'type': 'string', 'required': True},
    'recovery_only': {'type': 'bool', 'default': False},
    'operator_retry_reason': {'type': 'string', 'required': False},
    'retry_incident_id': {'type': 'string', 'required': False},
}


def store(root):
    path = root / 'monitor.sqlite3'
    if path.exists():
        private(path)
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.execute('PRAGMA journal_mode=DELETE')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS checkpoints (monitor TEXT PRIMARY KEY, cutoff TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS incidents (monitor TEXT, identity TEXT, record TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, retry_at TEXT, result TEXT, PRIMARY KEY(monitor,identity));
        CREATE TABLE IF NOT EXISTS publications (fingerprint TEXT PRIMARY KEY, status TEXT, payload TEXT);
    ''')
    return db


def fingerprint(repo, record, sha, scope):
    signature = record.get('stack_trace') or record['message']
    locations = re.findall(r'File ["\']([^"\']+)["\'], line (\d+)|\bat ([\w.$]+)\(([^():]+):(\d+)\)', signature)
    for key in ('trace_id', 'request_id', 'instance_id'):
        if record.get(key):
            signature = signature.replace(record[key], '<variable>')
    for pattern in scope.get('variable_patterns', []):
        signature = re.sub(pattern, '<variable>', signature)
    signature = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b\d{4}-\d\d-\d\dT\S+|\b\d+\b', '<variable>', signature)
    return digest([repo, record['service'], record['environment'], sha, signature, locations, VERSION])


def triage_gate(proposal, records, files):
    decision = proposal.get('decision')
    if decision not in ('code_change_required', 'non_code', 'needs_context'):
        raise Blocked('Invalid triage decision')
    if not isinstance(proposal.get('summary'), str) or not 0 < len(proposal['summary']) <= 1000:
        raise Blocked('Missing bounded triage summary')
    if decision == 'code_change_required':
        ids = {row['evidence_id'] for row in records}
        refs = proposal.get('evidence_ids', [])
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in ids for ref in refs)
                or any(not isinstance(proposal.get(key), str) or not 0 < len(proposal[key]) <= 5000 for key in ('cause', 'fix_direction'))
                or not isinstance(proposal.get('verification'), list) or not proposal['verification']
                or any(not isinstance(row, str) or not row for row in proposal['verification'])):
            raise Blocked('Code-change decision lacks supplied evidence and regression direction')
        locations = proposal.get('source_locations', [])
        if not locations:
            raise Blocked('Missing pinned source location')
        for location in locations:
            if (location.get('path') not in files or type(location.get('line')) is not int
                    or not 1 <= location['line'] <= len(files[location['path']].splitlines())):
                raise Blocked('Source reference is outside supplied pinned source')
    return decision


def publication(db, policy, fp, payload, recovery=False, allow_write=True):
    repo = policy['repository']
    with locked(Path(policy['state_root']) / 'locks', ['publication', repo, fp]):
        rows = db.execute('SELECT fingerprint,status,payload FROM publications').fetchall()
        related = [(identity, status, json.loads(saved)) for identity, status, saved in rows
                   if json.loads(saved).get('fingerprint') == fp]
        related.sort(key=lambda row: row[2]['episode'])
        current = related[-1] if related else None
        if current:
            identity, status, saved = current
            if status == 'published':
                issue = api(repo, 'issues/' + str(saved['issue_number']), policy)
                if issue.get('state') == 'open' or instant(payload.get('occurred_at', saved['occurred_at'])) <= instant(issue.get('closed_at') or saved['occurred_at']):
                    return dict(saved, status='issue_reused')
                if recovery:
                    return dict(saved, status='issue_reused')
                current = None
            else:
                payload = saved
        if not current:
            if recovery or not allow_write:
                raise Blocked('No authorized unsubmitted Issue intent')
            episode = len(related) + 1
            identity = digest([repo, fp, episode])
            marker = '<!-- cao-incident ' + identity + ' -->'
            payload = dict(payload, repository=repo, fingerprint=fp, episode=episode, marker=marker,
                           publisher=publisher_identity(policy))
            payload['body'] += '\n' + marker
            metadata_match = re.search(r'<!-- cao-incident-data ([A-Za-z0-9+/=]+) -->', payload['body'])
            if metadata_match:
                metadata = json.loads(base64.b64decode(metadata_match[1]))
                metadata['episode_id'] = identity
                encoded = base64.b64encode(json.dumps(metadata, sort_keys=True).encode()).decode()
                payload['body'] = payload['body'].replace(metadata_match[0], '<!-- cao-incident-data ' + encoded + ' -->')
            if related:
                payload['body'] += '\n\nPrevious occurrence: ' + related[-1][2]['issue_url']
            with db:
                db.execute('INSERT INTO publications VALUES (?,?,?)', (identity, 'reserved', json.dumps(payload)))
            status = 'reserved'
        marker = payload['marker']
        matches = [issue for issue in pages(repo, 'issues?state=all', policy, 1000)
                   if marker in (issue.get('body') or '') and issue.get('user', {}).get('login') == payload['publisher']]
        if len(matches) > 1:
            raise Blocked('Multiple Issues match one publication intent')
        if matches:
            issue = matches[0]
            if digest(issue.get('body')) != digest(payload['body']):
                raise Blocked('Published Issue marker/body changed')
        elif status == 'sending' or not allow_write:
            return dict(payload, status='reconciling')
        else:
            with db:
                db.execute('UPDATE publications SET status=? WHERE fingerprint=?', ('sending', identity))
            issue = api(repo, 'issues', policy, 'POST', {'title': payload['title'], 'body': payload['body']})
        if type(issue.get('number')) is not int or not issue.get('html_url', '').startswith('https://github.com/' + repo + '/issues/'):
            raise Blocked('Invalid GitHub Issue publication identity')
        result = dict(payload, status='issue_created', issue_number=issue['number'], issue_url=issue['html_url'])
        with db:
            db.execute('UPDATE publications SET status=?,payload=? WHERE fingerprint=?', ('published', json.dumps(result), identity))
        return result


def publisher_identity(policy):
    return json.loads(command(['gh', 'api', 'user'], env=credentials(policy)))['login']


def analyze(record, scope, policy, work, cutoff, inputs=None):
    sha = revision(record, scope, policy)
    files = source_files(policy['repository'], sha, scope['source_paths'], policy)
    records = [record]
    for round_number, minutes in enumerate((5, 15, 60)):
        records += evidence(record, scope, policy, minutes, cutoff)
        records = list({row['evidence_id']: row for row in records}.values())
        if len(records) > 10000 or len(json.dumps(records).encode()) > 5000000:
            raise Blocked('Incident evidence budget exhausted')
        if inputs:
            admission(inputs, 'exception-triager')
        proposal = model(work, 'exception-triager', {'incident': record, 'records': records,
                         'files': files, 'deployed_sha': sha, 'round': round_number},
                         'triage-' + record['evidence_id'][:16] + '-' + str(round_number))
        decision = triage_gate(proposal, records, files)
        if decision != 'needs_context':
            return decision, proposal, sha, records
        # A structured request may select only this configured scope and canonical filters.
        for request in proposal.get('context_requests', []):
            if (request.get('service') != scope['service'] or request.get('environment') != scope['environment']
                    or set(request) - {'service', 'environment', 'filters', 'missing_fact'}):
                raise Blocked('Model context request escaped configured scope')
            t0 = instant(record['occurred_at'])
            records += collect(scope, policy, 'context', t0 - dt.timedelta(minutes=minutes),
                               min(t0 + dt.timedelta(minutes=minutes), cutoff), request.get('filters'))
        # Deterministic 5/15/60 windows ensure the model never selects arbitrary endpoints/queries.
    return 'needs_context', proposal, sha, records


def issue_payload(record, proposal, sha, fp, repository, monitor_id, records):
    metadata = {'contract_version': '1.0', 'service': record['service'], 'environment': record['environment'],
                'occurred_at': record['occurred_at'], 'deployed_sha': sha,
                'workflow': WORKFLOW, 'version': VERSION, 'fingerprint': fp, 'monitor_id': monitor_id,
                'queries': [row['provenance'] for row in records if row['evidence_id'] in proposal['evidence_ids']][:10],
                'filters': {k: record[k] for k in ('trace_id', 'request_id', 'instance_id') if record.get(k)}}
    encoded = base64.b64encode(json.dumps(metadata, sort_keys=True).encode()).decode()
    body = ('## Evidence-based code change\n\n' + proposal['summary'] + '\n\n' + proposal['cause']
            + '\n\nDeployed commit: `' + sha + '`\n\nFix direction: ' + proposal['fix_direction']
            + '\n\nRegression verification: ' + json.dumps(proposal['verification'])
            + '\n\nMCP evidence IDs: ' + ', '.join(proposal['evidence_ids'])
            + '\n\nSource locations: ' + json.dumps(proposal['source_locations'])
            + '\n\nObserved at: ' + record['occurred_at'] + '\nService: ' + record['service'] + '\nEnvironment: ' + record['environment']
            + '\n\n<!-- cao-incident-data ' + encoded + ' -->\n<!-- cao-incident ' + fp + ' -->')
    links = ['https://github.com/' + repository + '/blob/' + sha + '/' + quote(row['path'], safe='/') + '#L' + str(row['line'])
             for row in proposal['source_locations']]
    excerpts = [{'evidence_id': row['evidence_id'], 'occurred_at': row['occurred_at'], 'message': row['message'][:1000],
                 'provenance': row['provenance']} for row in records if row['evidence_id'] in proposal['evidence_ids']][:10]
    body += '\n\nPinned source: ' + '\n'.join(links) + '\n\nBounded MCP excerpts and actual query coverage:\n\n```json\n' + json.dumps(excerpts, ensure_ascii=False, indent=2) + '\n```'
    redact(body)
    if len(body.encode()) > 30000:
        raise Blocked('Issue body exceeds publisher budget')
    return {'title': proposal['summary'][:200], 'body': body, 'occurred_at': record['occurred_at']}


def process(inputs):
    if inputs.get('recovery_only'):
        policy = recovery_policy(inputs)
        try:
            admission(inputs, 'exception-triager')
            allow_write = True
        except Blocked:
            allow_write = False
    else:
        policy = admission(inputs, 'exception-triager')
    scope = policy['monitors'].get(inputs['monitor_id'])
    if not scope:
        raise Blocked('Unknown operator monitor')
    root = private(policy['state_root'], True)
    if inputs.get('recovery_only') and not (root / 'monitor.sqlite3').exists():
        raise Blocked('Missing retained monitor publication store')
    dry = inputs.get('publish_mode', 'issue') == 'dry-run'
    if inputs.get('publish_mode', 'issue') not in ('issue', 'dry-run'):
        raise Blocked('Invalid publication mode')
    db = sqlite3.connect(':memory:') if dry else store(root)
    if dry:
        db.executescript('CREATE TABLE checkpoints (monitor TEXT PRIMARY KEY, cutoff TEXT);')
    historical = bool(inputs.get('since') or inputs.get('until'))
    if historical and not (inputs.get('since') and inputs.get('until')):
        raise Blocked('Historical intervals require both boundaries')
    key = digest([policy['repository'], inputs['monitor_id']])
    results = []
    try:
        with locked(root / 'locks', ['monitor', key]):
            if inputs.get('recovery_only'):
                for fp, payload in db.execute("SELECT fingerprint,payload FROM publications WHERE status!='published'").fetchall():
                    if json.loads(payload).get('repository') != policy['repository']:
                        continue
                    result = publication(db, policy, json.loads(payload)['fingerprint'], json.loads(payload), recovery=True, allow_write=allow_write)
                    results.append(result)
                    if result['status'] in ('issue_created', 'issue_reused'):
                        with db:
                            for mon, identity, previous in db.execute('SELECT monitor,identity,result FROM incidents').fetchall():
                                if json.loads(previous or '{}').get('fingerprint') == result['fingerprint']:
                                    db.execute('UPDATE incidents SET status=?,result=? WHERE monitor=? AND identity=?',
                                               (result['status'], json.dumps(result), mon, identity))
                return {'status': 'reconciling' if any(r['status'] == 'reconciling' for r in results) else 'reconciled', 'incidents': results}
            cutoff = now() - dt.timedelta(minutes=2)
            checkpoint = db.execute('SELECT cutoff FROM checkpoints WHERE monitor=?', (key,)).fetchone()
            start = instant(inputs['since']) if historical else (instant(checkpoint[0]) - dt.timedelta(minutes=5) if checkpoint else cutoff - dt.timedelta(minutes=15))
            end = instant(inputs['until']) if historical else cutoff
            if not historical:
                end = min(end, start + dt.timedelta(hours=24))
            if end > cutoff or end - start > dt.timedelta(hours=24):
                raise Blocked('Collection interval exceeds stable cutoff or 24-hour budget')
            events = collect(scope, policy, 'exceptions', start, end)
            # Retain every collected event and watermark in one transaction only after complete coverage.
            if not dry:
                with db:
                    for event in events:
                        db.execute('INSERT OR IGNORE INTO incidents(monitor,identity,record,status) VALUES (?,?,?,?)',
                                   (key, event['evidence_id'], json.dumps(event), 'pending'))
                    if not historical:
                        db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?)', (key, timestamp(end)))
                    if inputs.get('retry_incident_id'):
                        if not inputs.get('operator_retry_reason'):
                            raise Blocked('Operator retry requires corrective-change evidence')
                        prior = db.execute('SELECT status,result FROM incidents WHERE monitor=? AND identity=?',
                                           (key, inputs['retry_incident_id'])).fetchone()
                        if not prior or prior[0] not in ('blocked', 'failed'):
                            raise Blocked('Only retained terminal incidents may be explicitly retried')
                        previous = json.loads(prior[1] or '{}')
                        previous.setdefault('retry_history', []).append({'reason': inputs['operator_retry_reason'],
                                                                       'at': timestamp(now()), 'previous_status': prior[0]})
                        db.execute('UPDATE incidents SET status=?,result=? WHERE monitor=? AND identity=?',
                                   ('pending', json.dumps(previous), key, inputs['retry_incident_id']))
                pending = db.execute("SELECT identity,record,attempts FROM incidents WHERE monitor=? AND (status='pending' OR (status='deferred' AND retry_at<=?)) ORDER BY identity", (key, timestamp(now()))).fetchall()
            else:
                pending = [(event['evidence_id'], json.dumps(event), 0) for event in events]
            for identity, serialized, attempts in pending[:scope.get('max_incidents', 20)]:
                event = json.loads(serialized)
                status, retry_at, result = 'blocked', None, {}
                publication_started = False
                retry_hint = 0
                work = Path(tempfile.mkdtemp(prefix='incident-', dir=private(policy['workspace_root'], True)))
                try:
                    decision, proposal, sha, records = analyze(event, scope, policy, work, cutoff, inputs)
                    result = {'decision': decision, 'summary': proposal['summary'], 'deployed_sha': sha}
                    status = 'non_code' if decision == 'non_code' else 'blocked'
                    if decision == 'needs_context' and instant(event['occurred_at']) + dt.timedelta(minutes=60) > cutoff and attempts < 3:
                        status = 'deferred'
                    if decision == 'code_change_required':
                        fp = fingerprint(policy['repository'], event, sha, scope)
                        payload = issue_payload(event, proposal, sha, fp, policy['repository'], inputs['monitor_id'], records)
                        if dry:
                            status, result = 'dry-run', dict(result, fingerprint=fp, candidate=payload)
                        else:
                            admission(inputs, 'exception-triager')
                            publication_started = True
                            result['fingerprint'] = fp
                            result = publication(db, policy, fp, payload)
                            status = result['status']
                except ReadFailure as error:
                    status = 'deferred' if error.retryable and attempts < 3 else 'blocked'
                    retry_hint = error.retry_after_seconds
                except Blocked:
                    status = 'blocked'
                except Exception:
                    status = 'reconciling' if publication_started else 'failed'
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                if status == 'deferred':
                    retry_at = timestamp(now() + dt.timedelta(minutes=(5, 10, 20)[min(attempts, 2)]))
                    retry_at = max(retry_at, timestamp(now() + dt.timedelta(seconds=retry_hint)))
                    if result.get('decision') == 'needs_context':
                        retry_at = max(retry_at, timestamp(instant(event['occurred_at']) + dt.timedelta(minutes=62)))
                result.update(status=status, evidence_id=identity)
                results.append(result)
                if not dry:
                    with db:
                        prior = db.execute('SELECT result FROM incidents WHERE monitor=? AND identity=?', (key, identity)).fetchone()
                        history = json.loads(prior[0] or '{}').get('retry_history', []) if prior else []
                        if history:
                            result['retry_history'] = history
                        db.execute('UPDATE incidents SET status=?,attempts=?,retry_at=?,result=? WHERE monitor=? AND identity=?',
                                   (status, attempts + 1, retry_at, json.dumps(result), key, identity))
            return {'status': 'dry-run' if dry else ('reconciling' if any(r['status'] == 'reconciling' for r in results) else 'processed'), 'incidents': results, 'interval': [timestamp(start), timestamp(end)]}
    finally:
        db.close()


if __name__ == '__main__':
    main()
