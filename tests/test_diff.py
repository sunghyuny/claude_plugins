from api_test_pilot.diff import diff_specs


def _types(changes):
    return {(c["type"], c["location"]) for c in changes}


def test_no_changes(v1):
    result = diff_specs(v1, v1)
    assert result["breaking_count"] == 0 and result["non_breaking_count"] == 0


def test_breaking_changes_detected(v1, v2):
    result = diff_specs(v1, v2)
    breaking = _types(result["breaking"])
    assert ("operation_removed", "GET /owners") in breaking
    assert ("type_changed", "GET /pets/{petId} param 'petId' (path)") in breaking
    assert ("required_param_added", "GET /pets param 'region' (query)") in breaking
    assert ("response_field_removed", "GET /pets/{petId} response 200.tag") in breaking
    assert ("enum_value_removed", "GET /pets param 'status' (query)") in breaking
    assert ("required_field_added", "POST /pets body.species") in breaking


def test_non_breaking_changes_detected(v1, v2):
    non_breaking = _types(diff_specs(v1, v2)["non_breaking"])
    assert ("operation_added", "GET /stores") in non_breaking
    assert ("field_added", "GET /pets response 200[].age") in non_breaking
    # 응답 enum이 줄어드는 건 클라이언트를 깨지 않는다
    assert ("response_enum_value_removed", "GET /pets/{petId} response 200.status") in non_breaking


def test_reverse_direction(v1, v2):
    # v2 → v1: /stores 삭제는 breaking, /owners 추가는 non-breaking
    result = diff_specs(v2, v1)
    assert ("operation_removed", "GET /stores") in _types(result["breaking"])
    assert ("operation_added", "GET /owners") in _types(result["non_breaking"])
    # 반대로 응답 enum에 값이 추가되면 breaking
    assert ("response_enum_value_added", "GET /pets/{petId} response 200.status") in _types(result["breaking"])
