"""실서버 E2E에서 드러난 경계 상황 회귀 테스트."""

import io
import json
import time

import httpx
import on_spec_edit
import pytest
import respx

from api_test_pilot.cases import generate_cases, sample_value
from api_test_pilot.diff import diff_specs
from api_test_pilot.export import render_pytest
from api_test_pilot.runner import build_url, call_endpoint, run_cases
from api_test_pilot.spec import Spec, SpecError, unsupported_refs, validate_structure

BASE = "http://api.test/v1"
OK = {"200": {"description": "ok"}}


def _spec(paths):
    return Spec({"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": paths})


def _by_id(cases):
    return {c["id"]: c for c in cases}


def _get_with(params, responses=None):
    return {"get": {"parameters": params, "responses": responses or OK}}


def _json_response(schema):
    return {"200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}


# --- 케이스 생성: 예시값과 제약 ---


def test_sample_value_respects_constraints():
    assert sample_value({"type": "string", "minLength": 5, "pattern": "^[A-Z]+$"}) == "STRING"
    assert sample_value({"type": "string", "maxLength": 3}) == "str"
    assert sample_value({"type": "string", "minLength": 8}) == "stringst"
    assert sample_value({"type": "integer", "minimum": -5, "maximum": 0}) == 0
    assert sample_value({"type": "integer", "exclusiveMinimum": 10}) == 11  # 3.1 숫자 형식
    assert sample_value({"type": "integer", "minimum": 10, "exclusiveMinimum": True}) == 11  # 3.0 bool 형식
    assert sample_value({"type": "integer", "minimum": 3, "multipleOf": 5}) == 5
    assert sample_value({"type": "string", "examples": ["SKU-001"]}) == "SKU-001"


def test_parameter_and_body_level_examples_are_used():
    spec = _spec({
        "/lookup": _get_with([{"name": "sku", "in": "query", "required": True, "schema": {"type": "string"},
                               "examples": {"a": {"value": "SKU-001"}}}]),
        "/items": {"post": {"requestBody": {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "properties": {"code": {"type": "string"}}},
            "example": {"code": "ABCDE"}}}}, "responses": {"201": {"description": "ok"}}}},
    })
    cases = _by_id(generate_cases(spec, include_writes=True))
    assert cases["get_lookup:happy"]["query"] == {"sku": "SKU-001"}
    assert cases["post_items:happy"]["body"] == {"code": "ABCDE"}


def test_not_found_value_respects_format_and_bounds():
    nf = {"404": {"description": "nf"}, **OK}

    def path_param(schema):
        return [{"name": "id", "in": "path", "required": True, "schema": schema}]

    spec = _spec({
        "/orders/{id}": _get_with(path_param({"type": "string", "format": "uuid"}), nf),
        "/slots/{id}": _get_with(path_param({"type": "integer", "maximum": 100}), nf),
        "/kinds/{id}": _get_with(path_param({"type": "string", "enum": ["a", "b"]}), nf),
    })
    cases = _by_id(generate_cases(spec))
    assert cases["get_orders_id:not_found"]["path_params"]["id"] == "ffffffff-ffff-4fff-bfff-ffffffffffff"
    # 범위가 좁거나(maximum) enum이면 '확실히 없는 값'을 만들 수 없어 생략한다
    # (범위 밖 값은 422, 범위 안 값은 실제로 있을 수 있음)
    assert "get_slots_id:not_found" not in cases
    assert "get_kinds_id:not_found" not in cases


# --- 실행 결과 크기 ---


@respx.mock
def test_large_failure_preview_is_truncated():
    spec = _spec({"/pets": {"get": {"responses": _json_response(
        {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}})}}})
    respx.get(BASE + "/pets").mock(return_value=httpx.Response(200, json=[{"id": "bad", "pad": "x" * 50}] * 2000))
    result = run_cases(spec, generate_cases(spec), BASE)["results"][0]
    assert result["passed"] is False
    assert isinstance(result["response_preview"], str) and len(result["response_preview"]) < 600


