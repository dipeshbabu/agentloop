import zipfile

import pytest

from scripts.lowest_requirements import lowest_pins


def _wheel(tmp_path, requires):
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    metadata = "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n" + "".join(
        f"Requires-Dist: {line}\n" for line in requires
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo-1.0.dist-info/METADATA", metadata)
    return str(path)


def test_core_requirements_pin_to_their_floor_and_extras_are_skipped(tmp_path):
    wheel = _wheel(
        tmp_path,
        ["typer>=0.16.0", "rich >= 13.0.0", 'streamlit>=1.34.0; extra == "dashboard"'],
    )
    assert lowest_pins(wheel) == ["typer==0.16.0", "rich==13.0.0"]


@pytest.mark.parametrize("requirement", ["typer", "typer>=0.16,<1", "typer~=0.16"])
def test_requirements_without_a_plain_floor_are_rejected(tmp_path, requirement):
    with pytest.raises(SystemExit, match="plain floor"):
        lowest_pins(_wheel(tmp_path, [requirement]))


def test_a_wheel_without_core_requirements_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="no core requirements"):
        lowest_pins(_wheel(tmp_path, ['rich>=13; extra == "x"']))
