from api_test_pilot.cases import generate_cases, sample_value


def _by_id(cases):
    return {c["id"]: c for c in cases}


def test_reads_only_by_default(v1):
    cases = generate_cases(v1)
    assert all(c["method"] == "GET" for c in cases)
    assert any(c["writes"] for c in generate_cases(v1, include_writes=True))


def test_case_kinds_for_get_pet(v1):
    cases = _by_id(generate_cases(v1, operation_id="getPet"))
    assert cases["getPet:happy"]["path_params"] == {"petId": 7}  # example 값 사용
    assert cases["getPet:happy"]["expected_status"] == [200]
    assert cases["getPet:not_found"]["expected_status"] == [404]
    assert cases["getPet:invalid_type"]["path_params"]["petId"] == "abc"
    assert 422 in cases["getPet:invalid_type"]["expected_status"]


def test_missing_required_query(v1):
    cases = _by_id(generate_cases(v1, operation_id="listPets"))
    assert cases["listPets:happy"]["query"] == {"status": "available"}  # enum 첫 값, 선택 파라미터는 생략
    assert "status" not in cases["listPets:missing_required"]["query"]
    assert 400 in cases["listPets:missing_required"]["expected_status"]


def test_body_cases(v1):
    cases = _by_id(generate_cases(v1, operation_id="createPet"))
    assert cases["createPet:happy"]["body"] == {"name": "Tom", "tag": "string"}
    assert cases["createPet:missing_required"]["body"] is None


def test_non_json_body_is_skipped(v1):
    cases = generate_cases(v1, operation_id="uploadPhoto")
    assert len(cases) == 1 and "octet-stream" in cases[0]["skip"]


def test_tag_filter(v1):
    ops = {c["operation_id"] for c in generate_cases(v1, tag="pets", include_writes=True)}
    assert ops == {"listPets", "createPet", "getPet"}


def test_sample_value_formats():
    assert sample_value({"type": "string", "format": "email"}) == "user@example.com"
    assert sample_value({"type": "integer", "minimum": 5}) == 5
    assert sample_value({"type": ["null", "boolean"]}) is True
    assert sample_value({"type": "array", "items": {"type": "integer"}}) == [1]
    assert sample_value({"type": "object", "properties": {"id": {"type": "integer", "readOnly": True}}}) == {}
    assert sample_value({"oneOf": [{"type": "string"}, {"type": "integer"}]}) == "string"
