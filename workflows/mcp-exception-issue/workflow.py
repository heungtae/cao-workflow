"""Generated deployment resource. Edit workflows/_incident and run the builder."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from cao_workflow import emit_output, get_inputs, step

SHA = re.compile(r'[0-9a-f]{40}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
SECRET = re.compile(r'gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
MAX_INPUT = 120000


class Blocked(ValueError):
    pass


class ReadFailure(Blocked):
    def __init__(self, retryable=False, retry_after_seconds=0):
        super().__init__('External MCP read failed')
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def instant(value):
    if not isinstance(value, str):
        raise Blocked('Timestamp must be an RFC3339 string')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        raise Blocked('Timestamp requires an explicit timezone')
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)


def timestamp(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def now():
    return dt.datetime.now(dt.timezone.utc)


def private(path, directory=False):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise Blocked('Private path must be absolute without symlink components')
    if directory:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise Blocked('Invalid private path ownership or permissions')
    return path


def atomic(path, data):
    path = Path(path)
    private(path.parent, True)
    if path.exists():
        private(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(root, key):
    root = private(root, True)
    fd = os.open(root / digest(key), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise Blocked('Invalid lock ownership')
    with os.fdopen(fd, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('Another execution owns this lock') from None
        yield


def command(argv, cwd=None, env=None, timeout=180):
    safe_env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', ''),
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    if env:
        safe_env.update(env)
    result = subprocess.run(argv, cwd=cwd, env=safe_env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Blocked(f'{argv[0]} failed (exit {result.returncode}); private output omitted')
    return result.stdout


def credentials(policy):
    path = policy.get('github_token_file')
    return {'GH_TOKEN': private(path).read_text().strip()} if path else {}


def api(repo, path, policy, method='GET', payload=None):
    args = ['gh', 'api', '--method', method, f'repos/{repo}/{path}']
    if payload is not None:
        # Private body file avoids command-line source/Issue text and shell interpretation.
        with tempfile.NamedTemporaryFile(mode='w', prefix='cao-api-', delete=False) as stream:
            json.dump(payload, stream)
            filename = stream.name
        try:
            return json.loads(command(args + ['--input', filename], env=credentials(policy)))
        finally:
            os.unlink(filename)
    return json.loads(command(args, env=credentials(policy)))


def pages(repo, path, policy, limit=300):
    rows = []
    for page in range(1, limit // 100 + 2):
        result = api(repo, f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}', policy)
        if not isinstance(result, list) or len(rows) + len(result) > limit:
            raise Blocked('GitHub discussion/list exceeds completeness budget')
        rows.extend(result)
        if len(result) < 100:
            return rows
    raise Blocked('Incomplete GitHub list')


def load_policy(inputs):
    repo = inputs['repository'].lower()
    if not REPO.fullmatch(repo) or '..' in repo:
        raise Blocked('Invalid repository identity')
    path = private(inputs['policy_path'])
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or repo not in data.get('repositories', {}):
        raise Blocked('Repository missing from versioned operator policy')
    expected = inputs.get('expected_policy_digest')
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise Blocked('Frozen policy identity mismatch')
    policy = data['repositories'][repo]
    for key in ('state_root', 'workspace_root'):
        private(policy[key], True)
    workspace = Path(policy['workspace_root'])
    if path.is_relative_to(workspace) or Path(policy['state_root']).is_relative_to(workspace) or Path(inputs['execution_state_path']).is_relative_to(workspace):
        raise Blocked('Policy, state and journals must stay outside model workspace')
    files = [policy['github_token_file']] if policy.get('github_token_file') else []
    for connection in policy.get('mcp_connections', {}).values():
        files += [connection['token_file']] if connection.get('token_file') else []
        files += list(connection.get('env_files', {}).values())
    for credential in files:
        if private(credential).is_relative_to(workspace):
            raise Blocked('Credential files must stay outside model workspace')
    policy = dict(policy, repository=repo)
    return policy


def resource_digest(workflow_path, profile_path, config_path):
    return digest([hashlib.sha256(Path(p).read_bytes()).hexdigest()
                   for p in (workflow_path, profile_path, config_path)])


def admission(inputs, agent):
    if os.getuid() == 0:
        raise Blocked('Use a non-root workflow service user')
    policy = load_policy(inputs)
    journal = json.loads(private(inputs['execution_state_path']).read_text())
    if journal.get('workflow') != WORKFLOW or journal.get('inputs', {}).get('repository', '').lower() != policy['repository']:
        raise Blocked('Execution journal identity mismatch')
    verify_journal_inputs(inputs, journal)
    actual = resource_digest(__file__, journal['profile_path'], journal['codex_config_path'])
    if actual != inputs.get('expected_resource_digest'):
        raise Blocked('Frozen dependency identity mismatch')
    import tomllib
    config = tomllib.loads(private(journal['codex_config_path']).read_text())
    if (config.get('sandbox_mode') != 'read-only' or config.get('approval_policy') != 'never'
            or config.get('shell_environment_policy', {}).get('inherit') != 'none'):
        raise Blocked('Named read-only credential-isolated Codex configuration required')
    # CAO profiles must disable persisted memory injection.
    if 'codexProfile: cao_incident_readonly' not in Path(journal['profile_path']).read_text():
        raise Blocked('Unexpected model profile')
    base_url = os.environ.get('CAO_WORKFLOW_BASE_URL')
    if not base_url:
        raise Blocked('CAO workflow server identity missing')
    with urlopen(base_url.rstrip('/') + '/settings/memory', timeout=10) as response:
        settings = json.load(response)
    if settings.get('enabled') is not False:
        raise Blocked('Disable CAO server memory injection for incident workflows')
    return policy


def recovery_policy(inputs):
    """Allow read-only outcome reconciliation even when authorization has changed."""
    journal = json.loads(private(inputs['execution_state_path']).read_text())
    if journal.get('workflow') != WORKFLOW or journal.get('inputs', {}).get('repository') != inputs['repository']:
        raise Blocked('Recovery journal identity mismatch')
    verify_journal_inputs(inputs, journal)
    policy = journal.get('reconciliation_policy')
    if not isinstance(policy, dict) or policy.get('repository') != inputs['repository'].lower():
        raise Blocked('Missing retained reconciliation binding')
    return policy


def verify_journal_inputs(inputs, journal):
    frozen = journal.get('inputs', {})
    for key, value in frozen.items():
        if key != 'recovery_only' and inputs.get(key) != value:
            raise Blocked('Execution input differs from frozen journal')
    for key in ('operator_retry_reason', 'retry_incident_id', 'issue_number', 'monitor_id', 'since', 'until'):
        if inputs.get(key) != frozen.get(key):
            raise Blocked('Execution acquired an unauthorized input override')


def redact(value):
    serialized = json.dumps(value, ensure_ascii=False)
    if SECRET.search(serialized):
        raise Blocked('Credential-shaped evidence cannot enter model/publication')
    return value


def model(work, agent, task, step_id):
    serialized = json.dumps(redact(task), ensure_ascii=False)
    if len(serialized.encode()) > MAX_INPUT:
        raise Blocked('Model context exceeds 120KB budget')
    carrier = private(work / 'inputs', True)
    token = 'CAO_INCIDENT_INPUT_' + secrets.token_hex(16)
    path = carrier / (token + '.json')
    atomic(path, task)
    try:
        result = step('codex', agent, ': ' + token, step_id=step_id + '-' + digest(task)[:16],
                      recovery='manual', timeout=900, working_directory=str(work))
        raw = str(result.output).strip()
        matches = list(re.finditer(r'(?m)^• (?=\{\s*")', raw))
        if matches:
            raw = raw[matches[-1].end():]
            raw = re.sub(r'(?<=\w)\n {2,}(?=\w)', ' ', raw)
            raw = re.sub(r'\n {2,}', '', raw)
            value, _ = json.JSONDecoder().raw_decode(raw)
        else:
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise Blocked('Model must return one JSON object')
        return redact(value)
    finally:
        path.unlink(missing_ok=True)


def relative(name):
    if not isinstance(name, str) or not name:
        raise Blocked('Missing source path')
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name or '\\' in name or '\x00' in name
            or any(p in ('.', '..', '.git', '.codex', '.agents', '.aws', 'AGENTS.md', 'credentials', 'auth.json', 'hosts.yml') or p.startswith('.env') for p in path.parts)):
        raise Blocked('Unsafe source path')
    return name


def source_files(repo, sha, names, policy):
    if not SHA.fullmatch(sha):
        raise Blocked('Source revision must be an exact commit SHA')
    if len(names) > 40:
        raise Blocked('Source context exceeds file budget')
    files = {}
    for name in names:
        relative(name)
        row = api(repo, f'contents/{quote(name, safe="/")}?ref={sha}', policy)
        if row.get('type') != 'file' or row.get('encoding') != 'base64' or row.get('size', 40001) > 40000:
            raise Blocked('Source file is unavailable, unsafe or over budget')
        value = base64.b64decode(row['content']).decode('utf-8')
        if len(value.encode()) > 40000 or '\x00' in value:
            raise Blocked('Binary or oversized source')
        files[name] = value
    return files


def resolve_sha(repo, ref, policy):
    row = api(repo, 'commits/' + quote(ref, safe=''), policy)
    sha = row.get('sha', '')
    if not SHA.fullmatch(sha):
        raise Blocked('GitHub could not resolve revision')
    return sha


def revision(record, scope, policy):
    candidates = set()
    for key in ('deployment_sha', 'deployment_ref'):
        if record.get(key):
            if not isinstance(record[key], str) or len(record[key]) > 200:
                raise Blocked('Invalid deployment reference')
            candidates.add(resolve_sha(policy['repository'], record[key], policy))
    for row in scope.get('deployments', []):
        if instant(row['start']) <= instant(record['occurred_at']) < instant(row['end']):
            if not row.get('instance_id') or row['instance_id'] == record.get('instance_id'):
                candidates.add(resolve_sha(policy['repository'], row['revision'], policy))
    connection = policy.get('mcp_connections', {}).get(scope.get('connection'), {})
    if not candidates and 'revision' in connection.get('tools', {}):
        request = {'contract_version': '1.0', 'service': scope['service'], 'environment': scope['environment'], 'at': record['occurred_at']}
        if record.get('instance_id'):
            request['instance_id'] = record['instance_id']
        rows = asyncio.run(asyncio.wait_for(collect_async(connection, 'revision', request), timeout=180))
        for candidate in rows:
            for key in ('deployment_sha', 'deployment_ref'):
                if candidate.get(key):
                    candidates.add(resolve_sha(policy['repository'], candidate[key], policy))
    if len(candidates) != 1:
        raise Blocked('Unambiguous deployed revision is required')
    return candidates.pop()


def validate_page(payload, request):
    if (not isinstance(payload, dict) or payload.get('contract_version') != '1.0'
            or not isinstance(payload.get('query_id'), str) or not payload['query_id']
            or not isinstance(payload.get('records'), list)
            or len(payload['records']) > request['limit']):
        raise Blocked('Invalid external MCP response contract')
    instant(payload['observed_at'])
    coverage = payload['coverage']
    if (instant(coverage['start']) != instant(request['start']) or instant(coverage['end']) != instant(request['end'])
            or coverage.get('complete') is not True or coverage.get('reason') is not None):
        raise Blocked('MCP evidence coverage is incomplete')
    cursor = payload.get('next_cursor')
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise Blocked('Invalid continuation cursor')
    for record in payload['records']:
        if any(not isinstance(record.get(k), str) or not record[k] for k in ('record_id', 'source_id', 'service', 'environment')):
            raise Blocked('Invalid MCP record identity')
        if (not isinstance(record.get('message'), str) or record['service'] != request['service']
                or record['environment'] != request['environment']
                or record['source_id'] not in request['source_ids']
                or not instant(request['start']) <= instant(record['occurred_at']) < instant(request['end'])
                or any(record.get(k) != v for k, v in request.get('filters', {}).items())):
            raise Blocked('MCP record escaped authorized query')
        for key in ('severity', 'stack_trace', 'trace_id', 'request_id', 'instance_id', 'deployment_sha', 'deployment_ref'):
            if key in record and (not isinstance(record[key], str) or (key != 'stack_trace' and not record[key])):
                raise Blocked('Invalid optional MCP record metadata')
    return payload


async def collect_async(connection, operation, request, max_records=10000, max_bytes=5000000):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    import httpx
    tools = connection['tools']
    tool = tools[operation]
    async with contextlib.AsyncExitStack() as stack:
        if connection['transport'] == 'stdio':
            argv = connection['argv']
            if not isinstance(argv, list) or not argv or not Path(argv[0]).is_absolute():
                raise Blocked('External stdio executable must be absolute')
            env = {'PATH': os.environ.get('PATH', '')}
            for key, file in connection.get('env_files', {}).items():
                env[key] = private(file).read_text().strip()
            errlog = stack.enter_context(open(os.devnull, 'w'))
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(command=argv[0], args=argv[1:], env=env), errlog=errlog))
        elif connection['transport'] == 'streamable-http':
            endpoint = connection['url']
            parsed = urlsplit(endpoint)
            if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
                raise Blocked('Remote MCP endpoint requires HTTPS without URL credentials')
            headers = {}
            if connection.get('token_file'):
                headers['Authorization'] = 'Bearer ' + private(connection['token_file']).read_text().strip()
            client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=False))
            streams = await stack.enter_async_context(streamable_http_client(endpoint, http_client=client))
        else:
            raise Blocked('Unsupported externally provided MCP transport')
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        discovered, seen = {}, set()
        cursor = None
        while True:
            listed = await session.list_tools(cursor=cursor)
            discovered.update({row.name: row.inputSchema for row in listed.tools})
            cursor = listed.nextCursor
            if cursor is None:
                break
            if cursor in seen or len(seen) >= 20:
                raise Blocked('MCP tool discovery pagination failed')
            seen.add(cursor)
        if tool not in discovered:
            raise Blocked('Configured MCP read tool missing')
        import jsonschema
        schema = discovered[tool]
        required = {'contract_version', 'service', 'environment', 'at'} if operation == 'revision' else {'contract_version', 'service', 'environment', 'start', 'end'}
        if not required <= set(schema.get('required', [])):
            raise Blocked('External tool schema lacks canonical required fields')
        if connection.get('schema_digests', {}).get(operation) != digest(schema):
            raise Blocked('External tool schema differs from operator-qualified binding')
        records, cursors, query_id, observed = [], set(), None, None
        args = dict(request)
        while True:
            jsonschema.validate(args, schema)
            result = await session.call_tool(tool, args)
            payload = result.structuredContent
            if payload is None:
                texts = [row.text for row in result.content if getattr(row, 'type', '') == 'text']
                if len(texts) != 1:
                    raise Blocked('Ambiguous MCP tool response')
                payload = json.loads(texts[0])
            if result.isError:
                hint = payload.get('retry_after_seconds', 0)
                if (payload.get('contract_version') != '1.0' or type(payload.get('retryable')) is not bool
                        or not isinstance(payload.get('message'), str) or not isinstance(payload.get('code'), str)
                        or type(hint) not in (int, float) or not math.isfinite(hint) or not 0 <= hint <= 604800):
                    raise Blocked('Invalid canonical MCP error contract')
                retryable = payload['retryable'] and payload['code'] in ('RATE_LIMITED', 'TIMEOUT', 'BACKEND_UNAVAILABLE')
                raise ReadFailure(retryable, hint)
            if operation == 'revision':
                if (payload.get('contract_version') != '1.0' or payload.get('status') != 'resolved'
                        or payload.get('reason') is not None or not isinstance(payload.get('candidates'), list)
                        or len(payload['candidates']) != 1):
                    raise Blocked('MCP deployment identity is unresolved or ambiguous')
                row = payload['candidates'][0]
                if (row.get('service') != request['service'] or row.get('environment') != request['environment']
                        or not (row.get('deployment_sha') or row.get('deployment_ref'))
                        or not (row.get('provenance_id') or row.get('metadata_source'))):
                    raise Blocked('Invalid MCP deployment provenance')
                return payload['candidates']
            validate_page(payload, request)
            if query_id is not None and (payload['query_id'] != query_id or payload['observed_at'] != observed):
                raise Blocked('MCP pagination changed query snapshot')
            query_id, observed = payload['query_id'], payload['observed_at']
            for row in payload['records']:
                row = dict(row)
                row['evidence_id'] = digest([connection['id'], row['source_id'], row['record_id']])
                row['provenance'] = {'server_id': connection['id'], 'tool': tool, 'query': request,
                                     'query_id': query_id, 'retrieved_at': timestamp(now())}
                records.append(row)
            if len(records) > max_records or len(json.dumps(records).encode()) > max_bytes:
                raise Blocked('MCP evidence budget exhausted')
            cursor = payload['next_cursor']
            if cursor is None:
                return records
            if cursor in cursors or len(cursors) >= 100:
                raise Blocked('Repeated or excessive MCP continuation cursors')
            cursors.add(cursor)
            args['cursor'] = cursor


def collect(scope, policy, operation, start, end, filters=None):
    if start >= end or operation not in ('exceptions', 'context'):
        raise Blocked('Invalid MCP interval or operation')
    filters = filters or {}
    if set(filters) - {'trace_id', 'request_id', 'instance_id'} or any(not isinstance(v, str) or not v for v in filters.values()):
        raise Blocked('Only canonical equality filters are accepted')
    connection = policy['mcp_connections'][scope['connection']]
    request = {'contract_version': '1.0', 'service': scope['service'], 'environment': scope['environment'],
               'source_ids': scope['source_ids'], 'start': timestamp(start), 'end': timestamp(end),
               'limit': scope.get('page_limit', 500), 'filters': filters}
    if not request['source_ids'] or type(request['limit']) is not int or not 1 <= request['limit'] <= 1000:
        raise Blocked('Invalid MCP authorized sources/page limit')
    try:
        records = asyncio.run(asyncio.wait_for(collect_async(connection, operation, request), timeout=180))
        for row in records:
            for key in ('message', 'stack_trace'):
                if key in row:
                    row[key] = SECRET.sub('[REDACTED]', row[key])
                    for pattern in policy.get('redact_patterns', []):
                        row[key] = re.sub(pattern, '[REDACTED]', row[key])
        return records
    except (Blocked,):
        raise
    except (TimeoutError, OSError):
        raise ReadFailure(True) from None
    except Exception:
        raise Blocked('External MCP transport/contract validation failed') from None


def evidence(record, scope, policy, minutes, cutoff):
    t0 = instant(record['occurred_at'])
    start, end = t0 - dt.timedelta(minutes=minutes), min(t0 + dt.timedelta(minutes=minutes), cutoff)
    filters = {key: record[key] for key in ('trace_id', 'request_id', 'instance_id') if record.get(key)}
    records = collect(scope, policy, 'context', start, end, filters)
    for name in scope.get('related_monitors', []):
        related = policy['monitors'][name]
        # Cross-service acquisition must have an explicit operator-bound repository.
        if related.get('repository', policy['repository']).lower() != policy['repository']:
            raise Blocked('Cross-repository related source is not qualified in this release')
        records += collect(related, policy, 'context', start, end, {k: v for k, v in filters.items() if k != 'instance_id'})
    return records


def finish(inputs, result):
    result.update(workflow=WORKFLOW, version=VERSION, repository=inputs.get('repository'),
                  run_id=os.environ.get('CAO_WORKFLOW_RUN_ID', 'local'))
    emit_output(result)


def main():
    inputs = get_inputs()
    try:
        result = process(inputs)
    except Blocked:
        result = {'status': 'blocked', 'reason': 'Admission, evidence or publication gate blocked; inspect private execution state'}
    except Exception:
        result = {'status': 'failed', 'reason': 'Execution failed; no fresh retry authorized'}
    finish(inputs, result)


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
