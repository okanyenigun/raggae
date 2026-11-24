<p align="center">
  <img src="https://raw.githubusercontent.com/okanyenigun/raggae/master/docs/assets/logo.png" alt="raggae logo" width="240">
</p>

# raggae

Raggae is a modular Python SDK for building retrieval-augmented generation (RAG)
pipelines: turning source material into useful evidence for language models.
Its scope is the full RAG workflow, from document ingestion to retrieval and
answer generation.

Use individual workers when you need one operation, or a stage coordinator to
run related workers together. Policies configure limits and behavior, and
structured results report what happened without requiring you to parse logs.

## Installation

```bash
pip install raggae
```

## Available capabilities

| Stage | What it does today | Coordinator |
|---|---|---|
| Validation | Checks filenames, content signatures, encryption, resource limits, active content, and PDF readability; returns a decision with findings. | `DocumentInspector` |
| Normalization | Converts text encodings, fixes image orientation, decrypts PDFs, removes active content and metadata, and converts formats. | `DocumentNormalizer` |
| Layout analysis | Locates text, table, and image regions; suggests reading order; links captions; detects repeated headers and footers. | `DocumentLayoutAnalyzer` |
| Text reading | Reads text from plain text, Markdown, JSON, CSV, Excel, Word, PowerPoint, HTML, PDF, and images (OCR), as whole documents or selected layout regions. | Independent readers |
| Image extraction | Extracts pictures from PDF, Word, PowerPoint, Excel, HTML, Markdown, and image files, as whole documents or selected layout regions. | Independent extractors |
| Table extraction | Extracts tables as grids of cells, with merged cells and header cells kept, from PDF, Word, PowerPoint, Excel, HTML, Markdown, CSV, and image files (OCR), as whole documents or selected layout regions. | Independent extractors |

## Quick start

### Inspect a document

```python
from raggae.documents.validation import DocumentInspector

report = DocumentInspector().validate("uploaded.pdf")
print(report.decision)  # accept, accept_with_warnings, needs_password, or reject
for item in report.findings:
    print(item.finding.code, item.finding.message)
```

### Normalize a document

```python
from raggae.documents.normalization import DocumentNormalizer

result = DocumentNormalizer().normalize(source="uploaded.pdf", output_path="prepared.pdf")
print(result.outcome)  # changed, no_change, or failed
```

The normalizer picks a sequence of workers from the file's extension and never
overwrites the original. Format conversion needs Pandoc or LibreOffice installed
separately.

### Analyze document layout

```bash
pip install "raggae[layout-pdf]"
```

```python
from raggae.documents.layout_analysis import DocumentLayoutAnalyzer

report = DocumentLayoutAnalyzer().analyze("report.pdf")
for region in report.layout.regions:
    print(region.id, region.kind, region.bbox)
```

Discovery supports PDF, Word, PowerPoint, Excel, and HTML, plus scanned PDFs
and images through a local Paddle or Docling model. `DocumentRegionRenderer`
draws the regions on the pages (`layout-visual`).

### Read text

```bash
pip install "raggae[layout-pdf,text-pdf]"
```

```python
from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.text_reader import PdfplumberTextReader

layout = NativePdfLayoutAnalyzer().analyze("report.pdf")
text_ids = [region.id for region in layout.regions if region.kind != "table"]

result = PdfplumberTextReader().read("report.pdf", layout=layout, region_ids=text_ids)
for block in result.blocks:
    print(block.page_index, block.region_id, block.text)
```

Every reader returns blocks with their page, region, format (`plain_text`,
`markdown`, `json`, or `xml`), and method (`native`, `ocr`, or `vision`), plus
findings for anything left unread. Omit `layout` to read whole documents.

### Extract images

```bash
pip install "raggae[layout-pdf,image-pdf]"
```

```python
from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.image_extractor import PdfiumImageExtractor

layout = NativePdfLayoutAnalyzer().analyze("report.pdf")

result = PdfiumImageExtractor().extract("report.pdf", layout=layout)
for image in result.images:
    print(image.page_index, image.region_id, image.media_type, image.width, image.height, image.method)
```

