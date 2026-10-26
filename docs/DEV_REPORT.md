# api-test-pilot 개발·수정 보고서

> 작성일: 2026-10-26 · 대상 버전: 0.1.0 (미배포)
> 측정 환경: Windows 10 Pro, Python 3.12, uv, Claude Code CLI (headless `-p` 모드)

이 문서는 플러그인을 처음 만든 상태(0.1.0)에서 시작해서, 실제 환경 테스트로 찾은 문제를 **어디를 어떻게 고쳤고 수치가 얼마나 달라졌는지** 차례로 기록합니다.

---

## 요약

| 지표 | 처음 (0.1.0) | 현재 | 변화 |
|---|---|---|---|
| 단위 테스트 | 38개 통과 | **83개 통과** | 회귀 테스트 +45 |
| 스펙이 아닌 파일 편집 시 훅 1회 | 약 400 ms | **약 150 ms** | 파이썬을 띄우지 않음 |
| 공개 Petstore 19건 실행 시간 | 4.70 s | **1.22 s** (동시 8) | 약 3.9배 |
| `/api-test --save` 실제 실행 턴 수 | 10턴 | **6턴** | -4턴 |
| 같은 breaking change 경고 (편집 4번) | 약 896 토큰 | **약 226 토큰** | 한 번만 알림 |
| `/api-test` 1회 MCP 데이터 (엔드포인트 19개) | 약 7,656 토큰 | **약 1,336 토큰** | **-83%** |
| `/api-test` 1회 MCP 데이터 (엔드포인트 200개) | 약 184,744 토큰 | **약 7,601 토큰** | **-96%** |
| 스펙이 아닌 4.4MB JSON 편집 시 훅 시간 | 29,455 ms (제한 20초 초과) | **527 ms** | **약 56배 빨라짐** |
| 큰 실패 응답 1건의 결과 크기 | 380,802 bytes | **1,429 bytes** | **약 266분의 1** |
| 경계 상황 서버 통과율 | 4 / 13 (31%) | **8 / 12 (67%)** | 거짓 실패 5건 해소 |
| 잘못된 케이스 1건 때문에 전체 실행이 멈추는 경우 | 5개 시나리오 중 3개 | **0개** | |
| 인증 테스트 | 없음 | `unauthorized` 케이스, `auth_issue` 원인 표시, 로그인 리다이렉트 판정 | 새 기능 |
| 세션 기반 서버의 잘못된 인증 판정 | 8건 중 2건 (거짓 통과 1, 거짓 실패 1) | **0건** | |
| 결과 URL | `/search` (query 없음) | `/search?q=string&page=abc` + 실패 시 `curl` 재현 명령 | |

남은 실패는 모두 실제 서버 동작(데이터가 없어서 난 404, 의도한 500, 토큰 없이 호출한 401)이고, 플러그인이 잘못 판정한 거짓 실패는 없습니다.

---

## 0단계. 처음 만든 상태 (0.1.0)

### 구성
| 구성 요소 | 내용 |
|---|---|
| MCP 서버 | `load_spec`, `list_endpoints`, `generate_cases`, `run_cases`, `call_endpoint`, `diff_specs`, `save_snapshot`, `export_pytest` (도구 8개) |
| Skill | `/api-test`, `/api-diff` |
| Agent | `api-reviewer` (breaking change 영향도와 실패 원인 진단) |
| Hook | PreToolUse/PostToolUse(스펙 편집 검증), Stop(테스트 결과 요약) |
| CI | GitHub Actions (ubuntu/windows pytest, `claude plugin validate`) |

### 실제 환경 1차 검증 (수정 전)
| 항목 | 방법 | 결과 |
|---|---|---|
| 단위 테스트 | `uv run pytest` (네트워크는 respx mock) | 38개 통과 |
| 플러그인 검증 | `claude plugin validate .` | 통과 |
| 실제 HTTP 호출 | 로컬 FastAPI 데모 서버 | 7개 통과 / 1개 실패. 실패는 일부러 스펙과 다르게 만든 `/broken` |
| 공개 서버 | Petstore v3 (읽기 요청만) | 12개 통과 / 3개 실패. 예시 id가 실제로 없어서 난 404 |
| 내보낸 pytest 단독 실행 | `pytest tests/generated` | MCP 실행 결과와 같음 (7/1) |
| MCP stdio | `.mcp.json`과 같은 명령으로 띄운 클라이언트 | 도구 8개 노출, 호출 정상 |
| 훅 | 편집 이벤트를 흉내 냄 | breaking 9건 경고, 구조 오류 block, 스펙이 아닌 파일은 통과, Stop 요약 1회 |
| Claude Code | `claude --plugin-dir . -p "/api-test ..."`, `/api-diff` | 결과표, 원인 진단, 파일 저장, 에이전트 호출 모두 정상 |

기본 흐름은 정상이었습니다. 하지만 경계 상황을 넣은 2차 테스트에서 아래 문제들이 드러났습니다.

### 경계 상황 2차 테스트 (수정 전 기준선)
경계 상황 서버(uuid id, 범위 제한, pattern, 파라미터 수준 example, 큰 응답, Bearer 인증, 제약이 있는 POST body)를 따로 만들어 측정했습니다.

| 측정 항목 | 값 |
|---|---|
| 경계 서버 결과 | **4 통과 / 9 실패** (13건) |
| 큰 실패 응답 1건(5000개 배열)의 결과 크기 | **380,802 bytes** |
| 스펙이 아닌 4.4MB JSON의 PreToolUse 훅 | **29,455 ms** (훅 제한 시간 20초 초과) |
| 일반 파일을 편집할 때 훅 1회 | 356~360 ms |
| Stop 훅 1회 | 169 ms |
| Swagger 2.0 (Petstore v2) | 7 통과 / 9 실패 (실패는 서버 데이터와 스펙 불일치) |

