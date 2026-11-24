"""Self-contained PDF fixtures for table extraction; no downloaded files."""

from io import BytesIO

import pikepdf


def make_pdf(
    *contents, rotation=0, crop=None, media=None, unit=1, password=None, size=(300, 400)
):
    """A PDF with one page per content stream (300 × 400 by default), text in
    Helvetica (/F1)."""
    with pikepdf.Pdf.new() as document, BytesIO() as output:
        for content in contents:
            page = document.add_blank_page(page_size=size)
            font = document.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                )
            )
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            page.Contents = document.make_stream(content)
            page.Rotate = rotation
            page.UserUnit = unit
            if crop is not None:
                page.CropBox = pikepdf.Array(crop)
            if media is not None:
                page.MediaBox = pikepdf.Array(media)
        options = {}
        if password is not None:
            options["encryption"] = pikepdf.Encryption(
                user=password, owner="table-fixture-owner"
            )
        document.save(output, **options)
        return output.getvalue()


def text(x, y, words, size=10):
    return f"BT /F1 {size} Tf {x} {y} Td ({words}) Tj ET\n".encode()


def ruled(dx=0, dy=0):
    """A heading and a bordered table: "Part" merged down two rows, "Price" across
    two columns. Shifted by (dx, dy) for pages whose origin is not (0, 0)."""
    lines = f"""0.5 w
{30 + dx} {275 + dy} 240 75 re S
{110 + dx} {325 + dy} m {270 + dx} {325 + dy} l S
{30 + dx} {300 + dy} m {270 + dx} {300 + dy} l S
{110 + dx} {275 + dy} m {110 + dx} {350 + dy} l S
{190 + dx} {275 + dy} m {190 + dx} {325 + dy} l S
""".encode()
    words = [
        (35, 335, "Part"),
        (115, 335, "Price"),
        (115, 310, "2025"),
        (195, 310, "2026"),
        (35, 285, "Motor"),
        (115, 285, "110"),
        (195, 285, "120"),
    ]
    return (
        text(30 + dx, 380 + dy, "Price list", 14)
        + lines
        + b"".join(text(x + dx, y + dy, word) for x, y, word in words)
    )


RULED = ruled()
# (row, column, row_span, column_span, text) of the ruled table.
RULED_GRID = [
    (0, 0, 2, 1, "Part"),
    (0, 1, 1, 2, "Price"),
    (1, 1, 1, 1, "2025"),
    (1, 2, 1, 1, "2026"),
    (2, 0, 1, 1, "Motor"),
    (2, 1, 1, 1, "110"),
    (2, 2, 1, 1, "120"),
]

# A table with no borders: words aligned in three columns, then a footnote.
UNRULED = (
    text(30, 380, "Units sold", 14)
    + b"".join(
        text(x, 340 - number * 16, word)
        for number, row in enumerate(
            [
                ("Region", "Year", "Units"),
                ("North", "2025", "1200"),
                ("North", "2026", "1350"),
                ("South", "2025", "900"),
                ("South", "2026", "980"),
            ]
        )
        for x, word in zip((40, 120, 200), row)
    )
    + text(40, 220, "Figures are rounded to the nearest unit and may not add up.")
)

# Grid lines with no text in any cell.
EMPTY_GRID = b"0.5 w 30 200 240 60 re S 30 230 m 270 230 l S 150 200 m 150 260 l S"

# A two-cell table whose first cell holds two lines.
TWO_LINES = (
    b"0.5 w 30 250 240 50 re S 150 250 m 150 300 l S\n"
    + text(35, 285, "First line")
    + text(35, 265, "Second line")
    + text(155, 285, "Other")
)


def shipments(merged=False):
    """An A4 page with a heading and a ruled 4 × 3 table, the kind Docling's layout
    model recognizes. ``merged`` merges the first header cell across two columns."""
    rows = [
        ["Item", "Qty", "Cost"],
        ["Shipping labels", "90", "45"],
        ["Packing tape", "18", "12"],
        ["Bubble wrap", "40", "30"],
    ]
    if merged:
        rows[0] = ["Item and quantity", "", "Cost"]
    left, top, column, height = 72, 200, 140, 26

    def at(size, x, y, words):
        return text(x, 842 - y, words, size)

    content = at(22, 72, 90, "Quarterly shipments") + at(
        11, 72, 140, "Totals by item for the quarter."
    )
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value:
                content += at(11, left + c * column + 8, top + r * height + 17, value)
    for r in range(len(rows) + 1):
        y = 842 - (top + r * height)
        content += f"0.8 w {left} {y} m {left + 3 * column} {y} l S\n".encode()
    for c in range(4):
        x = left + c * column
        start = 842 - top - (height if merged and c == 1 else 0)
        content += f"0.8 w {x} {start} m {x} {842 - (top + len(rows) * height)} l S\n".encode()
    return content
