import httpx
import pytest
import respx

from api_test_pilot.spec import Spec, SpecError, parse_text, to_json_schema, validate_structure


def test_load_file_and_summary(v1):
    info = v1.summary_dict()
    assert info["title"] == "Mini Petstore"
    assert info["endpoint_count"] == 5
    assert info["base_urls"] == ["http://api.test/v1"]


def test_refs_are_resolved(v1):
    op = v1.find("getPet")
    schema = op.responses["200"]
    assert "$ref" not in str(schema)
    assert schema["properties"]["id"]["type"] == "integer"


def test_parameters_and_body(v1):
    op = v1.find("listPets")
    params = {p.name: p for p in op.parameters}
    assert params["status"].required and params["status"].location == "query"
    assert not params["limit"].required
    body = v1.find("createPet").request_body
    assert body["required"] and body["json"]
    assert v1.find("uploadPhoto").request_body["json"] is False


@respx.mock
def test_load_url_without_servers_uses_spec_host():
    # FastAPI 기본 스펙처럼 servers가 없으면 스펙을 받아온 서버가 base URL
    spec_text = '{"openapi": "3.1.0", "info": {"title": "FastAPI", "version": "0.1.0"}, "paths": {}}'
    respx.get("http://localhost:8000/openapi.json").mock(return_value=httpx.Response(200, text=spec_text))
    spec = Spec.load("http://localhost:8000/openapi.json")
    assert spec.base_urls() == ["http://localhost:8000"]


def test_swagger2_is_normalized():
    raw = parse_text(
        """
swagger: "2.0"
info: {title: Legacy, version: "1"}
host: legacy.test
basePath: /api
schemes: [http]
paths:
  /items/{id}:
    get:
      operationId: getItem
      parameters:
        - {name: id, in: path, required: true, type: integer}
      responses:
        "200": {description: OK, schema: {type: object, properties: {id: {type: integer}}}}
    put:
      parameters:
        - {name: id, in: path, required: true, type: integer}
        - {name: item, in: body, required: true, schema: {type: object}}
      responses:
        "200": {description: OK}
"""
    )
    spec = Spec(raw, "legacy")
    assert spec.base_urls() == ["http://legacy.test/api"]
    get = spec.find("getItem")
    assert get.parameters[0].schema["type"] == "integer"
    assert get.responses["200"]["properties"]["id"]["type"] == "integer"
    put = spec.find("put_items_id")  # operationId가 없으면 자동 생성
    assert put.request_body["required"]


def test_validate_structure_reports_problems():
    errors = validate_structure(
        {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {"nope": {}, "/a": {"get": {"responses": {"200": {"$ref": "#/components/x"}}}}}}
    )
    joined = "\n".join(errors)
    assert "info.version" in joined
    assert "'/'로 시작" in joined
    assert "#/components/x" in joined


def test_validate_structure_ok(v1):
    assert validate_structure(v1.raw) == []


def test_missing_file_raises():
    with pytest.raises(SpecError):
        Spec.load("does/not/exist.yaml")


def test_to_json_schema_nullable_and_exclusive():
    s = to_json_schema({"type": "integer", "nullable": True, "minimum": 0, "exclusiveMinimum": True,
                        "properties": {"example": {"type": "string"}}})
    assert s["type"] == ["integer", "null"]
    assert s["exclusiveMinimum"] == 0 and "minimum" not in s
    assert "example" in s["properties"]  # 이름이 example인 속성은 지우지 않는다