---

## 1차 수정 — 🔴 버그 8건

### #1 큰 파일을 편집하면 훅이 시간 초과됨
- **원인:** 스펙인지 판단하려고 편집한 파일 전체를 매번 `yaml.safe_load`(순수 Python 파서)로 읽었습니다. `package-lock.json` 같은 큰 파일에서 29초가 걸렸습니다.
- **수정 위치:**
  - `scripts/on_spec_edit.py`
    - `SPEC_MARKER` 정규식을 추가했습니다. `openapi:`/`swagger:` 키가 보이지 않으면 파싱하지 않습니다.
    - `_read()`를 추가해 20MB 상한을 두었습니다.
    - 원래 스펙이던 파일(편집 전 사본이 있는 파일)은 표식이 지워졌어도 계속 검사합니다.
  - `server/src/api_test_pilot/spec.py` `parse_text()`
    - JSON은 `json.loads`로 먼저 파싱합니다.
    - YAML은 libyaml(`CSafeLoader`)이 있으면 그것을 씁니다.
- **결과:**

| 파일 | 수정 전 | 수정 후 |
|---|---|---|
| 스펙이 아닌 4.4MB JSON, pre | 29,455 ms | **527 ms** |
| 스펙이 아닌 4.4MB JSON, post | (시간 초과) | **498 ms** |
| 실제 스펙 2.3MB JSON, pre | — | 472 ms |
| 실제 스펙 2.3MB JSON, post (breaking 감지 포함) | — | 1,085 ms |

### #2 실패한 응답을 자르지 않고 통째로 넣음
- **원인:** `runner.py`에서 응답이 JSON이면 `response_preview`에 본문 전체를 넣었습니다. 이 결과는 그대로 MCP 응답, 즉 Claude 컨텍스트에 들어갑니다.
- **수정 위치:** `runner.py`
  - `_preview()`를 추가했습니다. 직렬화한 길이가 500자를 넘으면 앞부분만 남기고 전체 길이를 함께 적습니다.
  - `call_endpoint`도 JSON 응답을 4,000자에서 자릅니다. 실제 id를 찾는 데 쓰이므로 조금 더 넉넉하게 두었습니다.
  - 작은 응답은 원래 구조 그대로 둡니다.
- **결과:** 결과 크기가 **380,802 bytes에서 1,429 bytes**로 줄었습니다. 경계 서버 전체 실행 결과도 4,458자에서 3,579자로 줄었습니다.

### #3 not_found 케이스가 404가 아니라 422를 받음 (거짓 실패)
- **원인:** 타입만 보고 값을 골랐습니다(문자열은 `"nonexistent-api-test-pilot"`, 숫자는 `987654321`). 그래서 uuid 형식이나 `maximum: 100` 제약에 걸려 서버가 422를 돌려줬습니다.
- **수정 위치:** `cases.py`
  - `not_found_value()`를 추가해 형식별 값을 씁니다(uuid는 `ffffffff-ffff-4fff-bfff-ffffffffffff` 등).
  - `pattern`과 길이 제약을 지키는 후보를 고릅니다.
  - enum이 있거나 `maximum` 때문에 값이 작아지면 케이스를 **만들지 않습니다**.
- **수정하면서 바로잡은 것:** 처음에는 "범위 안의 최댓값"을 넣었습니다. 그런데 Petstore v2의 `orderId`(maximum 10)는 10번 주문이 실제로 있어서 200이 났습니다. 범위가 막혀 있으면 "확실히 없는 값"을 만들 수 없다고 보고, 케이스를 생략하는 쪽으로 바꿨습니다.

### #4 happy 케이스 값이 스키마 제약을 무시함
- **원인:**
  - `sample_value()`가 `example`/`default`/`enum`만 읽고 `pattern`, `minLength`, `maxLength`, `maximum`, exclusive 경계를 무시했습니다.
  - 3.1/FastAPI 방식인 `examples`(배열)와, springdoc이 쓰는 파라미터·미디어 타입 수준의 `example`/`examples`도 읽지 않았습니다.
- **수정 위치:**
  - `cases.py`
    - `_numeric_bounds()`, `_number_in_range()`를 추가했습니다. 3.0의 bool 형식과 3.1의 숫자 형식 exclusive 경계, `multipleOf`를 모두 처리합니다.
    - `_string_value()`를 추가했습니다. 흔한 문자열 후보를 차례로 대 보며 pattern과 길이를 만족하는 첫 값을 고릅니다.
  - `spec.py`
    - `_with_example()`을 추가해 파라미터·body 수준의 예시값을 스키마로 옮깁니다.
    - Swagger 2.0 파라미터의 제약 키(`minLength`, `pattern` 등)도 이제 읽습니다.
- **결과:** 경계 서버의 `/codes`(pattern), `/lookup`(파라미터 examples), `/items`(제약이 있는 body)가 422에서 정상 통과로 바뀌었습니다.

### #5 diff가 잘못 잡거나 놓침
| 상황 | 수정 전 | 수정 후 |
|---|---|---|
| `/pets/{id}` → `/pets/{petId}` (이름만 변경) | `operation_removed` (거짓 breaking) | `path_param_renamed` (non-breaking) |
| `oneOf` 안의 응답 필드 삭제 | 감지 못 함 | `response_field_removed` |
| `allOf`로 상속한 필드 삭제 | 감지 못 함 | `response_field_removed` |
| request body 통째로 삭제 | 아무것도 보고 안 함 | `body_removed` (breaking) |
| 요청 query `integer` → `number` | `type_changed` (거짓 breaking) | `type_compatible_change` (non-breaking) |
| 응답 `integer` → `number` | `type_changed` | `type_changed` (그대로. 클라이언트가 깨질 수 있음) |

