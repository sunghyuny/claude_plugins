---
name: api-test
description: OpenAPI/Swagger 스펙(URL 또는 파일)으로 API 테스트를 자동 생성하고 실행한다. "API 테스트해줘", "엔드포인트 점검", "openapi.json으로 테스트", "FastAPI 서버 테스트", "스웨거 기반 테스트 만들어줘" 같은 요청에 사용한다. --save를 주면 독립 실행 가능한 pytest 파일과 md 리포트를 남긴다.
argument-hint: <스펙 URL 또는 파일 경로> [--base-url URL] [--tag 태그] [--writes] [--save]
---

# /api-test — OpenAPI 스펙 기반 API 테스트

api-test-pilot MCP 서버의 도구(`generate_cases`, `run_cases`, `call_endpoint`, `get_cases`)로 작업한다. 처음에 이 도구들을 한 번의 ToolSearch로 함께 불러온다.

**턴 절약 원칙**: 기본 흐름은 `generate_cases` → (필요하면 `call_endpoint`) → `run_cases` → (`--save`면 리포트 Write)이다.
- `load_spec`은 부르지 않는다. `generate_cases`에 스펙 위치를 바로 넣으면 스펙 요약이 같이 온다.
- `save_snapshot`도 부르지 않는다. `run_cases`가 스냅샷을 자동으로 저장한다.
- `export_pytest`도 따로 부르지 않는다. `run_cases`의 `export_pytest=true`로 처리한다.

**토큰 절약 원칙**: 케이스 전체 내용은 서버가 보관한다. `run_cases`·`export_pytest`에 케이스 내용을 다시 보내지 말고 `case_ids`(생략하면 전체)만 쓴다. 값을 고칠 때는 `overrides`에 바꿀 부분만 넣는다. `get_cases`는 body를 고쳐야 할 때처럼 요약만으로 부족할 때만, 필요한 id만 골라 부른다. 전체 결과가 필요하면 `results_file`을 Read로 읽는다.

## 인자
`$ARGUMENTS`에서 다음을 읽는다.
- 첫 번째 값: 스펙 위치. 예: `http://localhost:8000/openapi.json`(FastAPI), `./openapi.yaml`
  - 없으면 프로젝트에서 `openapi*.{json,yaml,yml}`, `swagger*.{json,yaml,yml}`을 찾아보고, 그래도 없으면 사용자에게 묻는다.
- `--base-url URL`: 실제로 호출할 서버 주소. 없으면 `generate_cases` 결과의 `spec.base_urls[0]`을 쓴다. 후보가 없으면 사용자에게 묻는다.
- `--tag 태그`: 해당 태그의 엔드포인트만 테스트한다.
- `--writes`: POST/PUT/PATCH/DELETE 케이스까지 만든다(기본값은 읽기 요청만).
- `--save`: pytest 파일과 리포트 파일을 남긴다.

## 절차
1. **스펙 로드 + 케이스 생성**: `generate_cases(<스펙 URL 또는 경로>, tag=..., include_writes=...)`.
   - `error`가 오면 원인을 보여주고 멈춘다.
   - `spec.warnings`가 있으면 결과 맨 위에 적는다.
   - base_url은 `--base-url`이 없으면 `spec.base_urls[0]`을 쓴다.
   - 이후 호출에는 결과의 `spec_id`를 쓴다.
   - 케이스는 `'<id> <METHOD> <경로?query> → <기대> [표시]'` 한 줄 요약으로 온다. 엔드포인트 목록은 이 요약으로 대신하고, `list_endpoints`는 파라미터 상세가 꼭 필요할 때만 부른다.
