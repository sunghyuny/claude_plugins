# api-test-pilot

> OpenAPI 스펙 하나로 API 테스트를 자동 생성·실행하고, 스펙이 바뀌면 breaking change를 잡아내는 Claude Code 플러그인

OpenAPI(Swagger) 스펙의 URL이나 파일만 주면, 엔드포인트마다 테스트 케이스(정상 요청, 404, 타입 오류, 필수값 누락)를 만들어 실제 서버를 호출합니다. 상태 코드와 응답 스키마를 검증하고, 결과를 독립 실행 가능한 pytest 파일로 남길 수 있습니다. 스펙 파일을 편집하면 훅이 자동으로 구조를 검증하고, 이전 버전과 비교해서 하위 호환이 깨지는 변경을 알려줍니다.

FastAPI, Spring(springdoc), NestJS(@nestjs/swagger), Django(drf-spectacular)처럼 OpenAPI 스펙을 내보내는 서버라면 모두 테스트할 수 있습니다.

## 구성

| 구성 요소 | 이름 | 역할 |
|---|---|---|
| **MCP 서버** | `api-test-pilot` | 스펙 로드·파싱, 케이스 생성, 호출·검증, diff, pytest 내보내기 (도구 9개) |
| **Skill** | `/api-test` | 스펙 로드 → 케이스 생성 → 실행 → 결과표 → (선택) pytest와 리포트 저장 |
| **Skill** | `/api-diff` | 두 스펙(또는 스냅샷)을 비교하고 api-reviewer에게 영향 분석을 맡김 |
| **Agent** | `api-reviewer` | breaking change 영향도(상/중/하) 분류, 프로젝트 안의 클라이언트 사용처 탐색, 테스트 실패 원인 진단 |
| **Hook** | PreToolUse / PostToolUse | 스펙 편집 전 사본 저장 → 편집 후 구조 검증과 breaking change 경고 |
| **Hook** | Stop | 이번 응답에서 테스트를 돌렸다면 통과/실패 요약을 한 줄로 표시 |

### MCP 도구

| 도구 | 설명 |
|---|---|
| `load_spec` | URL/파일에서 스펙 로드 (OpenAPI 3.x, Swagger 2.0 / JSON, YAML) |
| `list_endpoints` | method, path, operationId, 파라미터, 응답 코드 목록 |
| `generate_cases` | happy / not_found / invalid_type / missing_required / unauthorized 케이스 생성(기본은 읽기 요청만). 케이스는 서버가 보관하고 한 줄 요약만 반환 |
| `get_cases` | 보관 중인 케이스의 전체 내용 조회 (필요한 id만) |
| `run_cases` | 보관 중인 케이스를 `case_ids`로 골라 실행(`overrides`로 값 수정). 상태 코드와 응답 스키마를 검증하고, 실패 위주로 압축해서 반환. 스냅샷 자동 저장, `export_pytest=true`면 pytest 파일과 리포트 경로까지 한 번에 처리 |
| `call_endpoint` | 임의 요청 1회 (실제 id 조회 등) |
| `diff_specs` | breaking / non-breaking 변경 분류 (`old`에 `snapshot`을 넣으면 마지막 스냅샷과 비교) |
| `save_snapshot` | 현재 스펙을 비교 기준으로 저장 |
| `export_pytest` | 보관 중인 케이스를 독립 실행 가능한 pytest 파일로 저장 |

## 요구 사항

