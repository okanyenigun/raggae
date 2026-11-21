"""Transcribe PDF pages with the Granite-Docling vision-language model, offline."""

import re
from pathlib import Path
from typing import Any
from .docling import DoclingPdfTextReader
from ..models import FindingCode, TextBlock, TextReadFinding, TextReadMethod


class GraniteDoclingTextReader(DoclingPdfTextReader):
    """Return one block per document element that Granite-Docling reads from page images.

    Docling's VLM pipeline renders each PDF page and asks IBM's
    ``granite-docling-258M`` model (Apache-2.0) to transcribe it into Docling's
    document structure, through Hugging Face Transformers. ``model_repo`` names
    the Hugging Face repository of the model (default that one); it must speak
    the same DocTags format as Granite-Docling. Stored PDF text is
    not used, so every block has ``method="vision"``; otherwise blocks follow
    ``DoclingPdfTextReader`` (one per element, tables as Markdown, every content
    layer, ``page_index`` from provenance). A page the model did not finish
    (output cut short or inference failed) contributes no blocks and gets a
    ``page_incomplete`` finding. ``models_path`` must contain the model in a
    folder named after the repository with ``/`` replaced by ``--`` (for the
    default, ``ibm-granite--granite-docling-258M``); it is never downloaded. Layout
    regions are unsupported. Requires the opt-in ``raggae[text-docling]``
    extra.
    """

    _method: TextReadMethod = "vision"
    _pictures_message = (
        "This page has pictures; the model located them but did not transcribe "
        "their contents."
    )
    _empty_message = "The model found no text on this page."

    def __init__(
        self,
        *,
        models_path: Path | str,
        model_repo: str = "ibm-granite/granite-docling-258M",
    ) -> None:
        if not isinstance(models_path, (Path, str)):
            raise TypeError("models_path must be a path to local Docling VLM models")
        if not isinstance(model_repo, str):
            raise TypeError("model_repo must be a string such as 'owner/name'")
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", model_repo):
            raise ValueError("model_repo must look like 'owner/name'")
        path = Path(models_path).expanduser()
        # Docling looks for the model in this folder of models_path.
        folder = model_repo.replace("/", "--")
        if not (path / folder / "config.json").is_file():
            raise ValueError(
                f"models_path must contain {folder} (the {model_repo} model); "
                "download it yourself, it is never downloaded automatically."
            )
        self._model_repo = model_repo
        super().__init__(models_path=path)

    @property
    def name(self) -> str:
        return "text_reader_granite_docling"

    def _load(self) -> Any:
        """Build Docling's VLM converter once per reader; the model loads here."""
        if self._converter is not None:
            return self._converter
        try:
            from docling.datamodel import vlm_model_specs
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import VlmPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.pipeline.vlm_pipeline import VlmPipeline
        except ImportError as error:
            raise ImportError(
                "Granite-Docling reading requires: pip install 'raggae[text-docling]'"
            ) from error
        vlm_options = vlm_model_specs.GRANITEDOCLING_TRANSFORMERS.model_copy(deep=True)
        vlm_options.repo_id = self._model_repo
        options = VlmPipelineOptions(
            artifacts_path=self._models_path,
            vlm_options=vlm_options,
            force_backend_text=False,
            generate_picture_images=False,
            enable_remote_services=False,
        )
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=VlmPipeline, pipeline_options=options
                )
            },
        )
        try:
            converter.initialize_pipeline(InputFormat.PDF)
        except (OSError, RuntimeError, ValueError, KeyError, ImportError) as error:
            raise ValueError(
                f"models_path does not contain a usable {self._model_repo} model."
            ) from error
        self._converter = converter
        return converter

    def _elements(self, result: Any) -> tuple[list[TextBlock], list[TextReadFinding]]:
        """Drop pages the model did not finish; report each one."""
        blocks, findings = self._document_elements(result.document)
        unfinished = sorted(
            {error.page_no - 1 for error in result.errors if error.page_no is not None}
        )
        if not unfinished and not result.errors:
            return blocks, findings
        blocks = [block for block in blocks if block.page_index not in unfinished]
        findings = [
            finding
            for finding in findings
            if finding.page_index is None or finding.page_index not in unfinished
        ]
        findings.extend(
            self._finding(
                FindingCode.PAGE_INCOMPLETE,
                "The model did not finish this page (output cut short or inference "
                "failed); its text was left out.",
                page_index=page,
            )
            for page in unfinished
        )
        if any(error.page_no is None for error in result.errors):
            findings.append(
                self._finding(
                    FindingCode.CONVERSION_PARTIAL,
                    "Docling reported errors on part of the document; some content may be missing.",
                )
            )
        findings.sort(key=lambda f: (f.page_index is None, f.page_index or 0))
        return blocks, findings
