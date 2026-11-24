"""Docling's PDF conversion as ``DoclingPdfTextReader`` runs it; shared by the Docling
text reader and the Docling table extractor.

The converter runs offline from a local models folder and never downloads.
Docling is imported only when a converter is built.
"""

import warnings
from io import BytesIO
from pathlib import Path
from typing import Any


def pdf_converter(models_path: Path) -> Any:
    """Docling's PDF converter, offline from ``models_path``.

    Layout and TableFormer models run; OCR, picture classification and
    description, page images, and remote services are off. The models load
    here. Raises ImportError without Docling, and ValueError when
    ``models_path`` holds no usable models.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        artifacts_path=models_path,
        do_ocr=False,
        do_table_structure=True,
        do_picture_classification=False,
        do_picture_description=False,
        generate_page_images=False,
        generate_picture_images=False,
        enable_remote_services=False,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    try:
        converter.initialize_pipeline(InputFormat.PDF)
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as error:
        raise ValueError(
            "models_path does not contain usable Docling layout and table models; "
            "download them with docling.utils.model_downloader.download_models"
        ) from error
    return converter


def decrypted(data: bytes, password: str | None) -> bytes | tuple[str, str]:
    """The PDF as Docling should get it: an encrypted one opened with ``password``
    (from the ``Artifact``) and saved without encryption.

    On failure, a finding code shared by the text readers and table extractors
    (``no_pages``, ``password_required``, ``invalid_password``,
    ``decode_failed``) and its message.
    """
    import pikepdf

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with pikepdf.Pdf.open(BytesIO(data), password=password or "") as document:
                if not len(document.pages):
                    return "no_pages", "PDF contains no usable pages."
                if not document.is_encrypted:
                    return data
                with BytesIO() as output:
                    document.save(output)
                    return output.getvalue()
    except pikepdf.PasswordError:
        code = "invalid_password" if password else "password_required"
        return code, "PDF could not be opened with the supplied password."
    except (pikepdf.PdfError, ValueError, OSError):
        return "decode_failed", "PDF could not be opened."
