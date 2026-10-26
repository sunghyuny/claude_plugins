import httpx
import respx

from api_test_pilot.cases import generate_cases
from api_test_pilot.runner import build_url, run_cases, validate_body

BASE = "http://api.test/v1"
PET = {"id": 7, "name": "Tom", "tag": None, "status": "available"}


def test_build_url_quotes_params():
    assert build_url(BASE + "/", "/pets/{petId}", {"petId": "a b/c"}) == BASE + "/pets/a%20b%2Fc"


def test_validate_body_nullable_ok_and_errors():
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "tag": {"type": "string", "nullable": True}}}
    assert validate_body(schema, {"id": 1, "tag": None}) == []
    errors = validate_body(schema, {"id": "x"})
    assert errors and errors[0].startswith("id:")


@respx.mock
def test_run_cases_pass_and_fail(v1):
    respx.get(f"{BASE}/pets/7").mock(return_value=httpx.Response(200, json=PET))
    respx.get(f"{BASE}/pets/987654321").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/pets/abc").mock(return_value=httpx.Response(200, json=PET))  # 서버 버그: 검증 안 함

    summary = run_cases(v1, generate_cases(v1, operation_id="getPet"), BASE)
    results = {r["id"]: r for r in summary["results"]}
    assert summary["total"] == 3 and summary["passed"] == 2 and summary["failed"] == 1
    assert results["getPet:happy"]["passed"]
    assert results["getPet:not_found"]["passed"]
    assert not results["getPet:invalid_type"]["passed"]
    assert results["getPet:invalid_type"]["status"] == 200


@respx.mock
def test_schema_mismatch_fails(v1):
    respx.get(f"{BASE}/pets/7").mock(return_value=httpx.Response(200, json={"name": "Tom"}))  # id 누락
    cases = [c for c in generate_cases(v1, operation_id="getPet") if c["kind"] == "happy"]
    result = run_cases(v1, cases, BASE)["results"][0]
    assert not result["passed"]
    assert any("'id' is a required property" in e for e in result["schema_errors"])
    assert result["response_preview"] == {"name": "Tom"}


@respx.mock
def test_query_and_body_are_sent(v1):
    route_list = respx.get(f"{BASE}/pets", params={"status": "available"}).mock(return_value=httpx.Response(200, json=[PET]))
    route_create = respx.post(f"{BASE}/pets").mock(return_value=httpx.Response(201, json=PET))
    cases = [c for c in generate_cases(v1, tag="pets", include_writes=True) if c["kind"] == "happy" and c["operation_id"] != "getPet"]
    summary = run_cases(v1, cases, BASE, headers={"Authorization": "Bearer t"})
    assert summary["passed"] == 2
    assert route_create.calls.last.request.headers["Authorization"] == "Bearer t"
    assert b'"name":"Tom"' in route_create.calls.last.request.content.replace(b" ", b"")
    assert route_list.called


def test_skipped_and_connection_error(v1):
    cases = generate_cases(v1, operation_id="uploadPhoto", include_writes=True)
    cases += [c for c in generate_cases(v1, operation_id="getPet") if c["kind"] == "happy"]
    with respx.mock:
        respx.get(f"{BASE}/pets/7").mock(side_effect=httpx.ConnectError("refused"))
        summary = run_cases(v1, cases, BASE)
    assert summary["skipped"] == 1 and summary["failed"] == 1 and summary["passed"] == 0
    assert "ConnectError" in summary["results"][1]["error"]
