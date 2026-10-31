"""Conservative word-to-block grouping; this does not determine reading order."""

from ...models import BoundingBox


def text_blocks(words: list[BoundingBox]) -> list[BoundingBox]:
    """Join words into lines, then nearby left-aligned lines into blocks.

    A wide horizontal gap splits columns. Different indentations may stay as
    separate blocks; this is preferable to joining unrelated neighbouring text.
    Input is in unscaled PDF units and is horizontal (swap axes for vertical text).
    """
    rows: list[list[BoundingBox]] = []
    for word in sorted(words, key=lambda box: (box[1], box[0])):
        if rows and abs(word[1] - rows[-1][0][1]) <= 3:
            rows[-1].append(word)
        else:
            rows.append([word])

    lines: list[BoundingBox] = []
    for row in rows:
        current: BoundingBox | None = None
        for word in sorted(row, key=lambda box: box[0]):
            if current is not None:
                height = min(current[3] - current[1], word[3] - word[1])
                if word[0] - current[2] <= max(6, height * 1.5):
                    current = _union(current, word)
                    continue
                lines.append(current)
            current = word
        if current is not None:
            lines.append(current)

    blocks: list[BoundingBox] = []
    last_lines: list[BoundingBox] = []
    for line in sorted(lines, key=lambda box: (box[1], box[0])):
        match = None
        for index in range(len(last_lines) - 1, -1, -1):
            previous = last_lines[index]
            height = min(previous[3] - previous[1], line[3] - line[1])
            gap = line[1] - previous[3]
            if abs(line[0] - previous[0]) <= 3 and -1 <= gap <= height * 0.8:
                match = index
                break
        if match is None:
            blocks.append(line)
            last_lines.append(line)
        else:
            blocks[match] = _union(blocks[match], line)
            last_lines[match] = line
    return blocks


def _union(first: BoundingBox, second: BoundingBox) -> BoundingBox:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )
