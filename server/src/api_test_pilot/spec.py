"""OpenAPI 스펙 로드, $ref 해석, 엔드포인트(operation) 추출.

OpenAPI 3.x를 기본으로 지원하고, Swagger 2.0은 테스트에 필요한 범위
(파라미터, body, 응답 스키마, host/basePath)만 3.x 형태로 맞춰서 읽는다.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


class SpecError(Exception):
    """스펙을 읽거나 해석할 수 없을 때."""


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def read_source(source: str, timeout: float = 15.0) -> str:
    """URL이나 파일 경로에서 스펙 원문을 읽는다."""
    if is_url(source):
        try:
            resp = httpx.get(source, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise SpecError(f"스펙을 내려받지 못했습니다: {source} ({e})") from e
        return resp.text
    path = Path(source).expanduser()
    if not path.is_file():
        raise SpecError(f"스펙 파일이 없습니다: {path.resolve()}")
    return read_text_file(path)


def read_text_file(path: Path) -> str:
    """UTF-8(BOM 허용)로 읽는다. 다른 인코딩이면 SpecError."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise SpecError(f"UTF-8로 읽을 수 없는 파일입니다: {path} (CP949 등으로 저장됐다면 UTF-8로 바꿔 저장해 주세요)") from e


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml이 있으면 수십 배 빠르다


def parse_text(text: str) -> dict[str, Any]:
    """JSON/YAML 원문을 dict로 바꾼다. JSON은 json으로 먼저 시도한다 (YAML 파서보다 훨씬 빠름)."""
    text = text.lstrip("﻿")
    data: Any = None
    if text.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if data is None:
        try:
            data = yaml.load(text, Loader=_YAML_LOADER)
        except yaml.YAMLError as e:
            raise SpecError(f"JSON/YAML 파싱 실패: {e}") from e
    if not isinstance(data, dict):
        raise SpecError("스펙 최상위가 객체(dict)가 아닙니다.")
    return data


def validate_structure(raw: dict[str, Any]) -> list[str]:
    """스펙의 기본 구조를 점검해서 문제 목록을 돌려준다. 빈 목록이면 정상."""
    errors: list[str] = []
    if "openapi" not in raw and "swagger" not in raw:
        errors.append("'openapi' 또는 'swagger' 버전 필드가 없습니다.")
    info = raw.get("info")
    if not isinstance(info, dict):
        errors.append("'info' 객체가 없습니다.")
    else:
        for key in ("title", "version"):
            if key not in info:
                errors.append(f"'info.{key}'가 없습니다.")
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        errors.append("'paths' 객체가 없습니다.")
        return errors
    for path, item in paths.items():
        if not str(path).startswith("/"):
            errors.append(f"경로는 '/'로 시작해야 합니다: {path}")
        if not isinstance(item, dict):
            errors.append(f"{path}: path item이 객체가 아닙니다.")
            continue
        for method, op in item.items():
            if method not in HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                errors.append(f"{method.upper()} {path}: operation이 객체가 아닙니다.")
            elif not op.get("responses"):
                errors.append(f"{method.upper()} {path}: 'responses'가 없습니다.")
    errors.extend(_dangling_refs(raw))
    return errors


