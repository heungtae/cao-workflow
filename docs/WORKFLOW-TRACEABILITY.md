# Workflow results, logging, and traceability

The current contracts are PR review v8, PR apply v3, MCP exception → Issue v2,
and Issue fix v2. This guide describes repository implementation and local
validation; it does not establish live model, MCP provider, or GitHub qualification.

## Read a result without rerunning the workflow

CAO 2.5.0 retains execution state and per-step outputs but omits script run-level
output from its result endpoint. Each workflow writes final evidence before
`emit_output`. Launchers join this evidence with the matching retained CAO state.
Missing final evidence is an error, including when resuming a completed run.

```bash
cao workflow status RUN_ID
cao workflow events RUN_ID --no-follow --json
python3 scripts/results.py --journal /absolute/execution.json --verify-cao
python3 scripts/results.py --run RUN_ID --root /absolute/evidence-root --verify-cao
```

`results.py` is read-only. `--verify-cao` queries retained CAO state; without it,
results are local evidence and do not assert the current CAO state. With
verification, `execution_verified` distinguishes a completed successful business
outcome from a blocked result or unavailable CAO state; it is not a fresh remote
GitHub verification. `--full`
includes the entire saved workflow output. An unresolved CAO lookup is displayed
as `cao_state=unresolved`; the query command's exit code is not a publication claim.

For direct CAO runs, supply `--input diagnostics_root=/absolute/evidence-root`.
For all repository launchers, use `--diagnostics-root /absolute/evidence-root`.
Resume uses recorded inputs and rejects overrides. Combined PR and incident runs
default to `run-evidence/` next to their execution journal. Standalone PR defaults
to `/tmp/cao-workflow-results`; select persistent storage for operational use.

Directories require mode `0700`, files require `0600`, and all paths must be
absolute, owned by the execution account, and free of symlink components.

## Evidence layout and interpretation

```text
evidence-root/RUN_ID/
  result.json                 latest generation's final output envelope
  result.GENERATION.json      immutable final output per generation
  events.jsonl                UTC ordered operational events
  events.previous.jsonl       one previous bounded event segment
  sequence.json               durable per-run event sequence
  test-IDENTITY.json          command/image/phase/exit/timeout/duration and output tails
  command-IDENTITY.json       failed deterministic command diagnostics
  review-N.publication.json   PR review intent and verified typed result
  pr-N.result.json            completed discovery result for one PR
```

The final envelope has `schema_version=1`, `generation`, and `output`. Output
includes workflow/version/run identity, deployed workflow SHA-256,
`evidence_directory`, and supplied snapshot/policy/resource expectations.
Journal-backed outputs also identify their execution/chain. Final results cannot
change within one generation. Native CAO recovery may write a higher generation;
previous evidence remains in generation files and journal `child_output_history`.

Events identify run, generation, workflow/version, subject, execution when known,
UTC timestamp, per-run sequence, elapsed time, stage and outcome. Model events
record step IDs and input digests, never prompt or model response content.
The stored workflow outcome and GitHub evidence must agree with CAO state before
completion is established. `completed` alone is not a review/Issue/push receipt.

Incident launchers exit nonzero for missing output, blocked/failed/reconciling
results and processed batches containing blocked, failed, deferred or reconciling
incidents. Expected monitor deferrals retain their scheduled retry information;
a nonzero launcher exit does not authorize a fresh submission.

## Diagnostics, redaction, and retention

Each stored test diagnostic contains the operator command, immutable image,
phase, exit code, timeout flag and elapsed time. stdout/stderr tails are limited
to 16,384 characters each with truncation flags. Known credential forms, bearer
values, token/password assignments, email addresses and authorized incident
redaction patterns are removed before diagnostic persistence. Command failure
files contain only executable identity, exit/error type and bounded redacted output.
Test output is private diagnostic evidence, not operational MCP evidence.

`events.jsonl` rotates after approximately 4 MiB and retains one previous
segment; its sequence continues across rotation and recovery. Final results,
publication intents and per-attempt diagnostics are not automatically deleted.
Operators must retain unresolved publication evidence. Resolved-run cleanup is
an operator action; this release does not provide an automatic pruning/export
service. Use existing safe file handling when archiving private evidence.

CAO server logs remain available separately. The server prints its effective log
path at startup, defaulting to `~/.aws/cli-agent-orchestrator/logs/cao_TIMESTAMP.log`.
At the next server start, `CAO_MEMORY_ENABLED=false CAO_LOG_LEVEL=DEBUG cao-server`
enables detailed server logging. Per-terminal logs live under `logs/terminal/`.
Those logs may contain model-session content and are not the workflow's final receipt.

## Regression and Issue requirement coverage

Issue fix v2 requires one outcome per selected nonempty specification section:
`title`, `body`, and `comment-ID`. Each addressed outcome references proposed
edit paths and edited regression test paths. Missing, duplicate, unknown or
unresolved outcomes block edits/publication. These references make the model's
coverage claim auditable; they do not mechanically prove all natural-language
requirements are correctly understood.