@respx.mock
def test_call_endpoint_truncates_large_json_only():
    pet = {"id": 7, "name": "Tom"}
    respx.get(BASE + "/pets").mock(return_value=httpx.Response(200, json=[pet] * 2000))
    respx.get(BASE + "/pets/7").mock(return_value=httpx.Response(200, json=pet))
    big = call_endpoint("GET", BASE + "/pets")["body"]
    assert isinstance(big, str) and len(big) < 4100
    assert call_endpoint("GET", BASE + "/pets/7")["body"] == pet  # 작은 응답은 구조 그대로


# --- diff ---


def _path_param_op(name):
    return _get_with([{"name": name, "in": "path", "required": True, "schema": {"type": "integer"}}])


def test_path_param_rename_is_not_breaking():
    result = diff_specs(_spec({"/pets/{id}": _path_param_op("id")}), _spec({"/pets/{petId}": _path_param_op("petId")}))
    assert result["breaking"] == []
    assert [c["type"] for c in result["non_breaking"]] == ["path_param_renamed"]


def test_changes_inside_one_of_and_all_of_are_detected():
    def one_of(props):
        return _spec({"/x": {"get": {"responses": _json_response({"oneOf": [{"type": "object", "properties": props}]})}}})

    removed = diff_specs(one_of({"a": {"type": "string"}, "b": {"type": "string"}}), one_of({"a": {"type": "string"}}))
    assert [(c["type"], c["location"]) for c in removed["breaking"]] == [
        ("response_field_removed", "GET /x response 200<oneOf[0]>.b")]

    def all_of(extra):
        schema = {"allOf": [{"type": "object", "properties": {"id": {"type": "integer"}}}, {"properties": extra}]}
        return _spec({"/y": {"get": {"responses": _json_response(schema)}}})

    removed = diff_specs(all_of({"name": {"type": "string"}}), all_of({}))
    assert [(c["type"], c["location"]) for c in removed["breaking"]] == [
        ("response_field_removed", "GET /y response 200.name")]


def test_request_body_removed_is_breaking():
    body = {"content": {"application/json": {"schema": {"type": "object"}}}}
    result = diff_specs(_spec({"/x": {"post": {"requestBody": body, "responses": OK}}}),
                        _spec({"/x": {"post": {"responses": OK}}}))
    assert [c["type"] for c in result["breaking"]] == ["body_removed"]


def test_type_widening_depends_on_direction():
    def query(t):
        return _spec({"/x": _get_with([{"name": "n", "in": "query", "schema": {"type": t}}])})

    widened = diff_specs(query("integer"), query("number"))
    assert widened["breaking"] == []
    assert [c["type"] for c in widened["non_breaking"]] == ["type_compatible_change"]

    def response(t):
        return _spec({"/x": {"get": {"responses": _json_response({"type": "object", "properties": {"n": {"type": t}}})}}})

    assert [c["type"] for c in diff_specs(response("integer"), response("number"))["breaking"]] == ["type_changed"]


# --- 스펙 로드 ---


def test_non_utf8_file_raises_spec_error(tmp_path):
    path = tmp_path / "cp949.yaml"
    path.write_bytes("openapi: 3.0.0\ninfo: {title: 한글, version: '1'}\npaths: {}\n".encode("cp949"))
    with pytest.raises(SpecError, match="UTF-8"):
        Spec.load(str(path))


def test_utf8_bom_json_loads(tmp_path):
    path = tmp_path / "bom.json"
    path.write_text('﻿{"openapi": "3.0.0", "info": {"title": "한글", "version": "1"}, "paths": {}}', encoding="utf-8")
    assert Spec.load(str(path)).title == "한글"


def test_external_refs_are_reported_but_not_structure_errors():
    raw = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
           "paths": {"/x": {"get": {"responses": {"200": {"$ref": "./common.yaml#/responses/Ok"}}}}}}
    assert validate_structure(raw) == []  # 유효한 스펙이므로 훅이 block하면 안 된다
    assert any("./common.yaml#/responses/Ok" in w for w in unsupported_refs(raw))


