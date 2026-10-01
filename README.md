# CAO Workflow Management

CAO Workflow와 Agent Profile을 Git에서 관리하고 CAO runtime에 설치하는 저장소입니다.
Git이 원본이며, CAO 홈은 배포 대상입니다.

| Workflow | 역할 |
| --- | --- |
| `github-pr-review` | PR을 Code/Security/Test 관점에서 검토하고 변경 줄에 리뷰 게시 |
| `github-pr-apply` | 리뷰 조치안을 후보 patch로 만들고 검증 후 선택적으로 push |

Review와 apply는 `scripts/run.sh`에서 한 번에 실행할 수 있습니다.
모델은 read-only profile로 리뷰와 수정안을 제안하며, 파일 변경과 GitHub 게시는 워크플로가 처리합니다.

## 1. 실행 환경 준비

- Linux/WSL, Python 3.11 이상
- CAO 2.5.0 이상 (`cao`, `cao-server`), Codex CLI 및 Codex 인증
- `git`, `gh`, `jq`, `tmux`
- GitHub 인증: `gh auth login` 또는 `GH_TOKEN`/`GITHUB_TOKEN`
  - 조회: Contents read, Pull requests read, Issues read
  - 리뷰 게시: Pull requests write
  - 브랜치 push: Contents write

CAO 서버와 실행 스크립트는 같은 `CODEX_HOME`을 사용해야 합니다.
다음은 기본 위치인 `~/.codex`를 사용하는 예입니다.

```bash
mkdir -p ~/.codex
cp config/cao_pr_review_readonly.config.toml ~/.codex/
cp config/cao_pr_apply_readonly.config.toml ~/.codex/
```

Apply에는 같은 호스트의 Docker, 사전 준비된 불변 test image,
[운영자 정책](config/apply-policy.example.json)이 추가로 필요합니다.
정책의 저장소·리뷰 작성자·image digest·테스트 명령을 실제 값으로 설정하고,
모델 작업 디렉터리 밖의 절대 경로에 권한 `0600`으로 보관하세요.
기본 정책은 push를 허용하지 않습니다.
자세한 설정은 [운영 가이드](docs/OPERATIONS.md)를 참고하세요.

## 2. 검증 및 설치

별도 터미널에서 `cao-server`를 실행한 뒤 아래 명령을 진행합니다.

```bash
make validate
make test
make install
make doctor
make status
```

설치와 갱신은 `manifest.json`에 등록된 리소스만 관리하며, 동일한 파일은 건너뜁니다.
소유권이 없거나 runtime에서 수정된 파일은 덮어쓰지 않습니다.

## 3. PR 리뷰 실행

```bash
# 열린 PR 검색 및 리뷰
./scripts/run.sh github-pr-review --repository owner/repo

# 특정 PR 검토, 게시 생략
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run

# 특정 PR 검토 및 게시 (기본 모드)
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --publish-mode review

# 기존 리뷰 marker가 있어도 다시 검토
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review
```

Finding은 inline review comment로, 요약은 `COMMENT` review 본문으로 게시합니다.
같은 HEAD/base SHA와 workflow 버전의 marker가 있으면 건너뜁니다.
`--dry-run`도 실제 모델 검토를 수행하지만 게시하거나 marker를 남기지 않습니다.
자동 APPROVE, REQUEST_CHANGES, merge는 하지 않습니다.

추가 옵션은 `--include-drafts`, `--base-branch`, `--workspace-root`, `--model`,
`--no-publish`, `--detach`입니다. 상세 입력은 [PR Review](docs/GITHUB-PR-REVIEW.md)에 있습니다.

## 4. 리뷰 후 조치 실행

기존 review 명령에 `--apply`와 `--policy`를 추가합니다.

```bash
# Review → apply → 테스트 → 후보 patch 저장
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json

# 정책과 검증 조건을 충족하면 PR 브랜치에 push
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json --apply-mode push

# 다시 리뷰한 결과로 조치
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review \
  --apply --policy /absolute/operator/apply-policy.json
```

