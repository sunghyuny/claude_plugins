import ast
import json

from api_test_pilot import store
from api_test_pilot.cases import generate_cases
from api_test_pilot.export import default_filename, render_pytest
from api_test_pilot.spec import Spec


def test_render_pytest_is_valid_python(v1):
    cases = generate_cases(v1, include_writes=True)
    source = render_pytest(v1, cases, "http://api.test/v1")
    ast.parse(source)
    assert 'os.environ.get("API_BASE_URL", "http://api.test/v1")' in source
    assert "getPet:not_found" in source
    assert default_filename(v1) == "test_mini_petstore.py"


def test_render_pytest_survives_triple_quotes(v1):
    cases = generate_cases(v1, operation_id="createPet", include_writes=True)
    cases[0]["body"] = {"name": 'a """ b'}
    namespace: dict = {}
    source = render_pytest(v1, cases, "http://x")
    cases_literal = source.split('json.loads(r"""')[1].split('""")')[0]
    assert json.loads(cases_literal)[0]["body"]["name"] == 'a """ b'
    ast.parse(source)
    del namespace


def test_snapshot_roundtrip(tmp_path, v1):
    path = store.save_snapshot(v1.source, v1.raw, tmp_path)
    assert path.parent == tmp_path / ".api-test-pilot" / "snapshots"
    snap = store.load_snapshot(v1.source, tmp_path)
    assert Spec(snap["spec"]).title == "Mini Petstore"
    assert store.load_snapshot("other.yaml", tmp_path) is None


def test_save_last_run(tmp_path):
    summary = {"spec": "S", "total": 3, "passed": 1, "failed": 1, "skipped": 1,
               "results": [{"id": "a", "passed": True}, {"id": "b", "passed": False}, {"id": "c", "passed": None}]}
    data = json.loads(store.save_last_run(summary, tmp_path).read_text(encoding="utf-8"))
    assert data["failed_ids"] == ["b"] and data["reported"] is False


def _load_generated(tmp_path, spec, cases):
    import importlib.util

    path = tmp_path / "test_generated.py"
    path.write_text(render_pytest(spec, cases, "http://api.test/v1"), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("test_generated", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_generated_test_matches_runner_judgement(tmp_path, v1):
    import httpx
    import pytest

    cases = [c for c in generate_cases(v1, operation_id="getPet") if c["kind"] == "happy"]
    module = _load_generated(tmp_path, v1, cases)
    case = module.CASES[0]

    def client_returning(response):
        return httpx.Client(transport=httpx.MockTransport(lambda request: response))

    module.test_api(client_returning(httpx.Response(200, json={"id": 7, "name": "Tom"})), case)
    with pytest.raises(AssertionError, match="스키마 불일치"):
        module.test_api(client_returning(httpx.Response(200, json={"name": "Tom"})), case)
    # content-type만 JSON이고 본문이 평문 → 스펙이 객체를 약속했으므로 실패, 단 JSONDecodeError로 터지지 않는다
    plain = httpx.Response(200, text="ok", headers={"content-type": "application/json"})
    with pytest.raises(AssertionError, match="JSON이 아닙니다"):
        module.test_api(client_returning(plain), case)
    with pytest.raises(AssertionError, match="기대"):
        module.test_api(client_returning(httpx.Response(500)), case)
