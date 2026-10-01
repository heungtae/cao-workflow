"""CAO GitHub review application. Model proposes edits; deterministic code writes."""
from __future__ import annotations

import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from urllib.request import urlopen

from cao_workflow import emit_output, get_inputs, step

INPUTS = {
    "repository": {"type": "string", "required": True},
    "pr_number": {"type": "int", "required": True},
    "head_sha": {"type": "string", "required": True},
    "base_sha": {"type": "string", "required": False},
    "review_id": {"type": "int", "required": True},
    "policy_path": {"type": "string", "required": True},
    "expected_findings": {"type": "int", "required": False},
    "apply_mode": {"type": "string", "required": False, "default": "patch"},
    "workspace_root": {"type": "string", "required": False, "default": "/tmp/cao-pr-apply"},
    "model": {"type": "string", "required": False},
}
WORKFLOW = "github-pr-apply"
VERSION = "v1"
REVIEW_VERSION = "v6"
MAX_CONTEXT = 120000
MAX_FILE = 40000
MAX_DIFF = 2000000
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_RE = re.compile(r'(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}')


class ApplyFailure(RuntimeError):
    def __init__(self, result: dict):
        super().__init__('Application failed; candidate artifacts were retained')
        self.result = result


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 180, quiet: bool = False) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
                          stderr=subprocess.DEVNULL if quiet else subprocess.PIPE,
                          env=env, text=True, timeout=timeout, check=False)
    if proc.returncode:
        # Do not leak command output, credential-bearing URLs, or test output.
        raise RuntimeError(f"{args[0]} failed with exit code {proc.returncode}")
    return proc.stdout or ''


def api(repository: str, path: str) -> object:
    return json.loads(run(["gh", "api", f"repos/{repository}/{path}"]))


