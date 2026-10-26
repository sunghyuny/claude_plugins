"""두 스펙을 비교해서 breaking / non-breaking 변경을 분류한다.

기준(클라이언트 입장):
- 요청 쪽: 필수값이 새로 생기거나, 타입이 바뀌거나, 클라이언트가 보내던 것이 사라지면 breaking
- 응답 쪽: 필드나 성공 응답이 사라지거나, 타입이 바뀌면 breaking
- 무언가 추가되는 것(선택 파라미터, 응답 필드, 새 엔드포인트)은 non-breaking
"""

from __future__ import annotations

import re
from typing import Any

from .spec import Operation, Spec

MAX_DEPTH = 6
PATH_VAR = re.compile(r"\{([^}]+)\}")

# 클라이언트가 깨질 가능성이 큰 순서. 목록에 없는 종류는 뒤로 간다.
SEVERITY_ORDER = [
    "operation_removed",
    "success_response_removed",
    "required_param_added",
    "required_body_added",
    "required_field_added",
    "param_became_required",
    "body_became_required",
    "field_became_required",
    "type_changed",
    "param_removed",
    "body_removed",
    "response_field_removed",
    "request_field_removed",
    "request_variant_removed",
    "response_variant_added",
    "enum_value_removed",
    "response_enum_value_added",
    "field_became_optional",
]


def _change(kind: str, location: str, detail: str) -> dict[str, str]:
    return {"type": kind, "location": location, "detail": detail}


def _type_of(schema: dict[str, Any]) -> str | None:
    t = schema.get("type")
    if isinstance(t, list):
        return "|".join(sorted(x for x in t if x != "null")) or None
    if t is None and "properties" in schema:
        return "object"
    return t


def _accepts(wide: str, narrow: str) -> bool:
    """wide 타입이 narrow 타입의 값을 모두 받아들이는지 (integer는 number에 포함)."""
    wide_set = set(wide.split("|"))
    return all(t in wide_set or (t == "integer" and "number" in wide_set) for t in narrow.split("|"))


def _flatten_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    """allOf를 properties/required가 합쳐진 스키마 하나로 펼친다 (상속 표현을 비교하기 위해)."""
    if not isinstance(schema.get("allOf"), list):
        return schema
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    props = dict(merged.get("properties") or {})
    required = list(merged.get("required") or [])
    for sub in schema["allOf"]:
        if not isinstance(sub, dict):
            continue
        sub = _flatten_all_of(sub)
        props.update(sub.get("properties") or {})
        required += [r for r in sub.get("required") or [] if r not in required]
        for key in ("type", "items", "enum", "oneOf", "anyOf"):
            if key in sub and key not in merged:
                merged[key] = sub[key]
    if props:
        merged["properties"] = props
    if required:
        merged["required"] = required
    return merged


