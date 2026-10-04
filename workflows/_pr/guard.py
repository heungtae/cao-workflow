# BEGIN GENERATED PR GUARD -- edit workflows/_pr/guard.py
def automation_guard(inputs):
    """Optional manual-compatible gate; private journal is the trust boundary."""
    import hashlib
    path = inputs.get('execution_state_path')
    fields = ('expected_policy_digest', 'expected_resource_digest', 'authorized_policy_path')
    if not path:
        if any(inputs.get(k) for k in fields):
            raise ValueError('Automation expectations require an execution journal')
        return
    def private_file(value):
        p = Path(value)
        if not p.is_absolute() or any(x.is_symlink() for x in (p, *p.parents)):
            raise ValueError('Automation file path must be absolute without symlinks')
        info = p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Automation journal/policy must be owned mode 0600')
        return p
    journal = json.loads(private_file(path).read_text())
    request = journal['request']
    stage = 'review' if WORKFLOW == 'github-pr-review' else 'apply'
    if (journal.get('schema_version') != 1 or not re.fullmatch(r'[0-9a-f]{32}', journal.get('chain_id', ''))
            or os.environ.get('CAO_WORKFLOW_RUN_ID') != journal['chain_id'] + '-' + stage
            or inputs.get('repository', '').lower() != request['repository'].lower()
            or inputs.get('pr_number') != request['pr_number']
            or inputs.get('expected_head_sha', inputs.get('head_sha')) != request['head_sha']
            or inputs.get('expected_base_sha', inputs.get('base_sha')) != request['base_sha']):
        raise ValueError('Automation journal execution identity mismatch')
    for field, recorded in [('expected_policy_digest', 'policy_digest'), ('expected_resource_digest', 'resource_digest')]:
        if not inputs.get(field) or inputs[field] != request.get(recorded):
            raise ValueError('Automation expectation differs from frozen execution')
    if inputs.get('authorized_policy_path') != request.get('authorized_policy_path'):
        raise ValueError('Automation authorized policy path mismatch')
    if request.get('automation_config_path'):
        config = json.loads(private_file(request['automation_config_path']).read_text())
        current = next((b for b in config.get('bindings', []) if b.get('id') == request['binding']['id']), None)
        source = next((s for s in config.get('sources', []) if s.get('id') == request['binding']['source_id']), None)
        if (current != request['binding'] or not source or source.get('repository', '').lower() != request['repository']
                or source.get('type') != 'github_pull_requests' or config.get('state_root') != request['automation_state_root']):
            raise ValueError('Automation Binding authorization changed')
    for value in (request['policy_path'], request['authorized_policy_path']):
        if hashlib.sha256(private_file(value).read_bytes()).hexdigest() != request['policy_digest']:
            raise ValueError('Authorized/frozen operator policy changed')
    if stage == 'apply' and inputs.get('policy_path') != request['policy_path']:
        raise ValueError('Apply must use the frozen policy')
    resources = request['resources']
    actual = hashlib.sha256(json.dumps(resources, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if actual != request['resource_digest'] or str(Path(__file__).resolve()) not in request['guard_files']:
        raise ValueError('Automation resource identity mismatch')
    for value, expected in request['guard_files'].items():
        p = Path(value)
        if any(x.is_symlink() for x in (p, *p.parents)) or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise ValueError('PR dependency changed since admission')


def automation_record_output(inputs, output):
    automation_journal_update(inputs, 'child_outputs', output)


def automation_journal_update(inputs, kind, output):
    """Serialize workflow-owned evidence with coordinator journal updates."""
    path = inputs.get('execution_state_path')
    if not path:
        return
    if kind not in ('child_outputs', 'apply_publication'):
        raise ValueError('Invalid workflow journal field')
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('Invalid output journal')
    import fcntl
    import tempfile
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        info = os.fstat(lock.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Invalid journal lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = json.loads(path.read_text())
        rid = os.environ.get('CAO_WORKFLOW_RUN_ID')
        suffix = '-review' if WORKFLOW == 'github-pr-review' else '-apply'
        if (rid != journal['chain_id'] + suffix or output.get('run_id') != rid
                or output.get('workflow') != WORKFLOW
                or output.get('repository') != journal['request']['repository']):
            raise ValueError('Output journal execution identity mismatch')
        if kind == 'child_outputs':
            outputs = journal.setdefault('child_outputs', {})
            if rid in outputs and outputs[rid] != output:
                raise ValueError('A terminal child output cannot be overwritten')
            outputs[rid] = output
        else:
            if journal.get(kind) and journal[kind] != output:
                raise ValueError('Publication intent cannot be replaced')
            journal[kind] = output
        fd, filename = tempfile.mkstemp(prefix='.child-evidence-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(journal, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)
# END GENERATED PR GUARD
