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
