"""api-test-pilot MCP 서버. 도구 정의만 두고 실제 로직은 각 모듈에 맡긴다."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import cases as cases_mod
from . import diff as diff_mod
from . import export, runner, store
from .spec import Spec, SpecError, unsupported_refs, validate_structure

mcp = MCPServer(
    "api-test-pilot",
    instructions=(
        "OpenAPI 스펙 기반 API 테스트 도구. 기본 순서: generate_cases(스펙 URL/경로) → run_cases. "
        "spec 인자에는 spec_id나 스펙 URL/파일 경로를 넣는다. "
        "run_cases는 스냅샷을 자동 저장하고, export_pytest=true면 pytest 파일도 만든다. "
        "케이스는 서버가 보관하므로 run_cases/export_pytest에 케이스 내용을 다시 보내지 말고 case_ids만 쓴다 "
        "(생략하면 전체). 값을 바꿀 때는 overrides에 바꿀 부분만 넣는다. "
        "POST/PUT/PATCH/DELETE 케이스(writes=true)는 실제 데이터를 바꾸므로 사용자 확인 후에만 실행한다."
    ),
)

MAX_LISTED_CASES = 100
SPEC_ID = re.compile(r"[0-9a-f]{8}")  # Spec.id 형식 (source의 sha1 앞 8자리)

_specs: dict[str, Spec] = {}
_cases: dict[str, dict[str, dict[str, Any]]] = {}  # spec_id -> {case_id: case}


def _get(spec: str) -> Spec:
    """spec_id면 캐시에서, 아니면 URL/경로로 보고 새로 로드한다.
    MCP 서버가 재시작돼 캐시가 비었으면 저장해 둔 케이스 파일의 source로 다시 읽는다."""
    if spec in _specs:
        return _specs[spec]
    saved = store.load_cases(spec) if SPEC_ID.fullmatch(spec) else None
    loaded = Spec.load(saved["source"] if saved else spec)
    _specs[loaded.id] = loaded
    return loaded


def _error(e: Exception) -> dict[str, Any]:
    return {"error": str(e)}


def _stored(s: Spec) -> dict[str, dict[str, Any]]:
    if s.id not in _cases:
        saved = store.load_cases(s.id)
        _cases[s.id] = saved["cases"] if saved else {}
    return _cases[s.id]


def _persist(s: Spec) -> None:
    try:
        store.save_cases(s.id, s.source, _cases[s.id])
    except OSError:
        pass  # 디스크에 못 써도 메모리에는 남아 있다


def _select(s: Spec, case_ids: list[str] | None) -> list[dict[str, Any]]:
    stored = _stored(s)
    if not stored:
        raise SpecError("저장된 케이스가 없습니다. 먼저 generate_cases를 실행하세요.")
    if case_ids is None:
        return list(stored.values())
    unknown = [cid for cid in case_ids if cid not in stored]
    if unknown:
        raise SpecError(f"없는 case_id: {unknown[:10]} (generate_cases 결과의 id를 쓰세요)")
    return [stored[cid] for cid in case_ids]


@mcp.tool()
def load_spec(source: str) -> dict[str, Any]:
    """OpenAPI/Swagger 스펙을 URL이나 파일 경로에서 로드한다.

    FastAPI 서버라면 http://localhost:8000/openapi.json 처럼 넘기면 된다.
    반환: spec_id, title, version, endpoint_count, base_urls(테스트 대상 URL 후보), 구조 경고.
    """
    try:
        spec = Spec.load(source)
    except SpecError as e:
        return _error(e)
    _specs[spec.id] = spec
    return {**spec.summary_dict(), "warnings": validate_structure(spec.raw) + unsupported_refs(spec.raw)}


@mcp.tool()
def list_endpoints(spec: str, tag: str | None = None) -> dict[str, Any]:
    """스펙의 엔드포인트 목록(method, path, operation_id, 파라미터, 응답 코드)을 돌려준다."""
    try:
        s = _get(spec)
    except SpecError as e:
        return _error(e)
    ops = [op.summary_dict() for op in s.operations if tag is None or tag in op.tags]
    return {"spec_id": s.id, "count": len(ops), "endpoints": ops}


@mcp.tool()
def generate_cases(
    spec: str,
    operation_id: str | None = None,
    tag: str | None = None,
    include_writes: bool = False,
) -> dict[str, Any]:
    """테스트 케이스를 생성해서 서버에 보관하고, 한 줄 요약만 돌려준다.

    종류: happy / not_found / invalid_type / missing_required / unauthorized.
    기본값은 GET 등 읽기 요청만 만든다. 쓰기 요청까지 만들려면 include_writes=true.
    요약 형식: '<case_id> <METHOD> <실제 경로?query> → <기대 상태> [body, 쓰기, 인증 없이, 건너뜀]'.
    다시 호출하면 이 스펙의 보관 케이스를 새로 만든 것으로 바꾼다.
    값을 고치려면 run_cases의 overrides를 쓰고, body 등 전체 내용이 필요할 때만 get_cases를 부른다.
    spec에 URL/파일 경로를 바로 넣어도 된다. 결과의 spec(제목, base_urls, 구조 경고)이 load_spec을 대신한다.
    """
    try:
        s = _get(spec)
        result = cases_mod.generate_cases(s, operation_id, tag, include_writes)
    except SpecError as e:
        return _error(e)
    _cases[s.id] = {c["id"]: c for c in result}
    _persist(s)
    lines = [cases_mod.case_line(c) for c in result]
    summary = s.summary_dict()
    out: dict[str, Any] = {
        "spec_id": s.id,
        "spec": {
            **{k: summary[k] for k in ("title", "version", "openapi", "endpoint_count", "base_urls")},
            "warnings": validate_structure(s.raw) + unsupported_refs(s.raw),
        },
        "count": len(result),
        "by_kind": dict(Counter(c["kind"] for c in result)),
        "cases": lines[:MAX_LISTED_CASES],
    }
    if len(lines) > MAX_LISTED_CASES:
        out["cases_more"] = len(lines) - MAX_LISTED_CASES  # 나머지는 get_cases나 실행 결과로 확인
    return out


@mcp.tool()
def get_cases(spec: str, case_ids: list[str]) -> dict[str, Any]:
    """보관 중인 케이스의 전체 내용(path_params, query, headers, body, expected_status)을 돌려준다.
    body를 고쳐야 할 때처럼 요약으로 부족할 때만, 필요한 id만 골라서 부른다."""
    try:
        return {"cases": _select(_get(spec), case_ids)}
    except SpecError as e:
        return _error(e)


@mcp.tool()
def run_cases(
    spec: str,
    base_url: str,
    case_ids: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    export_pytest: bool = False,
    output_path: str | None = None,
) -> dict[str, Any]:
    """보관 중인 케이스를 실제 서버에 호출하고 상태 코드와 응답 스키마를 검증한다.
    실행이 끝나면 스펙 스냅샷을 자동으로 저장한다(save_snapshot을 따로 부를 필요 없음).
    export_pytest=true면 실행한 케이스를 pytest 파일로도 저장한다(기본 tests/generated/test_<title>.py).

    case_ids: 실행할 케이스 id 목록. 생략하면 보관 중인 전체.
    overrides: {case_id: {바꿀 부분}}. path_params/query/headers는 키 단위로 합치고 body/expected_status는 바꾼다.
      예: {"getPet:happy": {"path_params": {"petId": 10}}}. 바꾼 값은 저장되어 export_pytest에도 쓰인다.
    headers: 모든 케이스에 붙일 공통 헤더(예: {"Authorization": "Bearer ..."}).
      unauthorized 케이스에는 인증 헤더를 빼고 보낸다. 401/403·로그인 리다이렉트 실패에는 auth_issue가 붙는다.
    반환: 집계, 실패한 케이스의 상세(failures), 통과 목록(passed_cases: 'id → 상태'), 전체 결과 파일 경로.
    케이스 하나가 잘못돼도 전체가 멈추지 않고 그 케이스만 error로 기록한다.
    """
    try:
        s = _get(spec)
        if overrides:
            stored = _stored(s)
            unknown = [cid for cid in overrides if cid not in stored]
            if unknown:
                return _error(SpecError(f"overrides에 없는 case_id: {unknown[:10]}"))
            for cid, override in overrides.items():
                stored[cid] = cases_mod.apply_overrides(stored[cid], override)
            _persist(s)
        selected = _select(s, case_ids)
    except (SpecError, ValueError) as e:
        return _error(e)
    summary = runner.run_cases(s, selected, base_url, headers=headers, timeout=timeout)
    results_file = snapshot = None
    try:
        store.save_last_run(summary)
        results_file = str(store.save_results(summary))
        # 실행 시점의 스펙을 비교 기준으로 남긴다 (save_snapshot을 따로 부르는 턴을 줄이기 위해)
        snapshot = str(store.save_snapshot(s.source, s.raw))
    except OSError:
        pass
    out = runner.compact_summary(summary, results_file)
    if snapshot:
        out["snapshot"] = snapshot
    if export_pytest:
        out["exported"] = _write_pytest(s, selected, base_url, output_path)
        # 리포트 파일 이름을 서버가 정해 준다: Claude가 현재 시각을 알아내려고 명령을 실행하는 턴을 없애기 위해
        out["report_path"] = str(store.report_path(s.title))
    return out


@mcp.tool()
def call_endpoint(
    method: str,
    url: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """임의의 URL을 한 번 호출해서 상태 코드와 응답을 돌려준다 (수동 확인용)."""
    try:
        return runner.call_endpoint(method, url, query, body, headers)
    except Exception as e:
        return _error(e)


@mcp.tool()
def diff_specs(old: str, new: str) -> dict[str, Any]:
    """두 스펙(URL/파일 경로/spec_id)을 비교해서 breaking / non-breaking 변경을 분류한다.

    old에 'snapshot'을 넣으면 new 스펙의 마지막 스냅샷과 비교한다.
    """
    try:
        new_spec = _get(new)
        if old == "snapshot":
            snap = store.load_snapshot(new_spec.source)
            if snap is None:
                return _error(SpecError(f"스냅샷이 없습니다: {new_spec.source} (먼저 save_snapshot 실행)"))
            old_spec = Spec(snap["spec"], f"snapshot:{snap.get('saved_at', '')}")
        else:
            old_spec = _get(old)
    except SpecError as e:
        return _error(e)
    return diff_mod.diff_specs(old_spec, new_spec)


@mcp.tool()
def save_snapshot(spec: str) -> dict[str, Any]:
    """현재 스펙을 .api-test-pilot/snapshots/에 저장한다. 이후 diff와 편집 훅의 비교 기준이 된다."""
    try:
        s = _get(spec)
    except SpecError as e:
        return _error(e)
    path = store.save_snapshot(s.source, s.raw)
    return {"saved": str(path), "source": s.source}


@mcp.tool()
def export_pytest(
    spec: str,
    base_url: str,
    case_ids: list[str] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """보관 중인 케이스(overrides로 고친 값 포함)를 독립 실행 가능한 pytest 파일로 저장한다.
    case_ids를 생략하면 전체. 기본 위치: tests/generated/test_<title>.py"""
    try:
        s = _get(spec)
        cases = _select(s, case_ids)
    except SpecError as e:
        return _error(e)
    return _write_pytest(s, cases, base_url, output_path)


def _write_pytest(s: Spec, cases: list[dict[str, Any]], base_url: str, output_path: str | None) -> dict[str, Any]:
    path = Path(output_path) if output_path else Path("tests") / "generated" / export.default_filename(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export.render_pytest(s, cases, base_url, path.name), encoding="utf-8")
    return {"written": str(path.resolve()), "case_count": len(cases), "run": f"pytest {path}"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
