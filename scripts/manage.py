#!/usr/bin/env python3
"""Repository-owned CAO deployment management. Exit: 1 runtime, 2 validation, 3 prerequisite/config."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
STATE_NAME = "cao-workflow-project-state.json"


class ManagementError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise ManagementError(f"{' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()}")
    return result


def prerequisites(*, runtime: bool = False) -> None:
    names = ("cao", "git", "gh", "jq", "python3") if runtime else ("cao", "git", "gh", "jq", "python3")
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise ManagementError("Missing commands: " + ", ".join(missing), 3)
    version = command("cao", "--version").stdout.strip()
    match = re.search(r"version (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 5, 0):
        raise ManagementError(f"CAO 2.5.0 or later required; found {version}", 3)
    for group, verb in (("workflow", "validate"), ("workflow", "run"), ("profile", "validate"), ("profile", "remove")):
        help_text = command("cao", group, "--help").stdout
        if not re.search(rf"(?m)^  {verb}\s", help_text):
            raise ManagementError(f"CAO command unavailable: cao {group} {verb}", 3)


def load_manifest() -> dict:
    data = json.loads(MANIFEST.read_text())
    if data.get("schema_version") != 1:
        raise ManagementError("Unsupported manifest schema", 2)
    for kind in ("agents", "workflows"):
        names = [entry["name"] for entry in data[kind]]
        if len(names) != len(set(names)):
            raise ManagementError(f"Duplicate {kind} name", 2)
        for entry in data[kind]:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry["name"]):
                raise ManagementError(f"Invalid {kind} name: {entry['name']}", 2)
            source = (ROOT / entry["source"]).resolve()
            if not source.is_relative_to(ROOT) or not source.is_file():
                raise ManagementError(f"Missing or escaping source: {entry['source']}", 2)
    agents = {entry["name"] for entry in data["agents"]}
    for entry in data["workflows"]:
        if not set(entry["agents"]) <= agents:
            raise ManagementError(f"Unknown agent in {entry['name']}", 2)
    return data


def cao_home() -> Path:
    override = os.environ.get("CAO_HOME_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(command("cao", "config", "path").stdout.strip()).parent.resolve()


def state_path(home: Path) -> Path:
    return home / STATE_NAME


def load_state(home: Path) -> dict:
    path = state_path(home)
    if not path.exists():
        return {"schema_version": 1, "project": str(ROOT), "agents": {}, "workflows": {}}
    data = json.loads(path.read_text())
    if data.get("project") != str(ROOT) or data.get("schema_version") != 1:
        raise ManagementError("Deployment state belongs to another project or schema", 3)
    return data


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cao-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target(home: Path, kind: str, entry: dict) -> Path:
    directory = "agent-store" if kind == "agents" else "workflows"
    extension = ".md" if kind == "agents" else ".py"
    return home / directory / (entry["name"] + extension)


def profile_context(home: Path, name: str) -> Path:
    return home / "agent-context" / f"{name}.md"


def codex_review_profile_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "cao_pr_review_readonly.config.toml"


def codex_review_profile_ok() -> bool:
    try:
        profile = tomllib.loads(codex_review_profile_path().read_text())
        return profile.get("sandbox_mode") == "read-only" and profile.get("approval_policy") == "never"
    except (OSError, tomllib.TOMLDecodeError):
        return False


def codex_apply_profile_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "cao_pr_apply_readonly.config.toml"


def codex_apply_profile_ok() -> bool:
    try:
        profile = tomllib.loads(codex_apply_profile_path().read_text())
        return (profile.get("sandbox_mode") == "read-only" and profile.get("approval_policy") == "never"
                and profile.get("shell_environment_policy", {}).get("inherit") == "none")
    except (OSError, tomllib.TOMLDecodeError):
        return False


def deployed_workflow(name: str) -> Path:
    """Resolve only the exact owned, current deployment and its profile contexts."""
    chosen = selected(load_manifest(), name)
    home, resources = cao_home(), chosen
    state = load_state(home)
    for kind in ("workflows", "agents"):
        for entry in resources[kind]:
            dst, record = target(home, kind, entry), state[kind].get(entry['name'])
            expected = digest(ROOT / entry['source'])
            if (not record or dst.is_symlink() or not dst.is_file()
                    or digest(dst) != record['sha256'] or digest(dst) != expected):
                raise ManagementError(f"Missing/modified/outdated deployment: {entry['name']}; run update first", 3)
            if kind == 'agents':
                context = profile_context(home, entry['name'])
                if context.is_symlink() or not context.is_file() or digest(context) != expected:
                    raise ManagementError(f"Missing/modified profile context: {entry['name']}", 3)
    return target(home, 'workflows', chosen['workflows'][0])


def selected(manifest: dict, name: str | None) -> dict:
    if name is None:
        return manifest
    workflows = [w for w in manifest["workflows"] if w["name"] == name]
    if not workflows:
        raise ManagementError(f"Unknown workflow: {name}", 3)
    agent_names = set(workflows[0]["agents"])
    return {"agents": [a for a in manifest["agents"] if a["name"] in agent_names], "workflows": workflows}


def validate() -> dict:
    prerequisites()
    command(sys.executable, str(ROOT / 'scripts/build_incident_workflows.py'), '--check')
    command(sys.executable, str(ROOT / 'scripts/build_pr_guards.py'), '--check')
    manifest = load_manifest()
    defaults = json.loads((ROOT / "config/defaults.json").read_text())
    validate_defaults(defaults)
    import automation
    automation.validate_configuration(json.loads((ROOT / 'config/automation.example.json').read_text()))
    repositories = json.loads((ROOT / "config/repositories.example.json").read_text()).get("repositories")
    if not isinstance(repositories, list) or any(not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in repositories):
        raise ManagementError("Invalid repositories.example.json", 2)
    for entry in manifest['workflows']:
        example_input = json.loads((ROOT / entry['source']).with_name('config.example.json').read_text())
        if not isinstance(example_input, dict) or not isinstance(example_input.get("repository"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", example_input["repository"]):
            raise ManagementError(f"Invalid {entry['name']} config example", 2)
    for script in (ROOT / "scripts").rglob("*.sh"):
        result = command("bash", "-n", str(script), check=False)
        if result.returncode:
            raise ManagementError(f"Shell syntax failed: {script}: {result.stderr}", 2)
    for entry in manifest["agents"]:
        path = ROOT / entry["source"]
        content = path.read_text()
        if not re.search(rf"(?m)^name: {re.escape(entry['name'])}$", content):
            raise ManagementError(f"Profile name mismatch: {path}", 2)
        result = command("cao", "profile", "validate", str(path), check=False)
        if result.returncode:
            raise ManagementError(f"Profile invalid: {path}: {(result.stderr or result.stdout).strip()}", 2)
    for entry in manifest["workflows"]:
        source = ROOT / entry["source"]
        import ast
        tree = ast.parse(source.read_text(), filename=str(source))
        literals = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant) for target in node.targets if isinstance(target, ast.Name)}
        if literals.get('WORKFLOW') != entry['name'] or literals.get('VERSION') != entry['version']:
            raise ManagementError(f'Workflow name/version differs from manifest: {source}', 2)
        # CAO is commonly installed in its own uv tool venv, not system python.
        cao_python = Path(shutil.which("cao")).resolve().parent / "python"
        if not cao_python.is_file():
            raise ManagementError("Cannot locate CAO's Python environment", 3)
        lint_code = ("import sys; from pathlib import Path; "
                     "from cli_agent_orchestrator.services.script_lint import lint_script; "
                     "r=lint_script(Path(sys.argv[1]).read_text(), sys.argv[1]); "
                     "print(r.model_dump_json()); sys.exit(0 if r.status != 'fail' else 2)")
        result = command(str(cao_python), "-c", lint_code, str(source), check=False)
        if result.returncode:
            raise ManagementError(f"Workflow invalid: {source}: {(result.stdout or result.stderr).strip()}", 2)
    # CAO already supplies PyYAML. Parse manual Action YAML and syntax-check its
    # explicit Bash steps without installing another tool or executing the steps.
    action_code = ("import sys,yaml,subprocess; from pathlib import Path; "
                   "data=yaml.safe_load(Path(sys.argv[1]).read_text()); "
                   "assert isinstance(data,dict); "
                   "steps=[s for j in data.get('jobs',{}).values() for s in j.get('steps',[])]; "
                   "[subprocess.run(['bash','-n'],input=s['run'],text=True,capture_output=True,check=True) "
                   "for s in steps if 'run' in s and s.get('shell')=='bash']")
    for action in (ROOT / '.github/workflows').glob('*.yml'):
        result = command(str(cao_python), '-c', action_code, str(action), check=False)
        if result.returncode:
            raise ManagementError(f"Action syntax invalid: {action}", 2)
    print("All validations passed.")
    return manifest


def validate_defaults(defaults: dict) -> None:
    if defaults["publish_mode"] not in ("dry-run", "review"):
        raise ManagementError("Invalid default review policy", 2)
    if not isinstance(defaults["include_drafts"], bool) or not isinstance(defaults["workspace_root"], str):
        raise ManagementError("Invalid defaults schema", 2)
    if not defaults["workspace_root"].startswith("/"):
        raise ManagementError("workspace_root must be an absolute path", 2)


def install(name: str | None) -> None:
    manifest = selected(validate(), name)
    home = cao_home()
    state = load_state(home)
    changes = []
    for kind in ("agents", "workflows"):
        for entry in manifest[kind]:
            dst = target(home, kind, entry)
            src = ROOT / entry["source"]
            old = state[kind].get(entry["name"])
            if dst.is_symlink():
                raise ManagementError(f"Refusing symlink target: {dst}", 3)
            if dst.exists() and (not old or digest(dst) != old["sha256"]):
                raise ManagementError(f"Unowned or modified resource: {dst}", 3)
            if kind == "agents":
                context = profile_context(home, entry["name"])
                if context.is_symlink() or (context.exists() and (not old or digest(context) != old["sha256"])):
                    raise ManagementError(f"Unowned or modified profile context: {context}", 3)
            changes.append((kind, entry, src, dst))
    for kind, entry, src, dst in changes:
        context = profile_context(home, entry["name"]) if kind == "agents" else None
        if dst.exists() and digest(dst) == digest(src) and (context is None or (context.exists() and digest(context) == digest(src))):
            print(f"skip {kind}: {entry['name']} (up to date)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        previous = dst.read_bytes() if dst.exists() else None
        previous_context = context.read_bytes() if context and context.exists() else None
        try:
            if kind == "agents":
                # This is the supported profile installer; it also projects the profile to Codex.
                result = command("cao", "install", str(src), "--provider", "codex")
                if "Error:" in result.stdout or not dst.exists() or digest(dst) != digest(src) or not context.exists() or digest(context) != digest(src):
                    raise ManagementError(f"CAO did not install expected profile: {entry['name']}: {result.stdout}")
            else:
                fd, tmp = tempfile.mkstemp(prefix=".cao-workflow-", dir=dst.parent)
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(src.read_bytes())
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, dst)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        except Exception:
            if previous is None:
                dst.unlink(missing_ok=True)
            else:
                dst.write_bytes(previous)
            if context is not None:
                if previous_context is None:
                    context.unlink(missing_ok=True)
                else:
                    context.write_bytes(previous_context)
            raise
        state[kind][entry["name"]] = {"sha256": digest(dst), "version": entry.get("version", "")}
        atomic_json(state_path(home), state)
        print(f"installed {kind}: {entry['name']}")
    # Record ownership for a preexisting identical file only if it was already ours.
    print(f"CAO Home: {home}")


def uninstall(name: str | None, yes: bool, force: bool = False) -> None:
    manifest = selected(load_manifest(), name)
    home = cao_home()
    state = load_state(home)
    owned = []
    for kind in ("workflows", "agents"):
        for entry in manifest[kind]:
            record = state[kind].get(entry["name"])
            dst = target(home, kind, entry)
            if record:
                modified = dst.is_symlink() or (dst.exists() and digest(dst) != record["sha256"])
                context_modified = False
                if kind == "agents":
                    context = profile_context(home, entry["name"])
                    context_modified = context.is_symlink() or (context.exists() and digest(context) != record["sha256"])
                if modified or context_modified:
                    if not force:
                        raise ManagementError(f"Refusing modified resource: {dst}", 3)
                    print(f"Warning: Removing modified resource: {dst}", file=sys.stderr)
                owned.append((kind, entry["name"], dst))
    print("Project-owned removal targets:")
    for kind, resource, dst in owned:
        print(f"  {kind}: {resource} ({dst})")
    if not owned:
        return
    if not yes:
        if not sys.stdin.isatty() or input("Remove these resources? [y/N] ").lower() not in ("y", "yes"):
            raise ManagementError("Cancelled. Pass --yes for non-interactive uninstall.", 3)
    for kind, resource, dst in owned:
        if dst.exists():
            if kind == "agents":
                command("cao", "profile", "remove", resource, "--yes")
            else:
                # The CAO index is derived from files. A remote server may have a
                # different CAO_HOME_DIR; delete only our locally verified target.
                dst.unlink()
        if kind == "agents":
            profile_context(home, resource).unlink(missing_ok=True)
        state[kind].pop(resource)
        atomic_json(state_path(home), state)
        print(f"removed {kind}: {resource}")


def status() -> None:
    manifest = load_manifest()
    home = cao_home()
    state = load_state(home)
    print(f"CAO Version: {command('cao', '--version').stdout.strip()}")
    print(f"CAO Home: {home}")
    for kind in ("workflows", "agents"):
        print(kind.upper())
        for entry in manifest[kind]:
            dst = target(home, kind, entry)
            record = state[kind].get(entry["name"])
            if not record:
                label = "unmanaged" if dst.exists() else "missing"
            elif not dst.exists():
                label = "missing"
            elif digest(dst) != record["sha256"]:
                label = "modified"
            elif kind == "agents" and (not profile_context(home, entry["name"]).exists() or digest(profile_context(home, entry["name"])) != record["sha256"]):
                label = "context-missing-or-modified"
            elif digest(dst) != digest(ROOT / entry["source"]):
                label = "outdated"
            else:
                label = "up-to-date"
            print(f"  {entry['name']:<24} {label}")


def doctor() -> None:
    problems = 0
    for item in ("cao", "cao-server", "git", "gh", "jq", "tmux", "codex", "python3"):
        found = shutil.which(item)
        print(f"{item:<12} {'OK ' + found if found else 'MISSING: install before live runs'}")
        problems += not bool(found)
    auth = command("gh", "auth", "status", check=False) if shutil.which("gh") else None
    auth_ok = bool(auth and auth.returncode == 0 and "Failed to log in" not in auth.stderr and "Failed to log in" not in auth.stdout)
    print("GitHub auth:", "OK" if auth_ok else "MISSING/INVALID: run gh auth login or fix GITHUB_TOKEN")
    problems += not auth_ok
    profile_file = codex_review_profile_path()
    codex_ok = codex_review_profile_ok()
    print("Codex review profile:", "OK" if codex_ok else f"MISSING: copy config/cao_pr_review_readonly.config.toml to {profile_file}")
    problems += not codex_ok
    apply_ok = codex_apply_profile_ok()
    print("Codex apply profile:", "OK" if apply_ok else f"MISSING: copy config/cao_pr_apply_readonly.config.toml to {codex_apply_profile_path()}")
    problems += not apply_ok
    try:
        print("CAO config:", command("cao", "config", "path").stdout.strip())
        status()
        manifest = load_manifest()
        home = cao_home()
        state = load_state(home)
        for kind in ("agents", "workflows"):
            for entry in manifest[kind]:
                dst = target(home, kind, entry)
                record = state[kind].get(entry["name"])
                if not record or not dst.exists() or digest(dst) != record["sha256"] or (kind == "agents" and (not profile_context(home, entry["name"]).exists() or digest(profile_context(home, entry["name"])) != record["sha256"])):
                    problems += 1
                    print(f"Deployment issue: {kind}/{entry['name']} is missing or modified")
        workflow_list = command("cao", "workflow", "list", "--json", check=False)
        if workflow_list.returncode:
            problems += 1
            print("CAO workflow registry: unavailable; start cao-server and check CAO_API_PORT")
        else:
            indexed = {row["name"] for row in json.loads(workflow_list.stdout)}
            for entry in manifest["workflows"]:
                if entry["name"] not in indexed:
                    problems += 1
                    print(f"CAO workflow registry: missing {entry['name']}")
    except ManagementError as exc:
        print("CAO config: ERROR", exc)
        problems += 1
    if problems:
        raise ManagementError(f"Doctor found {problems} issue(s)", 3)


def run(argv: list[str]) -> None:
    if argv and argv[0] in ('mcp-exception-issue', 'github-issue-fix'):
        os.execv(sys.executable, [sys.executable, str(ROOT / 'scripts/incident_run.py'), *argv])
    parser = argparse.ArgumentParser(prog="run.sh")
    parser.add_argument("workflow")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, dest="pr_number")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--publish-mode", choices=("dry-run", "review"))
    parser.add_argument("--include-drafts", action="store_true")
    parser.add_argument("--force-review", action="store_true")
    parser.add_argument("--base-branch")
    parser.add_argument("--workspace-root")
    parser.add_argument("--model")
    parser.add_argument("--head-sha")
    parser.add_argument("--review-id", type=int)
    parser.add_argument("--policy", dest="policy_path")
    parser.add_argument("--apply-mode", choices=("patch", "push"))
    parser.add_argument("--apply", action="store_true", help="Run review then apply for one PR")
    parser.add_argument("--expected-findings", type=int)
    parser.add_argument("--detach", action="store_true")
    parser.add_argument('--state-root')
    parser.add_argument('--chain-id')
    parser.add_argument('--authorized-policy-path')
    for field in ('head-sha', 'base-sha', 'policy-digest', 'resource-digest'):
        parser.add_argument('--expected-' + field)
    args = parser.parse_args(argv)
    if args.apply:
        if args.workflow != 'github-pr-review' or not args.pr_number or args.pr_number <= 0 or not args.policy_path:
            raise ManagementError("--apply requires github-pr-review --pr N --policy /absolute/file", 3)
        if any((args.dry_run, args.no_publish, args.publish_mode == 'dry-run', args.include_drafts,
                args.base_branch, args.workspace_root, args.head_sha, args.review_id,
                args.expected_findings is not None, args.detach)):
            raise ManagementError("--apply requires published reviews; discovery, dry-run, standalone and detach flags are unsupported", 3)
        cmd = [sys.executable, str(ROOT / 'scripts/review_apply.py'), '--repository', args.repository,
               '--pr', str(args.pr_number), '--policy', args.policy_path,
               '--apply-mode', args.apply_mode or 'patch']
        if args.model:
            cmd += ['--model', args.model]
        if args.force_review:
            cmd.append('--force-review')
        for field in ('state_root', 'chain_id', 'authorized_policy_path', 'expected_head_sha', 'expected_base_sha',
                      'expected_policy_digest', 'expected_resource_digest'):
            value = getattr(args, field)
            if value is not None:
                cmd += ['--' + field.replace('_', '-'), value]
        os.execv(sys.executable, cmd)
    if any(getattr(args, field) for field in ('state_root', 'chain_id', 'authorized_policy_path', 'expected_head_sha',
                                             'expected_base_sha', 'expected_policy_digest', 'expected_resource_digest')):
        raise ManagementError('Automation chain options require --apply', 3)
    if args.workflow == 'github-pr-review' and any((args.policy_path, args.apply_mode, args.head_sha,
                                                  args.review_id, args.expected_findings is not None)):
        raise ManagementError("Use --apply for review plus apply, or github-pr-apply for standalone apply", 3)
    if args.workflow == 'github-pr-apply':
        args.apply_mode = args.apply_mode or 'patch'
    prerequisites(runtime=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or ".." in args.repository:
        raise ManagementError("Repository must be owner/name", 3)
    if args.pr_number is not None and args.pr_number <= 0:
        raise ManagementError("PR number must be positive", 3)
    if not codex_review_profile_ok():
        raise ManagementError(f"Configure read-only Codex profile first: {codex_review_profile_path()}", 3)
    manifest = load_manifest()
    defaults = json.loads((ROOT / "config/defaults.json").read_text())
    validate_defaults(defaults)
    selected(manifest, args.workflow)
    if args.workflow == 'github-pr-apply':
        if not args.pr_number or not args.review_id or not args.head_sha or not args.policy_path:
            raise ManagementError("Apply requires --pr, --review-id, --head-sha and --policy", 3)
        if args.review_id <= 0 or not re.fullmatch(r'[0-9a-f]{40}', args.head_sha):
            raise ManagementError("Invalid review ID or HEAD SHA", 3)
        if any((args.dry_run, args.no_publish, args.publish_mode, args.include_drafts, args.force_review, args.base_branch)):
            raise ManagementError("Review-only flags cannot be used for apply", 3)
        if not codex_apply_profile_ok():
            raise ManagementError(f"Configure apply profile first: {codex_apply_profile_path()}", 3)
        cmd = ['cao', 'workflow', 'run', str(deployed_workflow(args.workflow)), '--input', f'repository={args.repository}']
        for key in ('pr_number', 'review_id', 'head_sha', 'policy_path', 'apply_mode', 'expected_findings', 'workspace_root', 'model'):
            value = getattr(args, key)
            if value is not None:
                cmd += ['--input', f'{key}={value}']
        if args.detach:
            cmd.append('--detach')
        raise SystemExit(subprocess.call(cmd))
    home = cao_home()
    state = load_state(home)
    if args.workflow not in state["workflows"]:
        raise ManagementError("Workflow is not installed; run scripts/install.sh first", 3)
    entry = next(w for w in manifest["workflows"] if w["name"] == args.workflow)
    dst = target(home, "workflows", entry)
    if not dst.exists() or digest(dst) != state["workflows"][args.workflow]["sha256"]:
        raise ManagementError("Installed workflow is missing or modified", 3)
    # Address the exact deployed file. A server using another CAO_HOME_DIR then
    # rejects the path instead of running a different workflow with the same name.
    cmd = ["cao", "workflow", "run", str(dst), "--input", f"repository={args.repository}"]
    for key in ("pr_number", "base_branch", "workspace_root", "model"):
        value = getattr(args, key, None)
        if value is None and key in defaults:
            value = defaults[key]
        if value is not None:
            cmd += ["--input", f"{key}={value}"]
    for key in ("include_drafts", "force_review"):
        if getattr(args, key) or (key in defaults and defaults[key]):
            cmd += ["--input", f"{key}=true"]
    mode = "dry-run" if args.dry_run else (args.publish_mode or defaults["publish_mode"])
    if mode:
        cmd += ["--input", f"publish_mode={mode}"]
    if args.no_publish:
        cmd += ["--input", "publish=false"]
    if args.detach:
        cmd.append("--detach")
    raise SystemExit(subprocess.call(cmd))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == 'run':
        try:
            run(sys.argv[2:])
        except (ManagementError, OSError, ValueError, KeyError) as exc:
            code = exc.code if isinstance(exc, ManagementError) else 2
            print(f'ERROR: {exc}', file=sys.stderr)
            raise SystemExit(code)
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("install", "uninstall", "validate", "status", "list", "doctor", "run"))
    args, rest = parser.parse_known_args()
    try:
        if args.operation == "run":
            run(rest)
        elif args.operation in ("install", "uninstall"):
            sub = argparse.ArgumentParser()
            sub.add_argument("name", nargs="?")
            if args.operation == "uninstall":
                sub.add_argument("--yes", action="store_true")
                sub.add_argument("--force", action="store_true")
            opts = sub.parse_args(rest)
            if args.operation == "install":
                install(opts.name)
            else:
                uninstall(opts.name, opts.yes, opts.force)
        elif args.operation == "validate":
            validate()
        elif args.operation == "status":
            status()
        elif args.operation == "doctor":
            doctor()
        else:
            manifest = load_manifest()
            for kind in ("workflows", "agents"):
                print(kind.upper())
                for entry in manifest[kind]:
                    print("-", entry["name"])
    except (ManagementError, OSError, ValueError, KeyError, ImportError, SyntaxError) as exc:
        code = exc.code if isinstance(exc, ManagementError) else 2
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(code)


if __name__ == "__main__":
    main()