- **수정 위치:** `diff.py`
  - `PATH_VAR`로 경로의 `{…}`를 정규화해서 짝짓습니다.
  - `_flatten_all_of()`로 `allOf`를 합쳐서 비교합니다.
  - `oneOf`/`anyOf`는 순서대로 짝지어 비교하고, 변형 개수가 늘거나 줄면 보고합니다.
  - `_accepts()`로 호환되는 방향의 타입 변경을 판단합니다.

### #6 내보낸 pytest와 MCP 실행 결과의 URL이 다름
- **원인:** runner는 path 값을 `quote()`로 인코딩하는데, `export.py` 템플릿의 `_url()`은 인코딩하지 않았습니다. 그래서 `a b/c` 같은 값이 들어가면 두 쪽이 서로 다른 URL을 호출했습니다.
- **수정 위치:** `export.py` 템플릿의 `_url()`에서 `quote(str(value), safe="")`를 씁니다.

### #7 외부 `$ref`를 경고 없이 무시
- **원인:** `#/`로 시작하는 로컬 참조만 해석하고, `./common.yaml#/...` 같은 외부 참조는 아무 표시 없이 넘어갔습니다. 그래서 해당 응답의 스키마 검증이 조용히 빠졌습니다.
- **수정 위치:**
  - `spec.py`에 `unsupported_refs()`를 추가했습니다.
  - `server.py`의 `load_spec`이 이 결과를 `warnings`에 함께 돌려줍니다.
  - 유효한 스펙이므로 훅은 block하지 않습니다.

### #8 UTF-8이 아닌 파일에서 예외가 그대로 나감
- **원인:** `read_text(encoding="utf-8")`이 CP949 파일에서 `UnicodeDecodeError`를 냈는데, 이를 `SpecError`로 감싸지 않아 그대로 나갔습니다.
- **수정 위치:** `spec.py`에 `read_text_file()`을 추가했습니다. `utf-8-sig`로 BOM을 허용하고, 실패하면 "UTF-8로 바꿔 저장해 주세요"라는 `SpecError`를 냅니다. 편집 훅은 이 경우 block으로 알립니다.

### 1차 수정 결과
| 지표 | 수정 전 | 수정 후 |
|---|---|---|
| 단위 테스트 | 38 | **54** (`tests/test_edge_cases.py` +16) |
| 경계 서버 | 4 통과 / 9 실패 | **8 통과 / 4 실패** |
| Swagger 2.0 Petstore | 7 / 16 | 6 / 15 (실패 9건 같음. 확실하지 않은 not_found 1건 생략) |
| Claude Code `/api-test` (경계 서버) | — | 결과 같음, 실패 원인 분류 정확 |

---

## 2차 수정 — 인증(401) 케이스와 예외 처리

### 401/403 인증 케이스
- **배경:** 인증이 필요한 엔드포인트(`requires_auth: true`)를 감지만 하고, "인증 없이 호출하면 막히는가"는 테스트하지 않았습니다. 토큰 없이 실행하면 happy 케이스가 401로 실패했는데, 서버 버그와 구분되지 않았습니다.
- **수정 위치:**
  - `spec.py` `Spec.auth_header_names()`: `securitySchemes`(3.x)나 `securityDefinitions`(2.0)에서 인증 헤더 이름을 모읍니다. 기본은 `Authorization`이고, apiKey 헤더(예: `X-API-Key`)도 포함합니다.
  - `cases.py`: 인증이 필요한 operation에 `unauthorized` 케이스를 만듭니다. `auth: false`로 표시하고, 401과 403을 모두 기대값으로 둡니다(서버마다 다름).
  - `runner.py`
    - `unauthorized` 케이스에서는 **인증 헤더만** 빼고 보냅니다. `X-Tenant` 같은 다른 공통 헤더는 유지합니다.
    - 다른 케이스가 401/403으로 실패하면 `auth_issue`로 원인을 적습니다. 토큰을 안 보냈으면 "헤더로 토큰을 넘기세요", 보냈는데 거부됐으면 "토큰 만료나 권한을 확인하세요"입니다.
    - 요약에 `auth_issues` 건수를 추가했습니다.
  - `skills/api-test/SKILL.md`: `auth_issue`가 있는 실패는 서버 버그가 아니라 인증 설정 문제로 보고하도록 안내했습니다.
- **실제 FastAPI(HTTPBearer) 검증:**

| 상황 | happy | unauthorized | auth_issue |
|---|---|---|---|
| 토큰 없음 | 401 FAIL | 401 PASS | "인증 정보 없이 호출했습니다" |
| 잘못된 토큰 | 401 FAIL | 401 PASS | "보낸 인증 정보가 거부됐습니다" |
| 올바른 토큰 | 200 PASS | 401 PASS (토큰이 빠졌는지 확인) | 없음 |

### 예외 처리 (try/except)
- **배경:** 케이스 하나가 이상하면 `run_cases` 전체가 예외로 멈췄습니다. Claude가 값을 고쳐서 넘기는 구조라 충분히 생길 수 있는 일입니다.
- **수정 위치:** `runner.py`
  - `run_case()`
    - `UnicodeEncodeError`(헤더 값에 한글이 있음)를 잡아 "헤더 값에는 ASCII 문자만 쓸 수 있습니다"로 기록합니다.
    - `TypeError`/`ValueError`(JSON으로 바꿀 수 없는 body 등)를 잡아 "요청을 만들 수 없음"으로 기록합니다.
  - `_safe_run_case()`를 추가했습니다. 그 밖의 예외(`path` 키 누락 같은 잘못된 케이스)도 해당 케이스만 `error`로 남기고 다음 케이스를 계속 실행합니다.
