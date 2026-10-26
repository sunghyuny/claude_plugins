---
name: api-diff
description: 두 OpenAPI 스펙(파일, URL, 또는 마지막 스냅샷)을 비교해서 breaking change와 클라이언트 영향을 분석한다. "스펙 바뀐 거 비교해줘", "breaking change 있어?", "API 버전 차이", "하위 호환 깨졌는지 봐줘" 같은 요청이나, 스펙 편집 훅이 breaking change를 경고했을 때 사용한다.
argument-hint: <이전 스펙|snapshot> <새 스펙>
---

# /api-diff — 스펙 변경 비교와 영향 분석

## 인자
`$ARGUMENTS`
- 값이 두 개: `<이전> <새것>` (파일 경로나 URL)
- 값이 하나: `snapshot <그 값>`으로 보고, 마지막 `/api-test` 때 저장한 스냅샷과 비교한다.
- 값이 없음: 프로젝트의 `openapi*`/`swagger*` 파일을 찾아서 스냅샷과 비교한다. git 저장소라면 `git show HEAD:<파일>`을 스크래치패드에 저장해서 "마지막 커밋 대비"로 비교할 수도 있다고 제안한다.

## 절차
1. api-test-pilot MCP의 `diff_specs(old, new)`를 호출한다. `error`가 오면 원인을 보여주고 멈춘다(스냅샷이 없으면 `/api-test`를 먼저 실행하라고 안내한다).
2. `breaking_count`가 0이면 non-breaking 변경 목록만 짧게 보여주고 끝낸다.
3. breaking change가 있으면 **api-reviewer 에이전트**를 호출한다. 프롬프트에 다음을 넣는다.
   - `diff_specs` 결과 JSON 전체
   - 두 스펙의 위치
   - 현재 프로젝트 경로 (에이전트가 이 엔드포인트를 쓰는 클라이언트 코드를 찾을 수 있게)
4. 에이전트 리포트를 그대로 전달하되, 맨 위에 한 줄 요약을 붙인다. 예: `breaking 12건(영향 상 3 / 중 5 / 하 4) — v2 배포 전에 클라이언트 3곳 수정 필요`

## 원칙
- 스펙이나 코드를 직접 고치지 않는다. 사용자가 고쳐 달라고 할 때만 고친다.
- 의도한 변경일 수 있으므로 "잘못됐다"고 단정하지 않고, 하위 호환을 지키는 대안(새 필드는 optional로, 삭제 대신 deprecated 표시, 버전 경로 분리 등)을 함께 제시한다.
