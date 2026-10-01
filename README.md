# CAO Workflow Management

이 저장소는 CAO Workflow와 Agent Profile의 원본을 Git으로 관리하고 CAO runtime에 설치합니다. `github-pr-review`는 GitHub의 열린 PR을 검색하고, 새 HEAD SHA를 Code/Security/Test 관점에서 검토합니다. 각 finding은 변경 줄의 inline review comment로, 전체 요약은 하나의 PR review 본문으로 게시합니다. 리뷰어는 CAO `step()`으로 소유자 전용 `workspace_root`에서 실행합니다.

`github-pr-apply`와 상위 실행기는 review → apply를 한 요청으로 순서대로 처리합니다.
별도 read-only 모델이 치환안을 제안하고 워크플로가 후보 패치를 적용·검증합니다.
기본값은 `patch`이며, 정책이 허용한 PR 브랜치에는 선택적으로 `push`할 수 있습니다.

## Prerequisites

- Linux/WSL, CAO 2.5.0 이상 (`cao`, `cao-server`), Codex CLI, `git`, `gh`, `jq`, `tmux`, Python 3.11 이상
- `cao-server` 실행 및 Codex 인증
- `gh auth login` 또는 유효한 `GH_TOKEN`/`GITHUB_TOKEN`. 대상 저장소에 Contents read, Pull requests read, Issues read가 필요합니다. 게시에는 Pull requests write가 필요합니다.
- [Codex profile](config/cao_pr_review_readonly.config.toml)을 `$CODEX_HOME/cao_pr_review_readonly.config.toml`에 복사하세요(기본 위치 `~/.codex/`). CAO profile의 `codexProfile`이 이 read-only 설정을 선택합니다. 관리 스크립트의 `doctor`와 `run`이 profile을 확인합니다. CAO 서버와 wrapper가 같은 `CODEX_HOME`을 사용해야 합니다.
- Apply에는 [별도 Codex profile](config/cao_pr_apply_readonly.config.toml), 같은 호스트의 Docker, 사전 준비된 불변 test image, [운영자 apply 정책](config/apply-policy.example.json)이 필요합니다. 정책은 모델 작업 디렉터리 밖의 절대 경로에 mode `0600`으로 보관합니다. 예제 image digest와 bot 이름은 실제 값으로 바꿔야 합니다.

## Quick Start

```bash
# 위의 Codex profile과 GitHub/Codex 인증을 먼저 준비
cp config/cao_pr_review_readonly.config.toml ~/.codex/cao_pr_review_readonly.config.toml
cp config/cao_pr_apply_readonly.config.toml ~/.codex/cao_pr_apply_readonly.config.toml
cao-server                         # 별도 터미널
make validate
make test
make install
make doctor
make status
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
```

`--dry-run`도 실제 PR 컨텍스트 수집 및 CAO 리뷰 단계를 수행하지만 GitHub에 게시하지 않습니다. `cao workflow run`은 기본적으로 run id를 출력하고 완료까지 추적합니다. 같은 HEAD/base SHA와 workflow 버전의 marker가 있으면 건너뜁니다. 게시하지 않은 dry-run에는 marker가 남지 않습니다.

## Management

```bash
./scripts/list.sh
./scripts/validate.sh
./scripts/install.sh [github-pr-review]
./scripts/update.sh [github-pr-review]
./scripts/status.sh
./scripts/doctor.sh
./scripts/uninstall.sh [github-pr-review] --yes
```

Install/update는 검증 후 배포하며, 동일한 파일은 건너뜁니다. 설치 상태는 CAO 홈의 `cao-workflow-project-state.json`에 원본 저장소 경로와 SHA-256으로 기록됩니다. 이름이 같아도 다른 프로젝트 소유인 파일은 덮어쓰거나 삭제하지 않습니다. Runtime에서 수정된 파일은 기본적으로 설치와 삭제를 중단합니다. 이 프로젝트 소유로 기록된 수정 리소스를 삭제하려면 `./scripts/uninstall.sh [github-pr-review] --yes --force`를 사용합니다. `--force`는 수정 여부 검사만 건너뛰며 소유권 검사는 유지합니다. Profile은 `cao install`/`cao profile remove`를 사용합니다. CAO 2.5.0에 workflow create/update가 없어 Python 스크립트를 CAO workflow 디렉터리에 원자적으로 배치합니다.

## PR Review

```bash
./scripts/run.sh github-pr-review --repository owner/repo
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --publish-mode review
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review
```

기본값은 `review`입니다. 모든 finding을 inline review comment에, 최종 요약을 `COMMENT` review 본문에 넣습니다. 자동 APPROVE, REQUEST_CHANGES, merge는 하지 않습니다. `--include-drafts`, `--base-branch`, `--workspace-root`, `--model`, `--no-publish`, `--detach`도 지원합니다. 자세한 입력과 정책은 [PR Review 문서](docs/GITHUB-PR-REVIEW.md)에 있습니다.