- **결과:**

| 시나리오 | 수정 전 | 수정 후 |
|---|---|---|
| 헤더 값에 한글 | 전체 중단 (`UnicodeEncodeError`) | 그 케이스만 실패, 이유 표시 |
| body를 JSON으로 바꿀 수 없음 | 전체 중단 (`TypeError`) | 그 케이스만 실패, 이유 표시 |
| 케이스에 `path` 키 없음 | 전체 중단 (`KeyError`) | 그 케이스만 실패, 이유 표시 |
| base_url에 scheme 없음 | 처리됨 (`UnsupportedProtocol`) | 그대로 |
| 연결 실패나 타임아웃 | 처리됨 | 그대로 |

### 2차 수정 결과
단위 테스트가 **54개에서 58개**로 늘었습니다. 추가한 테스트는 인증 3개(케이스 생성, 헤더 제거, 원인 표시)와 예외 처리 1개입니다.

---

## 3차 수정 — 로그인 리다이렉트 판정, 결과 URL query 표시, curl 재현 명령

### 로그인 페이지 리다이렉트 (인증됨 → 엔드포인트 / 인증 안 됨 → 인증 페이지)
- **배경:** Spring Security나 세션 기반 서버는 인증 없이 호출하면 401 대신 **302로 로그인 페이지**로 보냅니다. 그런데 클라이언트가 리다이렉트를 자동으로 따라가서 판정이 두 방향으로 틀렸습니다.
  - **거짓 통과:** 인증 없이 호출한 happy 케이스가 로그인 페이지의 `200`을 받아 PASS로 판정됐습니다. 엔드포인트에는 닿지도 않았습니다.
  - **거짓 실패:** 정상적으로 막힌 `unauthorized` 케이스가 401/403이 아니라는 이유로 FAIL로 판정됐습니다.
- **판정 규칙:**

| 상태 | 서버 동작 | 판정 |
|---|---|---|
| 인증됨 | 엔드포인트가 리다이렉트 없이 응답 | 기존 기준(상태 코드, 스키마)대로 |
| 인증됨이지만 세션 만료 | 로그인 페이지로 리다이렉트 | **실패**. `auth_issue`: "보낸 인증 정보가 거부됨" |
| 인증 안 됨 (happy 등) | 로그인 페이지로 리다이렉트 | **실패**. `auth_issue`: "인증 정보 없이 호출함" |
| 인증 안 됨 (`unauthorized`) | 401/403 또는 로그인 페이지로 리다이렉트 | **통과** (`redirected_to` 표시) |
| 인증 안 됨 (`unauthorized`) | 로그인과 무관한 곳으로 리다이렉트 | **실패**. 인증 없이도 열려 있음 (`location` 표시) |

- **수정 위치:**
  - `runner.py`
    - `LOGIN_PATH`: login, signin, sso, oauth, auth, cas 등을 단어 경계로 판단합니다. `/authors` 같은 경로는 로그인으로 보지 않습니다.
    - `is_login_url()`: query(`next=...`)는 빼고 경로만 봅니다.
    - `unauthorized` 케이스는 `follow_redirects=False`로 보내서 첫 응답이 로그인 페이지로 가는 리다이렉트인지 확인합니다.
    - 다른 케이스는 리다이렉트를 따라간 뒤, 도착한 곳이 로그인 페이지면 실패로 봅니다. 이때 로그인 페이지 HTML은 스키마 검증 대상에서 뺍니다.
  - `spec.py` `auth_header_names()`: 쿠키 방식의 apiKey(세션 쿠키)를 인식합니다. `unauthorized` 케이스에서는 `Cookie` 헤더를 빼고 보냅니다.
  - `export.py`: 내보낸 pytest도 같은 규칙으로 판정합니다(`_is_login`, 케이스별 `auth` 표시).
- **세션 서버 검증 (로그인 리다이렉트 서버를 직접 만들어 측정):**

| 케이스 | 수정 전 | 수정 후 |
|---|---|---|
| `profile:happy` (인증 안 됨) | ✅ PASS (로그인 페이지 200, 거짓 통과) | ❌ FAIL, `auth_issue`: 로그인 페이지로 리다이렉트 |
| `profile:unauthorized` | ❌ FAIL (302, 거짓 실패) | ✅ PASS, `redirected_to: /login?next=/profile` |
| `profile:happy` (`SESSION=valid`) | — | ✅ PASS (엔드포인트 응답) |
| `profile:happy` (`SESSION=expired`) | — | ❌ FAIL, `auth_issue`: 보낸 인증 정보가 거부됨 |
| `authors:unauthorized` (로그인과 무관한 리다이렉트) | ❌ FAIL | ❌ FAIL (실제 문제), `location: /authors/list` |

내보낸 pytest를 세션 서버에 실행한 결과도 MCP 실행과 같았습니다(같은 2건 실패).

### 결과 URL에 query 표시 + 실패 케이스에 curl 재현 명령 (방안 A + C)
- **조사 결과:**
  - Schemathesis는 실패할 때마다 "Reproduce with:" 아래에 `curl` 명령을 보여 줍니다.
  - Dredd는 스펙에 적힌 그대로의 `uri`와 실제 값이 채워진 `fullPath`를 따로 둡니다.
  - 이를 참고해 **A(전체 URL)와 C(실패한 케이스에만 curl)**를 함께 적용했습니다.