def compare_schema(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    loc: str,
    direction: str,
    breaking: list[dict],
    non_breaking: list[dict],
    depth: int = 0,
) -> None:
    """direction: 'request' 또는 'response'."""
    if not isinstance(old, dict) or not isinstance(new, dict) or depth > MAX_DEPTH:
        return
    old, new = _flatten_all_of(old), _flatten_all_of(new)
    ot, nt = _type_of(old), _type_of(new)
    if ot and nt and ot != nt:
        # 요청은 받는 범위가 넓어지면, 응답은 보내는 범위가 좁아지면 기존 클라이언트가 그대로 동작한다
        compatible = _accepts(nt, ot) if direction == "request" else _accepts(ot, nt)
        if compatible:
            non_breaking.append(_change("type_compatible_change", loc, f"호환되는 타입 변경: {ot} → {nt}"))
        else:
            breaking.append(_change("type_changed", loc, f"타입 변경: {ot} → {nt}"))
        return

    for combo in ("oneOf", "anyOf"):
        old_variants, new_variants = old.get(combo) or [], new.get(combo) or []
        if not old_variants and not new_variants:
            continue
        # 변형(variant)은 순서로 짝짓는다. 이름이 없으니 순서를 바꾸면 오탐이 날 수 있다
        for i, (ov, nv) in enumerate(zip(old_variants, new_variants)):
            compare_schema(ov, nv, f"{loc}<{combo}[{i}]>", direction, breaking, non_breaking, depth + 1)
        count = f"{combo} 변형 {len(old_variants)}개 → {len(new_variants)}개"
        if len(new_variants) < len(old_variants):
            # 요청: 보내던 형태를 못 보냄 / 응답: 오던 형태가 안 옴 (클라이언트는 그대로 동작)
            if direction == "request":
                breaking.append(_change("request_variant_removed", loc, count))
            else:
                non_breaking.append(_change("response_variant_removed", loc, count))
        elif len(new_variants) > len(old_variants):
            if direction == "response":
                breaking.append(_change("response_variant_added", loc, f"{count} (모르는 형태의 응답이 올 수 있음)"))
            else:
                non_breaking.append(_change("request_variant_added", loc, count))

    old_enum, new_enum = old.get("enum"), new.get("enum")
    if old_enum and new_enum:
        removed = [v for v in old_enum if v not in new_enum]
        added = [v for v in new_enum if v not in old_enum]
        # 요청: 보낼 수 있던 값이 사라지면 breaking / 응답: 모르는 값이 새로 오면 breaking
        if direction == "request":
            if removed:
                breaking.append(_change("enum_value_removed", loc, f"enum 값 삭제: {removed}"))
            if added:
                non_breaking.append(_change("enum_value_added", loc, f"enum 값 추가: {added}"))
        else:
            if added:
                breaking.append(_change("response_enum_value_added", loc, f"응답에 새 enum 값: {added} (값을 엄격히 검사하는 클라이언트가 깨질 수 있음)"))
            if removed:
                non_breaking.append(_change("response_enum_value_removed", loc, f"응답 enum 값 삭제: {removed}"))

    old_props = old.get("properties") or {}
    new_props = new.get("properties") or {}
    old_req = set(old.get("required") or [])
    new_req = set(new.get("required") or [])

    for name, sub in old_props.items():
        sub_loc = f"{loc}.{name}"
        if name not in new_props:
            if direction == "response":
                breaking.append(_change("response_field_removed", sub_loc, "응답 필드 삭제"))
            else:
                breaking.append(_change("request_field_removed", sub_loc, "요청 필드 삭제 (보내던 값이 무시되거나 거부될 수 있음)"))
            continue
        compare_schema(sub, new_props[name], sub_loc, direction, breaking, non_breaking, depth + 1)

    for name in new_props.keys() - old_props.keys():
        sub_loc = f"{loc}.{name}"
        if direction == "request" and name in new_req:
            breaking.append(_change("required_field_added", sub_loc, "필수 요청 필드 추가"))
        else:
            non_breaking.append(_change("field_added", sub_loc, "필드 추가"))

    for name in (new_req - old_req) & old_props.keys():
        if direction == "request":
            breaking.append(_change("field_became_required", f"{loc}.{name}", "선택 → 필수로 변경"))
    for name in (old_req - new_req) & new_props.keys():
        if direction == "response":
            breaking.append(_change("field_became_optional", f"{loc}.{name}", "응답에서 항상 오던 필드가 선택으로 변경"))

    if "items" in old or "items" in new:
        compare_schema(old.get("items"), new.get("items"), f"{loc}[]", direction, breaking, non_breaking, depth + 1)


