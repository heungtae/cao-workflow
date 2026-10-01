# CAO Workflow Management

Manage CAO Workflows and Agent Profiles in Git and install them into the CAO runtime.
Git is the source of truth; CAO home is the deployment target.

| Workflow | Purpose |
| --- | --- |
| `github-pr-review` | Review PRs from Code/Security/Test perspectives and publish comments on changed lines |
| `github-pr-apply` | Turn review findings into a candidate patch, validate it, and optionally push |

Run review and apply together through `scripts/run.sh`.
Models use read-only profiles to review code and propose edits. Workflows handle file changes and GitHub publication.

## 1. Prepare the Environment

- Linux/WSL, Python 3.11 or later
- CAO 2.5.0 or later (`cao`, `cao-server`), Codex CLI, and Codex authentication
- `git`, `gh`, `jq`, `tmux`
- GitHub authentication: `gh auth login` or `GH_TOKEN`/`GITHUB_TOKEN`
  - Read access: Contents read, Pull requests read, Issues read
  - Publish reviews: Pull requests write
  - Push branches: Contents write

The CAO server and launcher scripts must use the same `CODEX_HOME`.
The following example uses the default location, `~/.codex`.

```bash
mkdir -p ~/.codex
cp config/cao_pr_review_readonly.config.toml ~/.codex/
cp config/cao_pr_apply_readonly.config.toml ~/.codex/
```

Apply also requires Docker on the same host, a preloaded immutable test image,
and an [operator policy](config/apply-policy.example.json).
Configure the actual repository, review authors, image digest, and test commands.
Store the policy at an absolute path outside the model working directories with permissions `0600`.
The default policy does not allow push.
See the [operations guide](docs/OPERATIONS.md) for setup details.

## 2. Validate and Install

Start `cao-server` in a separate terminal, then run:

```bash
make validate
make test
make install
make doctor
make status
```

Installation and updates manage only resources listed in `manifest.json` and skip identical files.
Unowned files and files modified in the runtime are never overwritten.

## 3. Run PR Reviews

```bash
# Discover and review open PRs
./scripts/run.sh github-pr-review --repository owner/repo

# Review a specific PR without publishing
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run

# Review and publish a specific PR (default mode)
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --publish-mode review

# Review again even if a review marker already exists
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review
```

Findings are published as inline review comments, with a summary in a `COMMENT` review body.
An existing marker for the same HEAD/base SHA and workflow version causes the review to be skipped.
`--dry-run` still performs model review but does not publish or leave a marker.
The workflow never automatically approves, requests changes, or merges a PR.

Additional options include `--include-drafts`, `--base-branch`, `--workspace-root`, `--model`,
`--no-publish`, and `--detach`. See [PR Review](docs/GITHUB-PR-REVIEW.md) for detailed inputs.

## 4. Apply Review Findings

Add `--apply` and `--policy` to the review command.

```bash
# Review → apply → test → save a candidate patch
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json

# Push to the PR branch when policy and validation conditions are met
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json --apply-mode push

# Apply findings from a fresh review
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review \
  --apply --policy /absolute/operator/apply-policy.json
```

- The default mode is `patch`. Select `push` explicitly to publish changes.
- Specify one PR with `--pr`. You can also use `--publish-mode review`, `--force-review`, and `--model`.
- `--dry-run`, `--no-publish`, `--detach`, and PR discovery options are unsupported for combined execution.
- The coordinator verifies the review ID and original HEAD/base. Partial application, test failures, or HEAD/base changes prevent push.
- Candidate patches and results are saved under `/tmp/cao-pr-apply/candidate-*`; checkouts are removed.

### Resume an Interrupted Run

Use the `state_path` printed when execution starts. The default state directory is
`/tmp/cao-pr-review-apply`, and state files have permissions `0600`.

```bash
./scripts/run-review-apply.sh --resume /tmp/cao-pr-review-apply/CHAIN.json
```

The coordinator queries the same run IDs and continues execution.
Retry terminal failed or cancelled runs by starting a new combined run.
See the [Apply guide](workflows/github-pr-apply/README.md) for standalone apply and workspace settings.

### Run Through GitHub Actions

The [manual Action](.github/workflows/pr-review-apply.yml) runs the same `run.sh` command
on a runner with the `self-hosted`, `linux`, and `cao` labels.
Set repository variables `CAO_WORKFLOW_PROJECT_ROOT` and `CAO_APPLY_POLICY_PATH`.
Prepare the manager checkout and installed resources to match the Action's commit SHA.

## Resource Management

The commands below target all resources. Add `github-pr-review` or `github-pr-apply`
to an install, update, or uninstall command to manage only that workflow and its associated profiles.

```bash
./scripts/list.sh
./scripts/install.sh
./scripts/update.sh
./scripts/status.sh
./scripts/doctor.sh
./scripts/uninstall.sh --yes
```

Ownership and SHA-256 hashes are recorded in `cao-workflow-project-state.json` in CAO home.
Removing modified resources is refused by default. After verifying project ownership,
use `uninstall.sh --yes --force` to bypass the modification check. Ownership checks still apply.

## Troubleshooting

| Symptom | What to Check |
| --- | --- |
| Codex profile error | Copy both profiles into the `CODEX_HOME` used by the server and launcher scripts |
| GitHub authentication error | Check `gh` authentication and permissions required for the operation |
| CAO server connection error | Check that `cao-server` is running and verify `CAO_API_PORT` |
| `unmanaged` / `modified` | Verify deployment ownership and Git sources before updating |
| Model response or context limit error | Check execution requirements and limits in the [operations guide](docs/OPERATIONS.md) |

CAO steps use fixed shell no-op input tokens and JSON responses.
To preserve this protection, server memory injection must be disabled or its source memory must be empty.
Do not store credentials or personal CAO state in this repository.

## Repository Structure and Documentation

| Path | Purpose |
| --- | --- |
| `manifest.json` | Single inventory of deployment resources and workflow versions |
| `agents/` | Agent Profile sources |
| `workflows/` | CAO script workflow sources |
| `config/` | Codex configuration and example inputs and policies |
| `scripts/` | Installation, validation, execution, status checks, and removal |
| `tests/` | Management, review, and apply tests and fixtures |
| `docs/` | Architecture, development, operations, and design documentation |

- [Architecture](docs/ARCHITECTURE.md): CAO contracts and execution structure
- [Workflow Development](docs/WORKFLOW-DEVELOPMENT.md): Workflow and profile development rules
- [Operations](docs/OPERATIONS.md): Operational setup, resume, and rollback
- [PR Review](docs/GITHUB-PR-REVIEW.md): Review inputs and publication policy
- [Review → Apply Design](docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md): Combined execution and validation contracts
- [MCP Exception → Issue Design](docs/MCP-EXCEPTION-ISSUE-DESIGN.md): Proposed independent workflow for MCP-only log analysis and GitHub Issue creation
- [Issue Fix → Push Design](docs/GITHUB-ISSUE-FIX-DESIGN.md): Proposed independent workflow for Issue-driven fixes, isolated validation, and branch push
- [External MCP Log Server Specification](docs/MCP-LOG-SERVER-SPEC.md): Provider-facing contract; server implementation, deployment, and operation are supplied externally
