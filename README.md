<p align="center">
  <img src="docs/assets/logo.png" alt="raggae logo" width="240">
</p>

# raggae

A Python SDK for RAG document understanding and retrieval.

Requires Python 3.13 or later.

## Installation

```bash
pip install raggae
```

## Document normalization

Use a worker on its own, or let `DocumentNormalizer` select a sequence from a
file's extension. Originals are never overwritten.

```python
from raggae.documents.normalization import StrictTextEncodingNormalizer

result = StrictTextEncodingNormalizer().normalize(
    source=b"Caf\xe9",
    output_path=None,  # Keep the changed copy in memory.
    declared_encoding="cp1252",
)
if result.artifact is not None:
    print(result.artifact.read_bytes().decode("utf-8"))  # Café
```

For a file-based flow:

```python
from raggae.documents.normalization import DocumentNormalizer

result = DocumentNormalizer().normalize(
    source="uploaded.pdf",
    output_path="prepared.pdf",  # A new path whose parent folder already exists.
)
print(result.outcome)  # changed, no_change, or failed
for step in result.report.steps:
    print(step.worker, step.report.outcome)
```

Default sequences include format-specific active-content and metadata cleanup;
pass `workers=(...)` to choose your own changes. A password-required PDF also
needs `password=...`. In-memory inputs need `source_format=...` when using the
default sequence. A `no_change` result returns the original artifact without
creating an output file; a `failed` result has no artifact.

Workers cover text encoding, image orientation, PDF decryption, selected
active-content and metadata removal, and explicit format conversion. The
`DocumentToHtmlConverter` requires a separately installed Pandoc, and
`LibreOfficeToPdfConverter` requires LibreOffice. Conversion does not perform
OCR, and sanitization is not a guarantee that a document is safe for every use.

For development checks and release setup, see [CI/CD documentation](docs/ci-cd.md).