The default `regression_strategy=before_after` requires operator-provided
`regression_commands` and a nonempty `regression_failure_pattern` identifying
the expected defect. The baseline retains the new tests and restores original
production files at the pinned base. Each configured regression check must fail
with the expected defect in the baseline, then pass on the candidate. Timeout,
a successful baseline, or a different failure does not establish the regression.
All normal `test_commands` must also pass on the candidate. All execution uses
credentialless, offline Docker copies with a read-only root and bounded resources.

For behavior where a direct baseline check is unsuitable, an operator can select
`regression_strategy=alternative` with a nonempty `regression_reason`. That
choice is recorded and candidate tests remain mandatory. It cannot be selected
by model output. Policies predating v2 need this regression configuration.

The candidate patch/digest is saved before tests, including on failure. Issue-fix
commits include Issue/base/execution/run/patch trailers; result comments include
immutable commit links, concise reason, execution/run IDs and validation outcomes.
Private diagnostic paths and stdout/stderr are omitted from GitHub comments.

## Publication and monitor recovery

PR review saves a publisher-bound exact payload before POST. Ambiguous sending
intents reconcile only against one matching review/author/commit and matching
comment membership/content. No missing or ambiguous match authorizes another
POST. Completed per-PR results survive a later discovery failure. An unresolved
sending intent blocks a failed review chain from declaring a safe new attempt.

Standalone PR apply can join an earlier run's final evidence from the same root
with retained CAO state when verifying an already-applied commit. Changing the
root does not allow guessing a previous outcome; preserve/select the original root.

Monitor `incident_attempts` records each attempt ID, run ID, start/end and outcome.
Normalized multiline records may supply `exception_group_id`, zero-based
`exception_sequence`, and `exception_group_size`. Only complete groups with
consistent scope/instance/revision metadata are combined, preserving underlying
record provenance. Missing/duplicate members block collection and retain the
checkpoint; timestamps alone never define a group.

## Requirement-to-implementation-to-evidence matrix

These IDs define the requirements tracked by this release. Unit/controlled tests
are distinct from live execution qualification.

| ID | Requirement | Implementation | Evidence |
| --- | --- | --- | --- |
| OBS-01 | Durable final results and identity-checked CAO join | `_shared/diagnostics.py`, `incident_run.retained_result`, `review_apply.retained_stage_result`, `results.py` | `test_diagnostics`: finish-before-emit, missing-output new/resume, actual retained API shape, standalone apply join, viewer |
| OBS-02 | Owner-only storage, ordered redacted bounded diagnostics | `diag_private`, `diag_event`, `diag_test_command`, `diag_command_failure` | `test_diagnostics`: private/symlink rejection, timeout/redaction/tail bounds, generation immutability |
| REC-01 | Pending-comment recovery without redeveloping or pushing | `incident_run.submit`, `fix.reconcile_comment` | `test_diagnostics`: actual API shape recovery child; `test_incidents`: ambiguous comment/reuse |
| REV-01 | Review snapshot and changed-line publication gates | review `process_pr`, `parse_result`, `aggregate`, `publication_gate` | `test_workflow` and recorded [prior live v7/v2 evidence](GITHUB-POLLING-VALIDATION.md) |
| REV-02 | Durable review intent, no blind repost | review `publish`, PR coordinator `run_retained` | `test_diagnostics`: ambiguous POST and exact authenticated review reconciliation |
| APPLY-01 | Comment outcomes, exact replacement, isolated validation, guarded push | apply `validate_review`, `apply_edits`, `test_candidate`, `publish` | `test_apply`, `test_apply_isolation` actual Docker gate |
| ISSUE-01 | Selected requirement outcomes gate every fix | fix `apply_proposal`, issue-fixer profile | `test_diagnostics`: unresolved/missing outcomes before mutation; `test_incidents`: all-edits validation |
| ISSUE-02 | Original defect detection then candidate pass | fix `verify_regression`, `test_candidate`, private operator policy | `test_diagnostics`: controlled original/candidate, environment/timeout rejection, real Docker before/after |
| ISSUE-03 | Candidate/commit/comment provenance | fix `process`, `reconcile_comment` | `test_incidents`: single-parent guarded push/reuse/comment; source inspection of trailers/summary |
| MCP-01 | MCP-only evidence and pinned GitHub source | incident `collect`, `revision`, `source_files` | `test_mcp_sdk`, `test_incidents` transport/scope/revision/coverage tests; no live provider claim |
| MCP-02 | Explicit multiline relationships and attempt lineage | monitor `group_exceptions`, `store`, `process` | `test_diagnostics`: complete ordered grouping/ambiguity rejection; monitor checkpoint tests |
| DEP-01 | Read-only profiles and owned/idempotent resources | manifest, manage, builders | `make validate`, `test_management` install/reinstall/ownership-checked removal |

The earlier [design review](WORKFLOW-DESIGN-REVIEW-2026-10-04.md) remains a baseline
record. New runtime/model/MCP/GitHub live qualification is still required for this
release; earlier evidence does not automatically qualify changed workflow bytes.
