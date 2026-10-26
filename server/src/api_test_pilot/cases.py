"""operation에서 테스트 케이스를 만든다.

케이스 종류:
- happy           : 필수값을 예시값으로 채운 정상 요청 → 문서화된 2xx 기대
- not_found       : path 파라미터에 존재하지 않을 값 → 404 기대 (404가 문서화된 경우)
- invalid_type    : 숫자 파라미터에 문자열 → 400/422 기대
- missing_required: 필수 query 파라미터나 필수 body 누락 → 400/422 기대
- unauthorized    : 인증이 필요한 operation을 인증 헤더 없이 호출 → 401/403 기대

케이스는 JSON으로 직렬화 가능한 dict라서, Claude가 값을 고친 뒤 run_cases에 그대로 넘길 수 있다.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .spec import Operation, Spec

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CLIENT_ERROR = [400, 422]
AUTH_ERROR = [401, 403]
NOT_FOUND_NUMBER = 987654321
NOT_FOUND_STRINGS = ("nonexistent-api-test-pilot", "zzzzzzzzzz", "ZZZZZZZZZZ", "999999999")
NOT_FOUND_FORMATS = {
    "uuid": "ffffffff-ffff-4fff-bfff-ffffffffffff",
    "date": "1900-01-01",
    "date-time": "1900-01-01T00:00:00Z",
    "email": "nonexistent@api-test-pilot.invalid",
}
# pattern이 있는 문자열에 넣어 볼 후보. 정규식에서 문자열을 생성하는 대신 흔한 모양을 차례로 대 본다
STRING_CANDIDATES = ("string", "STRING", "String1", "abc", "ABC", "abc123", "ABC123", "123", "a-b", "a_b")
STRING_FORMATS = {
    "date-time": "2024-01-01T00:00:00Z",
    "date": "2024-01-01",
    "email": "user@example.com",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "uri": "https://example.com",
}


def _numeric_bounds(schema: dict[str, Any], integer: bool) -> tuple[float | None, float | None]:
    """minimum/maximum과 exclusive 경계(3.0의 bool, 3.1의 숫자 둘 다)를 포함 범위로 바꾼다."""
    step = 1 if integer else 0.5
    lo, hi = schema.get("minimum"), schema.get("maximum")
    ex_lo, ex_hi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    if ex_lo is True and lo is not None:
        lo += step
    elif isinstance(ex_lo, (int, float)) and not isinstance(ex_lo, bool):
        lo = ex_lo + step if lo is None else max(lo, ex_lo + step)
    if ex_hi is True and hi is not None:
        hi -= step
    elif isinstance(ex_hi, (int, float)) and not isinstance(ex_hi, bool):
        hi = ex_hi - step if hi is None else min(hi, ex_hi - step)
    if integer:
        lo = None if lo is None else math.ceil(lo)
        hi = None if hi is None else math.floor(hi)
    return lo, hi


def _number_in_range(schema: dict[str, Any], integer: bool, preferred: float) -> float | None:
    lo, hi = _numeric_bounds(schema, integer)
    value = preferred
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        value = math.ceil(value / multiple) * multiple
        if hi is not None and value > hi:
            value -= multiple
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return None  # 범위가 비어 있음
    return int(value) if integer else float(value)


def _fits_string(schema: dict[str, Any], value: str) -> bool:
    if len(value) < schema.get("minLength", 0):
        return False
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        return False
    pattern = schema.get("pattern")
    if pattern:
        try:
            return re.search(pattern, value) is not None
        except re.error:
            return True  # 파이썬이 못 읽는 정규식이면 검사하지 않는다
    return True


def _fit_length(schema: dict[str, Any], value: str) -> str:
    min_len, max_len = schema.get("minLength", 0), schema.get("maxLength")
    if len(value) < min_len:
        value = (value * (min_len // max(len(value), 1) + 1))[:min_len]
    if max_len is not None and len(value) > max_len:
        value = value[:max_len]
    return value


def _string_value(schema: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    """후보 중 길이/pattern 제약을 만족하는 첫 값. 없으면 None."""
    for raw in candidates:
        value = _fit_length(schema, raw)
        if _fits_string(schema, value):
            return value
    return None


def sample_value(schema: dict[str, Any] | None, depth: int = 0) -> Any:
    """스키마에서 그럴듯한 예시값을 만든다. example > examples > default > enum > 타입별 기본값 순."""
    if not isinstance(schema, dict) or depth > 4:
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:  # 3.1 / JSON Schema
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for combo in ("allOf", "oneOf", "anyOf"):
        if schema.get(combo):
            if combo == "allOf":
                merged: dict[str, Any] = {}
                for sub in schema["allOf"]:
                    v = sample_value(sub, depth + 1)
                    if isinstance(v, dict):
                        merged.update(v)
                return merged
            return sample_value(schema[combo][0], depth + 1)

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t is None and "properties" in schema:
        t = "object"
    fmt = schema.get("format", "")

    if t == "object":
        return {
            name: sample_value(prop, depth + 1)
            for name, prop in (schema.get("properties") or {}).items()
            if not (isinstance(prop, dict) and prop.get("readOnly"))  # 요청에는 readOnly 필드를 보내지 않는다
        }
    if t == "array":
        item = sample_value(schema.get("items"), depth + 1)
        return [] if item is None else [item]
    if t in ("integer", "number"):
        value = _number_in_range(schema, t == "integer", 1)
        return (1 if t == "integer" else 1.0) if value is None else value
    if t == "boolean":
        return True
    if t == "string":
        if fmt in STRING_FORMATS:
            return STRING_FORMATS[fmt]
        return _string_value(schema, STRING_CANDIDATES) or _fit_length(schema, "string")
    return None


def not_found_value(schema: dict[str, Any]) -> Any:
    """형식과 제약은 지키면서 실제로는 없을 법한 path 값. 만들 수 없으면 None (not_found 케이스 생략).

    제약을 어기는 값을 넣으면 서버가 404가 아니라 422로 막아서 거짓 실패가 된다.
    """
    if schema.get("enum") or "const" in schema:
        return None  # 허용값이 정해져 있으면 '없는 값'을 만들 수 없다
    t = schema.get("type", "string")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t in ("integer", "number"):
        value = _number_in_range(schema, t == "integer", NOT_FOUND_NUMBER)
        # maximum에 걸려 값이 작아졌다면 범위 안의 실제 id일 수 있어 '없는 값'이라고 확신할 수 없다
        return None if value is None or value < NOT_FOUND_NUMBER else value
    fmt = schema.get("format", "")
    if fmt in NOT_FOUND_FORMATS:
        return NOT_FOUND_FORMATS[fmt]
    return _string_value(schema, NOT_FOUND_STRINGS)


def _base_request(op: Operation) -> dict[str, Any]:
    path_params: dict[str, Any] = {}
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    for p in op.parameters:
        if not p.required:
            continue
        value = sample_value(p.schema)
        if value is None:
            value = "string"
        if p.location == "path":
            path_params[p.name] = value
        elif p.location == "query":
            query[p.name] = value
        elif p.location == "header":
            headers[p.name] = str(value)
    body = None
    if op.request_body is not None:
        body = sample_value(op.request_body.get("schema"))
    return {"path_params": path_params, "query": query, "headers": headers, "body": body}


def _case(op: Operation, kind: str, description: str, request: dict[str, Any], expected: list[int]) -> dict[str, Any]:
    return {
        "id": f"{op.operation_id}:{kind}",
        "operation_id": op.operation_id,
        "kind": kind,
        "method": op.method,
        "path": op.path,
        "description": description,
        "writes": op.method in WRITE_METHODS,
        **request,
        "expected_status": expected,
    }


def _expected_success(op: Operation) -> list[int]:
    codes = sorted(int(s) for s in op.success_statuses() if s.isdigit())
    return codes or [200, 201, 202, 204]


def _expected_client_error(op: Operation) -> list[int]:
    documented = sorted(int(s) for s in op.responses if s.isdigit() and s.startswith("4") and s not in ("401", "403", "404"))
    return sorted(set(CLIENT_ERROR + documented))


def cases_for_operation(op: Operation) -> list[dict[str, Any]]:
    base = _base_request(op)
    if op.request_body is not None and not op.request_body.get("json", True):
        # multipart/octet-stream 등은 예시 body를 자동으로 만들 수 없다
        case = _case(op, "happy", f"{op.method} {op.path} 정상 요청", base, _expected_success(op))
        case["skip"] = f"JSON이 아닌 body({', '.join(op.request_body.get('media_types', []))})는 자동 생성 대상이 아님"
        return [case]
    cases = [_case(op, "happy", f"{op.method} {op.path} 정상 요청", base, _expected_success(op))]

    if op.security:
        # run_cases에 넘긴 인증 헤더를 빼고 보낸다 (auth=False). 401만 쓰는 서버와 403을 쓰는 서버가 모두 있다
        case = _case(op, "unauthorized", "인증 정보 없이 호출 → 401/403", base, AUTH_ERROR)
        case["auth"] = False
        cases.append(case)

    path_params = [p for p in op.parameters if p.location == "path"]
    if path_params and op.documented(404):
        values = {p.name: not_found_value(p.schema) for p in path_params}
        if all(v is not None for v in values.values()):
            req = {**base, "path_params": {**base["path_params"], **values}}
            cases.append(_case(op, "not_found", "존재하지 않는 리소스 → 404", req, [404]))

    numeric = [p for p in op.parameters if p.location in ("path", "query") and p.schema.get("type") in ("integer", "number")]
    if numeric:
        p = numeric[0]
        key = "path_params" if p.location == "path" else "query"
        req = {**base, key: {**base[key], p.name: "abc"}}
        cases.append(_case(op, "invalid_type", f"'{p.name}'에 숫자 대신 문자열 → 4xx", req, _expected_client_error(op)))

    required_query = [p for p in op.parameters if p.location == "query" and p.required]
    if required_query:
        p = required_query[0]
        req = {**base, "query": {k: v for k, v in base["query"].items() if k != p.name}}
        cases.append(_case(op, "missing_required", f"필수 query '{p.name}' 누락 → 4xx", req, _expected_client_error(op)))
    elif op.request_body and op.request_body.get("required"):
        req = {**base, "body": None}
        cases.append(_case(op, "missing_required", "필수 body 누락 → 4xx", req, _expected_client_error(op)))

    return cases


def generate_cases(
    spec: Spec,
    operation_id: str | None = None,
    tag: str | None = None,
    include_writes: bool = False,
) -> list[dict[str, Any]]:
    if operation_id:
        ops = [spec.find(operation_id)]
    else:
        ops = [op for op in spec.operations if tag is None or tag in op.tags]
    cases: list[dict[str, Any]] = []
    for op in ops:
        # operation_id로 콕 집어 요청한 경우에는 쓰기 메서드라도 만든다
        if not include_writes and not operation_id and op.method in WRITE_METHODS:
            continue
        cases.extend(cases_for_operation(op))
    return cases


# --- Claude에게 보내는 요약 ---
# 케이스 전체(JSON)는 서버가 보관하고, Claude에게는 한 줄 요약만 보낸다.
# 같은 케이스 목록이 generate → run → export로 컨텍스트에 세 번 들어가던 것을 한 번(요약)으로 줄이기 위함.

OVERRIDE_KEYS = ("path_params", "query", "headers", "body", "expected_status")


def case_line(case: dict[str, Any]) -> str:
    """'getPet:happy GET /pets/7?x=1 → 200 [body]'처럼 값이 보이는 한 줄."""
    path = case["path"]
    for name, value in (case.get("path_params") or {}).items():
        path = path.replace("{" + name + "}", str(value))
    query = "&".join(f"{k}={v}" for k, v in (case.get("query") or {}).items())
    expected = "/".join(str(s) for s in case.get("expected_status") or []) or "any"
    flags = []
    if case.get("body") is not None:
        flags.append("body")
    if case.get("writes"):
        flags.append("쓰기")
    if case.get("auth") is False:
        flags.append("인증 없이")
    if case.get("skip"):
        flags.append(f"건너뜀: {case['skip']}")
    line = f"{case['id']} {case['method']} {path}{'?' + query if query else ''} → {expected}"
    return line + (f" [{', '.join(flags)}]" if flags else "")


def apply_overrides(case: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """바꿀 부분만 덮어쓴다. path_params/query/headers는 키 단위로 합치고, body/expected_status는 통째로 바꾼다.
    dict 안에서 값을 None으로 주면 그 키를 지운다 (예: {"query": {"limit": None}})."""
    unknown = set(override) - set(OVERRIDE_KEYS)
    if unknown:
        raise ValueError(f"바꿀 수 없는 항목: {sorted(unknown)} (가능: {', '.join(OVERRIDE_KEYS)})")
    updated = dict(case)
    for key, value in override.items():
        if key in ("path_params", "query", "headers") and isinstance(value, dict):
            merged = {**(case.get(key) or {}), **value}
            updated[key] = {k: v for k, v in merged.items() if v is not None}
        else:
            updated[key] = value
    return updated
