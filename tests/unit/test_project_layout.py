from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
EXPECTED_PATHS = (
    ".gitignore",
    ".env.example",
    "README.md",
    "src/infineq/__init__.py",
    "tests/__init__.py",
)


@pytest.mark.parametrize("relative_path", EXPECTED_PATHS)
def test_required_project_path_exists(relative_path: str) -> None:
    assert (ROOT / relative_path).exists(), f"required project path is missing: {relative_path}"