- **수정 위치:** `runner.py`
  - 요청을 `client.build_request()`로 먼저 만들고, 실제로 보낸 URL을 `url`에 기록합니다. 그래서 runner와 서버가 보는 query 인코딩이 같습니다.
  - `mask_url()`: query로 보내는 API 키(`securitySchemes`의 `in: query`) 값을 `***`로 가립니다.
  - `to_curl()`: 메서드, 전체 URL, 보낸 헤더, JSON body로 명령을 만듭니다. 리다이렉트를 따라간 경우에는 `-L`을 붙입니다.
    - 인증 헤더 값은 `Bearer ***`처럼 방식만 남기고 가립니다.
    - `shlex.quote`로 작은따옴표가 들어간 body도 셸에서 안전하게 만듭니다.
  - 연결 실패로 끝난 케이스에도 재현 명령을 붙입니다.
- **결과:**

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| `invalid_type` 케이스 URL | `http://…/search` | `http://…/search?q=string&page=abc` |
| 만료된 세션의 재현 명령 | 없음 | `curl -X GET -L http://…/profile -H 'Cookie: ***'` |
| query API 키 | 노출 가능 | `?api_key=***` |

- **스킬 반영:** `/api-test` 결과표의 "요청" 칸에 query까지 적고, 실패 원인 아래에 `curl`을 코드 블록으로 붙입니다(PowerShell에서는 `curl.exe`로 안내). 로그인과 무관한 리다이렉트는 `location` 값으로 바로 진단하게 했습니다. 처음 Claude Code로 실행했을 때 `Location`을 보려고 `curl`을 따로 실행하려 했는데, 이 점을 고친 것입니다.

### 3차 수정 결과
| 지표 | 2차 후 | 3차 후 |
|---|---|---|
| 단위 테스트 | 58 | **63** (로그인 리다이렉트 2, URL·마스킹 1, curl body 1, export 1) |
| 데모 서버 | 7/1 | 7/1 (같음) |
| 경계 서버 | 8 통과 / 4 실패 (12건) | 9 통과 / 4 실패 (13건, `/me:unauthorized` 추가) |
| Claude Code `/api-test` (세션 서버) | — | 결과표에 query, 로그인 리다이렉트 판정, curl 모두 정상 표시 |

---

## 4차 수정 — 토큰 사용량 줄이기 (케이스 서버 보관, 결과 압축)

### 측정 방법
`/api-test` 한 번에 Claude 컨텍스트로 들어가는 데이터를 셌습니다. 도구 결과(출력)와, Claude가 다음 도구를 부를 때 넘기는 인자(입력)가 모두 포함됩니다. 토큰 수는 글자 수로 추정했습니다(JSON·영문은 약 4자, 한글은 약 1.5자당 1토큰). 측정 스크립트는 같은 추정식으로 수정 전과 후를 비교합니다.

### 원인
- **같은 케이스 목록이 컨텍스트에 세 번 들어갔습니다.** `generate_cases`가 케이스 전체(JSON)를 돌려주면, Claude가 그 목록을 `run_cases`와 `export_pytest` 인자로 다시 보냈습니다.
- **`run_cases` 결과에 통과한 케이스의 상세까지 모두 들어갔습니다.** 엔드포인트 200개 기준 결과 하나가 약 4만 4천 토큰이었습니다.
- **같은 정보를 두 번 받았습니다.** `list_endpoints`(약 1만 4천 토큰)와 `generate_cases`가 같은 정보를 담고 있었습니다.
- **에이전트에게도 큰 JSON을 넘겼습니다.** `api-reviewer`를 부를 때 실패 결과 JSON을 프롬프트에 붙여 넣었습니다.

### 수정 내용
| 위치 | 변경 |
|---|---|
| `server.py` `generate_cases` | 케이스를 서버 메모리와 `.api-test-pilot/cases/<spec_id>.json`에 보관합니다. 돌려주는 건 `'<id> <METHOD> <경로?query> → <기대> [body, 쓰기, 인증 없이]'` 한 줄 요약입니다(최대 100줄, 종류별 개수 포함). |
| `server.py` `get_cases` (새 도구) | body를 고쳐야 할 때처럼 요약으로 부족할 때만, 필요한 id의 전체 내용을 조회합니다. |
| `server.py` `run_cases` | 케이스 전체 대신 `case_ids`를 받습니다(생략하면 전체). `overrides={id: {바꿀 부분}}`로 값을 고치고, 고친 값은 보관되어 `export_pytest`에도 쓰입니다. 없는 id나 바꿀 수 없는 항목은 오류로 알려 줍니다. |
| `server.py` `export_pytest` | 케이스 전체 대신 `case_ids`(생략하면 전체)를 받습니다. |
| `server.py` `_get` | MCP 서버가 재시작돼도 저장된 케이스 파일의 source로 스펙을 다시 읽어 이어서 씁니다. |
| `runner.py` `compact_summary` | 실패 상세는 앞 30건만 남기고, 나머지 실패는 `'id → 상태 (원인)'` 한 줄로 줄입니다. 통과한 케이스는 `'id → 상태'`만 남기고, 빈 필드는 뺍니다. 전체 결과는 `.api-test-pilot/last_results.json`에 저장합니다. |
| `cases.py` | `case_line()`(한 줄 요약)과 `apply_overrides()`를 추가했습니다. dict 값은 키 단위로 합치고, 값을 `None`으로 주면 그 키를 지웁니다. |
| `skills/api-test/SKILL.md` | 기본 흐름에서 `list_endpoints`를 뺐습니다. `case_ids`와 `overrides`만 쓰고, 케이스 내용은 다시 보내지 않도록 했습니다. |
| `agents/api-reviewer.md` | 실패 JSON을 프롬프트로 받는 대신 `results_file` 경로를 받아 Read로 읽습니다. |

### 결과 — 단계별 컨텍스트 크기 (추정)
**Petstore v3 (실서버, 엔드포인트 19개, 케이스 19개)**

