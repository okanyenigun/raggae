"""One structural interface, with precise reports on concrete workers."""

from typing import get_args, get_origin, get_overloads, get_protocol_members, get_type_hints

import pytest

from raggae.documents import normalization
from raggae.documents.normalization import (
    CsvFormulaReport,
    CsvFormulaSanitizer,
    DocumentToHtmlReport,
    HtmlActiveContentReport,
    HtmlActiveContentSanitizer,
    HtmlMetadataReport,
    HtmlMetadataSanitizer,
    ImageMetadataReport,
    ImageMetadataSanitizer,
    ImageOrientationReport,
    ImageToPdfReport,
    LibreOfficeToPdfConverter,
    NormalizationRequest,
    NormalizationResult,
    NormalizationWorker,
    OfficeToPdfReport,
    OoxmlActiveContentReport,
    OoxmlActiveContentSanitizer,
    OoxmlMetadataReport,
    OoxmlMetadataSanitizer,
    DocumentToHtmlConverter,
    PdfAccessReport,
    PdfActiveContentReport,
    PdfActiveContentSanitizer,
    PdfMetadataReport,
    PdfMetadataSanitizer,
    PdfDecryptionNormalizer,
    PillowImageOrientationNormalizer,
    PillowImageToPdfConverter,
    StrictTextEncodingNormalizer,
    TextEncodingReport,
)
from raggae.documents.normalization.base import NormalizationWorker as BaseWorker


@pytest.mark.parametrize("worker_type,report_type", [
    (StrictTextEncodingNormalizer, TextEncodingReport),
    (PillowImageOrientationNormalizer, ImageOrientationReport),
    (PdfDecryptionNormalizer, PdfAccessReport),
    (PdfActiveContentSanitizer, PdfActiveContentReport),
    (OoxmlActiveContentSanitizer, OoxmlActiveContentReport),
    (HtmlActiveContentSanitizer, HtmlActiveContentReport),
    (CsvFormulaSanitizer, CsvFormulaReport),
    (PdfMetadataSanitizer, PdfMetadataReport),
    (OoxmlMetadataSanitizer, OoxmlMetadataReport),
    (HtmlMetadataSanitizer, HtmlMetadataReport),
    (ImageMetadataSanitizer, ImageMetadataReport),
    (PillowImageToPdfConverter, ImageToPdfReport),
    (DocumentToHtmlConverter, DocumentToHtmlReport),
    (LibreOfficeToPdfConverter, OfficeToPdfReport),
])
def test_every_worker_matches_shared_protocol_and_keeps_precise_report(
    worker_type, report_type
):
    worker = worker_type()
    assert isinstance(worker, NormalizationWorker)
    assert NormalizationWorker not in worker_type.__mro__
    # Every built-in worker exposes both public call forms. The request overload
    # still satisfies the shared protocol used by coordinators and custom workers.
    overloads = get_overloads(worker_type.normalize)
    assert len(overloads) == 2
    annotations = get_type_hints(overloads[0])
    assert annotations["request"] is NormalizationRequest
    assert get_origin(annotations["return"]) is NormalizationResult
    assert get_args(annotations["return"]) == (report_type,)


def test_shared_protocol_only_requires_name_and_normalize():
    assert get_protocol_members(NormalizationWorker) == {"name", "normalize"}
    annotations = get_type_hints(NormalizationWorker.normalize)
    assert annotations["request"] is NormalizationRequest
    assert annotations["return"] is NormalizationResult


def test_custom_worker_needs_no_inheritance_or_extra_metadata():
    class CustomWorker:
        name = "custom_text_worker"

        def normalize(
            self, request: NormalizationRequest
        ) -> NormalizationResult[TextEncodingReport]:
            return NormalizationResult(
                artifact=request.source,
                report=TextEncodingReport(
                    outcome="no_change", source_encoding="utf-8"
                ),
            )

    worker: NormalizationWorker = CustomWorker()
    assert isinstance(worker, NormalizationWorker)
    request = NormalizationRequest(source=b"Already UTF-8", output_path=None)
    result = worker.normalize(request)
    assert result.artifact is request.source
    assert result.outcome == "no_change"


def test_objects_missing_required_members_do_not_match():
    class NameOnly:
        name = "incomplete"

    class NormalizeOnly:
        def normalize(self, request):
            raise NotImplementedError

    assert not isinstance(NameOnly(), NormalizationWorker)
    assert not isinstance(NormalizeOnly(), NormalizationWorker)
    assert not isinstance(object(), NormalizationWorker)


def test_shared_protocol_is_exported_and_all_exports_resolve():
    assert NormalizationWorker is BaseWorker
    assert "NormalizationWorker" in normalization.__all__
    assert len(normalization.__all__) == len(set(normalization.__all__))
    for name in normalization.__all__:
        assert hasattr(normalization, name)
