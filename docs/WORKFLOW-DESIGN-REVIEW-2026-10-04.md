# 4개 워크플로 설계·기능·추적 가능성 검토

검토일: 2026-10-04. 기준 커밋: `e894910687405bcf2576ae986d8a42ddd622f881`.
대상 실행 경로는 사용자 확인에 따라 **CAO workflow 실행**이다.
이 문서는 검토 결과와 개선 제안이며, 런타임 코드 변경이나 배포 완료를 의미하지 않는다.

## 검토 범위와 증거

- [PR review 설계](GITHUB-PR-REVIEW.md), [PR apply/결합 설계](GITHUB-PR-REVIEW-APPLY-DESIGN.md),
  [MCP exception → Issue 설계](MCP-EXCEPTION-ISSUE-DESIGN.md), [Issue fix 설계](GITHUB-ISSUE-FIX-DESIGN.md).
- 각 workflow, 공유 `_incident`/`_pr` 코드, launchers, profiles, tests, 운영 문서를 대조했다.
- 설치된 CAO는 `cao --version` 기준 2.5.0이다. workflow/profile/events/result/server help와
  설치된 `api/main.py`, `services/script_runner.py`, `utils/logging.py`, `services/config_service.py`를 확인했다.
- `make test`: 118개 실행, 117개 통과, `CAO_TEST_IMAGE` 미설정 Docker 격리 테스트 1개 생략.
  실행 로그: `/tmp/cao-workflow-design-review-tests.log`. 이 결과는 실제 모델/MCP/GitHub publication 검증이 아니다.
- incident coordinator에 실제 retained API와 같은 `{run_id, state, steps}` 응답을 넣어
  신규 제출과 resume 경로의 차이를 제어된 mock으로 재현했다. 실제 incident publication은 수행하지 않았다.
- 기존 PR live 검증은 [기록된 증거](GITHUB-POLLING-VALIDATION.md)로만 확인했다. 이번에 재실행하지 않았다.

## 주요 발견 사항

### F01 — P1: incident 최종 결과 계약이 설치된 CAO API와 맞지 않는다

