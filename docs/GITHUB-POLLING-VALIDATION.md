# GitHub polling validation

Validated on 2026-10-04 with CAO 2.5.0, `github-pr-review` v7 and
`github-pr-apply` v2. The implementation was tested from the local worktree;
the feature changes have not been committed or pushed as part of this validation.

## Local validation

- `make validate`: passed, including generated PR gate consistency and the
  automation configuration example.
- `make test` in the installed CAO Python/SDK environment with `CAO_TEST_IMAGE`
  set to an available immutable Docker image: **118 tests passed, zero skipped**.
- The actual Docker isolation test passed: no network, credential environment,
  Git metadata, or write access to the original candidate.
- `systemd-analyze verify` passed for the collection service/timer and Worker
  service templates. The production user services were not enabled.
- Owned installation, repeated installation with all resources skipped as
  current, and ownership-checked removal passed in an isolated CAO home.

The automated cases include mixed paginated `200`/`304` responses, short pages
with a next-page link, rate limits/backoff, transactional checkpoint retention,
draft/fork/closure filtering, identity changes, atomic global claims, duplicate
Workers, dry-run immutability, retained execution IDs, missing evidence,
publication intent recovery, changed running authorization, and output-loop
suppression. Test-only registered Sources/Handlers exercise monitor-tick
coalescing, exclusive trigger validation and separate ingestion-lag health.
The MCP SDK checks use controlled client fixtures; no future production adapter
or MCP server was implemented or deployed.

## Live GitHub and CAO validation

Created [test PR #1](https://github.com/heungtae/cao-workflow/pull/1) on
`test/polling-smoke-20261004-e788668e`. The policy allowed only this exact branch
and `polling_smoke/**`. Production configuration/credentials were not added to Git.

| Evidence | Result |
| --- | --- |
| Source collection and dry-run | Eligible existing PR persisted as one job; dry-run did not launch CAO |
| Original HEAD | `6c0afe673a38c904bb32148e43adf27b3bb9739c` |
| Pinned base | `224f41edfdc7210b51cf9b6437abe9b0fe87481b` |
| Successful chain | `62fb51519f85fecf8bb09420e4b8e64e` |
| Review run | `62fb51519f85fecf8bb09420e4b8e64e-review` |
| Apply run | `62fb51519f85fecf8bb09420e4b8e64e-apply` |
| Published review | [COMMENT review 5403059944](https://github.com/heungtae/cao-workflow/pull/1#pullrequestreview-5403059944), one inline finding |
| Applied correction | `polling_smoke/arithmetic.py`: subtraction changed to addition |
| Isolated validation | `python3 -m unittest discover -s polling_smoke -v` passed in Docker |
| Verified pushed HEAD | [135a1e94300e9a8ee3c4d6794b449cb5da5b1389](https://github.com/heungtae/cao-workflow/commit/135a1e94300e9a8ee3c4d6794b449cb5da5b1389) |
| Remote commit check | Exact original HEAD as sole parent; only the intended arithmetic file changed |
| Output recollection | Real poll and dry-run both returned zero proposed jobs; Worker submitted no new run |
| Manual journal resume | Returned `completed`; retained CAO run count stayed 8 before/after |

The successful dependency digest was
`efc2950f157180d0136cee9737de0011db357cec94d40e85265b5e8a40d31da9`;
the private policy digest was
`4078f9a1203e2b55990c847ebbdc7bee30e9c975cb43e16232b974dd93f9d82c`.

Earlier attempts remain recorded: Codex's initial UI preference mutation stopped
publication; a completed run without retained output became blocked; missing
GitHub review-comment location fields and a trailing-slash repository API path
caused terminal application failures. The respective implementation fixes were
validated through distinct dependency/job identities. Terminal attempts were
not automatically rerun under their old identities.

The test PR remains open and unmerged for inspection. The isolated test server
was stopped and its managed PR resources uninstalled after verification. Temporary
credential copies were removed; private queue/journals/artifacts remain under
`/tmp/cao-polling-live-20261004` for evidence, rather than production operation.