| 단계 | 수정 전 | 수정 후 |
|---|---|---|
| `load_spec` 결과 | 65 | 65 |
| `list_endpoints` 결과 | 1,447 | 0 (기본 흐름에서 뺌) |
| `generate_cases` 결과 | 1,466 | **324** |
| `run_cases` 인자 | 1,475 (케이스 재전송) | **35** (id·overrides) |
| `run_cases` 결과 | 1,728 | **894** |
| `export_pytest` 인자 | 1,475 (케이스 재전송) | **18** |
| **합계** | **7,656** | **1,336 (-83%)** |

**중간 규모 스펙 (mock, 엔드포인트 200개, 케이스 600개, 실패 400건)**

| 단계 | 수정 전 | 수정 후 |
|---|---|---|
| `list_endpoints` 결과 | 13,953 | 0 |
| `generate_cases` 결과 | 42,179 | **1,176** |
| `run_cases` 인자 | 42,182 | **28** |
| `run_cases` 결과 | 44,169 | **6,304** (처음에는 실패 상세 400건 전부로 23,046 → 상세를 30건으로 제한) |
| `export_pytest` 인자 | 42,182 | **13** |
| **합계** | **184,744** | **7,601 (-96%)** |

### 결과 — 실제 Claude Code 실행 (`/api-test <Petstore v3> --save`)
`--output-format stream-json`으로 Claude가 도구에 실제로 넘긴 인자를 확인했습니다.

| 도구 호출 | 인자 크기 | 결과 크기 |
|---|---|---|
| `generate_cases` | 20자 | 1,236자 |
| `call_endpoint` (실제 id 조회) | 114자 | 2,261자 |
| `run_cases` | **253자** (overrides 3건만: `petId: 4`, `tags: tag1`, `username: user1`) | 3,176자 |
| `export_pytest` | **71자** | 304자 |

- Claude가 케이스 내용을 다시 보내지 않고, id와 바꿀 값만 넘겼습니다.
- 내보낸 pytest에 overrides 값(`petId: 4`)이 반영됐고, 단독 실행 결과도 MCP 실행과 같았습니다(14 통과 / 5 실패).
- 실행 전체: 10턴, 비용 $0.37, cache_read 271,229, cache_creation 27,945, output 4,572 토큰.

### 남은 토큰 사용
이제 MCP 데이터보다 **턴마다 반복되는 Claude Code 기본 컨텍스트**(시스템 프롬프트, 스킬 문서 등. 대부분 캐시로 읽힘)가 더 큰 비중을 차지합니다. 다음으로 줄일 수 있는 부분은 다음과 같습니다.
- **턴 수 줄이기:** 위 실행은 10턴이었습니다. 도구 로드(ToolSearch)와 리포트 파일 이름 확인(Glob) 같은 단계를 줄일 수 있습니다.
- **같은 경고 반복 막기:** breaking change가 남아 있으면 편집할 때마다 약 224토큰이 다시 들어갑니다.

### 4차 수정 결과
| 지표 | 3차 후 | 4차 후 |
|---|---|---|
| 단위 테스트 | 63 | **69** (`tests/test_server_tools.py` +6) |
| MCP 도구 | 8개 | 9개 (`get_cases` 추가) |

---

## 5차 수정 — 같은 경고 반복 막기, 턴 수 줄이기

### 같은 breaking change 경고 반복
- **원인:** `/api-test`로 스냅샷이 생긴 뒤에는 편집 훅이 항상 스냅샷과 비교합니다. 그래서 breaking change가 하나라도 남아 있으면, 버전 번호만 바꾸는 편집에도 같은 경고(약 224토큰)가 매번 Claude 컨텍스트에 다시 들어갔습니다.
- **수정 위치:**
  - `store.py`: `warned_path`, `load_warned`, `save_warned`를 추가했습니다. 파일별로 알린 항목을 `.api-test-pilot/warned/`에 기록합니다.
  - `scripts/on_spec_edit.py`: `type|location` 키로 이미 알린 항목을 빼고, **새로 생긴 것만** 알립니다. 메시지에는 "이전에 알린 N건은 생략"을 붙입니다. 해결된 항목은 기록에서 빠지므로, 다시 생기면 다시 알립니다.
- **결과 (스냅샷 v1 → v2로 편집 후, 무관한 편집 3번):**

| 편집 | 수정 전 | 수정 후 |
|---|---|---|
| 1번째 (breaking 9건 발생) | 224 토큰 | 226 토큰 |
| 2~4번째 (버전 번호만 변경) | 224 토큰 × 3 | **0** (출력 없음) |
| **합계** | **약 896** | **약 226** |

### 턴 수 줄이기
- **원인:** 4차 수정 후 실제 실행은 10턴이었습니다. 턴마다 Claude Code 기본 컨텍스트(약 2만 7천 토큰)를 캐시에서 다시 읽기 때문에, 턴 하나를 줄이면 그만큼 캐시 읽기가 줄어듭니다.
  - 10턴: ToolSearch → load_spec → generate_cases → call_endpoint → run_cases → save_snapshot → export_pytest → Glob(리포트 이름 확인) → Write → 마무리
- **수정 위치:**

| 합친 단계 | 방법 |
|---|---|
| `load_spec` + `generate_cases` | `generate_cases`에 스펙 URL을 바로 넣으면 `spec`(제목, base_urls, 경고)을 같이 돌려줍니다. |
| `run_cases` + `save_snapshot` | `run_cases`가 실행 후 스냅샷을 자동으로 저장합니다(`snapshot` 경로 반환). |
| `run_cases` + `export_pytest` | `run_cases(export_pytest=true)` 한 번으로 pytest 파일을 만듭니다(`exported` 반환). |
| Glob(리포트 이름 중복 확인) | 서버가 `report_path`(`YYYY-MM-DD-HHMM-<title>.md`, 겹치면 `-2`)를 정해 줍니다. |