Every extractor returns images with their page, region, media type, size, and
how they were obtained: `original` (the stored file, byte for byte), `reencoded`
(stored pixels saved as a new file), or `rendered` (a crop of the drawn page),
plus findings for anything left out. `image.data` holds the image file's bytes
and `image.to_base64()` the same as base64. Omit `layout` to extract from whole
documents.

`PdfiumImageExtractor` returns what a reader sees by default;
`PdfiumImageExtractor(mode="stored")` returns the stored picture files instead.
A chart drawn with lines has no stored file, so it is found only through a layout
region. Pictures that point to another file are reported, never opened or fetched.

### Extract tables

```bash
pip install "raggae[layout-pdf,text-pdf]"
```

```python
from raggae.documents.layout_analysis import NativePdfLayoutAnalyzer
from raggae.documents.table_extractor import PdfplumberTableExtractor

layout = NativePdfLayoutAnalyzer().analyze("report.pdf")

result = PdfplumberTableExtractor().extract("report.pdf", layout=layout)
for table in result.tables:
    print(table.page_index, table.region_id, table.method, table.row_count, table.column_count)
    print(table.to_markdown())
```

Every extractor returns tables as grids of cells: each cell once, at its top-left
position, with its row and column spans and whether the source declares it a
header cell. Each table has its page, region, an author-given caption if there is
one, and how the grid was obtained: `declared` (the format states it), `ruled`
(drawn lines), `aligned` (text positions), or `model` (a trained model), plus
findings for anything left out. `to_html()` keeps spans and header cells;
`to_markdown()`, `to_dataframe()`, `to_json()`, and `to_xml()` give one row per
record, with each column named after the header cells above it. Omit `layout` to
extract from whole documents.

`PdfplumberTableExtractor` searches each layout region for both drawn lines and
aligned text, so tables without borders are found too; without a layout it looks
for drawn lines only, since guessing from alignment on a whole page turns ordinary
paragraphs into tables. Pass `strategy="lines"`, `"text"`, or `"all"` to choose.

## Optional dependencies

| Extra | Purpose |
|---|---|
| `layout-pdf`, `layout-docx`, `layout-pptx`, `layout-xlsx`, `layout-html` | Layout discovery for each format. |
| `layout-visual` | PDF rendering for visual discovery and region previews. |
| `layout-paddle`, `layout-docling` | Local model-based layout detection. |
| `text-markdown`, `text-html`, `text-office`, `text-pandas` | Markdown, HTML, Office, and pandas-based readers. |
| `text-pdf` | pdfplumber, PDFium, and pdfminer PDF readers. |
| `text-docling`, `text-markitdown` | Docling and Granite-Docling, and MarkItDown PDF readers. |
| `text-tesseract`, `text-paddle` | OCR services for `OcrTextReader` (add `image-pdf` for PDF pages). |
| `text-unstructured` | Unstructured wrappers; also run `python -m spacy download en_core_web_sm`. |
| `text-pymupdf`, `text-pymupdf4llm` | PyMuPDF readers, and `PyMuPdfImageExtractor`. **AGPL-3.0 or Artifex commercial licence.** |
| `image-pdf` | PDF page rendering for `PdfiumImageExtractor` and OCR of PDF pages. |
| `table-ocr` | img2table for `Img2TableExtractor`: tables found in rendered PDF pages and image files (scans also need an OCR service, such as `text-tesseract`). |

Plain text, CSV, JSON, and pypdf readers, and the pikepdf, pypdf, and Pillow image
extractors, need no extra. The Word, PowerPoint, Excel, HTML, and Markdown image
extractors use the `text-office`, `text-html`, and `text-markdown` extras. The CSV
table extractor needs no extra; the other table extractors use the same extras as
the text readers for their format (`text-docling` for `DoclingTableExtractor`), and
`to_dataframe()` needs `text-pandas`. Nothing is downloaded at
run time: models, the Tesseract engine, and Ollama vision models must be
installed locally first.

Browse the [source code](https://github.com/okanyenigun/raggae) for the available
workers, policies, and stage coordinators.