# --- export ---


def test_exported_url_is_encoded_like_runner(v1):
    namespace = {}
    exec(compile(render_pytest(v1, [], "http://x"), "t.py", "exec"), namespace)
    case = {"path": "/pets/{petId}", "path_params": {"petId": "a b/c"}}
    assert namespace["_url"](case) == build_url("http://x", case["path"], case["path_params"])


# --- 편집 훅 ---


def _hook(monkeypatch, capsys, root, path, mode):
    event = {"cwd": str(root), "tool_name": "Edit", "tool_input": {"file_path": str(path)}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr("sys.argv", ["hook", mode])
    on_spec_edit.main()
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def test_large_non_spec_file_is_not_parsed(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"packages": {f"node_modules/p{i}": {"version": "1.0.0"} for i in range(50000)}}),
                    encoding="utf-8")

    def fail(*_):
        raise AssertionError("스펙이 아닌 파일을 파싱하면 안 된다")

    monkeypatch.setattr(on_spec_edit, "parse_text", fail)
    started = time.perf_counter()
    assert _hook(monkeypatch, capsys, tmp_path, lock, "pre") is None
    assert _hook(monkeypatch, capsys, tmp_path, lock, "post") is None
    assert time.perf_counter() - started < 2


@pytest.fixture
def edited_spec(tmp_path, v1_path, monkeypatch, capsys):
    spec = tmp_path / "openapi.yaml"
    spec.write_text(open(v1_path, encoding="utf-8").read(), encoding="utf-8")
    _hook(monkeypatch, capsys, tmp_path, spec, "pre")
    return tmp_path, spec


def test_spec_broken_beyond_recognition_still_blocks(edited_spec, monkeypatch, capsys):
    root, spec = edited_spec
    spec.write_text("info: [unclosed\n", encoding="utf-8")  # 스펙 표식까지 사라진 상태
    out = _hook(monkeypatch, capsys, root, spec, "post")
    assert out["decision"] == "block" and "파싱 실패" in out["reason"]


def test_spec_saved_as_cp949_blocks(edited_spec, monkeypatch, capsys):
    root, spec = edited_spec
    spec.write_bytes(("# 한글 주석\n" + spec.read_text(encoding="utf-8")).encode("cp949"))
    out = _hook(monkeypatch, capsys, root, spec, "post")
    assert out["decision"] == "block" and "UTF-8" in out["reason"]


def test_same_breaking_change_is_warned_only_once(tmp_path, v1_path, v2_path, monkeypatch, capsys):
    from api_test_pilot import store

    spec = tmp_path / "openapi.yaml"
    v1_text, v2_text = (open(p, encoding="utf-8").read() for p in (v1_path, v2_path))
    spec.write_text(v1_text, encoding="utf-8")
    store.save_snapshot(str(spec.resolve()), Spec.load(str(spec)).raw, tmp_path)  # /api-test 이후 상황

    def edit(text):
        spec.write_text(text, encoding="utf-8")
        return _hook(monkeypatch, capsys, tmp_path, spec, "post")

    first = edit(v2_text)
    assert "9건 새로 감지" in first["systemMessage"]
    assert edit(v2_text.replace("version: 2.0.0", "version: 2.0.1")) is None  # 같은 breaking → 조용히
    # v1부터 있던 사진 업로드 경로를 없앰 → 그 1건만 새로 알리고 기존 9건은 생략
    again = edit(v2_text.replace("  /pets/{petId}/photo:", "  /pets/{petId}/picture:"))
    assert "1건 새로 감지" in again["systemMessage"] and "이전에 알린 9건은 생략" in again["systemMessage"]
    assert "photo" in again["hookSpecificOutput"]["additionalContext"]
    assert "/owners" not in again["hookSpecificOutput"]["additionalContext"]
    assert edit(v1_text) is None  # 모두 되돌림 → 기록도 비워짐
    assert "9건 새로 감지" in edit(v2_text)["systemMessage"]  # 다시 생기면 다시 알린다


# --- 인증 ---


