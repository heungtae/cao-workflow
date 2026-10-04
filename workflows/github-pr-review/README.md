# github-pr-review v7

Review a selected PR or discover open PRs with Code, Security, and Test reviewers.
The workflow publishes one GitHub `COMMENT` review with changed-line inline
findings and a summary. It never approves, requests changes, or merges a PR.

[All workflows and common setup](../../README.md) ·
[Input/algorithm reference](../../docs/GITHUB-PR-REVIEW.md)

Commands below run from the repository root. Replace the repository and PR number.

## Prepare and install

1. Complete [common setup](../../README.md#setup-and-installation), including GitHub
   and Codex authentication and the CAO server.
2. Install [cao_pr_review_readonly.config.toml](../../config/cao_pr_review_readonly.config.toml)
   in the server/launcher's `CODEX_HOME`. Its trusted fixed workspace is
   `/tmp/cao-pr-review`; match that path if you customize `--workspace-root`.
   The workspace must be owned by the CAO account with mode `0700`.
3. Keep CAO memory injection disabled or empty. The model receives a fixed shell
   no-op token and reads bounded input from an owner-only file.
4. Grant Contents/Pull requests/Issues read access and Pull requests write access
   when publishing.

```bash
./scripts/install.sh github-pr-review
make status
```

Installation deploys `workflow.py` and five agent profiles: code, security, and
test reviewers, aggregator, and publication gate. The installed workflow is
`$CAO_HOME_DIR/workflows/github-pr-review.py`.

## Run

```bash
# Review one PR without publishing or leaving a deduplication marker.
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 --dry-run

# Publish findings for one PR (default mode).
./scripts/run.sh github-pr-review --repository owner/repository --pr 312

# Discover open, non-draft PRs.
./scripts/run.sh github-pr-review --repository owner/repository

# Review the same HEAD/base again despite an existing marker.
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 --force-review
```

Dry-run still executes model review. A published review marker identifies the
repository, PR, HEAD SHA, base SHA, and workflow version. A matching identity is
skipped; a new HEAD or changed base is reviewed again. Serialize triggers per PR
when duplicate publication must be avoided across machines.

| Option | Effect |
| --- | --- |
| `--pr N` | Select one open PR; omit for discovery |
| `--dry-run` / `--no-publish` | Review without GitHub writes |
| `--publish-mode review` | Explicitly select publication; this is the default |
| `--include-drafts` | Include draft PRs |
| `--base-branch BRANCH` | Filter by PR base branch |
| `--force-review` | Ignore an existing review marker |
| `--workspace-root PATH` | Use an absolute owner-only fixed workspace; update Codex trust config too |
| `--model MODEL` | Override the provider's default model |
| `--detach` | Submit and return a run ID without waiting |

To apply findings after review, use `--apply --policy PATH` for one explicit PR.
That coordinator publishes the review and has its own option restrictions; see
[PR apply](../github-pr-apply/README.md#review-and-apply-together).

[config.example.json](config.example.json) illustrates direct CAO inputs; it is
not automatically loaded. The workflow's `INPUTS` declaration is the runtime
contract. Wrapper flags such as `--pr` map to inputs such as `pr_number`.

## Check progress and results

Use the printed run ID, or find it with `cao workflow runs --limit 20`:

```bash
cao workflow status RUN_ID
cao workflow events RUN_ID --no-follow
cao workflow result RUN_ID --json
```

Inspect `output.results`, one entry per PR. Entries report the reviewed SHA,
result, and timestamps; completed reviews also include findings/publication data.
New publication returns `review_id` and `review_url`. Dry-run and skipped results
have no new review ID. Dry-run prints the proposed summary and inline comments
in the execution output. No discovered PRs produces an empty results list.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `already reviewed` / `skipped` | Check the HEAD/base marker; use `--force-review` if a repeat review is intended |
| No PRs discovered | Check open state, draft exclusion, base filter, and repository permissions |
| Folder trust prompt / Codex startup timeout | Match the fixed workspace's trusted path and mode `0700`; verify the server uses the expected `CODEX_HOME` |
| Invalid reviewer/aggregator JSON | Inspect CAO step events/result; one format retry is automatic, and remaining invalid output blocks publication |
| Context limit / no reviewable patches | Narrow the PR; binary/generated-only changes cannot produce a supported text review |
| HEAD/base/state changed during review | Run again against the current PR snapshot |
| No GitHub review after dry-run | Rerun without `--dry-run` or `--no-publish`; dry-run leaves no marker |

For server, authentication, or deployment problems, use
[common troubleshooting](../../README.md#troubleshooting) and
[operations](../../docs/OPERATIONS.md#troubleshooting).
A lost CLI connection should first be checked through the retained run result.