def _dangling_refs(raw: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                try:
                    _lookup(raw, ref)
                except SpecError:
                    errors.append(f"해석할 수 없는 $ref: {ref}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(raw)
    return sorted(set(errors))


def unsupported_refs(raw: dict[str, Any]) -> list[str]:
    """외부 파일/URL을 가리키는 $ref 목록 경고. 구조 오류는 아니지만 해석하지 못해 검증이 빠진다."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                found.add(ref)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(raw)
    return [f"외부 $ref는 해석하지 않습니다 (해당 부분은 검증에서 빠짐): {ref}" for ref in sorted(found)]


def _lookup(root: dict[str, Any], ref: str) -> Any:
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SpecError(f"해석할 수 없는 $ref: {ref}")
        node = node[part]
    return node


def resolve_refs(node: Any, root: dict[str, Any], _stack: tuple[str, ...] = ()) -> Any:
    """로컬 $ref(#/...)를 펼친 사본을 돌려준다. 순환 참조는 빈 스키마로 끊는다."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _stack:
                return {}
            target = _lookup(root, ref)
            return resolve_refs(target, root, _stack + (ref,))
        return {k: resolve_refs(v, root, _stack) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_refs(v, root, _stack) for v in node]
    return node


@dataclass
class Parameter:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "in": self.location,
            "required": self.required,
            "type": self.schema.get("type", "any"),
        }


@dataclass
class Operation:
    method: str  # 대문자
    path: str
    operation_id: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    parameters: list[Parameter] = field(default_factory=list)
    request_body: dict[str, Any] | None = None  # {"required": bool, "schema": {...}}
    responses: dict[str, dict[str, Any] | None] = field(default_factory=dict)  # status -> schema
    security: bool = False

    def success_statuses(self) -> list[str]:
        return [s for s in self.responses if s.startswith("2") or s.upper() == "2XX"]

    def documented(self, status: int) -> bool:
        s = str(status)
        return s in self.responses or f"{s[0]}XX" in {k.upper() for k in self.responses}

    def response_schema(self, status: int) -> dict[str, Any] | None:
        s = str(status)
        for key in (s, f"{s[0]}XX", f"{s[0]}xx", "default"):
            if key in self.responses:
                return self.responses[key]
        return None

    def summary_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "tags": self.tags,
            "parameters": [p.to_dict() for p in self.parameters],
            "has_body": self.request_body is not None,
            "responses": sorted(self.responses),
            "requires_auth": self.security,
        }


class Spec:
    """로드된 스펙 하나. raw는 원본, operations는 $ref가 펼쳐진 operation 목록."""

    def __init__(self, raw: dict[str, Any], source: str = "<inline>"):
        self.raw = raw
        self.source = source
        self.id = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        self.is_swagger2 = str(raw.get("swagger", "")).startswith("2")
        self.operations = self._build_operations()

    @classmethod
    def load(cls, source: str) -> "Spec":
        raw = parse_text(read_source(source))
        if is_url(source):
            src = source
        else:
            src = str(Path(source).expanduser().resolve())
        return cls(raw, src)

    @property
    def title(self) -> str:
        return str(self.raw.get("info", {}).get("title", "untitled"))

    @property
    def version(self) -> str:
        return str(self.raw.get("info", {}).get("version", ""))

    def base_urls(self) -> list[str]:
        """servers(3.x) 또는 host/basePath(2.0)에서 base URL 후보를 만든다.
        상대 경로 서버는 스펙 URL을 기준으로 절대 경로로 바꾼다."""
        urls: list[str] = []
        if self.is_swagger2:
            host = self.raw.get("host")
            base_path = self.raw.get("basePath", "")
            schemes = self.raw.get("schemes") or ["https"]
            if host:
                urls = [f"{schemes[0]}://{host}{base_path}"]
            elif is_url(self.source):
                urls = [urljoin(self.source, base_path or "/")]
        else:
            for server in self.raw.get("servers") or []:
                url = str(server.get("url", ""))
                for name, var in (server.get("variables") or {}).items():
                    url = url.replace("{" + name + "}", str(var.get("default", "")))
                if not url:
                    continue
                if not is_url(url) and is_url(self.source):
                    url = urljoin(self.source, url)
                urls.append(url)
            if not urls and is_url(self.source):
                # servers가 없으면 스펙을 받아온 서버 자체가 기준 (FastAPI 기본값)
                urls = [urljoin(self.source, "/")]
        return [u.rstrip("/") for u in urls]

    def auth_header_names(self) -> set[str]:
        """인증에 쓰이는 헤더 이름(소문자). securitySchemes(3.x)나 securityDefinitions(2.0)에서 읽는다.
        unauthorized 케이스에서 이 헤더들만 빼고 보내기 위해 쓴다."""
        names = {"authorization"}  # http/oauth2/openIdConnect/basic은 모두 Authorization 헤더
        for scheme in self._api_key_schemes():
            if scheme.get("in") == "header":
                names.add(str(scheme.get("name", "")).lower())
            elif scheme.get("in") == "cookie":  # 세션 쿠키 인증: Cookie 헤더째로 뺀다
                names.add("cookie")
        return names

    def auth_query_names(self) -> set[str]:
        """query로 보내는 API 키 이름. 결과 URL에 표시할 때 값을 가린다."""
        return {str(s.get("name", "")) for s in self._api_key_schemes() if s.get("in") == "query"}

    def _api_key_schemes(self) -> list[dict[str, Any]]:
        schemes = (self.raw.get("components") or {}).get("securitySchemes") or self.raw.get("securityDefinitions") or {}
        return [s for s in schemes.values() if isinstance(s, dict) and s.get("type") == "apiKey"]

    def find(self, operation_id: str) -> Operation:
        for op in self.operations:
            if op.operation_id == operation_id:
                return op
        raise SpecError(f"operation_id를 찾을 수 없습니다: {operation_id}")

    def summary_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.id,
            "source": self.source,
            "title": self.title,
            "version": self.version,
            "openapi": str(self.raw.get("openapi") or self.raw.get("swagger")),
            "endpoint_count": len(self.operations),
            "base_urls": self.base_urls(),
        }

    def _build_operations(self) -> list[Operation]:
        ops: list[Operation] = []
        root = self.raw
        global_security = bool(root.get("security"))
        for path, item in (root.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            item = resolve_refs(item, root)
            shared_params = item.get("parameters") or []
            for method in HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                params, body = self._params_and_body(shared_params, op.get("parameters") or [], op)
                security = op.get("security")
                ops.append(
                    Operation(
                        method=method.upper(),
                        path=path,
                        operation_id=op.get("operationId") or _default_operation_id(method, path),
                        summary=op.get("summary", ""),
                        tags=list(op.get("tags") or []),
                        parameters=params,
                        request_body=body,
                        responses=self._responses(op.get("responses") or {}),
                        security=bool(security) if security is not None else global_security,
                    )
                )
        return ops

    def _params_and_body(
        self, shared: list[dict], own: list[dict], op: dict[str, Any]
    ) -> tuple[list[Parameter], dict[str, Any] | None]:
        merged: dict[tuple[str, str], dict] = {}
        for p in [*shared, *own]:  # operation 쪽이 path item 쪽을 덮어쓴다
            merged[(p.get("name", ""), p.get("in", ""))] = p
        params: list[Parameter] = []
        body: dict[str, Any] | None = None
        for p in merged.values():
            loc = p.get("in", "")
            if loc == "body":  # Swagger 2.0
                body = {"required": bool(p.get("required")), "schema": p.get("schema") or {}}
                continue
            if loc == "formData":
                continue
            schema = p.get("schema")
            if schema is None:  # Swagger 2.0: 타입 정보가 파라미터에 바로 붙어 있다
                schema = {
                    k: p[k]
                    for k in ("type", "format", "enum", "items", "default", "minimum", "maximum",
                              "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern")
                    if k in p
                }
            schema = _with_example(schema, p)
            params.append(
                Parameter(
                    name=p.get("name", ""),
                    location=loc,
                    required=bool(p.get("required")) or loc == "path",
                    schema=schema,
                )
            )
        rb = op.get("requestBody")
        if isinstance(rb, dict):
            content = rb.get("content") or {}
            body = {
                "required": bool(rb.get("required")),
                "schema": _with_example(_json_schema_of(content), _json_media_of(content)),
                "json": not content or any("json" in media for media in content),
                "media_types": list(content),
            }
        elif body is not None:
            body["json"] = True
            body["media_types"] = ["application/json"]
        return params, body

    def _responses(self, responses: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for status, resp in responses.items():
            if not isinstance(resp, dict):
                continue
            if self.is_swagger2:
                out[str(status)] = resp.get("schema")
            else:
                out[str(status)] = _json_schema_of(resp.get("content") or {}) or None
        return out


def _json_media_of(content: dict[str, Any]) -> dict[str, Any]:
    for media, obj in content.items():
        if "json" in media and isinstance(obj, dict):
            return obj
    return {}


def _with_example(schema: dict[str, Any], holder: dict[str, Any]) -> dict[str, Any]:
    """파라미터나 미디어 타입 수준의 example/examples를 스키마의 example로 옮긴다.
    (springdoc, FastAPI 등은 예시값을 스키마가 아니라 이쪽에 둔다)"""
    if not isinstance(holder, dict) or not isinstance(schema, dict):
        return schema
    if "example" in holder:
        return {**schema, "example": holder["example"]}
    examples = holder.get("examples")
    if isinstance(examples, dict):
        for ex in examples.values():
            if isinstance(ex, dict) and "value" in ex:
                return {**schema, "example": ex["value"]}
    return schema


def _json_schema_of(content: dict[str, Any]) -> dict[str, Any]:
    """content 맵에서 JSON 계열 미디어 타입의 스키마를 고른다."""
    for media, obj in content.items():
        if "json" in media and isinstance(obj, dict):
            return obj.get("schema") or {}
    for obj in content.values():
        if isinstance(obj, dict) and obj.get("schema"):
            return obj["schema"]
    return {}


def _default_operation_id(method: str, path: str) -> str:
    cleaned = path.strip("/").replace("{", "").replace("}", "").replace("/", "_").replace("-", "_")
    return f"{method}_{cleaned or 'root'}"


def to_json_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAPI 3.0 스키마의 비표준 키워드(nullable 등)를 JSON Schema로 맞춘다."""
    if not isinstance(schema, dict):
        return {}
    s = copy.deepcopy(schema)

    dropped = ("xml", "example", "discriminator", "externalDocs")

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            node = {
                k: ({pk: fix(pv) for pk, pv in v.items()} if k == "properties" and isinstance(v, dict) else fix(v))
                for k, v in node.items()
                if k not in dropped
            }
            # 3.0의 boolean exclusiveMinimum/Maximum → JSON Schema의 숫자 형식
            for bound, excl in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
                if isinstance(node.get(excl), bool):
                    if node.pop(excl) and bound in node:
                        node[excl] = node.pop(bound)
            if node.pop("nullable", False):
                t = node.get("type")
                if isinstance(t, str):
                    node["type"] = [t, "null"]
                elif isinstance(t, list) and "null" not in t:
                    node["type"] = [*t, "null"]
            return node
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(s)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