def _auth_spec():
    return Spec({
        "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"},
                                           "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
        "paths": {
            "/me": {"get": {"security": [{"bearer": []}], "responses": {**OK, "401": {"description": "unauth"}}}},
            "/public": {"get": {"responses": OK}},
        },
    })


def test_unauthorized_case_only_for_secured_operations():
    spec = _auth_spec()
    assert spec.auth_header_names() == {"authorization", "x-api-key"}
    cases = _by_id(generate_cases(spec))
    assert cases["get_me:unauthorized"]["expected_status"] == [401, 403]
    assert cases["get_me:unauthorized"]["auth"] is False
    assert "get_public:unauthorized" not in cases


@respx.mock
def test_unauthorized_case_strips_only_auth_headers():
    spec = _auth_spec()
    route = respx.get(BASE + "/me").mock(side_effect=lambda req: httpx.Response(
        200 if req.headers.get("Authorization") == "Bearer good" else 401))
    cases = [c for c in generate_cases(spec) if c["path"] == "/me"]
    headers = {"Authorization": "Bearer good", "X-API-Key": "k", "X-Tenant": "t1"}
    results = _by_id(run_cases(spec, cases, BASE, headers=headers)["results"])
    assert results["get_me:happy"]["passed"] and results["get_me:unauthorized"]["passed"]
    sent = route.calls[-1].request.headers  # unauthorized 케이스가 보낸 요청
    assert "authorization" not in sent and "x-api-key" not in sent and sent["x-tenant"] == "t1"


@respx.mock
def test_auth_failure_is_labelled():
    spec = _auth_spec()
    respx.get(BASE + "/me").mock(return_value=httpx.Response(401, json={"detail": "nope"}))
    happy = [c for c in generate_cases(spec) if c["id"] == "get_me:happy"]
    missing = run_cases(spec, happy, BASE)
    assert missing["auth_issues"] == 1 and "headers로 토큰" in missing["results"][0]["auth_issue"]
    rejected = run_cases(spec, happy, BASE, headers={"Authorization": "Bearer expired"})
    assert "거부" in rejected["results"][0]["auth_issue"]


# --- 잘못된 케이스가 전체 실행을 멈추지 않음 ---


@respx.mock
def test_one_bad_case_does_not_abort_the_run():
    spec = _spec({"/x": {"get": {"responses": OK}}})
    respx.get(BASE + "/x").mock(return_value=httpx.Response(200))
    good = generate_cases(spec)[0]
    bad = [
        {**good, "id": "korean-header", "headers": {"X-User": "우리"}},
        {**good, "id": "unserializable-body", "body": {1, 2}},
        {k: v for k, v in good.items() if k != "path"},  # 필수 키 누락
    ]
    out = run_cases(spec, [*bad, good], BASE)
    assert out["total"] == 4 and out["passed"] == 1 and out["failed"] == 3
    errors = [r["error"] for r in out["results"][:3]]
    assert "ASCII" in errors[0] and "JSON" in errors[1] and "KeyError" in errors[2]


# --- 로그인 페이지 리다이렉트 (세션 기반 인증) ---


def _session_spec():
    return Spec({
        "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
        "components": {"securitySchemes": {"session": {"type": "apiKey", "in": "cookie", "name": "SESSION"}}},
        "security": [{"session": []}],
        "paths": {"/profile": {"get": {"responses": OK}}, "/authors": {"get": {"responses": OK}}},
    })


def _session_server(request):
    """세션 쿠키가 없으면 /login으로 302. /authors는 auth가 들어간 이름이지만 로그인과 무관한 리다이렉트."""
    if request.url.path == "/v1/authors":
        return httpx.Response(302, headers={"Location": BASE + "/authors/list"})
    if request.url.path in ("/v1/login", "/v1/authors/list"):
        return httpx.Response(200, text="<html>page</html>")
    if request.headers.get("cookie") == "SESSION=valid":
        return httpx.Response(200)
    return httpx.Response(302, headers={"Location": "/v1/login?next=/profile"})