근거: [incident_run.submit](../scripts/incident_run.py#L33),
[incident finish](../workflows/_incident/common.py#L513),
[설계의 submission/resume 계약](MCP-EXCEPTION-ISSUE-DESIGN.md#submission-journal-and-resume-interface).

CAO 2.5.0의 `GET /workflows/runs/{id}/result`는 run-level `output`을 반환하지 않는다.
설치된 endpoint 주석과 구현 모두 이를 확인한다. `steps[].output`은 개별 모델 단계 출력이다.
incident `finish()`는 `emit_output()`만 호출하며, 최종 결과를 submission journal에 보존하지 않는다.
반면 런처의 신규 실행은 완료 응답에 `output`이 없으면 `CAO output identity mismatch`를 발생시킨다.

같은 run을 resume할 때는 `completed` 분기가 빈 output을 terminal 결과로 저장하고 반환한다.
최종 exit 판정도 빈 output을 실패로 분류하지 않아 exit 0이 가능하다.
따라서 신규 실행의 실패, 결과 없는 resume의 성공, pending publication recovery 누락이 동시에 가능하다.

재현 결과:

```text
New submission with retained API shape: CAO output identity mismatch
Resume with same retained API shape: {run_id: ..., state: completed, steps: []}
Launcher success predicate: True
```

`tests/test_incident_run.py`의 성공 fixture는 `output`이 있는 응답을 만들어 이 문제를 잡지 못한다.
PR coordinator는 [retained_stage_result](../scripts/review_apply.py#L143)에서 private journal의
`child_outputs`와 CAO state를 결합한다. incident에도 workflow-owned 최종 결과 저장, 동일 run identity 검증,
저장·갱신 잠금, 누락 시 blocked 판정, 동일한 신규/resume 판정 규칙이 필요하다.
특히 `processed`를 성공으로 표시하더라도 incident별 blocked/failed/deferred 수와 publication 결과를 별도로 보여야 한다.

### F02 — P1: Issue fix 회귀 검증과 요구사항별 완료 증거가 부족하다

근거: [설계의 regression 계약](GITHUB-ISSUE-FIX-DESIGN.md#5-isolated-validation),
[apply_proposal](../workflows/_incident/fix.py#L114), [test_candidate](../workflows/_incident/fix.py#L152),
[issue-fixer profile](../agents/issue-fixer.md).

구현은 regression test 경로를 수정하고 `regression_scenarios`가 존재하는지 검사하지만,
원본에서 새 회귀 테스트가 해당 결함 때문에 실패하고 후보에서 통과하는 절차는 없다.
후보에서만 정책 명령을 실행하므로 결함과 무관한 테스트 수정도 이 구조적 게이트를 통과할 수 있다.
이는 설계의 직접 before/after 검증이 가능한 경우에 대한 약속을 충족하지 않는다.

또한 설계는 미해결 Issue 요구사항이 있으면 publication을 막도록 요구하지만,
profile과 writer 계약에는 요구사항 ID별 outcome/미해결 목록이 없다.
전체 Issue가 해결됐다는 판단은 모델의 단일 `decision=fix`에 의존한다.

명시적 요구사항별 outcome과 edit/test 참조를 보존하고, 가능한 회귀 시나리오에는
원본+새 테스트 실행 → 기대한 결함 검출 → 후보 실행 통과를 추가해야 한다.
환경 실패와 결함 검출을 구분하고, before/after가 불가능한 경우 근거와 대체 검증을 기록해야 한다.
모든 Issue를 기계적으로 완전 검증할 수 있다는 보장을 추가하려는 제안은 아니다.

### F03 — P1: 실패 원인과 검증 출력이 보존되지 않아 원인 추적이 끊긴다

근거: [incident command](../workflows/_incident/common.py#L116),
[incident main](../workflows/_incident/common.py#L519),
[monitor exception 처리](../workflows/_incident/monitor.py#L290),
[PR apply test_candidate](../workflows/github-pr-apply/workflow.py#L370).

incident는 subprocess stdout/stderr를 capture하지만 실패 시 exit code 외 출력은 버린다.
monitor는 Blocked/일반 예외의 구체적 이유를 저장하지 않고 상태만 남기는 경로가 있다.
최상위 main도 일반적인 blocked/failed 메시지로 바꾸고 정상 반환하므로 CAO completed가 가능하다.
PR apply Docker 실행은 `quiet=True`로 stdout/stderr를 DEVNULL에 보낸다.
두 fixer의 검증 결과에는 통과한 명령만 기록되며 실패 명령의 출력·시간·exit code가 충분히 남지 않는다.
PR apply의 `result.json`에는 stage/error_type이 있어 일부 보완되지만 실패한 테스트를 진단하기에는 부족하다.

범위가 제한된 private 진단 파일에 실패 단계, 안정적인 reason_code, timeout/exit code,
소요 시간, 잘라내기·비밀 제거된 출력과 artifact 참조를 남겨야 한다.
원시 로그를 모델/일반 GitHub 댓글로 보내는 방식은 개선안에 포함하지 않는다.

### F04 — P2: Issue fix의 GitHub 산출물 provenance가 설계보다 적다

근거: [설계의 commit 계약](GITHUB-ISSUE-FIX-DESIGN.md#6-commit-and-branch-publication),
[설계의 결과 댓글 계약](GITHUB-ISSUE-FIX-DESIGN.md#7-issue-result-comment),
[commit 구현](../workflows/_incident/fix.py#L379), [댓글 구현](../workflows/_incident/fix.py#L237).

커밋 메시지는 `fix: address issue #N`이며 설계가 요구하는 base SHA, execution key,
CAO run provenance trailer가 없다. 결과 댓글에는 branch/commit과 일반적인 테스트 통과 문구만 있고,
실제 명령/outcome, fix summary, run ID는 없다. execution key는 숨겨진 marker에만 있다.
private state에는 상세 정보가 일부 존재하지만 GitHub에서 실행까지 역추적하기 어렵다.

Issue number/base SHA/execution key/run ID를 커밋에 기록하고, 댓글에는 bounded summary,
검증 명령 및 결과, 실행 ID와 immutable branch/commit 링크를 제공해야 한다.
credential이나 로컬 개인 경로는 GitHub 산출물에 기록하지 않는다.

### F05 — P2: PR review 최종 결과와 publication intent 보존이 부분적이다

근거: [review publication](../workflows/github-pr-review/workflow.py#L412),
[review process](../workflows/github-pr-review/workflow.py#L457),
[review main](../workflows/github-pr-review/workflow.py#L620),
[PR apply reconciliation](../workflows/github-pr-apply/workflow.py#L405).

PR review는 GitHub marker와 CAO model step을 남기지만 private chain journal 기록은 정상 최종 반환 시에 한다.
POST 응답 유실 또는 여러 PR discovery 중 후속 PR 실패 시 앞선 publication의 최종 결과가 보존되지 않을 수 있다.
GitHub marker는 재조사 근거가 되지만 posting intent/실행 identity별 durable 결과가 아니다.
자동 jobs는 force review를 사용하므로 marker 기반 skip만으로 모든 모호한 실패를 해결할 수 없다.
기존 queue의 안전한 blocked 처리는 중복 방지에 도움을 주지만 자동 복구 완료를 의미하지 않는다.

단독 review에는 chain journal fallback이 없다. 단독 PR apply의 already-applied 판정도
CAO retained `output`에 직접 의존해 설치된 API에서는 검증된 이전 적용을 재사용하지 못한다.

PR별 시작·종료 결과와 publication intent를 쓰기 전에 보존하고, typed review ID를 받으면 즉시 확정해야 한다.
정확한 원격 증거가 부족하면 새 POST 대신 reconciliation-required로 남겨야 한다.
최종 결과 저장 계약을 단독 실행에도 제공하고 discovery의 부분 완료를 명시해야 한다.

### F06 — P2: MCP monitor의 입력 이벤트 처리와 attempt lineage가 설계보다 좁다

근거: [설계의 incremental collection](MCP-EXCEPTION-ISSUE-DESIGN.md#1-admission-locking-and-incremental-collection),
[monitor store](../workflows/_incident/monitor.py#L19),
[incident 처리·저장](../workflows/_incident/monitor.py#L267).

설계에는 관계/순서에 따른 multiline stack trace grouping이 있지만 구현은 normalized record의
`evidence_id`마다 별도 incident로 처리한다. provider가 완성된 stack trace 한 record를 반환하면 문제는 없지만,
여러 record로 분리된 trace를 workflow가 묶는 기능은 없다. provider 책임으로 좁힐지 구현할지 명시해야 한다.

incidents 테이블은 현재 결과와 attempts counter를 갱신하며, operator retry history 외에는
각 자동 attempt의 run ID, 단계, 시작/종료, query/evidence digest, reason 이력이 없다.
Issue payload의 MCP provenance/source permalink는 구현되어 있지만 분석 실행까지의 연결은 불완전하다.
append-only attempt 기록과 execution/run/step 연결이 필요하다.

### F07 — P2: 설계·구현·검증 사이의 명시적 traceability 표가 없다

각 설계에 acceptance bullet은 있지만 안정적인 requirement ID와 구현/test/live 증거 매핑이 없다.
현재 test suite 통과만으로는 F01/F02 같은 실행 경로 공백을 발견하기 어렵다.
[PR apply 설계](GITHUB-PR-REVIEW-APPLY-DESIGN.md)의 상단은 live 검증 미수행으로 표시하지만
[polling validation](GITHUB-POLLING-VALIDATION.md)은 실제 PR review/apply/push 검증을 기록한다.
입출력 계약 표도 retained `output`을 직접 얻는 것으로 기술되어 최신 coordinator 구현과 다르다.
PR review 입력 표는 v7 automation 내부 입력을 포함하지 않아 직접 CAO 입력과 내부 전용 입력의 구분이 부족하다.

요구사항 → 함수/profile → test → 실행 증거를 연결하고, Implemented/Partial/Proposed와
static/unit/controlled/live 증거를 구분해야 한다. 과거 live 검증은 당시 revision/environment로만 유효하다.

## 워크플로별 기능·추적 매핑

아래 ID는 이번 검토의 참조 ID이며 기존 설계에 이미 도입된 requirement ID가 아니다.

| 참조 | 설계 요구사항 | 구현/시험 증거 | 판정 및 공백 |
| --- | --- | --- | --- |
| RV-01 | HEAD/base 고정, changed-line finding, deterministic COMMENT | review process/parse_result/aggregate/publication_gate; test_workflow | 핵심 구현. publication intent/단독 최종 결과는 Partial(F05) |
| AP-01 | comment별 outcomes, exact edits, isolated tests, 조건부 push | apply validate_review/apply_edits/test_candidate/publish; test_apply | 핵심 구현. 실패 테스트 진단·단독 재사용은 Partial(F03/F05) |
| CH-01 | review→apply, 동일 run-ID recovery, frozen dependencies | review_apply execute_stage/retained_stage_result; test_review_apply/test_automation; 기록된 polling live evidence | PR 결합 실행은 journal 보완. 실제 retained API 형식을 모든 경로에 적용해야 함 |
| MI-01 | MCP-only evidence, deployed GitHub SHA, incremental queue, Issue dedup/recovery | common collect/revision, monitor process/publication; test_mcp_sdk/test_incidents | 핵심 구현. 최종 결과 계약·grouping·attempt 이력은 Partial(F01/F06) |
| IF-01 | 독립 Issue fix, snapshot 고정, regression, isolated tests, branch/comment recovery | fix process/apply_proposal/reconcile_comment; test_incidents | 핵심 구현. before/after/요구사항 outcomes/provenance는 Partial(F02/F04) |
| OB-01 | 실행 결과·단계·검증·원격 publication 역추적 | CAO events/server logs, PR journal, monitor store, issue execution files | 여러 저장소에 분산. 공통 summary/진단 export 계약 없음(F01/F03/F07) |

적절히 제외된 기능은 자동 approval/merge/thread resolution, Issue 자동 fix chaining,
외부 MCP 서버 구현, 다중 호스트 worker이다. 이를 현재 기능 누락으로 분류하지 않았다.

## 지금 사용할 수 있는 로깅과 조회

CAO 자체에는 로깅이 있다. CLI 도움말과 설치 코드를 통해 확인한 명령:

```bash
cao workflow runs --limit 20
cao workflow status RUN_ID
cao workflow events RUN_ID --no-follow --json
cao workflow events RUN_ID --json
cao workflow result RUN_ID --json
```

`events --json`은 실시간 JSON 이벤트를 출력하며 끊긴 연결을 after-seq로 이어 받는다.
`--no-follow`는 현재까지의 이벤트 조회이고, events의 exit 0 자체도 업무 완료 증거가 아니다.
결과 API의 `steps[].output`은 모델/단계 결과이며 최종 publication 결과를 대체하지 않는다.

서버는 시작 시 `Server logs: .../cao_TIMESTAMP.log`를 출력한다.
기본 저장 위치는 `~/.aws/cli-agent-orchestrator/logs/`이며 실제 CAO home을 바꾸면 그 아래로 이동한다.
설치 코드에는 `CAO_LOG_LEVEL` → `logging.level` 매핑이 있다. 다음 서버 시작 때:

```bash
CAO_MEMORY_ENABLED=false CAO_LOG_LEVEL=DEBUG cao-server
```

이미 운영 중인 서버를 이 명령으로 중복 실행하지 않는다. 디버그 로그는 서버 동작 진단용이며
소실된 workflow 최종 결과나 버린 Docker stdout/stderr를 복원하지는 못한다.

terminal 로그는 CAO home의 `logs/terminal/<terminal_id>.log`에 저장된다.
model terminal 대화가 포함될 수 있으므로 private 진단 용도로 다룬다.
실행/step/terminal ID 연결에는 CAO step evidence/event가 필요하며 최종 업무 결과를 terminal 내용에서 추측하면 안 된다.

- PR 결합 실행: 출력된 `state_path` journal의 `child_outputs[RUN_ID]`, matching CAO terminal state,
  `apply_publication`, candidate `result.json`/`candidate.patch` 확인.
- MCP monitor: operator state의 `monitor.sqlite3`에서 incident/publication/checkpoint 확인.
  publication 결과와 개별 incident의 blocked/failed/deferred를 함께 확인.
- Issue fix: submission journal, `fix-<execution_id>.json` pointer,
  `issue-executions/<execution_key>.json`, candidate artifact, exact remote branch/commit/comment 확인.
- polling 사용 시에만 systemd user journal과 automation status가 추가로 있다.
  직접 CAO 실행에 이 경로가 자동 적용되는 것은 아니다.

```bash
python3 scripts/automation.py --config /absolute/automation.json status --json
journalctl --user -u cao-automation-worker.service -u cao-automation-poll.service
```

런처 stdout/stderr를 private 파일에 저장하거나 CAO events JSON을 별도 보관할 수 있다.
다만 CAO가 별도 프로세스로 실행하고 캡처한 workflow 출력이 런처 로그에 전부 나타나는 것은 아니다.
workflow 내부의 durable 결과/진단 저장을 추가해야 F01/F03을 해결할 수 있다.

## 제안하는 공통 실행 추적 계약

최종 결과, 이벤트, 검증 진단을 분리하되 같은 execution/run/step ID로 연결한다.

```text
execution_id / chain_id / job_id (해당 경로에만)
  → workflow + version + manager revision + policy/resource digests
  → run_id → step_id → attempt_id
  → subject(repository/PR/Issue/monitor) + head/base/deployed SHA
  → input/evidence/patch/test digests
  → publication intent → typed GitHub review/Issue/comment ID + commit SHA
```

1. **result.json**: schema version, workflow outcome, CAO state, failure stage/reason code,
   completeness/coverage, tests, artifact refs, publication certainty를 owner-only 파일에 원자적으로 저장.
   `emit_output` 전에 기록. 동일 실행의 identity/terminal 결과 덮어쓰기 규칙과 공유 file lock을 명시.
2. **events.jsonl**: stage_started/completed/failed, gate_decision, test_started/finished,
   publication_intent/confirmed/unknown, recovery_decision. UTC timestamp, per-run seq,
   elapsed_ms, attempt/run/step/subject를 포함. 원시 prompt/응답/로그/token 제외.
3. **test diagnostics**: command reference, image digest, timeout/exit code/duration,
   제한된 redacted stdout/stderr, size cap/truncation 표시. 반복 attempt별 파일과 digest 보존.
4. **summary/status**: execution ID 하나로 CAO state와 workflow outcome, 테스트/원격 산출물,
   unresolved 항목과 다음 조치를 조회. `completed + blocked` 같은 조합을 숨기지 않는다.
5. **retention/export**: 결과/이벤트/진단의 크기·기간·권한·rotation 정책과 redacted export를 설계.
   unresolved publication의 필수 증거는 자동 삭제하지 않는다. state 파일과 GitHub에 credential을 넣지 않는다.
6. **requirement matrix**: 안정적인 요구사항 ID와 구현/test/live evidence를 매핑.
   테스트는 outcome 없는 completed, timeout, pending write, restart, changed policy,
   remote conflict, missing journal, before/after regression을 포함해야 한다.

우선순위는 F01 결과 계약 통일 → F03 오류/테스트 진단 보존 → F02 회귀·완료 게이트 →
F04/F05/F06 실행 provenance 및 publication 복구 → F07 설계와 증거 매핑 동기화이다.
설계 갱신 후 review/fix semantics가 바뀌는 구현에는 maintainer 지침에 따라 workflow version 증가가 필요하다.