- [Claude Code](https://claude.com/claude-code)
- [uv](https://docs.astral.sh/uv/) (Python 3.11 이상은 uv가 알아서 준비합니다)

처음 실행할 때 uv가 MCP 서버의 의존성(mcp, httpx, pyyaml, jsonschema)을 설치하므로 몇 초 걸릴 수 있습니다.

## 설치

```bash
# Claude Code 안에서
/plugin marketplace add sunghyuny/claude_plugins
/plugin install api-test-pilot@api-test-pilot
```

로컬에서 개발할 때는 이렇게 실행합니다.
```bash
git clone https://github.com/sunghyuny/claude_plugins.git
claude --plugin-dir ./claude_plugins
```

## 사용법

### 1. API 테스트
```
/api-test https://petstore3.swagger.io/api/v3/openapi.json
/api-test http://localhost:8000/openapi.json --save          # FastAPI
/api-test ./openapi.yaml --base-url http://localhost:8080 --tag pets --writes
```
| 옵션 | 설명 |
|---|---|
| `--base-url` | 호출할 서버 주소 (기본값: 스펙의 `servers`, 없으면 스펙을 받아온 서버) |
| `--tag` | 해당 태그의 엔드포인트만 테스트 |
| `--writes` | POST/PUT/PATCH/DELETE까지 테스트 (실행 전에 확인을 받음) |
| `--save` | `tests/generated/test_<title>.py`와 `.api-test-pilot/reports/*.md` 저장 |

생성된 pytest 파일은 플러그인 없이도 실행됩니다.
```bash
API_BASE_URL=http://localhost:8000 pytest tests/generated/
```

### 2. 스펙 비교
```
/api-diff old/openapi.yaml openapi.yaml      # 두 파일 비교
/api-diff openapi.yaml                       # 마지막 /api-test 스냅샷과 비교
```

### 3. 자동 감지 (훅)
Claude가 `openapi.yaml` 같은 스펙 파일을 편집하면 자동으로 다음이 실행됩니다.
- 구조가 깨지면(필수 필드 누락, 해석할 수 없는 `$ref` 등) Claude에게 바로 알려서 고치게 합니다.
- 스냅샷(없으면 편집 직전 내용)과 비교해서 breaking change가 생기면 경고합니다. 같은 breaking change는 한 번만 알리고, 이후 편집에서는 새로 생긴 것만 알립니다.

```
⚠️ api-test-pilot: openapi.yaml에서 breaking change 2건 감지 (편집 전 대비)
- [operation_removed] GET /owners: 엔드포인트 삭제
- [type_changed] GET /pets/{petId} param 'petId' (path): 타입 변경: integer → string
```

## 판정 기준

**테스트 케이스**
| 종류 | 요청 | 기대 결과 |
|---|---|---|
| happy | 필수값을 example(파라미터·body 수준 포함) → examples → default → enum → 타입별 기본값 순으로 채움. 범위·길이·pattern 제약을 지킴 | 문서화된 2xx |
| not_found | path 파라미터에 존재하지 않을 값 (404가 문서화된 경우만). 형식(uuid 등)은 지키고, enum이나 maximum 때문에 확실히 없는 값을 만들 수 없으면 생략 | 404 |
| invalid_type | 숫자 파라미터에 `"abc"` | 400 / 422 |
| missing_required | 필수 query 또는 필수 body 생략 | 400 / 422 |
| unauthorized | 인증이 필요한 엔드포인트를 인증 헤더(쿠키 인증이면 Cookie)만 빼고 호출 | 401 / 403, 또는 로그인 페이지로 리다이렉트 |

**인증 흐름**
- 인증된 상태에서는 엔드포인트가 리다이렉트 없이 응답해야 통과입니다.
- 인증하지 않은 상태에서는 401/403을 돌려주거나 로그인 페이지로 리다이렉트하면 정상 차단으로 봅니다. Spring Security나 세션 기반 서버가 로그인 페이지로 보냅니다.
- 인증이 필요한 케이스가 리다이렉트를 따라가 로그인 페이지에 도착하면, 200이어도 **실패**로 봅니다.
- 401/403으로 실패하거나 로그인 페이지로 간 케이스에는 `auth_issue`로 원인(인증 정보 없음 / 보냈는데 거부됨)을 표시합니다.

**결과 표시**
- 결과의 `url`은 query까지 포함한 실제 요청 URL입니다. query로 보낸 API 키는 `***`로 가립니다.
- 실패한 케이스에는 바로 다시 실행할 수 있는 `reproduce`(curl 명령)를 붙입니다. 인증 헤더 값은 `Bearer ***`처럼 가립니다.
- 잘못된 케이스가 하나 있어도 전체 실행은 멈추지 않고, 그 케이스만 `error`로 기록합니다.

JSON이 아닌 body(multipart, octet-stream)를 받는 엔드포인트는 건너뜁니다. 외부 파일을 가리키는 `$ref`는 해석하지 않고 `load_spec` 경고로 알려줍니다.

**Breaking change** (클라이언트 기준)
- 요청: 엔드포인트 삭제, 필수 파라미터/필드 추가, 선택 → 필수 변경, 타입 변경, 파라미터 삭제, request body 삭제, enum 값 삭제, oneOf/anyOf 변형 삭제
- 응답: 성공 응답 코드 삭제, 필드 삭제, 타입 변경, 항상 오던 필드가 선택으로 변경, 새 enum 값 추가, oneOf/anyOf 변형 추가
- Non-breaking: 엔드포인트, 선택 파라미터, 응답 필드 추가, path 변수 이름만 변경(`/pets/{id}` → `/pets/{petId}`), 호환되는 타입 변경(요청 integer → number 등)
- `allOf`는 합쳐서 비교하고, `oneOf`/`anyOf`는 순서대로 짝지어 비교합니다.

## 생성되는 파일

```
<프로젝트>/
├─ tests/generated/test_<title>.py    # --save
└─ .api-test-pilot/                    # .gitignore에 추가하는 것을 권장
   ├─ snapshots/         # /api-test 시점의 스펙 (비교 기준)
   ├─ cases/             # 생성된 케이스 전체 (Claude에게는 요약만 보냄)
   ├─ reports/           # 테스트·리뷰 리포트 md
   ├─ last_run.json      # 마지막 실행 요약 (Stop 훅)
   ├─ warned/            # 편집 훅이 이미 알린 breaking change (반복 경고 방지)
   └─ last_results.json  # 마지막 실행 전체 결과 (리포트·api-reviewer가 필요할 때 읽음)
```

## 개발

```bash
cd server
uv sync
uv run pytest            # ../tests 실행 (네트워크는 respx로 mock)
claude plugin validate ..
```

```
api-test-pilot/
├─ .claude-plugin/        plugin.json, marketplace.json
├─ .mcp.json              MCP 서버 등록 (uv run --project server)
├─ server/                MCP 서버 (Python)
│  └─ src/api_test_pilot/ spec · cases · runner · diff · export · store · server
├─ skills/                api-test, api-diff
├─ agents/                api-reviewer
├─ hooks/hooks.json       PreToolUse / PostToolUse / Stop
├─ scripts/               훅 스크립트
└─ tests/                 pytest + fixture 스펙
```

## 라이선스

MIT
