WORKFLOW = 'github-issue-fix'
VERSION = 'v1'
INPUTS = {
    'repository': {'type': 'string', 'required': True},
    'issue_number': {'type': 'int', 'required': True},
    'policy_path': {'type': 'string', 'required': True},
    'apply_mode': {'type': 'string', 'default': 'push'},
    'execution_state_path': {'type': 'string', 'required': True},
    'expected_policy_digest': {'type': 'string', 'required': True},
    'expected_resource_digest': {'type': 'string', 'required': True},
    'recovery_only': {'type': 'bool', 'default': False},
    'operator_retry_reason': {'type': 'string', 'required': False},
}


def issue_snapshot(repo, number, policy, retained):
    issue = api(repo, f'issues/{number}', policy)
    if (issue.get('number') != number or issue.get('state') != 'open' or 'pull_request' in issue
            or not (issue.get('title') or issue.get('body'))):
        raise Blocked('Selected Issue must be open and have a specification')
    comments = pages(repo, f'issues/{number}/comments', policy)
    excluded, selected = [], []
    for comment in comments:
        verified = next((row for row in retained if row.get('comment_id') == comment['id']
                         and row.get('publisher') == comment.get('user', {}).get('login')
                         and row.get('body_digest') == digest(comment.get('body'))
                         and row.get('marker') in (comment.get('body') or '')), None)
        if verified:
            excluded.append(comment['id'])
        else:
            selected.append({'id': comment['id'], 'body': comment.get('body'), 'author': comment.get('user', {}).get('login')})
    specification = {'title': issue['title'], 'body': issue.get('body') or '', 'comments': selected}
    if len(json.dumps(specification).encode()) > 40000:
        raise Blocked('Complete Issue discussion exceeds context budget')
    return issue, specification, digest(specification), excluded


def validate_fix_policy(policy):
    if not policy.get('editable_paths') or not isinstance(policy['editable_paths'], list):
        raise Blocked('Editable source/test paths required')
    if not re.fullmatch(r'(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}', policy.get('test_image', '')):
        raise Blocked('Preloaded digest-pinned test container required')
    commands = policy.get('test_commands')
    if (not isinstance(commands, list) or not 1 <= len(commands) <= 10
            or any(not isinstance(row, list) or not row or any(not isinstance(a, str) or not a or '\x00' in a for a in row) for row in commands)):
        raise Blocked('Mandatory test commands must be operator argv arrays')
    if not policy.get('git_author_name') or not policy.get('git_author_email'):
        raise Blocked('Configured commit author required')
    if policy.get('branch_prefix', 'cao/issue-') != 'cao/issue-':
        raise Blocked('Initial release requires cao/issue- branch namespace')
    command(['docker', 'image', 'inspect', policy['test_image']])


def git(source, *args, policy=None):
    return command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                    '-c', 'core.attributesfile=/dev/null', '-c', 'filter.lfs.smudge=',
                    '-c', 'filter.lfs.process=', '-c', 'filter.lfs.required=false',
                    '-c', 'diff.external=', '-c', 'credential.helper=',
                    '-c', 'credential.helper=!gh auth git-credential', '-c', 'protocol.file.allow=never',
                    '-C', str(source), *args], env=credentials(policy) if policy else None)


def checkout(repo, sha, work, policy):
    source = work / 'source'
    source.mkdir(mode=0o700)
    git(source, 'init')
    git(source, 'remote', 'add', 'origin', f'https://github.com/{repo}.git')
    git(source, 'fetch', '--no-tags', '--depth=1', 'origin', sha, policy=policy)
    if git(source, 'rev-parse', 'FETCH_HEAD').strip() != sha:
        raise Blocked('Fetched source revision differs from pinned GitHub SHA')
    git(source, 'checkout', '--detach', sha)
    return source


def editable(source, name, policy):
    relative(name)
    if not any(fnmatch.fnmatchcase(name, p) for p in policy['editable_paths']):
        raise Blocked('Edit path not allowlisted')
    path = source / name
    for parent in [path, *path.parents]:
        if parent == source:
            break
        if parent.is_symlink():
            raise Blocked('Symlink source edits prohibited')
    if path.exists() and not path.is_file():
        raise Blocked('Only regular text file edits allowed')
    return path


