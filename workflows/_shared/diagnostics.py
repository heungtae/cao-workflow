"""Embedded in deployment scripts; private evidence is independent of CAO output."""
_DIAG = {}


def diag_private(value, directory=False):
    import os
    import stat
    from pathlib import Path
    p = Path(value)
    if not p.is_absolute() or any(x.is_symlink() for x in (p, *p.parents)):
        raise ValueError('Evidence paths must be absolute without symlinks')
    if directory:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = p.lstat()
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Invalid evidence ownership or permissions')
    return p


def diag_atomic(path, value):
    import json
    import os
    import tempfile
    diag_private(path.parent, True)
    if path.exists():
        diag_private(path)
    fd, name = tempfile.mkstemp(prefix='.evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def diag_init(inputs):
    import hashlib
    import os
    import re
    import secrets
    import time
    from pathlib import Path
    rid = os.environ.get('CAO_WORKFLOW_RUN_ID') or 'local-' + secrets.token_hex(16)
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', rid) or rid in ('.', '..'):
        raise ValueError('Invalid evidence run ID')
    root = inputs.get('diagnostics_root')
    if not root:
        journal = inputs.get('execution_state_path')
        root = str(diag_private(journal).parent / 'run-evidence') if journal else '/tmp/cao-workflow-results'
    directory = diag_private(diag_private(root, True) / rid, True)
    generation = os.environ.get('CAO_WORKFLOW_GENERATION', '0')
    if not re.fullmatch(r'\d{1,10}', generation):
        raise ValueError('Invalid evidence generation')
    _DIAG.clear()
    _DIAG.update(directory=directory, run_id=rid, workflow=WORKFLOW, version=VERSION,
                 repository=inputs.get('repository'), generation=int(generation), started=time.monotonic(),
                 subject={k: inputs[k] for k in ('pr_number', 'issue_number', 'monitor_id') if k in inputs},
                 patterns=[])
    _DIAG['workflow_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _DIAG['expectations'] = {k: inputs[k] for k in ('expected_head_sha', 'expected_base_sha',
                           'head_sha', 'base_sha', 'expected_policy_digest', 'expected_resource_digest') if k in inputs}
    if inputs.get('execution_state_path'):
        _DIAG['execution_id'] = Path(inputs['execution_state_path']).stem
    diag_event('run_started')
    print('Workflow evidence: ' + str(directory), flush=True)
    return directory


def diag_clean(value, tail=False):
    import re
    if isinstance(value, str):
        patterns = [r'gh[pousr]_[A-Za-z0-9_]{15,}', r'github_pat_[A-Za-z0-9_]{15,}',
                    r'AKIA[0-9A-Z]{16}', r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                    r'(?i)Bearer\s+[^\s"\']+',
                    r'(?i)\b[\w.-]*(?:password|secret|token|api[_-]?key|access[_-]?key)[\w.-]*["\']?\s*[=:]\s*["\']?[^\s,"\']+',
                    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
                    r'https?://[^\s/@]+:[^\s/@]+@'] + _DIAG.get('patterns', [])
        for pattern in patterns:
            value = re.sub(pattern, '[REDACTED]', value)
        return value[-16384:] if tail else value[:16384]
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.fullmatch(r'(?i)(?:password|secret|token|api[_-]?key|authorization|aws_secret_access_key)', str(k))
                else diag_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = []
        hide_next = False
        for item in value:
            cleaned.append('[REDACTED]' if hide_next else diag_clean(item))
            hide_next = isinstance(item, str) and bool(re.fullmatch(r'(?i)--?(?:password|secret|token|api-key|access-token)', item))
        return cleaned
    return value


