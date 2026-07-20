from __future__ import annotations

from pathlib import Path

import pytest

from scripts.generate_standalone_notices import (
    NoticeGenerationError,
    collect_distribution_notices,
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
        name: str = "demo-package",
        version: str = "1.2.3",
        files: list[str] | None = None,
    ) -> None:
        self.root = root
        self.metadata = FakeMetadata({"Name": name, "License-Expression": "MIT"}, ["LICENSE"])
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