## Review → Apply

```bash
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --apply \
  --policy /absolute/operator/apply-policy.json
# 브랜치 allowlist와 검증 조건을 충족할 때만 선택
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --apply \
  --policy /absolute/operator/apply-policy.json --apply-mode push
```

기존 review 명령에 `--apply --policy /absolute/operator/apply-policy.json`을 추가하면
review → apply를 순차 실행합니다. `--publish-mode review`, `--force-review`,
`--model`을 함께 사용할 수 있습니다. `--dry-run`/`--no-publish`는 apply와
함께 사용할 수 없으며, 연결 실행은 `--pr`로 하나의 PR을 지정해야 합니다.
`--apply-mode push`를 명시하지 않으면 후보 patch만 생성합니다.
기존 `run-review-apply.sh`는 재개 및 세부 workspace 설정용으로 유지합니다.

실행기는 정수 review ID와 원래 HEAD/base를 검증한 뒤 apply를 시작합니다.
모든 finding에 outcome이 필요하며 부분 적용은 푸시하지 않습니다. 후보 patch와
검증 결과는 고유한 `/tmp/cao-pr-apply/candidate-*` 디렉터리에 남고 checkout은
삭제합니다. 연결 상태 파일은 `/tmp/cao-pr-review-apply`에 mode `0600`으로 남으며,
연결이 끊긴 실행은 `./scripts/run-review-apply.sh --resume /tmp/cao-pr-review-apply/CHAIN.json`
으로 동일한 run ID를 조회해 이어갑니다. 실패·취소로 종결된 run의 재시도는
새 연결 실행에서 새 후보를 만듭니다.

수동 GitHub Action은 `cao` label이 있는 self-hosted Linux runner를 사용합니다.
저장소 변수 `CAO_WORKFLOW_PROJECT_ROOT`와 `CAO_APPLY_POLICY_PATH`를 설정하고,
고정된 관리 checkout과 설치 리소스를 Action의 commit SHA에 맞춰 준비합니다.
자세한 정책·제한과 운영 설정은 [Apply](workflows/github-pr-apply/README.md),
[설계서](docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md), [Operations](docs/OPERATIONS.md)에 있습니다.

## Structure

| 경로 | 역할 |
| --- | --- |
| `manifest.json` | 관리 대상과 Workflow 버전의 단일 목록 |
| `agents/` | CAO Markdown Profile 원본 |
| `workflows/` | CAO Python script-tier Workflow 원본 |
| `config/` | 기본값 및 환경 설정 예제 |
| `scripts/` | 설치, 검증, 실행, 상태, 삭제 |
| `tests/fixtures/` | GitHub API/리뷰 결과 테스트 자료 |
| `docs/` | 구조, 개발, 운영, 리뷰 정책 |

## Troubleshooting

- `make doctor`의 Codex profile 오류: `config/cao_pr_review_readonly.config.toml` 파일을 `$CODEX_HOME`에 복사하고 다시 확인합니다.
- `gh auth` 오류: 기존 인증 또는 토큰 권한을 확인합니다. 토큰은 이 저장소에 저장하지 않습니다.
- CAO server 연결 오류: `cao-server`를 실행하고 `CAO_API_PORT`가 서버 포트와 같은지 확인합니다.
- CAO 단계 오류: 각 모델 호출은 `step()`으로 기록됩니다. 워크플로가 보내는 프롬프트는 고정 형식의 shell no-op 토큰이며, 유효한 JSON 응답이 없으면 게시를 중단합니다. CAO의 메모리 주입은 프롬프트 앞에 별도 텍스트를 붙일 수 있으므로, 이 서버에서는 주입 대상 메모리가 비어 있는지 확인하거나 메모리 주입을 꺼야 이 보호가 유지됩니다.
- `unmanaged` 또는 `modified`: CAO runtime 파일을 직접 고치지 말고 소유권과 원본을 확인합니다. 기존 파일을 무조건 덮어쓰지 않습니다.
- 큰 PR에서 제한 오류: 100개 열린 PR, 300개 변경 파일, 파일당 24KB patch, 컨텍스트 chunk당 120KB를 넘으면 일부 자료만 조용히 리뷰하지 않고 실패하거나 제외 정책을 적용합니다.

현재 환경에서 확인한 CAO CLI 계약은 [Architecture](docs/ARCHITECTURE.md)에 기록했습니다. review → apply의 구현 계약은 [설계서](docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md)에 있습니다. 운영 및 rollback 절차는 [Operations](docs/OPERATIONS.md)에 있습니다.
