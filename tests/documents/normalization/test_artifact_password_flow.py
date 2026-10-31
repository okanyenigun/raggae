"""Credentials follow the actual artifact through a chain and publication."""

from typing import Literal

from pydantic import BaseModel
import pytest

from raggae.documents.normalization import (
    Artifact, DocumentNormalizer, NormalizationRequest, NormalizationResult, PdfDecryptionNormalizer,
)
from .test_pdf_access import encoded_pdf


class Report(BaseModel):
    outcome: Literal["changed", "no_change"]


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("intermediate", [False, True])
def test_decryption_chain_does_not_forward_original_password(disk, intermediate, tmp_path):
    seen = []
    class Observer:
        name = "observer"
        def normalize(self, request):
            seen.append(request.source)
            return NormalizationResult(artifact=request.source, report=Report(outcome="no_change"))
    source = Artifact(data=encoded_pdf(user="original-secret"), password="original-secret")
    request = NormalizationRequest(source=source, output_path=tmp_path / "final.pdf" if disk else None)
    result = DocumentNormalizer(
        workers=(PdfDecryptionNormalizer(), Observer()),
        intermediate_dir=tmp_path if intermediate else None,
    ).normalize(request)
    assert result.outcome == "changed"
    assert source.password == "original-secret"
    assert seen[0].password is None
    assert result.artifact.password is None
    assert "original-secret" not in repr(result)


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("intermediate", [False, True])
def test_explicit_output_password_survives_rename_and_final_copy(disk, intermediate, tmp_path):
    class Replacement:
        name = "replace-copy"
        def normalize(self, request):
            if request.output_path is None:
                artifact = Artifact(data=b"new copy", password="new-secret")
            else:
                request.output_path.write_bytes(b"new copy")
                artifact = Artifact(path=request.output_path, password="new-secret")
            return NormalizationResult(artifact=artifact, report=Report(outcome="changed"))
    original = Artifact(data=b"original", password="old-secret")
    result = DocumentNormalizer(
        workers=(Replacement(),), intermediate_dir=tmp_path if intermediate else None,
    ).normalize(source=original, source_format="pdf", output_path=tmp_path / "final.pdf" if disk else None)
    assert result.artifact.read_bytes() == b"new copy"
    assert result.artifact.password == "new-secret"
    assert original.password == "old-secret"


def test_request_no_longer_accepts_its_own_password():
    with pytest.raises(TypeError, match="password"):
        NormalizationRequest(source=b"data", output_path=None, password="secret")