def _compare_operation(old: Operation, new: Operation, breaking: list[dict], non_breaking: list[dict]) -> None:
    key = f"{old.method} {old.path}"
    # /pets/{id} → /pets/{petId}처럼 path 변수 이름만 바뀐 경우: 위치가 같으면 같은 파라미터로 본다
    renamed = {o: n for o, n in zip(PATH_VAR.findall(old.path), PATH_VAR.findall(new.path)) if o != n}
    if renamed:
        detail = ", ".join(f"{o} → {n}" for o, n in renamed.items())
        non_breaking.append(_change("path_param_renamed", key, f"path 변수 이름 변경: {detail} (URL은 그대로)"))

    def param_key(p):
        return (renamed.get(p.name, p.name) if p.location == "path" else p.name, p.location)

    old_params = {param_key(p): p for p in old.parameters}
    new_params = {(p.name, p.location): p for p in new.parameters}

    for k, p in old_params.items():
        loc = f"{key} param '{p.name}' ({p.location})"
        if k not in new_params:
            breaking.append(_change("param_removed", loc, "파라미터 삭제"))
            continue
        np = new_params[k]
        if np.required and not p.required:
            breaking.append(_change("param_became_required", loc, "선택 → 필수로 변경"))
        compare_schema(p.schema, np.schema, loc, "request", breaking, non_breaking)

    for k in new_params.keys() - old_params.keys():
        p = new_params[k]
        loc = f"{key} param '{p.name}' ({p.location})"
        if p.required:
            breaking.append(_change("required_param_added", loc, "필수 파라미터 추가"))
        else:
            non_breaking.append(_change("param_added", loc, "선택 파라미터 추가"))

    if old.request_body is None and new.request_body is not None and new.request_body.get("required"):
        breaking.append(_change("required_body_added", f"{key} body", "필수 request body 추가"))
    elif old.request_body is not None and new.request_body is None:
        breaking.append(_change("body_removed", f"{key} body", "request body 삭제 (보내던 값이 무시되거나 거부될 수 있음)"))
    elif old.request_body is not None and new.request_body is not None:
        if new.request_body.get("required") and not old.request_body.get("required"):
            breaking.append(_change("body_became_required", f"{key} body", "request body가 필수로 변경"))
        compare_schema(old.request_body.get("schema"), new.request_body.get("schema"), f"{key} body", "request", breaking, non_breaking)

    for status, schema in old.responses.items():
        loc = f"{key} response {status}"
        if status not in new.responses:
            if status.startswith("2"):
                breaking.append(_change("success_response_removed", loc, "성공 응답 코드 삭제"))
            else:
                non_breaking.append(_change("response_removed", loc, "응답 코드 삭제"))
            continue
        compare_schema(schema, new.responses[status], loc, "response", breaking, non_breaking)
    for status in new.responses.keys() - old.responses.keys():
        non_breaking.append(_change("response_added", f"{key} response {status}", "응답 코드 추가"))


def diff_specs(old: Spec, new: Spec) -> dict[str, Any]:
    breaking: list[dict] = []
    non_breaking: list[dict] = []
    # path 변수 이름은 URL에 드러나지 않으므로 {…}를 지운 모양으로 짝짓는다
    old_ops = {(o.method, PATH_VAR.sub("{}", o.path)): o for o in old.operations}
    new_ops = {(o.method, PATH_VAR.sub("{}", o.path)): o for o in new.operations}

    for k, op in old_ops.items():
        if k not in new_ops:
            breaking.append(_change("operation_removed", f"{op.method} {op.path}", "엔드포인트 삭제"))
        else:
            _compare_operation(op, new_ops[k], breaking, non_breaking)
    for k in sorted(new_ops.keys() - old_ops.keys()):
        op = new_ops[k]
        non_breaking.append(_change("operation_added", f"{op.method} {op.path}", "엔드포인트 추가"))

    rank = {kind: i for i, kind in enumerate(SEVERITY_ORDER)}
    breaking.sort(key=lambda c: rank.get(c["type"], len(rank)))  # 안정 정렬이라 같은 종류는 원래 순서 유지

    return {
        "old": {"title": old.title, "version": old.version, "source": old.source},
        "new": {"title": new.title, "version": new.version, "source": new.source},
        "breaking_count": len(breaking),
        "non_breaking_count": len(non_breaking),
        "breaking": breaking,
        "non_breaking": non_breaking,
    }
