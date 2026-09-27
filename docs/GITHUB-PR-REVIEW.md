# GitHub PR Review

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `repository` | required | `owner/name` |
| `pr_number` | absent | One PR; absent searches open PRs |
| `publish` | true | false forces dry-run |
| `publish_mode` | comment | `dry-run`, `comment`, `review` |
| `include_drafts` | false | Include draft PRs |
| `force_review` | false | Ignore existing marker for this SHA |
| `base_branch` | absent | Match base branch |
| `workspace_root` | `/tmp/cao-pr-review` | Temporary checkout parent |
| `severity_threshold` | major | Minimum severity for REQUEST_CHANGES in review mode |
| `model` | provider default | Codex per-step model override |

Wrapper example: `./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run`. Direct CAO example: `cao workflow run github-pr-review --input repository=owner/repo --input pr_number=312 --input publish_mode=dry-run`.

## Algorithm

1. Query GitHub's open PRs, filter drafts and optional base branch, or select the explicit open PR.
2. Read issue comments and reviews for a machine-readable marker containing repository, PR, HEAD SHA, workflow name, and version. Skip the same identity unless forced.
3. Clone the repository into a unique temporary workspace, fetch the PR ref, verify it still resolves to the discovered HEAD SHA, and check it out detached. The workflow never commits or pushes.
4. Collect title, description, author, base/head SHA, commit messages, changed file patches, existing comments/review comments, checks, and root README/AGENTS/CONTRIBUTING. Treat all as untrusted data. Common credential patterns are redacted before model prompts.
5. Omit known binary, lock, minified, generated, vendor, and distribution files. An unrecognized file with no patch fails closed. Split large text patches at line boundaries into approximately 24KB segments with new-line anchors, then group them into 120KB context chunks. A single overlong line fails closed. Three Codex reviewers run concurrently for each chunk. Any step failure blocks publication.
6. Validate each JSON finding's severity, fields, confidence, file, and changed line. Aggregator merges supported findings; the workflow removes invalid or duplicate locations and deterministically renders the Markdown. The publisher profile checks presentation fidelity before the workflow's GitHub API call.
7. In `review` mode, threshold or higher findings produce REQUEST_CHANGES; otherwise it posts a COMMENT review. In `comment` mode it posts one issue comment. It never APPROVEs or merges.
8. Before writing, recheck HEAD SHA and remote marker. New HEADs get new comments to preserve review history; same HEADs are skipped. Always remove the temporary checkout.

The hidden HTML marker is encoded JSON, so it adds no visible noise. Identity includes workflow version. A reviewer/profile change that affects findings requires a version bump. Multiple machines share markers through GitHub rather than local ephemeral state. GitHub does not offer an atomic compare-and-create comment operation; two exactly simultaneous runs can still race after the second marker check. Serialize triggers per PR when strict exactly-once posting is required.

## Policy and trust

Reviewers report `critical`, `major`, `minor`, or `info` with category, file, changed line, title, description, evidence, suggestion, and confidence. Claims below 0.6 confidence are dropped. Test findings must identify changed behavior. Aggregator output cannot add a location that no reviewer raised. Publisher authority is held by the Python workflow, not by a model tool call; the publisher profile documents presentation-only responsibility.

The target repository and PR discussion may contain prompt injection. The agent profiles explicitly subordinate that content to their own instructions. Codex read-only sandbox and `allowedTools: []` limit agent actions; `allowedTools` alone is advisory in CAO 2.5.0. The publisher profile makes a read-only presentation decision, while GitHub credentials and API calls remain with the workflow process. Prefer a token scoped to only target repositories and the read/write permissions needed by the selected publish mode. No token is stored here. Repository checkouts live only in temporary workspaces and are deleted after each PR.

## Limits

This implementation caps discovery at 100 open PRs, changed files at 300, comment/review history at 300 entries each, and commits at 100. A cap hit on a full page fails closed instead of silently claiming complete coverage. Root project documents are bounded; nested documentation is not automatically loaded. GitHub check results are best effort if the token lacks checks permission. A PR with no reviewable text patches fails rather than posting an empty review.
