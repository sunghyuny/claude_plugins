"""프로젝트 폴더의 .api-test-pilot/ 아래에 스냅샷과 실행 결과를 저장한다.

.api-test-pilot/
├─ snapshots/<이름>-<해시>.json   # /api-test 시점의 스펙 (breaking change 비교 기준)
├─ pre-edit/<해시>.json           # 훅이 편집 직전에 떠 둔 스펙 사본
├─ cases/<spec_id>.json           # generate_cases가 만든 케이스 (Claude에게는 요약만 보내고 여기 보관)
├─ warned/<해시>.json             # 편집 훅이 이미 알린 breaking change 목록 (같은 경고 반복 방지)
├─ reports/                       # 사람이 읽는 md 리포트 (Skill이 작성)
├─ last_run.json                  # 마지막 run_cases 요약 (Stop 훅이 읽음)
└─ last_results.json              # 마지막 run_cases 전체 결과 (리포트·api-reviewer가 필요할 때 읽음)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

DIR_NAME = ".api-test-pilot"


def workspace(root: str | Path | None = None) -> Path:
    return Path(root or Path.cwd()) / DIR_NAME


def _key(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]


def snapshot_path(source: str, root: str | Path | None = None) -> Path:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(source.split("?")[0]).stem)[:40] or "spec"
    return workspace(root) / "snapshots" / f"{stem}-{_key(source)}.json"


def save_snapshot(source: str, raw: dict[str, Any], root: str | Path | None = None) -> Path:
    path = snapshot_path(source, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "spec": raw}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(source: str, root: str | Path | None = None) -> dict[str, Any] | None:
    path = snapshot_path(source, root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pre_edit_path(source: str, root: str | Path | None = None) -> Path:
    return workspace(root) / "pre-edit" / f"{_key(source)}.json"


def warned_path(source: str, root: str | Path | None = None) -> Path:
    """편집 훅이 이 스펙 파일에 대해 이미 알린 breaking change 목록 (같은 경고를 반복하지 않기 위해)."""
    return workspace(root) / "warned" / f"{_key(source)}.json"


def load_warned(source: str, root: str | Path | None = None) -> set[str]:
    try:
        return set(json.loads(warned_path(source, root).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def save_warned(source: str, keys: set[str], root: str | Path | None = None) -> None:
    path = warned_path(source, root)
    if not keys:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys), ensure_ascii=False), encoding="utf-8")


def cases_path(spec_id: str, root: str | Path | None = None) -> Path:
    return workspace(root) / "cases" / f"{spec_id}.json"


def save_cases(spec_id: str, source: str, cases: dict[str, dict[str, Any]], root: str | Path | None = None) -> Path:
    path = cases_path(spec_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"spec_id": spec_id, "source": source, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": cases}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_cases(spec_id: str, root: str | Path | None = None) -> dict[str, Any] | None:
    """{"source": ..., "cases": {case_id: case}} 또는 None."""
    path = cases_path(spec_id, root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report_path(title: str, root: str | Path | None = None) -> Path:
    """.api-test-pilot/reports/YYYY-MM-DD-HHMM-<title>.md. 이미 있으면 -2, -3을 붙인다."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:40] or "api"
    base = workspace(root) / "reports" / f"{time.strftime('%Y-%m-%d-%H%M')}-{slug}"
    path, n = base.with_suffix(".md"), 2
    while path.exists():
        path, n = base.parent / f"{base.name}-{n}.md", n + 1
    return path


def results_path(root: str | Path | None = None) -> Path:
    return workspace(root) / "last_results.json"


def save_results(summary: dict[str, Any], root: str | Path | None = None) -> Path:
    path = results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def save_last_run(summary: dict[str, Any], root: str | Path | None = None) -> Path:
    path = workspace(root) / "last_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "finished_at": time.time(),
        "reported": False,
        "spec": summary.get("spec"),
        "base_url": summary.get("base_url"),
        "total": summary.get("total"),
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "skipped": summary.get("skipped", 0),
        "failed_ids": [r["id"] for r in summary.get("results", []) if r.get("passed") is False],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
