<p align="center">
  <img src="https://raw.githubusercontent.com/okanyenigun/raggae/master/docs/assets/logo.png" alt="raggae logo" width="240">
</p>

# raggae

Raggae is a modular Python SDK for building retrieval-augmented generation (RAG)
pipelines: turning source material into useful evidence for language models.
Its scope is the full RAG workflow, from document ingestion to retrieval and
answer generation.

Use individual workers when you need one operation, or use a stage coordinator
to run related workers together. Policies let you configure limits and behavior,
and structured results expose what happened without requiring you to parse logs.

## Installation

```bash
pip install raggae
```

## Available capabilities

| Stage | What it does today | Coordinator |
|---|---|---|
| Validation | Checks filenames, file identity, content signatures, PDF encryption, format-specific resource limits, active content, and PDF reading capability. Returns a decision with findings and a check history. | `DocumentInspector` |
| Normalization | Converts text encodings, applies image orientation, decrypts PDFs with a supplied password, removes selected active content and metadata, and explicitly converts supported formats. | `DocumentNormalizer` |
| Layout analysis | Locates document regions, suggests geometric reading order, identifies containment and caption candidates, and detects repeated PDF headers and footers. | `DocumentLayoutAnalyzer` |

Layout discovery supports PDF, Word (`.docx`), PowerPoint (`.pptx`), Excel
(`.xlsx`), and HTML. Model-based discovery supports PDFs and images through an
injected local detection service, with Paddle and Docling adapters available.

Document utilities also prepare annotated images showing region boxes and
reading-order arrows. You decide whether to display or save those images.

## Quick start

### Inspect a document

```python
from raggae.documents.validation import DocumentInspector

report = DocumentInspector().validate("uploaded.pdf")

print(report.decision)  # accept, accept_with_warnings, needs_password, or reject
print(report.accepted)
for item in report.findings:
    print(item.finding.code, item.finding.message)
```

The inspector combines the default checks. Workers and policies are also
importable directly from `raggae.documents.validation` for custom assemblies
or isolated use.

### Normalize a document

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
needs `source=Artifact(path="protected.pdf", password="...")` (import `Artifact`
from `raggae.documents.normalization`). In-memory inputs need `source_format=...` when using the
default sequence. A `no_change` result returns the original artifact without
creating an output file; a `failed` result has no artifact.

Workers cover text encoding, image orientation, PDF decryption, selected
active-content and metadata removal, and explicit format conversion. The
`DocumentToHtmlConverter` requires a separately installed Pandoc, and
`LibreOfficeToPdfConverter` requires LibreOffice. Conversion does not perform
OCR, and sanitization is not a guarantee that a document is safe for every use.

### Analyze document layout

Install the optional dependencies for the formats you use. For PDF discovery
and rendering:

```bash
pip install "raggae[layout-pdf,layout-visual]"
```

```python
from raggae.documents import DocumentRegionRenderer
from raggae.documents.layout_analysis import DocumentLayoutAnalyzer

report = DocumentLayoutAnalyzer().analyze("report.pdf")

print(report.outcome)
for region in report.layout.regions:
    print(region.id, region.kind, region.bbox)

if report.outcome in ("success", "partial"):
    images = DocumentRegionRenderer().render("report.pdf", layout=report.layout)
    try:
        for page_number, image in images.items():
            image.save(f"layout-page-{page_number}.png")
    finally:
        for image in images.values():
            image.close()
```

The report keeps discovery, reading-order suggestions, relationships, and
repeated-margin results together. Layout analysis returns locations and links;
text extraction and semantic content assembly are separate work.

For a single discovery worker, use, for example,
`NativePdfLayoutAnalyzer().analyze("report.pdf")`. Workers return a shared
`LayoutAnalysisResult` schema. PDF and image regions use rectangles; Word and
HTML use element paths, and spreadsheets use cell ranges.

### Optional layout dependencies

| Extra | Purpose |
|---|---|
| `layout-pdf` | Native PDF region discovery. |
| `layout-docx` | Word document structure. |
| `layout-pptx` | PowerPoint slide shapes. |
| `layout-xlsx` | Excel tables and occupied cell blocks. |
| `layout-html` | HTML document structure. |
| `layout-visual` | PDF rendering for visual discovery and annotated previews. |
| `layout-paddle` | Paddle-based local layout detection. |
| `layout-docling` | Docling-model local layout detection. |

Model-based discovery needs downloaded local model files and an explicitly
configured service. For visual PDF analysis, also install `layout-visual`.

Browse the [source code](https://github.com/okanyenigun/raggae) for the available
workers, policies, and stage coordinators.