def fix_files(source, policy):
    names = policy.get('source_paths', [])
    if not names or len(names) > 40:
        raise Blocked('Configure 1..40 source/test context paths')
    files = {}
    for name in names:
        relative(name)
        path = source / name
        if any(p.is_symlink() for p in [path, *path.parents] if p != source and p.is_relative_to(source)):
            raise Blocked('Symlink source context prohibited')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 40000:
                raise Blocked('Source context file is unsafe/oversized')
            content = path.read_text()
            if '\x00' in content:
                raise Blocked('Binary source context prohibited')
            files[name] = content
        elif name in policy.get('new_files', []):
            files[name] = None
        else:
            raise Blocked('Configured source context missing')
    return files


def apply_proposal(source, files, proposal, policy):
    decision = proposal.get('decision')
    if decision not in ('fix', 'no_change', 'needs_context') or not isinstance(proposal.get('reason'), str) or not proposal['reason']:
        raise Blocked('Invalid fix decision contract')
    if decision != 'fix':
        if proposal.get('edits'):
            raise Blocked('Non-fix decision cannot contain edits')
        return []
    edits = proposal.get('edits')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 40 or not proposal.get('regression_scenarios'):
        raise Blocked('Fix requires edits and meaningful regression scenarios')
    prepared, seen = [], set()
    for row in edits:
        name, old, new = row.get('path'), row.get('old'), row.get('new')
        if (name not in files or name in seen or not isinstance(old, str) or not isinstance(new, str)
                or new == old or '\x00' in new or len(new.encode()) > 40000):
            raise Blocked('Invalid or unsupported edit')
        path = editable(source, name, policy)
        original = files[name]
        if original is None:
            if name not in policy.get('new_files', []) or path.exists() or old or not new:
                raise Blocked('New file is not explicitly allowlisted')
            replacement = new
        else:
            if path.read_text() != original or not old or original.count(old) != 1:
                raise Blocked('Replacement must match unchanged source exactly once')
            replacement = original.replace(old, new, 1)
        redact(replacement)
        prepared.append((path, replacement))
        seen.add(name)
    if not any(any(fnmatch.fnmatchcase(name, p) for p in policy.get('regression_test_paths', [])) for name in seen):
        raise Blocked('Fix requires an edit to an operator-designated regression test path')
    for path, replacement in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(replacement)
    return sorted(seen)


