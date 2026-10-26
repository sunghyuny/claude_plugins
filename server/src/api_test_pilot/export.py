"""테스트 케이스를 독립 실행 가능한 pytest 파일로 만든다.

생성된 파일은 플러그인 없이 `pytest`만으로 돌아가야 하므로 httpx와 jsonschema만 쓴다.
기준 URL은 환경변수 API_BASE_URL로 바꿀 수 있다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .runner import LOGIN_PATH
from .spec import Spec, to_json_schema

TEMPLATE = '''r"""{title} API 테스트 — api-test-pilot이 자동 생성함.

스펙: {source}
실행: API_BASE_URL={base_url} pytest {filename}
인증: API_HEADERS='{{"Authorization": "Bearer ${{API_TOKEN}}"}}' 처럼 JSON으로 공통 헤더를 넘긴다.
      값의 $VAR / ${{VAR}}는 환경변수로 바뀐다. 인증 없이 보내는 케이스(unauthorized)에서는 인증 헤더만 빠진다.
"""

import json
import os
import re
from urllib.parse import quote

import httpx
import pytest

try:
    from jsonschema import Draft202012Validator
except ImportError:  # jsonschema가 없으면 상태 코드만 검사한다
    Draft202012Validator = None

BASE_URL = os.environ.get("API_BASE_URL", {base_url_literal}).rstrip("/")
EXTRA_HEADERS = {{k: os.path.expandvars(str(v)) for k, v in json.loads(os.environ.get("API_HEADERS") or "{{}}").items()}}
AUTH_HEADER_NAMES = {auth_header_names}
REDIRECT_STATUSES = {{301, 302, 303, 307, 308}}
LOGIN_PATH = re.compile({login_pattern}, re.IGNORECASE)

CASES = json.loads(r"""
{cases_json}
""")


def _url(case):
    path = case["path"]
    for name, value in case["path_params"].items():
        path = path.replace("{{" + name + "}}", quote(str(value), safe=""))  # runner와 같은 인코딩
    return BASE_URL + path


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=10.0, follow_redirects=True) as c:
        yield c


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_api(client, case):
    if case.get("skip"):
        pytest.skip(case["skip"])
    unauthenticated = case.get("auth") is False
    shared = {{k: v for k, v in EXTRA_HEADERS.items() if not (unauthenticated and k.lower() in AUTH_HEADER_NAMES)}}
    kwargs = {{"params": case["query"] or None, "headers": {{**shared, **case["headers"]}} or None}}
    if case["body"] is not None:
        kwargs["json"] = case["body"]
    resp = client.request(case["method"], _url(case), follow_redirects=not unauthenticated, **kwargs)

    # 인증 없이 보낸 요청이 로그인 페이지로 리다이렉트되면 정상 차단으로 본다
    if unauthenticated and resp.status_code in REDIRECT_STATUSES and _is_login(resp.headers.get("location", "")):
        return
    assert not (resp.history and _is_login(str(resp.url))), (
        f"{{case['description']}}: 로그인 페이지로 리다이렉트됨 ({{resp.url}}). 인증 정보를 확인하세요"
    )
    assert resp.status_code in case["expected_status"], (
        f"{{case['description']}}: 기대 {{case['expected_status']}}, 실제 {{resp.status_code}}\\n{{resp.text[:300]}}"
    )

    schema = case["response_schemas"].get(str(resp.status_code))
    if not schema or Draft202012Validator is None:
        return
    body = _json_body(resp)
    if body is _NOT_JSON:
        # content-type만 JSON이고 본문은 평문인 서버도 있다. 스펙이 객체/배열을 약속했을 때만 실패로 본다
        assert schema.get("type") not in ("object", "array"), "응답이 JSON이 아닙니다 (스펙은 JSON 스키마를 정의함)"
        return
    errors = [f"{{'/'.join(map(str, e.absolute_path)) or '(root)'}}: {{e.message}}"
              for e in Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(body)]
    assert not errors, "응답 스키마 불일치:\\n" + "\\n".join(errors[:10])


_NOT_JSON = object()


def _is_login(url):
    try:
        return LOGIN_PATH.search(httpx.URL(url).path) is not None
    except httpx.InvalidURL:
        return False


def _json_body(resp):
    if not resp.content or "json" not in resp.headers.get("content-type", ""):
        return _NOT_JSON
    try:
        return resp.json()
    except ValueError:
        return _NOT_JSON
'''


def default_filename(spec: Spec) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", spec.title.lower()).strip("_") or "api"
    return f"test_{slug}.py"


def render_pytest(spec: Spec, cases: list[dict[str, Any]], base_url: str, filename: str | None = None) -> str:
    exported = []
    for c in cases:
        try:
            op = spec.find(c["operation_id"])
            schemas = {
                str(s): to_json_schema(op.response_schema(s))
                for s in c.get("expected_status") or []
                if op.response_schema(s)
            }
        except Exception:
            schemas = {}
        exported.append(
            {
                "id": c["id"],
                "description": c.get("description", ""),
                "method": c["method"],
                "path": c["path"],
                "path_params": c.get("path_params") or {},
                "query": c.get("query") or {},
                "headers": c.get("headers") or {},
                "body": c.get("body"),
                "expected_status": c.get("expected_status") or [],
                "response_schemas": schemas,
                "skip": c.get("skip"),
                "auth": c.get("auth", True),
            }
        )
    cases_json = json.dumps(exported, ensure_ascii=False, indent=2)
    if '"""' in cases_json:  # raw 삼중따옴표 문자열이 깨지지 않게
        cases_json = cases_json.replace('"""', '\\u0022\\u0022\\u0022')
    return TEMPLATE.format(
        title=spec.title,
        source=spec.source,
        base_url=base_url,
        base_url_literal=json.dumps(base_url),
        filename=filename or default_filename(spec),
        cases_json=cases_json,
        login_pattern=repr(LOGIN_PATH.pattern),
        auth_header_names="{" + ", ".join(repr(n) for n in sorted(spec.auth_header_names())) + "}",
    )
