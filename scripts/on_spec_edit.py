"""OpenAPI 스펙 파일 편집 훅.

  pre  (PreToolUse,  Edit|Write) : 편집 직전 스펙 사본을 .api-test-pilot/pre-edit/에 저장
  post (PostToolUse, Edit|Write) : 1) 구조 검증 — 깨졌으면 Claude에게 block으로 알림
                                   2) 스냅샷(없으면 편집 직전 사본)과 diff — breaking change 경고

스펙 파일이 아니거나 예상 못 한 오류가 나면 조용히 통과한다. (훅 때문에 작업이 막히면 안 됨)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from api_test_pilot import store
from api_test_pilot.diff import diff_specs
from api_test_pilot.spec import Spec, SpecError, parse_text, read_text_file, validate_structure

SPEC_SUFFIXES = {".json", ".yaml", ".yml"}
MAX_LISTED = 8
MAX_SPEC_BYTES = 20 * 1024 * 1024
# 최상위가 아니어도 걸리지만, 파싱 전에 package-lock.json 같은 큰 비스펙 파일을 걸러내는 용도로 충분하다
SPEC_MARKER = re.compile(r"""\b(?:openapi|swagger)["']?\s*:""")


def _is_spec(raw: object) -> bool:
    return isinstance(raw, dict) and ("openapi" in raw or "swagger" in raw)


def _emit(payload: dict) -> None:
    # ASCII 이스케이프로 출력: Windows 콘솔 인코딩(cp949)과 무관하게 안전
    sys.stdout.write(json.dumps(payload))


def _read(path: Path, require_marker: bool) -> str | None:
    """원문을 읽는다. require_marker면 스펙 표식이 없는 파일은 None (파싱 비용을 피하기 위해)."""
    if not path.is_file() or path.stat().st_size > MAX_SPEC_BYTES:
        return None
    text = read_text_file(path)
    if require_marker and not SPEC_MARKER.search(text):
        return None
    return text


def _target(event: dict) -> Path | None:
    file_path = (event.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(event.get("cwd") or ".") / path
    path = path.resolve()
    return path if path.suffix.lower() in SPEC_SUFFIXES else None


def pre(event: dict, path: Path, root: Path) -> None:
    try:
        text = _read(path, require_marker=True)
        if text is None or not _is_spec(parse_text(text)):
            return
    except SpecError:
        return
    backup = store.pre_edit_path(str(path), root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(text, encoding="utf-8")


def post(event: dict, path: Path, root: Path) -> None:
    backup = store.pre_edit_path(str(path), root)
    had_backup = backup.is_file()
    try:
        _check(path, root, backup if had_backup else None)
    finally:
        if had_backup:
            backup.unlink(missing_ok=True)


def _check(path: Path, root: Path, backup: Path | None) -> None:
    try:
        # 원래 스펙이던 파일은 표식이 지워졌어도 검사해야 한다 (스펙이 깨진 것이므로)
        text = _read(path, require_marker=backup is None)
        if text is None:
            return
        raw = parse_text(text)
    except SpecError as e:
        if backup is not None:  # 원래 스펙이던 파일이 파싱 불가 상태가 됨
            _emit({"decision": "block", "reason": f"[api-test-pilot] {path.name}: {e}"})
        return
    if not _is_spec(raw):
        return

    errors = validate_structure(raw)
    if errors:
        listed = "\n".join(f"- {e}" for e in errors[:MAX_LISTED])
        _emit({"decision": "block", "reason": f"[api-test-pilot] {path.name} 스펙 구조 오류:\n{listed}"})
        return

    snapshot = store.load_snapshot(str(path), root)
    if snapshot is not None:
        baseline, label = Spec(snapshot["spec"], "snapshot"), f"스냅샷({snapshot.get('saved_at', '')})"
    elif backup is not None:
        baseline, label = Spec(parse_text(read_text_file(backup)), "pre-edit"), "편집 전"
    else:
        return

    result = diff_specs(baseline, Spec(raw, str(path)))
    # 이미 알린 breaking change는 다시 알리지 않는다. 스냅샷 기준이면 같은 경고가 편집할 때마다
    # Claude 컨텍스트에 반복해서 들어가기 때문 (해결된 항목은 목록에서 빠지고, 새로 생긴 것만 알린다)
    current = {f"{c['type']}|{c['location']}" for c in result["breaking"]}
    warned = store.load_warned(str(path), root)
    store.save_warned(str(path), current, root)
    new = [c for c in result["breaking"] if f"{c['type']}|{c['location']}" not in warned]
    if not new:
        return
    lines = [f"- [{c['type']}] {c['location']}: {c['detail']}" for c in new[:MAX_LISTED]]
    if len(new) > MAX_LISTED:
        lines.append(f"- ... 외 {len(new) - MAX_LISTED}건")
    detail = "\n".join(lines)
    known = result["breaking_count"] - len(new)
    known_note = f" (이전에 알린 {known}건은 생략)" if known else ""
    _emit(
        {
            "systemMessage": f"⚠️ api-test-pilot: {path.name}에서 breaking change {len(new)}건 새로 감지 ({label} 대비){known_note}",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"[api-test-pilot] {path.name} 편집으로 {label} 대비 breaking change가 "
                    f"{len(new)}건 새로 생겼습니다{known_note}:\n{detail}\n"
                    "의도한 변경인지 사용자에게 알리고, 자세한 영향 분석이 필요하면 /api-diff 실행을 제안하세요."
                ),
            },
        }
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    try:
        event = json.loads(sys.stdin.read() or "{}")
        path = _target(event)
        if path is None:
            return
        root = Path(event.get("cwd") or Path.cwd())
        (pre if mode == "pre" else post)(event, path, root)
    except Exception:
        return  # 훅 오류로 사용자 작업을 막지 않는다


if __name__ == "__main__":
    main()