def test_candidate(source, work, policy):
    copy = work / 'test-source'
    shutil.copytree(source, copy, symlinks=True, ignore=shutil.ignore_patterns('.git', '.env', '.env.*'))
    results = []
    try:
        for argv in policy['test_commands']:
            name = 'cao-issue-test-' + secrets.token_hex(12)
            args = ['docker', 'run', '--name', name, '--rm', '--pull=never', '--network=none',
                    '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                    '--pids-limit=128', '--memory=2g', '--cpus=2', '--user', f'{os.getuid()}:{os.getgid()}',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--env', 'HOME=/tmp',
                    '--mount', f'type=bind,src={copy},dst=/work', '--workdir', '/work',
                    '--entrypoint', argv[0], policy['test_image'], *argv[1:]]
            try:
                command(args, timeout=600)
            finally:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30, check=False)
            results.append({'command': argv, 'result': 'passed'})
        return results
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def incident_context(issue, specification, policy, work, base_files):
    # Plain Issue log excerpts are excluded from model evidence. Incident scopes are operator bound.
    match = re.search(r'<!-- cao-incident-data ([A-Za-z0-9+/=]+) -->', issue.get('body') or '')
    metadata = policy.get('issue_incidents', {}).get(str(issue['number']))
    if match:
        if issue.get('user', {}).get('login') not in policy.get('incident_publishers', []):
            raise Blocked('Unverified incident producer marker')
        metadata = json.loads(base64.b64decode(match[1], validate=True))
        if (metadata.get('contract_version') != '1.0' or metadata.get('workflow') != 'mcp-exception-issue'
                or metadata.get('version') != 'v1' or not re.fullmatch(r'[0-9a-f]{64}', metadata.get('fingerprint', ''))):
            raise Blocked('Unrecognized incident provenance schema')
    if metadata is None:
        # Operator expressly declares ordinary code Issues; unknown incident data is never inferred.
        if not policy.get('allow_code_issues', False):
            raise Blocked('Issue requires operator incident metadata or code-Issue admission')
        discussion = json.dumps(specification)
        if re.search(r'```(?:logs?|text)\b|Traceback \(most recent call last\)|\b\d{4}-\d{2}-\d{2}T\S+.*(?:ERROR|FATAL)|\\n\s*at [\w.$]+\([^)]*:\d+\)', discussion):
            raise Blocked('Operational log excerpts require operator-bound MCP incident metadata')
        return {}, specification
    scope = next((s for s in policy.get('monitors', {}).values()
                  if s['service'] == metadata.get('service') and s['environment'] == metadata.get('environment')), None)
    if scope is None:
        raise Blocked('Incident metadata outside configured scope')
    occurred = instant(metadata['occurred_at'])
    filters = metadata.get('filters', {})
    if (not isinstance(filters, dict) or set(filters) - {'trace_id', 'request_id', 'instance_id'}
            or any(not isinstance(value, str) or not value for value in filters.values())):
        raise Blocked('Incident metadata filters escaped canonical equality scope')
    cutoff = now() - dt.timedelta(minutes=2)
    if occurred >= cutoff:
        raise Blocked('Incident time is not yet stable')
    record = {'service': scope['service'], 'environment': scope['environment'],
              'occurred_at': metadata['occurred_at'], **filters}
    if metadata.get('deployed_sha'):
        record['deployment_sha'] = metadata['deployed_sha']
    records = evidence(record, scope, policy, 5, cutoff)
    # Deployed identity must be supported by current MCP metadata/operator interval, not an Issue claim.
    deployed = {revision(row, scope, policy) for row in records if row.get('deployment_sha') or row.get('deployment_ref')}
    if not deployed:
        deployed.add(revision(dict(record, deployment_sha=None), scope, policy))
    if len(deployed) != 1:
        raise Blocked('Rolling deployment evidence is ambiguous')
    sha = deployed.pop()
    if metadata.get('deployed_sha') and metadata['deployed_sha'] != sha:
        raise Blocked('Issue deployed revision disagrees with authoritative evidence')
    old_files = source_files(policy['repository'], sha, list(base_files), policy)
    # Do not pass Issue body/discussion as operational evidence to the model.
    sanitized = {'title': specification['title'], 'body': 'Operational evidence is supplied exclusively through MCP records.', 'comments': []}
    return {'records': records, 'deployed_sha': sha, 'deployed_files': old_files, 'scope': scope, 'incident': record}, sanitized


def branch_sha(repo, branch, policy):
    # Query a complete prefix listing so missing refs do not require interpreting transport failures.
    rows = api(repo, 'git/matching-refs/heads/' + quote(branch, safe=''), policy)
    if not isinstance(rows, list):
        raise Blocked('Invalid GitHub ref response')
    matches = [row['object']['sha'] for row in rows if row['ref'] == 'refs/heads/' + branch]
    if len(matches) > 1:
        raise Blocked('Ambiguous branch identity')
    return matches[0] if matches else None


def reconcile_comment(state, path, policy, allow_write=True):
    repo, number = policy['repository'], state['issue_number']
    remote = branch_sha(repo, state['branch'], policy)
    if remote is None:
        state['status'] = 'reconciling'
        atomic(path, state)
        return state
    if remote != state['commit_sha']:
        raise Blocked('Remote branch does not match retained commit')
    marker = '<!-- cao-issue-fix ' + state['execution_key'] + ' -->'
    body = f'Fix pushed to `{state["branch"]}` at `{state["commit_sha"]}`.\n\nConfigured regression checks passed.\n\n' + marker
    publisher = state.get('publisher')
    if not publisher:
        publisher = json.loads(command(['gh', 'api', 'user'], env=credentials(policy)))['login']
        state.update(publisher=publisher, marker=marker, body_digest=digest(body))
        atomic(path, state)
    matches = [row for row in pages(repo, f'issues/{number}/comments', policy)
               if row.get('user', {}).get('login') == publisher and row.get('body') == body]
    if len(matches) > 1:
        raise Blocked('Multiple matching result comments')
    if matches:
        comment = matches[0]
    elif state.get('comment_sending') or not allow_write:
        state['status'] = 'pushed_comment_pending'
        atomic(path, state)
        return state
    else:
        state['comment_sending'] = True
        atomic(path, state)
        comment = api(repo, f'issues/{number}/comments', policy, 'POST', {'body': body})
    if comment.get('user', {}).get('login') != publisher or comment.get('body') != body:
        raise Blocked('Result comment identity mismatch')
    state.update(status='pushed', comment_id=comment['id'])
    atomic(path, state)
    return state


