"""Run an explicitly configured local jq filter and adapt its JSON results."""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from .utils import failure, load_document, loads_json, prepare_source, require_encoding
from ..models import (
    FindingCode,
    TextBlock,
    TextContentFormat,
    TextReadFinding,
    TextReadResult,
)
from ...layout_analysis.models import LayoutAnalysisResult
from ...normalization.artifacts import Artifact
from ...normalization.models import TextEncoding


class JqJsonTextReader:
    """Return one block per result of a trusted, application-supplied jq filter.

    Invoke the local executable without a shell or temporary files. Filters are
    configuration, never inferred from source content. This is not a sandbox for
    untrusted filters, and numeric transformations follow the installed engine.
    """

    def __init__(
        self,
        expression: str,
        *,
        encoding: TextEncoding = "utf-8",
        executable: Path | str | None = None,
    ) -> None:
        self._encoding: TextEncoding = require_encoding(encoding)
        if not isinstance(expression, str):
            raise TypeError("expression must be a string")
        if not expression.strip():
            raise ValueError("expression cannot be blank")
        if executable is not None and not isinstance(executable, (Path, str)):
            raise TypeError("executable must be a Path, string, or None")
        if executable is not None and not str(executable).strip():
            raise ValueError("executable cannot be blank")
        self._expression = expression
        self._executable = "jq" if executable is None else str(executable)

    @property
    def name(self) -> str:
        return "text_reader_jq_json"

    def read(
        self,
        source: Path | str | bytes | Artifact,
        *,
        layout: LayoutAnalysisResult | None = None,
        region_ids: Sequence[str] | None = None,
    ) -> TextReadResult:
        prepared = prepare_source(
            source,
            reader=self.name,
            layout=layout,
            region_ids=region_ids,
        )
        if isinstance(prepared, TextReadResult):
            return prepared
        loaded = load_document(prepared, encoding=self._encoding, reader=self.name)
        if isinstance(loaded, TextReadResult):
            return loaded
        text, _ = loaded
        executable = shutil.which(self._executable)
        if executable is None:
            raise FileNotFoundError(
                "JSON query reading requires a local jq executable. Install jq or configure executable=..."
            )
        try:
            process = subprocess.run(
                [
                    executable,
                    "--compact-output",
                    "--monochrome-output",
                    "--ascii-output",
                    "--",
                    self._expression,
                ],
                input=text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="strict",
                shell=False,
                check=False,
            )
        except (OSError, UnicodeError):
            return failure(
                self.name,
                FindingCode.QUERY_FAILED,
                "The local jq process could not complete.",
                page_index=0,
            )
        if process.returncode == 3:
            raise ValueError("The jq expression could not be compiled.")
        if process.returncode != 0:
            return failure(
                self.name,
                FindingCode.QUERY_FAILED,
                "The jq query failed; no query results were returned.",
                page_index=0,
            )
        blocks = []
        try:
            # Split LF framing, not str.splitlines(): JSON strings may contain
            # literal Unicode line-separator characters.
            for line in process.stdout.split("\n"):
                if line.strip():
                    loads_json(line)
                    blocks.append(
                        TextBlock(
                            text=line.strip(),
                            page_index=0,
                            content_format=TextContentFormat.JSON,
                        )
                    )
        except (ValueError, RecursionError):
            return failure(
                self.name,
                FindingCode.QUERY_FAILED,
                "The jq process returned invalid JSON results.",
                page_index=0,
            )
        return TextReadResult(
            reader=self.name,
            outcome="success",
            blocks=tuple(blocks),
            findings=()
            if blocks
            else (
                TextReadFinding(
                    code=FindingCode.EMPTY,
                    message="The jq query produced no results.",
                    page_index=0,
                ),
            ),
        )