- **수정하면서 바로잡은 것:** 처음에는 스킬에 "파일 이름에 시각을 넣어라"라고만 적었습니다. 그랬더니 Claude가 현재 시각을 알아내려고 명령을 두 번(PowerShell, Bash) 실행해서 8턴이 됐습니다. 리포트 경로를 서버가 직접 정해 주도록 바꿔서 이 두 번을 없앴습니다.
- **결과 (실제 Claude Code, `/api-test <Petstore v3> --save`):**

| 지표 | 4차 후 | 5차 1차 시도 | **5차 최종** |
|---|---|---|---|
| 턴 수 | 10 | 8 | **6** |
| 도구 호출 | ToolSearch, load_spec, generate_cases, call_endpoint, run_cases, save_snapshot, export_pytest, Glob, Write | (시각 조회 명령 2번 추가) | ToolSearch, generate_cases, call_endpoint, run_cases, Write |
| cache_read 토큰 | 271,229 | 276,208 | **195,665 (-28%)** |
| 비용 (USD) | 0.369 | 0.382 | **0.357** |

비용은 조금만 줄었습니다. 남은 비용의 대부분이 첫 턴의 캐시 생성(약 2만 8천 토큰)과 출력(리포트 본문 등 약 4,700토큰)이라서, 턴 수를 줄여서는 크게 달라지지 않습니다.

### 5차 수정 결과
| 지표 | 4차 후 | 5차 후 |
|---|---|---|
| 단위 테스트 | 69 | **72** (경고 반복 1, 한 번에 실행 1, 스펙 바로 생성 1) |
| 생성되는 파일 | 6종 | 6종 (같음) + `warned/` |

---

## 6차 수정 — 훅 오버헤드, 토큰 환경변수 참조, format 검증, 동시 실행

### 훅 오버헤드
- **원인:** 모든 Edit/Write마다 PreToolUse와 PostToolUse 훅이 `uv run ... python`을 띄웠습니다. 파이썬 환경을 준비하는 데만 약 0.4초가 걸려서, 스펙이 아닌 파일을 편집할 때도 편집 한 번에 약 0.8초가 들었습니다.
- **수정 위치:**
  - `scripts/hook_gate.sh`(새 파일): 훅 입력에 `.json"`/`.yaml"`/`.yml"`가 있을 때만 `on_spec_edit.py`를 실행합니다. Stop 훅은 `.api-test-pilot/last_run.json`이 있을 때만 `on_stop.py`를 실행합니다.
  - `hooks/hooks.json`: 세 훅 모두 `sh hook_gate.sh <pre|post|stop>`을 거치게 했습니다.
  - 거르는 기준은 넉넉하게 잡았습니다. 애매하면 파이썬으로 넘기고, 최종 판단은 기존 파이썬 스크립트가 합니다.
- **결과 (Windows, 셸 시작 포함):**

| 훅 | 수정 전 | 수정 후 |
|---|---|---|
| PreToolUse, 스펙이 아닌 파일 | 378~410 ms | **144~164 ms** |
| Stop, 테스트 결과 없음 | 188~209 ms | **152~184 ms** |
| PostToolUse, 스펙 파일 (breaking 경고) | 동작 | 동작 (같음) |

남은 시간은 대부분 Windows에서 셸 프로세스를 띄우는 비용입니다. 테스트는 시간 대신 동작으로 검증합니다. 플러그인 위치를 없는 경로로 줘서, `uv`가 실행되면 오류가 나게 하고 아무 출력도 없는지 확인합니다.

### 토큰을 환경변수로 참조
- **원인:** 인증 토큰을 `run_cases(headers=...)`에 값 그대로 넣어야 해서, 토큰이 도구 인자로 대화 기록에 남았습니다. 내보낸 pytest에는 인증 헤더를 넣을 방법이 없었고, 인증이 필요한 `/openapi.json`도 불러올 수 없었습니다.
- **수정 위치:**
  - `spec.py` `expand_env()`: 헤더 값의 `${NAME}`을 MCP 서버 환경변수로 바꿉니다. 없는 변수가 있으면 이름을 알려 주는 오류를 냅니다.
  - `runner.py`: 실제로 보내는 헤더는 변수를 채운 값이고, curl 재현 명령에는 `${NAME}` 참조를 그대로 보여 줍니다. `call_endpoint`도 같은 방식입니다.
  - `server.py`
    - `run_cases`는 요청을 보내기 전에 공통 헤더의 변수부터 확인합니다. 없는 변수가 있으면 요청을 하나도 보내지 않습니다.
    - `load_spec`과 `generate_cases`에 `spec_headers`를 추가했습니다(보호된 스펙 URL용).
  - `export.py`: 내보낸 pytest가 `API_HEADERS` 환경변수(JSON)를 공통 헤더로 씁니다. 값의 `$VAR`/`${VAR}`는 환경변수로 바뀌고, `unauthorized` 케이스에서는 인증 헤더만 빠집니다.
- **결과:** 경계 서버 `/me`에 `Authorization: Bearer ${EDGE_TOKEN}`을 넘겼을 때 happy는 200, unauthorized는 401로 둘 다 통과했습니다. 결과 JSON 어디에도 토큰 값이 없었습니다. 없는 변수를 쓰면 요청 없이 바로 오류가 납니다.

### format 검증
- **원인:** 응답 스키마 검증에 `format`을 쓰지 않아서, `"created_at": "yesterday"` 같은 값도 통과했습니다. 게다가 jsonschema는 보조 패키지가 없으면 `date-time`과 `uri`를 검사하지 않고 조용히 넘어갑니다.
- **수정 위치:**
  - `runner.py` `validate_body`와 내보낸 pytest 템플릿에 `format_checker=Draft202012Validator.FORMAT_CHECKER`를 켰습니다.
  - `server/pyproject.toml`에 `rfc3339-validator`(date-time)와 `rfc3986-validator`(uri)를 추가했습니다. jsonschema의 `format-nongpl` 묶음은 의존성이 많아서 필요한 두 개만 넣었습니다.
