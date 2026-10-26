#!/bin/sh
# 훅 입구. uv(파이썬 환경, 약 0.4초)를 띄우기 전에 셸에서 대상인지 먼저 거른다.
#   pre|post : 편집 대상이 .json/.yaml/.yml일 때만 on_spec_edit.py 실행
#   stop     : 이 프로젝트에 테스트 결과(last_run.json)가 있을 때만 on_stop.py 실행
# 거르는 기준은 넉넉하게 잡는다. 애매하면 파이썬 쪽으로 넘기고, 최종 판단은 파이썬 스크립트가 한다.

mode="$1"
input=$(cat)

run() {
    printf '%s' "$input" | uv run --quiet --project "$CLAUDE_PLUGIN_ROOT/server" python "$CLAUDE_PLUGIN_ROOT/scripts/$1" $2
}

case "$mode" in
    pre|post)
        case "$input" in
            *'.json"'* | *'.yaml"'* | *'.yml"'* | *'.JSON"'* | *'.YAML"'* | *'.YML"'*) run on_spec_edit.py "$mode" ;;
        esac
        ;;
    stop)
        if [ -f "$PWD/.api-test-pilot/last_run.json" ]; then
            run on_stop.py
        fi
        ;;
esac
exit 0
