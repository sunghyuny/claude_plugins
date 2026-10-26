"""Stop 훅: 이번 응답 중에 run_cases가 실행됐으면 결과를 한 줄로 요약해서 보여준다.

.api-test-pilot/last_run.json의 reported 플래그로 같은 결과를 두 번 보여주지 않는다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

FRESH_SECONDS = 60 * 60


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        path = Path(event.get("cwd") or Path.cwd()) / ".api-test-pilot" / "last_run.json"
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("reported") or time.time() - data.get("finished_at", 0) > FRESH_SECONDS:
            return
        data["reported"] = True
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        icon = "✅" if not data.get("failed") else "❌"
        msg = (
            f"{icon} api-test-pilot [{data.get('spec')}] "
            f"통과 {data.get('passed', 0)} / 실패 {data.get('failed', 0)} / 건너뜀 {data.get('skipped', 0)}"
        )
        failed = data.get("failed_ids") or []
        if failed:
            msg += " — 실패: " + ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else "")
        sys.stdout.write(json.dumps({"systemMessage": msg}))  # ASCII 이스케이프: 콘솔 인코딩과 무관
    except Exception:
        return


if __name__ == "__main__":
    main()
