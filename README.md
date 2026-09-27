# CAO Workflow Management

이 저장소는 CAO Workflow와 Agent Profile의 원본을 Git으로 관리하고 CAO runtime에 설치합니다. 첫 워크플로 `github-pr-review`는 GitHub의 열린 PR을 검색하고, 새 HEAD SHA만 Code/Security/Test 관점에서 리뷰한 뒤 결과를 PR에 게시합니다. GitHub 트리거는 포함하지 않습니다.

## Prerequisites

- Linux/WSL, CAO 2.5.0 이상 (`cao`, `cao-server`), Codex CLI, `git`, `gh`, `jq`, `tmux`, Python 3.11 이상
- `cao-server` 실행 및 Codex 인증
- `gh auth login` 또는 유효한 `GH_TOKEN`/`GITHUB_TOKEN`. 대상 저장소에 Contents read, Pull requests read, Issues read가 필요합니다. 게시 시 Issues write(comment mode) 또는 Pull requests write(review mode)가 필요합니다.
- CAO 2.5.0 Codex provider의 기본 실행은 sandbox를 우회하므로, [Codex profile](config/cao_pr_review_readonly.config.toml)을 `$CODEX_HOME/cao_pr_review_readonly.config.toml`에 복사하세요(기본 위치 `~/.codex/`). 관리 스크립트의 `doctor`와 `run`이 이를 확인합니다. CAO 서버와 wrapper가 같은 `CODEX_HOME`을 사용해야 합니다.

## Quick Start

```bash
# 위의 Codex profile과 GitHub/Codex 인증을 먼저 준비
cp config/cao_pr_review_readonly.config.toml ~/.codex/cao_pr_review_readonly.config.toml
cao-server                         # 별도 터미널
make doctor
make validate
make test
make install
make status
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
```

`--dry-run`도 실제 PR 컨텍스트 수집 및 Codex 리뷰 단계를 수행하지만 GitHub에 게시하지 않습니다. `cao workflow run`은 기본적으로 run id를 출력하고 완료까지 추적합니다. 이미 리뷰한 같은 HEAD SHA는 게시 여부에 관계없이 GitHub marker가 있으면 건너뜁니다. 게시하지 않은 dry-run에는 marker가 남지 않습니다.

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

Install/update는 검증 후 배포하며, 동일한 파일은 건너뜁니다. 설치 상태는 CAO 홈의 `cao-workflow-project-state.json`에 원본 저장소 경로와 SHA-256으로 기록됩니다. 이름이 같아도 다른 프로젝트 소유거나 runtime에서 수정된 파일은 덮어쓰거나 삭제하지 않습니다. Profile은 `cao install`/`cao profile remove`를 사용합니다. CAO 2.5.0에 workflow create/update가 없어 Python 스크립트를 CAO workflow 디렉터리에 원자적으로 배치합니다.

## PR Review

```bash
./scripts/run.sh github-pr-review --repository owner/repo
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --publish-mode review
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review --severity-threshold critical
```

기본값은 `comment`이고 자동 APPROVE 또는 merge는 하지 않습니다. `review` mode의 quality gate는 threshold 이상의 finding이 있으면 REQUEST_CHANGES, 없으면 COMMENT review를 게시합니다. `--include-drafts`, `--base-branch`, `--workspace-root`, `--model`, `--no-publish`, `--detach`도 지원합니다. 자세한 입력과 정책은 [PR Review 문서](docs/GITHUB-PR-REVIEW.md)에 있습니다.

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
- `unmanaged` 또는 `modified`: CAO runtime 파일을 직접 고치지 말고 소유권과 원본을 확인합니다. 기존 파일을 무조건 덮어쓰지 않습니다.
- 큰 PR에서 제한 오류: 100개 열린 PR, 300개 변경 파일, 파일당 24KB patch, 컨텍스트 chunk당 120KB를 넘으면 일부 자료만 조용히 리뷰하지 않고 실패하거나 제외 정책을 적용합니다.

현재 환경에서 확인한 CAO CLI 계약은 [Architecture](docs/ARCHITECTURE.md)에 기록했습니다. 운영 및 rollback 절차는 [Operations](docs/OPERATIONS.md)에 있습니다.