@respx.mock
def test_login_redirect_counts_as_blocked_only_without_auth():
    spec = _session_spec()
    respx.route().mock(side_effect=_session_server)
    cases = [c for c in generate_cases(spec) if c["path"] == "/profile"]

    anonymous = _by_id(run_cases(spec, cases, BASE)["results"])
    assert anonymous["get_profile:unauthorized"]["passed"]  # 인증 안 됨 → 로그인 페이지 = 정상 차단
    assert anonymous["get_profile:unauthorized"]["redirected_to"].startswith("/v1/login")
    assert anonymous["get_profile:happy"]["passed"] is False  # 로그인 페이지의 200을 통과로 보면 안 된다
    assert "로그인 페이지" in anonymous["get_profile:happy"]["auth_issue"]

    logged_in = _by_id(run_cases(spec, cases, BASE, headers={"Cookie": "SESSION=valid"})["results"])
    assert logged_in["get_profile:happy"]["passed"]  # 인증됨 → 엔드포인트 응답
    assert logged_in["get_profile:unauthorized"]["passed"]  # 쿠키는 빠지고 로그인 페이지로


@respx.mock
def test_non_login_redirect_is_not_mistaken_for_login():
    spec = _session_spec()
    respx.route().mock(side_effect=_session_server)
    results = _by_id(run_cases(spec, [c for c in generate_cases(spec) if c["path"] == "/authors"], BASE)["results"])
    assert results["get_authors:happy"]["passed"] and "redirected_to" not in results["get_authors:happy"]
    assert results["get_authors:unauthorized"]["passed"] is False  # 막지 않는 엔드포인트는 실제 문제
    assert results["get_authors:unauthorized"]["location"] == BASE + "/authors/list"  # 어디로 보냈는지 남김


# --- 결과 URL과 재현 명령 ---


@respx.mock
def test_result_url_includes_query_and_masks_secrets():
    spec = Spec({
        "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
        "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "query", "name": "api_key"}}},
        "paths": {"/search": _get_with([{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}],
                                       {**OK, "400": {"description": "bad"}})},
    })
    respx.get(BASE + "/search").mock(return_value=httpx.Response(500, text="boom"))
    case = {**generate_cases(spec)[0], "query": {"q": "a b", "api_key": "secret"}}
    result = run_cases(spec, [case], BASE, headers={"Authorization": "Bearer secret", "X-Trace": "1"})["results"][0]
    assert result["url"] == BASE + "/search?q=a+b&api_key=***"
    assert "secret" not in result["reproduce"]
    assert "'Authorization: Bearer ***'" in result["reproduce"] and "'X-Trace: 1'" in result["reproduce"]
    assert result["reproduce"].startswith("curl -X GET '" + BASE + "/search?q=a+b&api_key=***'")


@respx.mock
def test_reproduce_includes_json_body():
    spec = _spec({"/items": {"post": {"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "properties": {"name": {"type": "string", "example": "it's"}}}}}},
        "responses": {"201": {"description": "ok"}}}}})
    respx.post(BASE + "/items").mock(return_value=httpx.Response(500))
    result = run_cases(spec, generate_cases(spec, include_writes=True)[:1], BASE)["results"][0]
    assert "-H 'Content-Type: application/json'" in result["reproduce"]
    assert "-d '{\"name\": \"it'\"'\"'s\"}'" in result["reproduce"]  # 작은따옴표도 셸에서 안전하게


def test_exported_pytest_accepts_login_redirect_for_unauthorized(tmp_path):
    spec = _session_spec()
    namespace = {}
    exec(compile(render_pytest(spec, generate_cases(spec), BASE), "t.py", "exec"), namespace)
    assert namespace["_is_login"]("/v1/login?next=/profile") and not namespace["_is_login"](BASE + "/authors/list")
    exported = json.loads(render_pytest(spec, generate_cases(spec), BASE).split('json.loads(r"""')[1].split('""")')[0])
    assert {c["id"]: c["auth"] for c in exported}["get_profile:unauthorized"] is False
