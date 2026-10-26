from pathlib import Path

import pytest

from api_test_pilot.spec import Spec

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def v1_path() -> str:
    return str(FIXTURES / "petstore_v1.yaml")


@pytest.fixture
def v2_path() -> str:
    return str(FIXTURES / "petstore_v2_breaking.yaml")


@pytest.fixture
def v1(v1_path) -> Spec:
    return Spec.load(v1_path)


@pytest.fixture
def v2(v2_path) -> Spec:
    return Spec.load(v2_path)
