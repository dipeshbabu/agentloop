from __future__ import annotations

from pathlib import Path

import pytest

import scripts.generate_standalone_notices as notices_module
from scripts.generate_standalone_notices import (
    NoticeGenerationError,
    collect_distribution_notices,
    generate_notice_file,
    render_notices,
)


class FakeMetadata(dict[str, str]):
    def __init__(self, values: dict[str, str], license_files: list[str]) -> None:
        super().__init__(values)
        self._license_files = license_files

    def get_all(self, key: str) -> list[str] | None:
        return self._license_files if key == "License-File" else None


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str | None = "demo-package",
        version: str | None = "1.2.3",
        files: list[str] | None = None,
        license_files: list[str] | None = None,
    ) -> None:
        self.root = root
        values = {"License-Expression": "MIT"}
        if name is not None:
            values["Name"] = name
        self.metadata = FakeMetadata(values, license_files or ["LICENSE"])
        self.version = version
        self.files = files

    def locate_file(self, path: object) -> Path:
        return self.root / str(path)


def test_notice_document_contains_exact_versions_and_license_text(tmp_path: Path) -> None:
    relative_license = "demo_package-1.2.3.dist-info/licenses/LICENSE"
    license_path = tmp_path / relative_license
    license_path.parent.mkdir(parents=True)
    license_path.write_text("complete demo license", encoding="utf-8")
    notices = collect_distribution_notices([FakeDistribution(tmp_path, files=[relative_license])])

    document = render_notices(
        target="linux-x86_64",
        python_version="3.13.5",
        python_license="complete CPython license",
        distributions=notices,
    )

    assert "Target: linux-x86_64" in document
    assert "CPython 3.13.5" in document
    assert "complete CPython license" in document
    assert "demo-package 1.2.3" in document
    assert "Declared license: MIT" in document
    assert "complete demo license" in document


def test_notice_generation_fails_when_distribution_has_no_license_file(
    tmp_path: Path,
) -> None:
    distribution = FakeDistribution(tmp_path, files=["demo_package/__init__.py"])

    with pytest.raises(
        NoticeGenerationError,
        match=r"No license or attribution file found for demo-package==1\.2\.3",
    ):
        collect_distribution_notices([distribution])


def test_notice_order_is_stable_for_reversed_packages_and_files(tmp_path: Path) -> None:
    paths = {
        "alpha-1.0.dist-info/licenses/LICENSE": "alpha license",
        "zulu-2.0.dist-info/licenses/NOTICE": "zulu notice",
        "zulu-2.0.dist-info/licenses/LICENSE": "zulu license",
    }
    for relative_path, content in paths.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    alpha = FakeDistribution(
        tmp_path,
        name="alpha",
        version="1.0",
        files=["alpha-1.0.dist-info/licenses/LICENSE"],
    )
    zulu_reversed = FakeDistribution(
        tmp_path,
        name="zulu",
        version="2.0",
        files=[
            "zulu-2.0.dist-info/licenses/NOTICE",
            "zulu-2.0.dist-info/licenses/LICENSE",
        ],
        license_files=["NOTICE", "LICENSE"],
    )
    zulu_forward = FakeDistribution(
        tmp_path,
        name="zulu",
        version="2.0",
        files=[
            "zulu-2.0.dist-info/licenses/LICENSE",
            "zulu-2.0.dist-info/licenses/NOTICE",
        ],
        license_files=["LICENSE", "NOTICE"],
    )

    reversed_notices = collect_distribution_notices([zulu_reversed, alpha])
    forward_notices = collect_distribution_notices([alpha, zulu_forward])

    assert reversed_notices == forward_notices
    document = render_notices(
        target="linux-x86_64",
        python_version="3.13.5",
        python_license="CPython license",
        distributions=reversed_notices,
    )
    forward_document = render_notices(
        target="linux-x86_64",
        python_version="3.13.5",
        python_license="CPython license",
        distributions=forward_notices,
    )
    assert document == forward_document
    assert document.index("alpha 1.0") < document.index("zulu 2.0")
    assert document.index("zulu-2.0.dist-info/licenses/LICENSE") < document.index(
        "zulu-2.0.dist-info/licenses/NOTICE"
    )


@pytest.mark.parametrize(
    ("name", "version"),
    [(None, "1.0"), ("", "1.0"), ("demo", None), ("demo", "")],
)
def test_notice_generation_rejects_missing_or_empty_name_and_version(
    tmp_path: Path,
    name: str | None,
    version: str | None,
) -> None:
    distribution = FakeDistribution(tmp_path, name=name, version=version, files=[])

    with pytest.raises(
        NoticeGenerationError,
        match="Installed distribution is missing its name or version",
    ):
        collect_distribution_notices([distribution])


def test_empty_distribution_input_renders_cpython_notice_only() -> None:
    notices = collect_distribution_notices([])

    assert notices == ()
    document = render_notices(
        target="linux-x86_64",
        python_version="3.13.5",
        python_license="CPython license",
        distributions=notices,
    )
    assert "CPython 3.13.5" in document
    assert "Declared license:" not in document


def test_generate_notice_file_writes_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python_license = tmp_path / "PYTHON-LICENSE"
    python_license.write_text("complete CPython license", encoding="utf-8")
    relative_license = "demo-1.0.dist-info/licenses/LICENSE"
    package_license = tmp_path / relative_license
    package_license.parent.mkdir(parents=True)
    package_license.write_text("complete package license", encoding="utf-8")
    distribution = FakeDistribution(
        tmp_path,
        name="demo",
        version="1.0",
        files=[relative_license],
    )
    monkeypatch.setattr(notices_module, "_python_license_path", lambda: python_license)
    monkeypatch.setattr(notices_module.metadata, "distributions", lambda: [distribution])
    output = tmp_path / "nested" / "THIRD_PARTY_NOTICES-test.txt"

    generate_notice_file("test-target", output)

    document = output.read_text(encoding="utf-8")
    assert document.startswith("AgentLoop standalone third-party notices\n")
    assert "Target: test-target" in document
    assert "complete CPython license" in document
    assert "demo 1.0" in document
    assert "complete package license" in document
    assert document.endswith("\n")