def process(inputs):
    allow_write = True
    if inputs.get('recovery_only'):
        policy = recovery_policy(inputs)
        try:
            admission(inputs, 'issue-fixer')
        except Blocked:
            allow_write = False
    else:
        policy = admission(inputs, 'issue-fixer')
    number = inputs['issue_number']
    if type(number) is not int or number <= 0 or inputs.get('apply_mode', 'push') not in ('push', 'patch'):
        raise Blocked('Invalid Issue number or execution mode')
    root = private(policy['state_root'], True)
    index = private(root / 'issue-executions', True)
    execution = Path(inputs['execution_state_path']).stem
    work_pointer = root / ('fix-' + execution + '.json')
    with locked(root / 'locks', [policy['repository'], number]):
        retained = [json.loads(private(p).read_text()) for p in index.glob('*.json')]
        retained = [row for row in retained if row.get('issue_number') == number]
        if inputs.get('recovery_only'):
            if not work_pointer.exists():
                raise Blocked('Missing retained publication state')
            state_path = Path(json.loads(private(work_pointer).read_text())['state_path'])
            if state_path.parent != index:
                raise Blocked('Retained execution index path escaped state root')
            state = json.loads(private(state_path).read_text())
            if not state.get('commit_sha') or not state.get('push_intent'):
                raise Blocked('No retained push to reconcile')
            return reconcile_comment(state, state_path, policy, allow_write=allow_write)
        issue, specification, snapshot, excluded = issue_snapshot(policy['repository'], number, policy, retained)
        repo_data = api(policy['repository'], '', policy)
        base_ref = policy.get('base_branch', repo_data['default_branch'])
        base_sha = resolve_sha(policy['repository'], base_ref, policy)
        key = digest([policy['repository'], number, snapshot, base_sha, inputs['expected_policy_digest'], inputs['expected_resource_digest'], VERSION, inputs.get('apply_mode', 'push')])
        path = index / (key + '.json')
        atomic(work_pointer, {'state_path': str(path)})
        for previous in retained:
            same_contract = (previous.get('snapshot_digest') == snapshot and previous.get('base_sha') == base_sha
                             and previous.get('policy_digest') == inputs['expected_policy_digest']
                             and previous.get('resource_digest') == inputs['expected_resource_digest']
                             and previous.get('mode') == inputs.get('apply_mode', 'push'))
            pending_publication = previous.get('status') in ('reconciling', 'pushed_comment_pending')
            if previous.get('push_intent') and (same_contract or pending_publication):
                previous_path = index / (previous['execution_key'] + '.json')
                atomic(work_pointer, {'state_path': str(previous_path)})
                return reconcile_comment(previous, previous_path, policy, allow_write=same_contract)
        if path.exists():
            state = json.loads(private(path).read_text())
            if state.get('commit_sha') and state.get('push_intent'):
                return reconcile_comment(state, path, policy)
            if not inputs.get('operator_retry_reason') or state.get('status') not in ('failed', 'blocked', 'needs_context'):
                return state
            previous_key = key
            key = digest([key, execution, inputs['operator_retry_reason']])
            path = index / (key + '.json')
            atomic(work_pointer, {'state_path': str(path)})
            if path.exists():
                return json.loads(private(path).read_text())
        else:
            previous_key = None
        validate_fix_policy(policy)
        work = Path(tempfile.mkdtemp(prefix='issue-fix-', dir=private(policy['workspace_root'], True)))
        state = {'status': 'developing', 'execution_key': key, 'issue_number': number, 'snapshot_digest': snapshot,
                 'excluded_comment_ids': excluded, 'base_sha': base_sha, 'base_ref': base_ref,
                 'artifact_path': str(work), 'branch': f'cao/issue-{number}-{key[:16]}', 'mode': inputs.get('apply_mode', 'push')}
        state.update(previous_execution_key=previous_key, operator_retry_reason=inputs.get('operator_retry_reason'),
                     policy_digest=inputs['expected_policy_digest'], resource_digest=inputs['expected_resource_digest'])
        atomic(path, state)
        try:
            source = checkout(policy['repository'], base_sha, work, policy)
            files = fix_files(source, policy)
            context, safe_specification = incident_context(issue, specification, policy, work, files)
            task = {'specification': safe_specification, 'files': files, 'base_sha': base_sha,
                    'operational_evidence': {k: v for k, v in context.items() if k not in ('scope', 'incident')}}
            for round_number in range(3):
                admission(inputs, 'issue-fixer')
                proposal = model(work, 'issue-fixer', task, 'fix-' + str(round_number))
                if proposal.get('decision') != 'needs_context' or not context or round_number == 2:
                    break
                context['records'] = evidence(context['incident'], context['scope'], policy, (15, 60)[round_number], now() - dt.timedelta(minutes=2))
                task['operational_evidence']['records'] = context['records']
            changed = apply_proposal(source, files, proposal, policy)
            if proposal['decision'] != 'fix':
                state.update(status=proposal['decision'], reason=proposal['reason'])
                atomic(path, state)
                return state
            state['tests'] = test_candidate(source, work, policy)
            # Tests run against a disposable copy; candidate remains unchanged.
            git(source, 'add', '--', *changed)
            patch = git(source, 'diff', '--cached', '--no-ext-diff', '--binary')
            if len(patch.encode()) > 2000000:
                raise Blocked('Candidate diff budget exhausted')
            (work / 'candidate.patch').write_text(patch)
            os.chmod(work / 'candidate.patch', 0o600)
            state.update(status='patch_ready', changed_paths=changed, reason=proposal['reason'])
            atomic(path, state)
            if state['mode'] == 'patch':
                return state
            admission(inputs, 'issue-fixer')
            _, _, latest, _ = issue_snapshot(policy['repository'], number, policy, retained)
            if latest != snapshot or resolve_sha(policy['repository'], base_ref, policy) != base_sha:
                raise Blocked('Issue specification or base changed before push')
            if branch_sha(policy['repository'], state['branch'], policy) is not None:
                raise Blocked('Generated branch already exists without retained ownership')
            git(source, '-c', 'user.name=' + policy['git_author_name'], '-c', 'user.email=' + policy['git_author_email'],
                'commit', '-m', f'fix: address issue #{number}')
            commit = git(source, 'rev-parse', 'HEAD').strip()
            if git(source, 'rev-parse', 'HEAD^').strip() != base_sha:
                raise Blocked('Commit parent differs from validated base')
            state.update(commit_sha=commit, push_intent=True, status='reconciling')
            atomic(path, state)
            # Empty expected ref permits only creation; competing writers fail without overwrite/rebase.
            git(source, 'push', '--force-with-lease=refs/heads/' + state['branch'] + ':', 'origin',
                commit + ':refs/heads/' + state['branch'], policy=policy)
            if branch_sha(policy['repository'], state['branch'], policy) != commit:
                raise Blocked('Push outcome requires reconciliation')
            state['status'] = 'pushed_comment_pending'
            atomic(path, state)
            admission(inputs, 'issue-fixer')
            return reconcile_comment(state, path, policy)
        except Exception:
            if state.get('push_intent'):
                # Preserve an already verified push even when its result comment fails.
                state['status'] = 'pushed_comment_pending' if state['status'] == 'pushed_comment_pending' else 'reconciling'
            else:
                state['status'] = 'failed'
            atomic(path, state)
            if state.get('push_intent'):
                return state
            raise


if __name__ == '__main__':
    main()
