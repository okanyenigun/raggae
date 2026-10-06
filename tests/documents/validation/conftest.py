"""Small, deterministic fixtures shared by the validation tests."""

import pytest
from collections.abc import Callable
from pathlib import Path
from raggae.documents.validation.schemas.result import Finding


@pytest.fixture
def file_factory(tmp_path: Path) -> Callable[..., Path]:
    def make_file(
        name: str = "document.txt", content: bytes = b"Hello, document!"
    ) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    return make_file


@pytest.fixture
def missing_path(tmp_path: Path) -> Path:
    return tmp_path / "missing-document.txt"


@pytest.fixture
def scalar_facts() -> dict:
    return {"text": "hello", "integer": 7, "decimal": 1.5, "flag": True, "empty": None}


@pytest.fixture
def finding_factory() -> Callable[..., Finding]:
    def make_finding(code: str = "test.example", **detail) -> Finding:
        return Finding(code=code, message=f"Example for {code}.", detail=detail)

    return make_finding


@pytest.fixture
def forbidden_operation() -> Callable[..., None]:
    def fail_if_called(*args, **kwargs) -> None:
        pytest.fail("A forbidden I/O, parser, or decoding operation was called.")

    return fail_if_called


@pytest.fixture
def pdf_factory(tmp_path: Path) -> Callable[..., Path]:
    """Build tiny real PDFs; no rendering or external PDF application is used."""
    import pikepdf

    def make_pdf(name="document.pdf", *, pages=1, page_size=(72, 72), encryption=None):
        path = tmp_path / name
        with pikepdf.Pdf.new() as document:
            for _ in range(pages):
                document.add_blank_page(page_size=page_size)
            document.save(path, encryption=encryption)
        return path

    return make_pdf