def pages(repository: str, path: str, limit: int = 300) -> list[dict]:
    result = []
    for page in range(1, (limit + 99) // 100 + 1):
        rows = api(repository, f"{path}?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("Invalid GitHub list response")
        result.extend(rows)
        if len(rows) < 100:
            return result
        if len(result) >= limit:
            raise ValueError("GitHub pagination limit reached")
    raise ValueError("Incomplete GitHub list")


def private_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Workspace must be absolute and not a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Workspace must be owner-only (0700)")
    return path.resolve()


def lock_pr(root: Path, repository: str, number: int):
    directory = private_directory(root / "locks")
    key = hashlib.sha256(f"{repository.lower()}#{number}".encode()).hexdigest()
    fd = os.open(directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("Invalid PR lock ownership")
    stream = os.fdopen(fd, "w")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        stream.close()
        raise RuntimeError("Another run owns this PR lock") from None
    return stream


def load_policy(path: str, repository: str) -> dict:
    requested = Path(path)
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError("Policy must be an absolute operator-owned file")
    info = requested.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Policy must be owned by this user with mode 0600")
    data = json.loads(requested.read_text())
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported apply policy schema")
    policy = data.get("repositories", {}).get(repository.lower())
    if not isinstance(policy, dict):
        raise ValueError("Repository is absent from apply policy")
    for key in ("review_authors", "editable_paths"):
        if not isinstance(policy.get(key), list) or not policy[key] or any(not isinstance(x, str) or not x for x in policy[key]):
            raise ValueError(f"Invalid policy {key}")
    for key in ("context_paths", "new_files", "push_branches"):
        if not isinstance(policy.get(key, []), list) or any(not isinstance(x, str) or not x for x in policy.get(key, [])):
            raise ValueError(f"Invalid policy {key}")
    image = policy.get("test_image", "")
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ValueError("Tests require a digest-pinned container image")
    commands = policy.get("test_commands")
    if not isinstance(commands, list) or not commands or len(commands) > 10:
        raise ValueError("At least one operator-configured test is required")
    if any(not isinstance(c, list) or not c or any(not isinstance(a, str) or not a or '\x00' in a for a in c) for c in commands):
        raise ValueError("Test commands must be argv arrays")
    return policy


def review_marker(repository: str, number: int, sha: str) -> str:
    payload = {"repository": repository.lower(), "pr": number, "head_sha": sha,
               "workflow": "github-pr-review", "version": REVIEW_VERSION}
    token = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
    return f"<!-- cao-review {token} -->"


def check_pr(pr: dict, repository: str, number: int, sha: str | None = None) -> None:
    if (pr.get("number") != number or pr.get("state") != "open" or pr.get("draft")
            or pr.get("base", {}).get("repo", {}).get("full_name", "").lower() != repository.lower()
            or not SHA_RE.fullmatch(pr.get("head", {}).get("sha", ""))):
        raise ValueError("PR identity/state does not match the request")
    if sha and pr["head"]["sha"] != sha:
        raise ValueError("PR HEAD changed; a new review is required")


def changed_lines(patch: str) -> set[int]:
    result, line = set(), None
    for raw in patch.splitlines():
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if match:
            line = int(match.group(1))
        elif line is not None and raw.startswith('+') and not raw.startswith('+++'):
            result.add(line)
            line += 1
        elif line is not None and raw.startswith(' '):
            line += 1
    return result


def validate_review(repository: str, number: int, sha: str, review_id: int,
                    policy: dict, expected_findings: int | None = None,
                    base_sha: str | None = None, validate_locations: bool = True) -> tuple[dict, list[dict]]:
    review = api(repository, f"pulls/{number}/reviews/{review_id}")
    author = review.get("user", {}).get("login")
    if (type(review.get("id")) is not int or review["id"] != review_id
            or review.get("state") != "COMMENTED" or review.get("commit_id") != sha
            or author not in policy["review_authors"]
            or review_marker(repository, number, sha) not in (review.get("body") or "")
            or (base_sha and f'<!-- cao-review-base {base_sha} -->' not in (review.get('body') or ''))
            or (review.get("html_url") or '').lower() != f"https://github.com/{repository}/pull/{number}#pullrequestreview-{review_id}".lower()):
        raise ValueError("Review author/identity/HEAD/marker is not eligible for apply")
    if not validate_locations:
        return review, []
    comments = pages(repository, f"pulls/{number}/reviews/{review_id}/comments", 100)
    files = pages(repository, f"pulls/{number}/files")
    allowed = {f["filename"]: changed_lines(f.get("patch") or "") for f in files}
    if expected_findings is not None and (type(expected_findings) is not int or expected_findings != len(comments)):
        raise ValueError("Review finding/comment count differs")
    if not comments:
        return review, []
    seen = set()
    for comment in comments:
        cid, path, line = comment.get("id"), comment.get("path"), comment.get("line")
        if (type(cid) is not int or cid <= 0 or cid in seen
                or comment.get("pull_request_review_id") != review_id
                or comment.get("user", {}).get("login") != author
                or comment.get("commit_id") != sha or comment.get("in_reply_to_id")
                or comment.get("side") != "RIGHT" or type(line) is not int
                or line not in allowed.get(path, set()) or not isinstance(comment.get("body"), str)):
            raise ValueError("Inline comment is not an original finding on the reviewed changed line")
        seen.add(cid)
    return review, comments


def safe_path(source: Path, relative: str, policy: dict) -> Path:
    path = PurePosixPath(relative)
    if (not isinstance(relative, str) or not relative or path.is_absolute()
            or relative != path.as_posix() or any(p in ("..", ".git") for p in path.parts)
            or '\\' in relative or '\x00' in relative
            or any(p.startswith('.env') or p in ("credentials", "auth.json", "hosts.yml") for p in path.parts)
            or not any(fnmatch.fnmatchcase(relative, pattern) for pattern in policy["editable_paths"])):
        raise ValueError("Edit path is outside the operator allowlist")
    candidate = source / relative
    for parent in [candidate, *candidate.parents]:
        if parent == source:
            break
        if parent.is_symlink():
            raise ValueError("Symlink edit paths are forbidden")
    if not candidate.resolve().is_relative_to(source.resolve()):
        raise ValueError("Edit path escapes checkout")
    return candidate


def git(source: Path, *args: str) -> str:
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "core.attributesfile=/dev/null", "-c", "core.autocrlf=false",
                "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                "-c", "diff.external=", "-c", "protocol.file.allow=never", "-C", str(source), *args])


def checkout(repository: str, number: int, sha: str, root: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix="candidate-", dir=root))
    source = work / "source"
    try:
        run(['git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
             'clone', '--no-checkout', f'https://github.com/{repository}.git', str(source)])
        git(source, "fetch", "--no-tags", "origin", f"refs/pull/{number}/head")
        if git(source, "rev-parse", "FETCH_HEAD").strip() != sha:
            raise ValueError("PR HEAD changed during checkout")
        git(source, "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=",
            "-c", "filter.lfs.required=false", "checkout", "--detach", sha)
        return work
    except Exception:
        shutil.rmtree(work)
        raise


def context_files(source: Path, comments: list[dict], policy: dict) -> dict[str, str | None]:
    names = {c["path"] for c in comments} | set(policy.get("context_paths", [])) | set(policy.get("new_files", []))
    if len(names) > 40:
        raise ValueError("Apply context file count exceeds 40")
    files = {}
    for name in sorted(names):
        path = safe_path(source, name, policy)
        if not path.exists() and name in policy.get("new_files", []):
            files[name] = None
        elif not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError("Apply context requires bounded regular text files")
        else:
            text = path.read_text(encoding="utf-8")
            if '\x00' in text:
                raise ValueError("Binary edit files are forbidden")
            files[name] = text
    return files


def response_object(raw: str) -> dict:
    raw = raw.strip()
    matches = list(re.finditer(r'(?m)^• (?=\{\s*")', raw))
    if matches:
        raw = raw[matches[-1].end():]
        raw = re.sub(r"(?<=\w)\n {2,}(?=\w)", " ", raw)
        raw = re.sub(r"\n {2,}", "", raw)
        value, _ = json.JSONDecoder().raw_decode(raw)
    else:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Apply response is not an object")
    return value


def propose(root: Path, files: dict, comments: list[dict], model: str | None) -> dict:
    task = {"files": files, "comments": [{k: c[k] for k in ('id', 'path', 'line', 'body')} for c in comments]}
    serialized = json.dumps(task, ensure_ascii=False)
    if len(serialized.encode()) > MAX_CONTEXT:
        raise ValueError("Apply context exceeds budget")
    if re.search(r'gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', serialized):
        raise ValueError('Credential-shaped data cannot be sent as apply context')
    # No target checkout path, Git config, or credential is supplied to the model.
    inputs_dir = private_directory(root / "inputs")
    token = "CAO_APPLY_INPUT_" + secrets.token_hex(16)
    path = inputs_dir / f"{token}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(serialized)
        options = {"model": model} if model else {}
        result = step("codex", "pr-review-applier", f": {token}",
                      step_id="propose-edits", recovery="manual", timeout=900,
                      working_directory=str(root), **options)
        return response_object(str(result.output))
    finally:
        path.unlink(missing_ok=True)


def apply_edits(source: Path, files: dict, comments: list[dict], proposal: dict, policy: dict) -> list[dict]:
    edits, outcomes = proposal.get("edits"), proposal.get("outcomes")
    if not isinstance(edits, list) or not isinstance(outcomes, list) or len(edits) > 40:
        raise ValueError("Invalid edits/outcomes contract")
    expected = {c["id"] for c in comments}
    ids, cleaned = [], []
    for row in outcomes:
        if (not isinstance(row, dict) or type(row.get("comment_id")) is not int
                or row.get("status") not in ("addressed", "unaddressed")
                or not isinstance(row.get("reason"), str) or len(row["reason"]) > 1000):
            raise ValueError("Invalid per-comment outcome")
        ids.append(row["comment_id"])
        reason = re.sub(r'gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[0-9A-Z]{16}', '[REDACTED]', row['reason'])
        cleaned.append({'comment_id': row['comment_id'], 'status': row['status'], 'reason': reason})
    if set(ids) != expected or len(ids) != len(expected):
        raise ValueError("Each original finding requires exactly one outcome")
    addressed = {o['comment_id'] for o in outcomes if o['status'] == 'addressed'}
    prepared, seen = [], set()
    supported = set()
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            raise ValueError("Invalid edit path")
        name, old, new = edit['path'], edit.get('old'), edit.get('new')
        references = edit.get('comment_ids')
        if (name not in files or name in seen or not isinstance(old, str) or not isinstance(new, str)
                or old == new or len(new.encode()) > MAX_FILE or '\x00' in new
                or not isinstance(references, list) or not references
                or any(type(cid) is not int or cid not in addressed for cid in references)):
            raise ValueError("Edit is not supported by the supplied files/findings")
        path = safe_path(source, name, policy)
        original = files[name]
        if original is None:
            if path.exists() or old or not new:
                raise ValueError("Invalid new-file edit")
            content = new
        else:
            if path.read_text() != original or not old or original.count(old) != 1:
                raise ValueError("Replacement must match the unchanged file exactly once")
            content = original.replace(old, new, 1)
        prepared.append((path, content))
        supported.update(references)
        seen.add(name)
    if addressed != supported:
        raise ValueError("An addressed finding must have a supporting edit")
    # Validate every edit before mutating any candidate file.
    for path, content in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return cleaned


def test_candidate(source: Path, work: Path, policy: dict) -> list[dict]:
    copy = work / "test-source"
    shutil.copytree(source, copy, symlinks=True, ignore=shutil.ignore_patterns('.git', '.env', '.env.*'))
    results = []
    try:
        for command in policy['test_commands']:
            name = "cao-apply-test-" + secrets.token_hex(12)
            args = ["docker", "run", "--name", name, "--rm", "--pull=never", "--network=none",
                    "--label", "cao-workflow=github-pr-apply", "--label",
                    "cao.apply.run_id=" + os.environ.get('CAO_WORKFLOW_RUN_ID', 'local'),
                    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--pids-limit=128", "--memory=2g", "--cpus=2", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--env", "HOME=/tmp",
                    "--mount", f"type=bind,src={copy},dst=/work", "--workdir", "/work",
                    "--entrypoint", command[0], policy['test_image'], *command[1:]]
            try:
                run(args, timeout=600, quiet=True)
            finally:
                # A timed-out CLI can leave a container behind. Never leak a worker.
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
            results.append({"command": command, "result": "passed"})
        return results
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def apply_key(inputs: dict, policy: dict) -> str:
    value = {k: inputs[k] for k in ('repository', 'pr_number', 'head_sha', 'review_id')}
    value['repository'] = value['repository'].lower()
    value.update(version=VERSION, policy=policy)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def reconciled(repository: str, pr: dict, sha: str, review_id: int, key: str) -> bool:
    if pr['head']['sha'] == sha:
        return False
    commit = api(repository, f"commits/{pr['head']['sha']}")
    message = commit.get('commit', {}).get('message', '')
    matches = (len(commit.get('parents', [])) == 1 and commit['parents'][0].get('sha') == sha
               and f"CAO-Apply-Key: {key}" in message.splitlines()
               and f"CAO-Review-ID: {review_id}" in message.splitlines())
    if not matches:
        return False
    runs = re.findall(r'^CAO-Apply-Run: ([A-Za-z0-9._-]{1,128})$', message, re.MULTILINE)
    if len(runs) != 1:
        raise ValueError('Remote apply marker lacks a unique CAO run; reconcile manually')
    prior = retained_result(runs[0])
    output = prior.get('output') or {}
    if (prior.get('run_id') != runs[0] or prior.get('state') != 'completed'
            or output.get('run_id') != runs[0] or output.get('workflow') != WORKFLOW
            or output.get('version') != VERSION or output.get('result') != 'applied'
            or output.get('apply_mode') != 'push' or output.get('apply_key') != key
            or output.get('head_sha') != sha or output.get('review_id') != review_id
            or output.get('pr') != pr['number'] or output.get('repository', '').lower() != repository.lower()
            or output.get('commit_sha') != pr['head']['sha']):
        raise ValueError('Remote marker is not backed by a verified completed CAO application')
    return True


def retained_result(run_id: str) -> dict:
    base_url = os.environ.get('CAO_API_BASE_URL', '')
    if not base_url.startswith(('http://', 'https://')):
        raise ValueError('CAO journal endpoint is unavailable for reconciliation')
    with urlopen(f"{base_url.rstrip('/')}/workflows/runs/{quote(run_id, safe='')}/result", timeout=30) as response:
        value = json.loads(response.read(2000000))
    if not isinstance(value, dict):
        raise ValueError('Invalid retained CAO application result')
    return value


def push_gate(repository: str, pr: dict, policy: dict) -> str:
    head = pr['head']
    branch = head.get('ref', '')
    if (head.get('repo', {}).get('full_name', '').lower() != repository.lower()
            or not any(fnmatch.fnmatchcase(branch, p) for p in policy.get('push_branches', []))
            or branch == pr['base']['ref']):
        raise ValueError("Fork/base/non-allowlisted branch push is forbidden")
    info = api(repository, f"branches/{quote(branch, safe='')}")
    if info.get('protected') is not False or info.get('name') != branch:
        raise ValueError("Protected or unknown branch push is forbidden")
    repo = api(repository, "")
    if repo.get('default_branch') == branch:
        raise ValueError("Default branch push is forbidden")
    for key in ('git_author_name', 'git_author_email'):
        if not isinstance(policy.get(key), str) or not policy[key] or any(c in policy[key] for c in '\r\n\x00'):
            raise ValueError("Push requires an operator-configured Git author")
    return branch


def publish(repository: str, pr: dict, source: Path, work: Path, inputs: dict, policy: dict, key: str) -> str:
    latest = api(repository, f"pulls/{inputs['pr_number']}")
    check_pr(latest, repository, inputs['pr_number'], inputs['head_sha'])
    branch = push_gate(repository, latest, policy)
    if latest['head']['ref'] != pr['head']['ref'] or latest['head']['repo']['full_name'] != pr['head']['repo']['full_name']:
        raise ValueError("PR branch ownership changed")
    if latest['base']['sha'] != pr['base']['sha']:
        raise ValueError('PR base changed before publication')
    git(source, "check-ref-format", f"refs/heads/{branch}")
    remote = f"https://github.com/{repository}.git"
    remote_sha = git(source, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                     "ls-remote", remote, f"refs/heads/{branch}").split()
    if len(remote_sha) != 2 or remote_sha[0] != inputs['head_sha']:
        raise ValueError("Remote HEAD changed before push")
    message = work / "commit-message"
    run_id = os.environ.get('CAO_WORKFLOW_RUN_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', run_id):
        raise ValueError('Publication requires a valid CAO run identity')
    message.write_text(f"fix: address CAO PR review {inputs['review_id']}\n\nCAO-Review-ID: {inputs['review_id']}\n"
                       f"CAO-Original-Head: {inputs['head_sha']}\nCAO-Apply-Key: {key}\nCAO-Apply-Run: {run_id}\n")
    git(source, "-c", f"user.name={policy['git_author_name']}", "-c", f"user.email={policy['git_author_email']}",
        "-c", "commit.gpgsign=false", "commit", "--file", str(message))
    commit = git(source, "rev-parse", "HEAD").strip()
    if git(source, 'show', '-s', '--format=%P', commit).strip() != inputs['head_sha']:
        raise ValueError('Published commit must have exactly the reviewed HEAD as its parent')
    git(source, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
        "push", f"--force-with-lease=refs/heads/{branch}:{inputs['head_sha']}", remote, f"HEAD:refs/heads/{branch}")
    verified = git(source, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                   "ls-remote", remote, f"refs/heads/{branch}").split()
    if not verified or verified[0] != commit:
        raise RuntimeError("Push result cannot be verified; reconcile before retry")
    return commit


def process(inputs: dict) -> dict:
    if os.getuid() == 0:
        raise ValueError('Apply must run as a non-root service user')
    repository, number, sha, rid = (inputs[k] for k in ('repository', 'pr_number', 'head_sha', 'review_id'))
    if (not isinstance(repository, str) or not REPO_RE.fullmatch(repository) or '..' in repository
            or type(number) is not int or number <= 0 or type(rid) is not int or rid <= 0
            or not isinstance(sha, str) or not SHA_RE.fullmatch(sha)
            or inputs.get('apply_mode', 'patch') not in ('patch', 'push')):
        raise ValueError("Invalid apply inputs")
    policy = load_policy(inputs['policy_path'], repository)
    root = private_directory(Path(inputs.get('workspace_root', '/tmp/cao-pr-apply')))
    if Path(inputs['policy_path']).resolve().is_relative_to(root):
        raise ValueError("Operator policy must be outside the model workspace")
    key = apply_key(inputs, policy)
    base = {"workflow": WORKFLOW, "version": VERSION, "repository": repository,
            "pr": number, "head_sha": sha, "review_id": rid, "apply_mode": inputs.get('apply_mode', 'patch'),
            "run_id": os.environ.get('CAO_WORKFLOW_RUN_ID', ''), "apply_key": key}
    with lock_pr(root, repository, number):
        pr = api(repository, f"pulls/{number}")
        check_pr(pr, repository, number)
        base_sha = inputs.get('base_sha', pr['base']['sha'])
        if not SHA_RE.fullmatch(base_sha) or pr['base']['sha'] != base_sha:
            raise ValueError('PR base changed; a new review is required')
        validate_review(repository, number, sha, rid, policy, base_sha=base_sha, validate_locations=False)
        if reconciled(repository, pr, sha, rid, key):
            return {**base, "result": "skipped", "reason": "already applied", "commit_sha": pr['head']['sha']}
        check_pr(pr, repository, number, sha)
        review, comments = validate_review(repository, number, sha, rid, policy, inputs.get('expected_findings'), base_sha)
        if not comments:
            return {**base, "result": "skipped", "reason": "no findings"}
        if base['apply_mode'] == 'push':
            push_gate(repository, pr, policy)
        work = checkout(repository, number, sha, root)
        source = work / 'source'
        result = {**base, "result": "failed", "artifact_directory": str(work)}
        try:
            result['stage'] = 'context'
            files = context_files(source, comments, policy)
            result['stage'] = 'proposal'
            proposal = propose(root, files, comments, inputs.get('model'))
            result['stage'] = 'edits'
            outcomes = apply_edits(source, files, comments, proposal, policy)
            result['outcomes'] = outcomes
            result['stage'] = 'diff'
            git(source, "add", "--all")
            names = git(source, "diff", "--cached", "--name-only", "-z").split('\x00')
            names = [n for n in names if n]
            for name in names:
                safe_path(source, name, policy)
                if name not in files:
                    raise ValueError("Unexpected changed file")
            git(source, "diff", "--cached", "--check")
            diff = git(source, "diff", "--cached", "--binary", "--no-ext-diff")
            if len(diff.encode()) > MAX_DIFF:
                raise ValueError("Candidate patch exceeds budget")
            patch = work / 'candidate.patch'
            patch.write_text(diff)
            patch.chmod(0o600)
            result.update(changed_files=names, patch_path=str(patch), patch_sha256=hashlib.sha256(diff.encode()).hexdigest())
            if not names:
                result.update(result='partial', reason='no supported edits')
                return result
            result['stage'] = 'tests'
            result['checks'] = test_candidate(source, work, policy)
            result['stage'] = 'recheck'
            latest = api(repository, f"pulls/{number}")
            check_pr(latest, repository, number, sha)
            if latest['base']['sha'] != base_sha or latest['head']['ref'] != pr['head']['ref'] or latest['head']['repo']['full_name'] != pr['head']['repo']['full_name']:
                raise ValueError('PR base or branch ownership changed during apply')
            partial = any(row['status'] != 'addressed' for row in outcomes)
            result['result'] = 'partial' if partial else 'applied'
            if base['apply_mode'] == 'push' and not partial:
                result['stage'] = 'publish'
                result['commit_sha'] = publish(repository, pr, source, work, inputs, policy, key)
            elif base['apply_mode'] == 'push':
                result['reason'] = 'partial application is never pushed'
            result['stage'] = 'complete'
            return result
        except Exception as exc:
            result.update(result='failed', error_type=type(exc).__name__, reason='application or verification failed; inspect retained artifacts and CAO step status')
            raise ApplyFailure(result) from None
        finally:
            path = work / 'result.json'
            path.write_text(json.dumps(result, indent=2))
            path.chmod(0o600)
            # Retain the patch/result, never retain repository credentials/config.
            shutil.rmtree(source, ignore_errors=True)


def main() -> None:
    def interrupt(signum, frame):
        raise InterruptedError('Apply interrupted')
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        emit_output(process(get_inputs()))
    except Exception as exc:
        emit_output(getattr(exc, 'result', {"workflow": WORKFLOW, "version": VERSION, "result": "failed",
                    "run_id": os.environ.get('CAO_WORKFLOW_RUN_ID', ''), "error": type(exc).__name__}))
        raise RuntimeError("Apply failed; inspect retained result and CAO journal") from None


if __name__ == '__main__':
    main()
