"""MCP 도구 계층: 케이스를 서버가 보관하고 Claude와는 요약·id만 주고받는지."""

import json

import httpx
import pytest
import respx

from api_test_pilot import server

BASE = "http://api.test/v1"


@pytest.fixture
def tools(tmp_path, monkeypatch, v1_path):
    monkeypatch.chdir(tmp_path)  # .api-test-pilot/는 현재 폴더에 생긴다
    server._specs.clear()
    server._cases.clear()
    sid = server.load_spec(v1_path)["spec_id"]
    return sid, tmp_path


def test_generate_returns_lines_not_full_cases(tools):
    sid, root = tools
    out = server.generate_cases(sid)
    assert all(isinstance(line, str) for line in out["cases"])
    assert "getPet:happy GET /pets/7 → 200" in out["cases"]
    assert out["by_kind"]["happy"] >= 1
    saved = json.loads((root / ".api-test-pilot" / "cases" / f"{sid}.json").read_text(encoding="utf-8"))
    assert saved["cases"]["getPet:happy"]["path_params"] == {"petId": 7}  # 전체 내용은 서버 쪽에


@respx.mock
def test_run_by_ids_with_overrides_and_compact_result(tools):
    sid, root = tools
    server.generate_cases(sid)
    route = respx.get(BASE + "/pets/10").mock(return_value=httpx.Response(200, json={"id": 10, "name": "Tom"}))
    respx.get(BASE + "/pets/987654321").mock(return_value=httpx.Response(500, text="boom"))
    out = server.run_cases(sid, BASE, case_ids=["getPet:happy", "getPet:not_found"],
                           overrides={"getPet:happy": {"path_params": {"petId": 10}}})
    assert route.called
    assert out["total"] == 2 and out["passed_cases"] == ["getPet:happy → 200"]
    [failure] = out["failures"]
    assert failure["id"] == "getPet:not_found" and failure["status"] == 500 and "passed" not in failure
    assert "results" not in out and out["results_file"].endswith("last_results.json")
    full = json.loads((root / ".api-test-pilot" / "last_results.json").read_text(encoding="utf-8"))
    assert len(full["results"]) == 2  # 상세 전체는 파일로

    # 고친 값은 보관되어 내보내기에도 쓰인다
    written = server.export_pytest(sid, BASE, case_ids=["getPet:happy"])["written"]
    assert '"petId": 10' in open(written, encoding="utf-8").read()


def test_unknown_ids_and_missing_cases_are_errors(tools):
    sid, _ = tools
    assert "generate_cases" in server.run_cases(sid, BASE)["error"]
    server.generate_cases(sid)
    assert "없는 case_id" in server.run_cases(sid, BASE, case_ids=["nope:happy"])["error"]
    assert "없는 case_id" in server.run_cases(sid, BASE, overrides={"nope:happy": {"query": {}}})["error"]
    assert "바꿀 수 없는" in server.run_cases(sid, BASE, overrides={"getPet:happy": {"method": "DELETE"}})["error"]


def test_cases_survive_server_restart(tools):
    sid, _ = tools
    server.generate_cases(sid)
    server._specs.clear()  # MCP 서버가 재시작된 상황
    server._cases.clear()
    assert server.get_cases(sid, ["getPet:happy"])["cases"][0]["path_params"] == {"petId": 7}


def test_generate_from_source_replaces_load_spec(tmp_path, monkeypatch, v1_path):
    monkeypatch.chdir(tmp_path)
    server._specs.clear()
    server._cases.clear()
    out = server.generate_cases(v1_path)  # load_spec 없이 바로
    assert out["spec"]["title"] and out["spec"]["base_urls"] == [BASE]
    assert out["spec"]["warnings"] == []
    assert out["spec_id"] in server._cases


@respx.mock
def test_run_saves_snapshot_and_exports_in_one_call(tools):
    sid, root = tools
    server.generate_cases(sid)
    respx.route().mock(return_value=httpx.Response(200, json=[]))
    out = server.run_cases(sid, BASE, case_ids=["listPets:happy"], export_pytest=True)
    assert out["snapshot"] and (root / ".api-test-pilot" / "snapshots").is_dir()
    assert out["exported"]["case_count"] == 1
    assert out["report_path"].endswith(".md") and "reports" in out["report_path"]
    assert open(out["exported"]["written"], encoding="utf-8").read().count('"id": "listPets:happy"') == 1
    assert "exported" not in server.run_cases(sid, BASE, case_ids=["listPets:happy"])  # 기본은 내보내지 않음


def test_many_failures_are_summarised_beyond_limit():
    from api_test_pilot.runner import MAX_DETAILED_FAILURES, compact_summary

    results = [{"id": f"op{i}:happy", "passed": False, "status": 500, "expected_status": [200],
                "schema_errors": [], "url": "http://x"} for i in range(MAX_DETAILED_FAILURES + 5)]
    out = compact_summary({"total": len(results), "results": results})
    assert len(out["failures"]) == MAX_DETAILED_FAILURES
    assert out["failures_more"][0] == f"op{MAX_DETAILED_FAILURES}:happy → 500 (기대 200)"
    assert len(out["failures_more"]) == 5


def test_override_none_removes_key(tools):
    sid, _ = tools
    server.generate_cases(sid)
    with respx.mock:
        respx.get(url__startswith=BASE + "/pets").mock(return_value=httpx.Response(200, json=[]))
        server.run_cases(sid, BASE, case_ids=["listPets:happy"], overrides={"listPets:happy": {"query": {"status": None}}})
    assert server.get_cases(sid, ["listPets:happy"])["cases"][0]["query"] == {}