2. **값 다듬기**: happy 케이스의 path 파라미터가 예시값(1, "string" 등)이라 실제 데이터와 맞지 않을 것 같으면, 같은 리소스의 목록 엔드포인트(예: `GET /pets`)를 먼저 `call_endpoint`로 호출해서 실제 id를 구한다. 그리고 `run_cases`의 `overrides`로 그 값만 바꾼다(예: `{"getPetById:happy": {"path_params": {"petId": 10}}}`). 바꾼 경우 결과에 적는다.
3. **쓰기 요청 확인**: 요약에 `[쓰기]` 표시가 있는 케이스가 있으면 실행 전에 반드시 사용자에게 대상 URL과 해당 줄을 보여주고 확인을 받는다. 거절하면 그 케이스를 뺀 `case_ids`로 실행한다. 주소에 localhost, 127.0.0.1, dev, staging, test가 들어 있지 않으면 운영 서버일 수 있으므로 한 번 더 경고한다.
4. **인증**: 요약에 `[인증 없이]` 케이스(`unauthorized`)가 있는 엔드포인트는 인증이 필요하다. 사용자가 토큰을 주지 않았다면, 그 엔드포인트의 다른 케이스는 401/403이 날 수 있다고 미리 알린다. 토큰을 받으면 `headers`로 넘긴다. 토큰 값은 출력이나 파일에 남기지 않는다. 인증이 필요한 엔드포인트에는 `unauthorized` 케이스(인증 헤더 없이 호출 → 401/403 기대)가 자동으로 생기므로, 토큰을 넘겨도 이 케이스에서는 빠진다. 결과에 `auth_issue`가 있는 실패는 서버 버그가 아니라 인증 설정 문제로 분류해서 보고한다.
5. **실행**: `run_cases(spec_id, base_url, case_ids=..., overrides=..., headers=..., export_pytest=<--save 여부>)`. 전체를 돌릴 때는 `case_ids`를 생략한다. 결과는 집계와 함께 다음으로 구성된다.
   - `failures`: 실패 상세. 많으면 앞 30건만 있고, 나머지는 `failures_more`에 한 줄씩 있다.
   - `passed_cases`: 통과한 케이스의 `'id → 상태'` 목록
   - `results_file`: 전체 결과 파일 경로
   - `snapshot`: 자동 저장된 스냅샷 경로. 이후 스펙을 편집하면 훅이 이 스냅샷과 비교해서 breaking change를 알려준다.
   - `exported`: `export_pytest=true`일 때 만든 pytest 파일. overrides로 고친 값이 반영되어 있다.
   - `report_path`: `export_pytest=true`일 때 서버가 정해 준 리포트 파일 경로
6. **결과 보고**(아래 형식).
7. **--save일 때만**:
    - 보고 내용 전체를 `run_cases` 결과의 `report_path`에 그대로 Write한다. 파일 이름은 서버가 정해 주므로 날짜나 시각을 알아내거나 같은 이름이 있는지 확인하는 명령을 실행하지 않는다.
    - 실행 방법을 알려준다: `API_BASE_URL=<url> pytest <exported.written>`

## 결과 형식
한국어로 쓴다.

```
## API 테스트 결과 — <title> <version>
대상: <base_url> · 통과 N / 실패 N / 건너뜀 N

| 결과 | 케이스 | 요청 | 기대 | 실제 |
|---|---|---|---|---|
| ❌ | getPetById:happy | GET /pet/10 | 200 | 404 |
| ✅ | getPetById:not_found | GET /pet/987654321 | 404 | 404 |
```
- 실패한 케이스를 먼저 보여주고, 통과한 케이스가 많으면 접어서 개수만 적는다.
- 실패 원인은 아래 세 가지로 나눠서 한 줄씩 설명한다.
  - **스펙 불일치**: `schema_errors`가 있음(응답 형태가 스펙과 다름) → 스펙이나 서버 중 어느 쪽을 고쳐야 하는지 적는다.
  - **서버 동작 문제**: 잘못된 입력에 2xx를 주는 경우처럼 검증이 빠진 경우.
  - **테스트 데이터 문제**: 예시 id가 실제로 없어서 404가 나는 경우처럼, 테스트 값만 바꾸면 되는 경우.
  - **인증 문제**: `auth_issue`가 있음 → 서버 버그가 아니라 토큰·세션 설정 문제로 분류하고 `auth_issue` 문구를 그대로 옮긴다.
- "요청" 칸에는 결과의 `url`에서 base_url을 뺀 경로를 query까지 그대로 적는다(예: `GET /search?q=string&page=abc`). 가려진 값(`***`)은 그대로 둔다.
- 실패한 케이스의 `reproduce`(curl 명령)는 실패 원인 아래에 코드 블록으로 붙인다. 인증 값은 이미 `***`로 가려져 있으니 실제 토큰으로 바꿔 넣지 않는다. Windows PowerShell 사용자에게는 `curl.exe`로 실행하라고 덧붙인다.
- `unauthorized` 케이스가 로그인 페이지로 리다이렉트되어 통과한 경우(`redirected_to` 있음)는 "401 대신 로그인 페이지로 이동"이라고 실제 칸에 적는다. 로그인과 무관한 곳으로 리다이렉트되어 실패했다면 `location`에 이동한 위치가 있으니, 따로 호출하지 말고 그 값으로 "인증 없이도 열려 있음"을 진단한다.
- `undocumented_status: true`인 결과는 "스펙에 없는 상태 코드"라고 따로 표시한다.
- 통과한 케이스는 `passed_cases`의 id와 상태만으로 접어서 보여준다. 통과 케이스의 요청 URL을 보여주려고 따로 조회하지 않는다.
- `failures_more`에 있는 실패는 한 줄로만 나열하고, 같은 원인끼리 묶어서 설명한다.
- 실패가 많거나 원인이 애매하면 `api-reviewer` 에이전트에게 분석을 맡길 수 있다고 제안한다. 이때 실패 JSON을 프롬프트에 붙여 넣지 말고, `results_file` 경로와 스펙 위치, base_url만 넘긴다.