def diag_event(event, **fields):
    import datetime
    import fcntl
    import json
    import os
    import time
    if not _DIAG:
        return
    if fields.get('stage'):
        _DIAG['stage'] = fields['stage']
    directory = _DIAG['directory']
    fd = os.open(directory / 'events.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        diag_private(directory / 'events.lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'events.jsonl'
        if path.exists():
            diag_private(path)
        seq_path = directory / 'sequence.json'
        seq = json.loads(diag_private(seq_path).read_text())['seq'] + 1 if seq_path.exists() else 1
        row = dict(schema_version=1, seq=seq, at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   run_id=_DIAG['run_id'], workflow=_DIAG['workflow'], version=_DIAG['version'],
                   repository=_DIAG['repository'], generation=_DIAG['generation'], event=event,
                   elapsed_ms=int((time.monotonic() - _DIAG['started']) * 1000), **_DIAG['subject'])
        row.update(diag_clean(fields))
        row['workflow_sha256'] = _DIAG['workflow_sha256']
        if 'execution_id' in _DIAG:
            row['execution_id'] = _DIAG['execution_id']
        if _DIAG.get('attempt_id'):
            row.setdefault('attempt_id', _DIAG['attempt_id'])
        # Keep two bounded files. Durable terminal results/publication intents are never rotated.
        if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
            previous = directory / 'events.previous.jsonl'
            if previous.exists():
                diag_private(previous)
            os.replace(path, previous)
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        diag_atomic(seq_path, {'seq': seq})


def diag_result(output):
    import fcntl
    import json
    import os
    if not _DIAG:
        return output
    if (output.get('run_id') != _DIAG['run_id'] or output.get('workflow') != _DIAG['workflow']
            or output.get('repository') != _DIAG['repository']):
        raise ValueError('Final evidence identity mismatch')
    output = dict(output, evidence_directory=str(_DIAG['directory']), generation=_DIAG['generation'],
                  workflow_sha256=_DIAG['workflow_sha256'])
    output.setdefault('version', _DIAG['version'])
    output.update(_DIAG['expectations'])
    if 'execution_id' in _DIAG:
        output['execution_id'] = _DIAG['execution_id']
    directory = _DIAG['directory']
    row = dict(schema_version=1, generation=_DIAG['generation'], output=output)
    fd = os.open(directory / 'result.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        diag_private(directory / 'result.lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'result.json'
        generation_path = directory / ('result.' + str(row['generation']) + '.json')
        if generation_path.exists() and json.loads(diag_private(generation_path).read_text()) != row:
            raise ValueError('Generation result cannot be replaced')
        if path.exists():
            old = json.loads(diag_private(path).read_text())
            if old['generation'] > row['generation'] or (old['generation'] == row['generation'] and old != row):
                raise ValueError('Terminal result cannot be replaced in the same generation')
        diag_atomic(generation_path, row)
        diag_atomic(path, row)
    diag_event('run_finished', outcome=output.get('status', output.get('result', 'completed')))
    return output


def diag_journal_result(inputs, output):
    import fcntl
    import json
    import os
    value = inputs.get('execution_state_path')
    if not value:
        return
    path = diag_private(value)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        diag_private(str(path) + '.lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        rid = output.get('run_id')
        if (rid not in state.get('run_ids', []) or state.get('workflow') != output.get('workflow')
                or state.get('inputs', {}).get('repository') != output.get('repository')):
            raise ValueError('Incident journal output identity mismatch')
        outputs = state.setdefault('child_outputs', {})
        if rid in outputs and outputs[rid] != output:
            if output.get('generation', 0) <= outputs[rid].get('generation', 0):
                raise ValueError('Terminal incident result cannot be replaced in the same generation')
            state.setdefault('child_output_history', []).append(outputs[rid])
        outputs[rid] = output
        diag_atomic(path, state)


def diag_test_command(argv, *, env=None, cwd=None, timeout=600, phase='candidate', command_ref=None):
    import subprocess
    import time
    started = time.monotonic()
    diag_event('test_started', phase=phase, command_ref=command_ref)
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        row = {'phase': phase, 'command': command_ref, 'exit_code': proc.returncode,
               'result': 'passed' if proc.returncode == 0 else 'failed', 'timeout': False,
               'stdout': proc.stdout or '', 'stderr': proc.stderr or ''}
    except subprocess.TimeoutExpired as exc:
        row = {'phase': phase, 'command': command_ref, 'exit_code': None, 'result': 'failed', 'timeout': True,
               'stdout': exc.stdout or '', 'stderr': exc.stderr or ''}
    for key in ('stdout', 'stderr'):
        if isinstance(row[key], bytes):
            row[key] = row[key].decode(errors='replace')
        row[key + '_truncated'] = len(row[key]) > 16384
        row[key] = diag_clean(row[key], tail=True)
    row['command'] = diag_clean(row['command'])
    row['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    if argv[0] == 'docker' and '--entrypoint' in argv:
        row['test_image'] = argv[argv.index('--entrypoint') + 2]
    if _DIAG:
        import secrets
        path = _DIAG['directory'] / ('test-' + secrets.token_hex(12) + '.json')
        diag_atomic(path, row)
        row['diagnostic_path'] = str(path)
    diag_event('test_finished', phase=phase, exit_code=row['exit_code'], timeout=row['timeout'],
               result=row['result'], diagnostic_path=row.get('diagnostic_path'))
    return row


def diag_command_failure(executable, error_type, exit_code=None, stdout='', stderr=''):
    import secrets
    if not _DIAG:
        return
    row = diag_clean({'executable': executable, 'error_type': error_type, 'exit_code': exit_code,
                      'stdout': diag_clean(str(stdout or ''), tail=True), 'stderr': diag_clean(str(stderr or ''), tail=True)})
    path = _DIAG['directory'] / ('command-' + secrets.token_hex(12) + '.json')
    diag_atomic(path, row)
    diag_event('command_failed', executable=executable, error_type=error_type,
               exit_code=exit_code, diagnostic_path=str(path))
