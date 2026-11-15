from pathlib import Path

import pytest

import unfurl  # noqa: F401 -- puts the vendored toscaparser on the path
from toscaparser.utils.validateutils import validate_version

# The Rust solver checks `version` constraints against the same table
# (`version_constraints_agree_with_python` in rust/solver/src/topology.rs).
TABLE = Path(__file__).parent.parent / "rust/solver/tests/version_requirements.tsv"


def _cases():
    for line in TABLE.read_text().splitlines():
        if line and not line.startswith("#"):
            requirement, version, expected = line.split("\t")
            yield pytest.param(requirement, version, expected == "yes", id=f"{requirement} {version}")


@pytest.mark.parametrize("requirement, version, expected", list(_cases()))
def test_version_requirements_agree_with_the_solver(requirement, version, expected):
    assert validate_version(requirement, version) == expected
