"""테스트 케이스를 실제로 호출하고 상태 코드와 응답 스키마를 검증한다."""

from __future__ import annotations

import json
import re
import shlex
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from jsonschema import Draft202012Validator

from .spec import Spec, SpecError, to_json_schema

MAX_BODY_PREVIEW = 500
MAX_CALL_BODY = 4000  # call_endpoint는 실제 id를 찾는 데 쓰이므로 조금 더 넉넉하게
MASK = "***"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# 로그인/인증 페이지로 보이는 경로. 단어 경계로 봐서 /authors 같은 경로는 걸리지 않게 한다
LOGIN_PATH = re.compile(
    r"(?<![a-z])(login|log-in|logon|signin|sign-in|sso|oauth2?|auth|authorize|authenticate|cas)(?![a-z])",
    re.IGNORECASE,
)


def build_url(base_url: str, path: str, path_params: dict[str, Any]) -> str:
    for name, value in path_params.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    return base_url.rstrip("/") + path


def validate_body(schema: dict[str, Any] | None, body: Any) -> list[str]:
    """응답 body를 스키마로 검증해서 오류 메시지 목록을 돌려준다."""
    if not schema:
        return []
    validator = Draft202012Validator(to_json_schema(schema))
    try:
        found = sorted(validator.iter_errors(body), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # 스펙 쪽 스키마가 잘못된 경우 검증 자체를 건너뛴다 (테스트 실패로 치지 않음)
        return []
    errors = []
    for err in found:
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{loc}: {err.message}")
        if len(errors) >= 10:
            errors.append("... (오류 10개 이상, 생략)")
            break
    return errors


def _parse_body(resp: httpx.Response) -> tuple[Any, bool]:
    if not resp.content:
        return None, False
    if "json" in resp.headers.get("content-type", ""):
        try:
            return resp.json(), True
        except ValueError:
            pass
    return resp.text, False


def run_case(
    spec: Spec,
    case: dict[str, Any],
    base_url: str,
    client: httpx.Client,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = build_url(base_url, case["path"], case.get("path_params") or {})
    auth_names = spec.auth_header_names()
    unauthenticated = case.get("auth") is False
    shared = headers or {}
    if unauthenticated:  # unauthorized 케이스: 인증 헤더만 빼고 나머지 공통 헤더는 유지
        shared = {k: v for k, v in shared.items() if k.lower() not in auth_names}
    req_headers = {**shared, **(case.get("headers") or {})}
    kwargs: dict[str, Any] = {"params": case.get("query") or None, "headers": req_headers}
    if case.get("body") is not None:
        kwargs["json"] = case["body"]

    result: dict[str, Any] = {
        "id": case.get("id"),
        "kind": case.get("kind"),
        "method": case["method"],
        "url": url,
        "expected_status": case.get("expected_status"),
    }
    if case.get("skip"):
        result.update(passed=None, skipped=case["skip"], status=None, schema_errors=[])
        return result
    try:
        request = client.build_request(case["method"], url, **kwargs)
    except UnicodeEncodeError as e:
        result.update(passed=False, status=None, schema_errors=[],
                      error=f"요청을 만들 수 없음: 헤더 값에는 ASCII 문자만 쓸 수 있습니다 ({e.object[e.start:e.end]!r})")
        return result
    except (TypeError, ValueError, httpx.InvalidURL) as e:  # JSON으로 바꿀 수 없는 body, 잘못된 query 값 등
        result.update(passed=False, status=None, error=f"요청을 만들 수 없음: {type(e).__name__}: {e}", schema_errors=[])
        return result
    shown_url = mask_url(str(request.url), spec.auth_query_names())
    result["url"] = shown_url  # query까지 포함한 실제 요청 URL

    started = time.perf_counter()
    try:
        # 인증 없이 보내는 케이스는 리다이렉트를 따라가지 않는다: '로그인 페이지로 보냈다'는 사실 자체가 판정 근거
        resp = client.send(request, follow_redirects=not unauthenticated)
    except httpx.HTTPError as e:
        result.update(passed=False, status=None, error=f"요청 실패: {type(e).__name__}: {e}", schema_errors=[],
                      reproduce=to_curl(request, shown_url, req_headers, case.get("body"), auth_names))
        return result
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    result["status"] = resp.status_code

    expected = case.get("expected_status") or []
    status_ok = not expected or resp.status_code in expected
    body, is_json = _parse_body(resp)

    login_redirect = None
    if unauthenticated and resp.status_code in REDIRECT_STATUSES:
        location = resp.headers.get("location", "")
        result["location"] = location  # 로그인과 무관한 리다이렉트여도 어디로 보냈는지 남긴다 (진단용)
        if is_login_url(location):
            login_redirect = location
            status_ok = True  # 401/403 대신 로그인 페이지로 보내는 서버(Spring Security, 세션 기반 등)
    elif resp.history and is_login_url(str(resp.url)):
        login_redirect = str(resp.url)  # 리다이렉트를 따라가 로그인 페이지에 도착: 엔드포인트에 닿지 못함
        status_ok = False
    if login_redirect:
        result["redirected_to"] = login_redirect

    schema_errors: list[str] = []
    undocumented = False
    try:
        op = spec.find(case["operation_id"]) if case.get("operation_id") else None
    except SpecError:
        op = None
    if op is not None and not login_redirect:  # 로그인 페이지 응답은 엔드포인트 스키마와 비교할 대상이 아니다
        undocumented = not op.documented(resp.status_code) and "default" not in op.responses
        schema = op.response_schema(resp.status_code)
        if schema and is_json:
            schema_errors = validate_body(schema, body)
        elif schema and schema.get("type") in ("object", "array") and not is_json:
            schema_errors = ["응답이 JSON이 아닙니다 (스펙은 JSON 스키마를 정의함)."]

    result["schema_errors"] = schema_errors
    result["undocumented_status"] = undocumented
    result["passed"] = status_ok and not schema_errors
    if not result["passed"]:
        result["response_preview"] = _preview(body, is_json)
        result["reproduce"] = to_curl(request, shown_url, req_headers, case.get("body"), auth_names,
                                      follow=bool(resp.history))
        if not unauthenticated and (resp.status_code in (401, 403) or login_redirect):
            # 서버 버그가 아니라 인증 설정 문제일 가능성이 높다. 원인을 따로 표시해서 실패 진단을 돕는다
            sent = any(k.lower() in auth_names for k in req_headers)
            where = "로그인 페이지로 리다이렉트됐습니다" if login_redirect else f"{resp.status_code}로 거부됐습니다"
            result["auth_issue"] = (
                f"{where}: 보낸 인증 정보가 거부됨 (토큰·세션 만료나 권한을 확인하세요)" if sent
                else f"{where}: 인증 정보 없이 호출함 (run_cases의 headers로 토큰이나 쿠키를 넘기세요)"
            )
    return result


def is_login_url(url: str) -> bool:
    """로그인/인증 페이지 URL인지. query(next=... 등)는 보지 않고 경로만 본다."""
    try:
        path = httpx.URL(url).path
    except httpx.InvalidURL:
        return False
    return LOGIN_PATH.search(path) is not None


def mask_url(url: str, secret_params: set[str]) -> str:
    """query로 보낸 API 키 값을 가린다."""
    if not secret_params:
        return url
    parsed = httpx.URL(url)
    if not any(name in parsed.params for name in secret_params):
        return url
    pairs = [(k, MASK if k in secret_params else v) for k, v in parsed.params.multi_items()]
    return str(parsed.copy_with(query=urlencode(pairs, safe="*").encode()))


def _mask_value(value: str) -> str:
    # 'Bearer xxx' → 'Bearer ***' (방식은 남겨서 어떤 인증인지 알 수 있게)
    scheme, _, rest = value.partition(" ")
    return f"{scheme} {MASK}" if rest else MASK


def to_curl(request: httpx.Request, url: str, headers: dict[str, str], body: Any, auth_names: set[str],
            follow: bool = False) -> str:
    """실패한 요청을 그대로 다시 보내는 curl 명령 (POSIX 셸 따옴표). 인증 값은 가린다."""
    parts = ["curl", "-X", request.method]
    if follow:
        parts.append("-L")
    parts.append(shlex.quote(url))
    for name, value in headers.items():
        shown = _mask_value(str(value)) if name.lower() in auth_names else str(value)
        parts += ["-H", shlex.quote(f"{name}: {shown}")]
    if body is not None:
        parts += ["-H", shlex.quote("Content-Type: application/json"),
                  "-d", shlex.quote(json.dumps(body, ensure_ascii=False))]
    return " ".join(parts)


def _safe_run_case(spec: Spec, case: Any, base_url: str, client: httpx.Client,
                   headers: dict[str, str] | None) -> dict[str, Any]:
    """케이스 하나가 잘못돼도(필수 키 누락 등) 나머지 케이스는 계속 실행한다."""
    try:
        return run_case(spec, case, base_url, client, headers)
    except Exception as e:
        info = case if isinstance(case, dict) else {}
        return {
            "id": info.get("id"),
            "kind": info.get("kind"),
            "method": info.get("method"),
            "url": None,
            "expected_status": info.get("expected_status"),
            "status": None,
            "schema_errors": [],
            "passed": False,
            "error": f"케이스를 처리할 수 없음: {type(e).__name__}: {e}",
        }


def _preview(body: Any, is_json: bool, limit: int = MAX_BODY_PREVIEW) -> Any:
    """응답 미리보기. 큰 응답이 MCP 결과(=Claude 컨텍스트)를 채우지 않도록 자른다."""
    if not is_json:
        return str(body or "")[:limit]
    text = json.dumps(body, ensure_ascii=False)
    if len(text) <= limit:
        return body  # 작으면 구조를 그대로 보여준다
    return f"{text[:limit]}... (총 {len(text)}자 중 앞부분만 표시)"


def run_cases(
    spec: Spec,
    cases: list[dict[str, Any]],
    base_url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    own_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        results = [_safe_run_case(spec, c, base_url, client, headers) for c in cases]
    finally:
        if own_client:
            client.close()
    passed = sum(1 for r in results if r["passed"] is True)
    skipped = sum(1 for r in results if r["passed"] is None)
    return {
        "spec": spec.title,
        "base_url": base_url,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed - skipped,
        "skipped": skipped,
        "auth_issues": sum(1 for r in results if r.get("auth_issue")),
        "results": results,
    }


MAX_LISTED_PASSED = 100
MAX_DETAILED_FAILURES = 30  # 실패가 많으면 대개 같은 원인의 반복이라, 나머지는 한 줄로 줄이고 파일로 넘긴다
# 실패 상세에서 빼는 필드: 판정에 이미 반영됐거나(passed) 진단에 쓰이지 않는 값
DROPPED_FAILURE_KEYS = ("passed", "elapsed_ms")


def compact_summary(summary: dict[str, Any], results_file: str | None = None) -> dict[str, Any]:
    """Claude에게 돌려줄 요약. 상세는 실패한 케이스에만 붙이고, 통과·건너뜀은 한 줄로 줄인다.
    전체 결과는 results_file에 있으니 리포트 작성이나 api-reviewer가 필요할 때 읽는다."""
    results = summary.get("results", [])
    failed = [r for r in results if r.get("passed") is False]
    failures = [
        {k: v for k, v in r.items()
         if k not in DROPPED_FAILURE_KEYS and v not in (None, [], {}, "") and not (k == "undocumented_status" and v is False)}
        for r in failed[:MAX_DETAILED_FAILURES]
    ]
    passed = [f"{r['id']} → {r.get('status')}" for r in results if r.get("passed") is True]
    out = {k: v for k, v in summary.items() if k != "results"}
    out["failures"] = failures
    if len(failed) > MAX_DETAILED_FAILURES:
        out["failures_more"] = [_failure_line(r) for r in failed[MAX_DETAILED_FAILURES:]]
    out["passed_cases"] = passed[:MAX_LISTED_PASSED]
    if len(passed) > MAX_LISTED_PASSED:
        out["passed_more"] = len(passed) - MAX_LISTED_PASSED
    skipped = [f"{r['id']}: {r.get('skipped')}" for r in results if r.get("passed") is None]
    if skipped:
        out["skipped_cases"] = skipped
    if results_file:
        out["results_file"] = results_file
    return out


def _failure_line(r: dict[str, Any]) -> str:
    if r.get("error"):
        reason = "요청 오류"
    elif r.get("auth_issue"):
        reason = "인증"
    elif r.get("schema_errors"):
        reason = f"스키마 {len(r['schema_errors'])}건"
    else:
        reason = f"기대 {'/'.join(map(str, r.get('expected_status') or []))}"
    return f"{r['id']} → {r.get('status')} ({reason})"


def call_endpoint(
    method: str,
    url: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        kwargs: dict[str, Any] = {"params": query, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        resp = client.request(method.upper(), url, **kwargs)
    parsed, is_json = _parse_body(resp)
    return {
        "status": resp.status_code,
        "headers": {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "location", "content-length")},
        "body": _preview(parsed, is_json, MAX_CALL_BODY),
    }