- **결과:**

| 값 | 수정 전 | 수정 후 |
|---|---|---|
| `date-time: "yesterday"` | 통과 | **실패** |
| `email: "nope"` | 통과 | **실패** |
| `uuid: "123"` | 통과 | **실패** |
| `uri: "not a uri"` | 통과 | **실패** |
| `int64` 같은 모르는 format | 통과 | 통과 (검사 방법 없음) |

공개 Petstore에서는 format 때문에 새로 실패한 케이스가 없었습니다.

### 동시 실행
- **원인:** 케이스를 하나씩 순서대로 호출해서, 큰 스펙에서는 네트워크 대기 시간이 그대로 쌓였습니다.
- **수정 위치:**
  - `runner.py` `run_cases(concurrency=N)`(1~16): 연속된 읽기 요청을 스레드 풀로 묶어 동시에 보냅니다.
  - 쓰기 요청(POST/PUT/PATCH/DELETE)은 경계로 삼습니다. 앞의 읽기가 모두 끝난 뒤 혼자 실행되고, 뒤의 읽기는 쓰기가 끝난 다음에 시작합니다. 그래서 "만들고 조회" 같은 순서 의존이 깨지지 않습니다.
  - 결과 순서는 항상 입력 순서와 같습니다.
  - `server.py`의 `run_cases`에 `concurrency` 인자를 추가했습니다. 스킬은 케이스가 20건 이상이면 8로 실행하고, 운영 서버로 보이면 1로 둡니다.
- **결과:**

| 측정 | 순서대로 | 동시 8 |
|---|---|---|
| 공개 Petstore 19건 (실서버, 2회 중 빠른 값) | 4.70 s | **1.22 s** |
| mock 서버 8건, 응답마다 0.15초 지연 (단위 테스트) | 약 1.2 s | 3분의 1 미만 (테스트 조건) |

두 방식의 판정 결과는 같았습니다(통과 10 / 실패 9).

### 6차 수정 결과
| 지표 | 5차 후 | 6차 후 |
|---|---|---|
| 단위 테스트 | 72 | **83** (`tests/test_hardening.py` +11) |
| 의존성 | mcp, httpx, pyyaml, jsonschema | + rfc3339-validator, rfc3986-validator |

---

## 참고 — URL 표시 방안 조사

3차 수정 전에 결과의 `url`에는 query가 빠져 있었습니다(`/codes?code=STRING`이 `/codes`로 보임). 다른 도구들이 어떻게 하는지 조사했습니다.

- **Schemathesis:** 실패한 케이스마다 "Reproduce with:" 아래에 그대로 실행할 수 있는 `curl` 명령(메서드, 전체 URL, 헤더, body)을 보여 줍니다. 최근 버전은 케이스 ID와 `st replay`로 재실행도 지원합니다.
- **Dredd:** 요청을 method, uri, headers 블록으로 보여 줍니다. 스펙에 적힌 그대로의 `uri`와 파라미터가 채워진 `fullPath`를 따로 둡니다.

| 방안 | 장점 | 단점 |
|---|---|---|
| A. `url`에 query를 붙인 전체 URL | 간단하고 바로 복사해 쓸 수 있음 | query에 들어간 apiKey가 노출될 수 있어 가려야 함 |
| B. `path`(템플릿)와 `url`(전체)을 분리 (Dredd 방식) | 어느 엔드포인트인지와 실제 요청을 둘 다 볼 수 있음 | 필드가 늘어남 |
| C. 실패한 케이스에만 `reproduce` curl 명령 (Schemathesis 방식) | 헤더와 body까지 포함해 바로 재현 가능 | 출력이 길어짐. 토큰을 가리는 처리가 꼭 필요함 |

이 중 **A와 C를 함께 쓰기로 정하고 3차 수정에 반영했습니다.**

---

## 남은 과제

### 🟡 개선
- ~~훅 오버헤드~~, ~~토큰 환경변수 참조·`API_HEADERS`·보호된 스펙 URL~~, ~~`format` 검증~~, ~~동시 실행~~ → 6차 수정에서 해결했습니다.
- ~~같은 breaking change 경고 반복~~ → 5차 수정에서 해결했습니다.
- 남은 것:
  - MCP 서버가 재시작된 뒤 보호된 스펙 URL을 다시 읽으려면 `spec_headers`를 다시 넘겨야 합니다. 토큰을 디스크에 남기지 않으려고 헤더는 저장하지 않습니다.
  - 동시 실행은 같은 서버로 가는 연결 수 제한을 따로 두지 않습니다(최대 16).

### 📦 배포 전 정리
- ~~자리표시자 채우기~~ → `sunghyuny/claude_plugins`로 채웠습니다(README 설치 명령, `plugin.json`의 author·repository, `marketplace.json`의 owner).
- ~~`.gitignore`에 `.claude/settings.local.json` 추가~~ → 첫 커밋 때 반영했습니다.

---

## 참고 자료
- [Schemathesis CLI Tutorial](https://schemathesis.readthedocs.io/en/stable/tutorials/cli/)
- [Schemathesis — crash reproduction](https://schemathesis.readthedocs.io/en/latest/guides/crash-reproduction/)
- [Dredd — Data Structures](https://dredd.readthedocs.io/en/latest/data-structures/)
- [Dredd — How It Works](https://dredd.readthedocs.io/en/latest/how-it-works.html)