- 기본 모드는 `patch`입니다. `push`는 명시적으로 선택해야 합니다.
- `--pr`로 하나의 PR을 지정해야 하며, `--publish-mode review`, `--force-review`, `--model`을 함께 사용할 수 있습니다.
- `--dry-run`, `--no-publish`, `--detach` 및 PR 검색 옵션은 연결 실행에서 지원하지 않습니다.
- Review ID와 원래 HEAD/base를 검증합니다. 부분 조치, 테스트 실패, HEAD/base 변경 시 push하지 않습니다.
- 후보 patch와 결과는 `/tmp/cao-pr-apply/candidate-*`에 저장하고 checkout은 삭제합니다.

### 중단된 연결 실행 재개

실행 시 출력되는 `state_path`를 사용합니다. 기본 상태 디렉터리는
`/tmp/cao-pr-review-apply`이며 상태 파일 권한은 `0600`입니다.

```bash
./scripts/run-review-apply.sh --resume /tmp/cao-pr-review-apply/CHAIN.json
```

동일한 run ID를 조회해 이어갑니다. 실패·취소로 종결된 실행은 새 연결 실행으로 재시도합니다.
개별 apply 및 세부 workspace 설정은 [Apply 가이드](workflows/github-pr-apply/README.md)를 참고하세요.

### GitHub Actions에서 실행

[수동 Action](.github/workflows/pr-review-apply.yml)은 `self-hosted`, `linux`, `cao`
label의 runner에서 동일한 `run.sh` 명령을 실행합니다.
저장소 변수 `CAO_WORKFLOW_PROJECT_ROOT`와 `CAO_APPLY_POLICY_PATH`를 설정하고,
관리 checkout과 설치 리소스를 Action의 commit SHA에 맞춰 준비해야 합니다.

## 리소스 관리

아래 명령은 전체 리소스를 대상으로 합니다. 설치·갱신·삭제 명령 뒤에
`github-pr-review` 또는 `github-pr-apply`를 지정하면 해당 workflow와 관련 profile만 관리합니다.

```bash
./scripts/list.sh
./scripts/install.sh
./scripts/update.sh
./scripts/status.sh
./scripts/doctor.sh
./scripts/uninstall.sh --yes
```

소유권과 SHA-256은 CAO 홈의 `cao-workflow-project-state.json`에 기록합니다.
수정된 리소스의 삭제는 기본적으로 거부합니다. 이 프로젝트 소유임을 확인한 뒤
`uninstall.sh --yes --force`를 사용하면 수정 여부 검사만 생략하며 소유권 검사는 유지합니다.

## 문제 해결

| 증상 | 확인 사항 |
| --- | --- |
| Codex profile 오류 | 두 profile을 서버와 실행 스크립트가 사용하는 `CODEX_HOME`에 복사 |
| GitHub 인증 오류 | `gh` 인증 상태와 작업에 필요한 권한 확인 |
| CAO 서버 연결 오류 | `cao-server` 실행 상태와 `CAO_API_PORT` 확인 |
| `unmanaged` / `modified` | 배포 파일의 소유권과 Git 원본 확인 후 갱신 |
| 모델 응답·컨텍스트 제한 오류 | [운영 가이드](docs/OPERATIONS.md)의 실행 조건과 제한 확인 |

CAO 단계는 고정된 shell no-op 입력 토큰과 JSON 응답을 사용합니다.
이 보호를 유지하려면 서버의 메모리 주입 대상이 비어 있거나 메모리 주입이 꺼져 있어야 합니다.
인증 정보와 개인 CAO 상태는 저장소에 저장하지 않습니다.

## 저장소 구조와 문서

| 경로 | 역할 |
| --- | --- |
| `manifest.json` | 배포 리소스와 workflow 버전의 단일 목록 |
| `agents/` | Agent Profile 원본 |
| `workflows/` | CAO script workflow 원본 |
| `config/` | Codex 설정과 입력·정책 예제 |
| `scripts/` | 설치, 검증, 실행, 상태 조회, 삭제 |
| `tests/` | 관리·리뷰·조치 테스트와 fixture |
| `docs/` | 구조, 개발, 운영 및 설계 문서 |

- [Architecture](docs/ARCHITECTURE.md): CAO 계약과 실행 구조
- [Workflow Development](docs/WORKFLOW-DEVELOPMENT.md): workflow/profile 개발 규칙
- [Operations](docs/OPERATIONS.md): 운영 설정, 재개 및 rollback
- [PR Review](docs/GITHUB-PR-REVIEW.md): 리뷰 입력과 게시 정책
- [Review → Apply 설계](docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md): 연결 실행과 검증 계약
