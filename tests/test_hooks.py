import io
import json
import shutil
import time

import on_spec_edit
import on_stop
import pytest

from api_test_pilot import store


def _run(module, monkeypatch, capsys, event, *argv):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr("sys.argv", ["hook", *argv])
    module.main()
    out = capsys.readouterr().out
    return json.loads(out) if out else None


@pytest.fixture
def project(tmp_path, v1_path):
    spec = tmp_path / "openapi.yaml"
    shutil.copy(v1_path, spec)
    return tmp_path, spec


def _event(root, spec):
    return {"cwd": str(root), "tool_name": "Edit", "tool_input": {"file_path": str(spec)}}


def test_breaking_edit_warns(project, v2_path, monkeypatch, capsys):
    root, spec = project
    assert _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "pre") is None
    assert store.pre_edit_path(str(spec.resolve()), root).is_file()

    shutil.copy(v2_path, spec)  # Claude가 편집했다고 가정
    out = _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "post")
    assert "breaking change" in out["systemMessage"]
    assert "operation_removed" in out["hookSpecificOutput"]["additionalContext"]
    assert not store.pre_edit_path(str(spec.resolve()), root).exists()  # 사본은 정리된다


def test_non_breaking_edit_is_silent(project, monkeypatch, capsys):
    root, spec = project
    _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "pre")
    spec.write_text(spec.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 1.0.1"), encoding="utf-8")
    assert _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "post") is None


def test_broken_structure_blocks(project, monkeypatch, capsys):
    root, spec = project
    _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "pre")
    spec.write_text("openapi: 3.0.0\ninfo: {title: x}\npaths: {}\n", encoding="utf-8")
    out = _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "post")
    assert out["decision"] == "block" and "info.version" in out["reason"]


def test_snapshot_is_preferred_baseline(project, v1, v2_path, monkeypatch, capsys):
    root, spec = project
    store.save_snapshot(str(spec.resolve()), v1.raw, root)
    shutil.copy(v2_path, spec)  # pre 훅 없이 바뀐 경우에도 스냅샷과 비교한다
    out = _run(on_spec_edit, monkeypatch, capsys, _event(root, spec), "post")
    assert "스냅샷" in out["systemMessage"]


def test_non_spec_files_are_ignored(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "package.json"
    pkg.write_text('{"name": "x"}', encoding="utf-8")
    event = _event(tmp_path, pkg)
    assert _run(on_spec_edit, monkeypatch, capsys, event, "pre") is None
    assert _run(on_spec_edit, monkeypatch, capsys, event, "post") is None
    assert _run(on_spec_edit, monkeypatch, capsys, _event(tmp_path, tmp_path / "main.py"), "post") is None


def test_stop_reports_once(tmp_path, monkeypatch, capsys):
    store.save_last_run({"spec": "Mini", "total": 2, "passed": 1, "failed": 1,
                         "results": [{"id": "getPet:happy", "passed": False}]}, tmp_path)
    event = {"cwd": str(tmp_path)}
    out = _run(on_stop, monkeypatch, capsys, event)
    assert "실패 1" in out["systemMessage"] and "getPet:happy" in out["systemMessage"]
    assert _run(on_stop, monkeypatch, capsys, event) is None


def test_stop_ignores_stale_results(tmp_path, monkeypatch, capsys):
    path = store.save_last_run({"spec": "Mini", "total": 0, "passed": 0, "failed": 0}, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["finished_at"] = time.time() - 2 * 60 * 60
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _run(on_stop, monkeypatch, capsys, {"cwd": str(tmp_path)}) is None
