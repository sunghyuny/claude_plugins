"""남은 개선 4가지: 훅 입구 거르기, 환경변수 참조, format 검증, 동시 실행."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx

from api_test_pilot import server
from api_test_pilot.cases import generate_cases
from api_test_pilot.export import render_pytest
from api_test_pilot.runner import run_cases, validate_body
from api_test_pilot.spec import Spec, SpecError, expand_env

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://api.test/v1"
OK = {"200": {"description": "ok"}}


def _spec(paths, **extra):
    return Spec({"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": paths, **extra})


# --- 1. 훅 입구 (셸에서 먼저 거르기) ---

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="sh 없음")


def _gate(mode, payload, cwd, plugin_root=ROOT):
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)}
    out = subprocess.run(["sh", str(ROOT / "scripts" / "hook_gate.sh"), mode], input=json.dumps(payload).encode(),
                         capture_output=True, cwd=cwd, env=env, timeout=60)
    return out.stdout.decode("utf-8"), out.stderr.decode("utf-8", "replace")


@needs_sh
def test_gate_skips_python_for_non_spec_files(tmp_path):
    # 플러그인 위치를 없는 경로로 준다: uv가 실행되면 오류가 나므로, 조용하면 uv를 띄우지 않은 것
    nowhere = tmp_path / "no-plugin-here"
    event = {"tool_name": "Edit", "cwd": str(tmp_path), "transcript_path": "/x/t.jsonl",
             "tool_input": {"file_path": str(tmp_path / "main.py")}}
    assert _gate("pre", event, tmp_path, nowhere) == ("", "")
    assert _gate("stop", {"cwd": str(tmp_path)}, tmp_path, nowhere) == ("", "")  # 결과 파일 없음 → 건너뜀
    spec_event = {**event, "tool_input": {"file_path": str(tmp_path / "openapi.yaml")}}
    assert _gate("pre", spec_event, tmp_path, nowhere)[1] != ""  # 대조군: 스펙 파일이면 uv까지 간다


@needs_sh
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv 없음")
def test_gate_still_checks_spec_files(tmp_path, v1_path, v2_path):
    spec = tmp_path / "openapi.yaml"
    shutil.copy(v1_path, spec)
    event = {"tool_name": "Edit", "cwd": str(tmp_path), "tool_input": {"file_path": str(spec)}}
    _gate("pre", event, tmp_path)
    shutil.copy(v2_path, spec)
    out, _ = _gate("post", event, tmp_path)
    assert "breaking change" in json.loads(out)["systemMessage"]


# --- 2. 토큰을 환경변수로 참조 ---


def test_expand_env(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    assert expand_env({"Authorization": "Bearer ${API_TOKEN}", "X": "plain"}) == {
        "Authorization": "Bearer s3cret", "X": "plain"}
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(SpecError, match="MISSING_TOKEN"):
        expand_env({"Authorization": "Bearer ${MISSING_TOKEN}"})


@respx.mock
def test_env_token_is_sent_but_never_shown(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    monkeypatch.setenv("TENANT", "acme")
    spec = _spec({"/me": {"get": {"responses": OK}}})
    route = respx.get(BASE + "/me").mock(return_value=httpx.Response(500))
    result = run_cases(spec, generate_cases(spec), BASE,
                       headers={"Authorization": "Bearer ${API_TOKEN}", "X-Tenant": "${TENANT}"})["results"][0]
    sent = route.calls[0].request.headers
    assert sent["authorization"] == "Bearer s3cret" and sent["x-tenant"] == "acme"
    assert "s3cret" not in json.dumps(result) and "acme" not in json.dumps(result)
    assert "'X-Tenant: ${TENANT}'" in result["reproduce"]  # 참조 그대로 보여 준다


def test_missing_env_stops_run_before_any_request(tmp_path, monkeypatch, v1_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    server._specs.clear()
    server._cases.clear()
    sid = server.generate_cases(v1_path)["spec_id"]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200))
        out = server.run_cases(sid, BASE, headers={"Authorization": "Bearer ${NOPE_TOKEN}"})
    assert "NOPE_TOKEN" in out["error"] and not route.called


@respx.mock
def test_protected_spec_url_with_spec_headers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPEC_TOKEN", "t0k")
    server._specs.clear()
    raw = {"openapi": "3.0.0", "info": {"title": "Private", "version": "1"}, "paths": {"/x": {"get": {"responses": OK}}}}
    respx.get("http://private.test/openapi.json").mock(side_effect=lambda req: httpx.Response(
        200, json=raw) if req.headers.get("authorization") == "Bearer t0k" else httpx.Response(401))
    assert "error" in server.load_spec("http://private.test/openapi.json")
    out = server.generate_cases("http://private.test/openapi.json", spec_headers={"Authorization": "Bearer ${SPEC_TOKEN}"})
    assert out["spec"]["title"] == "Private"


def test_exported_pytest_reads_api_headers(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    monkeypatch.setenv("API_HEADERS", json.dumps({"Authorization": "Bearer ${API_TOKEN}", "X-Tenant": "acme"}))
    spec = _spec({"/me": {"get": {"responses": OK}}},
                 components={"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}})
    namespace = {}
    exec(compile(render_pytest(spec, [], BASE), "t.py", "exec"), namespace)
    assert namespace["EXTRA_HEADERS"] == {"Authorization": "Bearer s3cret", "X-Tenant": "acme"}
    assert namespace["AUTH_HEADER_NAMES"] == {"authorization", "x-api-key"}


# --- 3. format 검증 ---


def test_format_is_validated():
    schema = {"type": "object", "properties": {
        "at": {"type": "string", "format": "date-time"}, "mail": {"type": "string", "format": "email"},
        "id": {"type": "string", "format": "uuid"}, "home": {"type": "string", "format": "uri"},
        "n": {"type": "integer", "format": "int64"}}}
    good = {"at": "2024-01-01T00:00:00Z", "mail": "a@b.io", "id": "00000000-0000-4000-8000-000000000000",
            "home": "https://x.io", "n": 1}
    assert validate_body(schema, good) == []
    bad = {"at": "yesterday", "mail": "nope", "id": "123", "home": "not a uri", "n": 1}
    errors = validate_body(schema, bad)
    assert {e.split(":")[0] for e in errors} == {"at", "mail", "id", "home"}  # int64 같은 모르는 format은 통과


def test_exported_pytest_checks_format_too(v1):
    assert "format_checker=Draft202012Validator.FORMAT_CHECKER" in render_pytest(v1, [], BASE)


# --- 4. 동시 실행 ---


def _slow_spec():
    paths = {f"/r{i}": {"get": {"responses": OK}} for i in range(8)}
    paths["/items"] = {"post": {"responses": {"201": {"description": "ok"}}}}
    return _spec(paths)


def test_concurrency_speeds_up_reads_and_keeps_order():
    spec = _slow_spec()
    cases = [c for c in generate_cases(spec) if c["method"] == "GET"]

    def slow(_req):
        time.sleep(0.15)
        return httpx.Response(200)

    with respx.mock:
        respx.route().mock(side_effect=slow)
        started = time.perf_counter()
        sequential = run_cases(spec, cases, BASE)
        seq_time = time.perf_counter() - started
        started = time.perf_counter()
        parallel = run_cases(spec, cases, BASE, concurrency=8)
        par_time = time.perf_counter() - started
    assert [r["id"] for r in parallel["results"]] == [c["id"] for c in cases]  # 입력 순서 그대로
    assert parallel["passed"] == sequential["passed"] == len(cases)
    assert par_time < seq_time / 3


def test_writes_are_barriers():
    spec = _slow_spec()
    all_cases = generate_cases(spec, include_writes=True)
    post = next(c for c in all_cases if c["method"] == "POST")
    gets = [c for c in all_cases if c["method"] == "GET"]
    cases = gets[:3] + [post] + gets[3:6]
    log, lock = [], threading.Lock()

    def record(req):
        with lock:
            log.append(("start", req.method))
        time.sleep(0.05)
        with lock:
            log.append(("end", req.method))
        return httpx.Response(201 if req.method == "POST" else 200)

    with respx.mock:
        respx.route().mock(side_effect=record)
        out = run_cases(spec, cases, BASE, concurrency=8)
    assert out["passed"] == len(cases)
    post_start = log.index(("start", "POST"))
    post_end = log.index(("end", "POST"))
    assert all(e[0] == "end" or e[1] == "GET" for e in log[:post_start])
    assert sum(1 for e in log[:post_start] if e == ("end", "GET")) == 3  # 앞의 읽기 3개가 끝난 뒤 POST 시작
    assert all(e[1] == "GET" for e in log[post_end + 1:]) and len(log[post_end + 1:]) == 6  # 뒤의 읽기는 POST 끝난 뒤
