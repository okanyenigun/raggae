"""Plain boxes can be drawn without constructing layout models."""

from io import BytesIO

from PIL import Image
import pytest

from raggae.documents.utilities import DocumentRegionRenderer


def png():
    with BytesIO() as stream, Image.new("RGB", (80, 60), "white") as image:
        image.save(stream, format="PNG")
        return stream.getvalue()


def test_generic_boxes_labels_and_colors_are_not_mutated():
    boxes = {1: [(20, 20, 60, 50)]}
    labels = {1: ["Detected object"]}
    colors = {1: ["red"]}
    renderer = DocumentRegionRenderer(show_labels=False)
    images = renderer.render(png(), boxes=boxes, labels=labels, colors=colors)
    try:
        assert list(images) == [1]
        assert images[1].getpixel((20, 30)) == (255, 0, 0)
        assert images[1].size == (80, 60)
        assert boxes == {1: [(20, 20, 60, 50)]}
        assert labels == {1: ["Detected object"]}
        assert colors == {1: ["red"]}
    finally:
        images[1].close()


def test_empty_box_list_selects_a_blank_frame_and_generic_defaults_work():
    renderer = DocumentRegionRenderer(show_labels=False)
    blank = renderer.render(png(), boxes={1: []})
    drawn = renderer.render(png(), boxes={1: [(20, 20, 60, 50)]})
    try:
        assert blank[1].getpixel((20, 30)) == (255, 255, 255)
        assert drawn[1].getpixel((20, 30)) == (37, 99, 235)
        assert blank[1] is not drawn[1]
    finally:
        blank[1].close()
        drawn[1].close()


def test_selected_frame_number_is_not_a_result_list_index():
    with (
        BytesIO() as data,
        Image.new("RGB", (80, 60), "red") as first,
        Image.new("RGB", (80, 60), "green") as second,
    ):
        first.save(data, format="TIFF", save_all=True, append_images=[second])
        images = DocumentRegionRenderer().render(data.getvalue(), boxes={2: []})
    try:
        assert list(images) == [2]
        assert images[2].getpixel((40, 30)) == (0, 128, 0)
    finally:
        images[2].close()


@pytest.mark.parametrize(
    "boxes,error",
    [
        ({}, ValueError),
        ({0: []}, ValueError),
        ({-1: []}, ValueError),
        ({True: []}, TypeError),
        ({"1": []}, TypeError),
        ([], TypeError),
        ({1: [(0, 0, 5)]}, ValueError),
        ({1: [(-1, 0, 5, 5)]}, ValueError),
        ({1: [(0, 0, 0, 5)]}, ValueError),
        ({1: [(0, 5, 5, 4)]}, ValueError),
        ({1: [(0, 0, float("nan"), 5)]}, ValueError),
        ({1: [(0, 0, float("inf"), 5)]}, ValueError),
        ({1: [(0, False, 5, 5)]}, TypeError),
        ({1: [(0, "0", 5, 5)]}, TypeError),
    ],
)
def test_bad_generic_boxes_fail_before_source_is_opened(boxes, error):
    with pytest.raises(error):
        DocumentRegionRenderer().render("not-opened.pdf", boxes=boxes)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"labels": {2: ["wrong page"]}}, ValueError),
        ({"labels": {1: []}}, ValueError),
        ({"labels": {1: [5]}}, TypeError),
        ({"labels": {1: "x"}}, TypeError),
        ({"colors": {1: []}}, ValueError),
        ({"colors": {1: ["not-a-color"]}}, ValueError),
    ],
)
def test_labels_and_colors_must_match_the_selected_boxes(kwargs, error):
    with pytest.raises(error):
        DocumentRegionRenderer().render(
            "not-opened.pdf", boxes={1: [(0, 0, 5, 5)]}, **kwargs
        )


def test_missing_input_and_out_of_bounds_boxes_are_reported():
    renderer = DocumentRegionRenderer()
    with pytest.raises(TypeError, match="layout result or boxes"):
        renderer.render("not-opened.pdf")
    with pytest.raises(ValueError, match="fit"):
        renderer.render(png(), boxes={1: [(0, 0, 81, 20)]})
