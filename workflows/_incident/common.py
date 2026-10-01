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
