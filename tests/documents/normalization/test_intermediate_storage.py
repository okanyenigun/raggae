"""Coordinator storage choices, actual saved paths, and failure boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pikepdf
import pytest

from raggae.documents.normalization import (
    Artifact,
    CsvFormulaSanitizer,
    DocumentNormalizer,
    DocumentToHtmlConverter,
    HtmlActiveContentSanitizer,
    HtmlMetadataSanitizer,
    ImageMetadataSanitizer,
    LibreOfficeToPdfConverter,
    NormalizationRequest,
    NormalizationResult,
    OoxmlActiveContentSanitizer,
    OoxmlMetadataSanitizer,
    PdfMetadataSanitizer,
    PillowImageOrientationNormalizer,
    PillowImageToPdfConverter,
    SharedInputOutputHandler,
    StrictTextEncodingNormalizer,
)

from .test_coordinator import WorkerReport
from .test_image_orientation import encoded as encoded_image
from .test_keyword_arguments import fake_native_tools
from .test_ooxml_active_content import package, parts


class StorageWorker:
    """Use the same I/O contract as SDK workers, with predictable byte changes."""

    def __init__(self, name="worker", outcome="changed", *, error=None, before=None):
        self.name = name
        self.outcome = outcome
        self.error = error
        self.before = before
        self.requests = []

    def normalize(self, request: NormalizationRequest) -> NormalizationResult[WorkerReport]:
        self.requests.append(request)
        if self.before is not None:
            self.before(request)

        def transform(source, output):
            output.write(source.read() + b"!")
            if self.error is not None:
                raise self.error
            return WorkerReport(outcome=self.outcome, label=self.name)

        report, artifact = SharedInputOutputHandler().execute(
            source=request.source, output_path=request.output_path, transform=transform,
        )
        return NormalizationResult(report=report, artifact=artifact)


@pytest.mark.parametrize("mode", ["memory", "folder", "overrides"])
@pytest.mark.parametrize("outcomes", [
    ("changed", "changed"), ("changed", "no_change"),
    ("no_change", "changed"), ("no_change", "no_change"),
])
@pytest.mark.parametrize("final_file", [False, True])
@pytest.mark.parametrize("file_source", [False, True])
def test_storage_modes_follow_artifacts_and_save_only_changes(
    mode, outcomes, final_file, file_source, tmp_path,
):
    folder = tmp_path / "work"
    folder.mkdir()
    original = tmp_path / "original.bin"
    original.write_bytes(b"original")
    request = NormalizationRequest(
        source=Artifact(path=original, password="secret") if file_source else Artifact(data=b"original", password="secret"),
        output_path=tmp_path / "final.bin" if final_file else None,
        declared_encoding="cp1252", source_format="custom",
    )
    workers = tuple(StorageWorker("same", outcome) for outcome in outcomes)
    overrides = (tmp_path / "first.bin", None) if mode == "overrides" else None
    result = DocumentNormalizer(
        workers=workers,
        intermediate_dir=folder if mode == "folder" else None,
        step_output_paths=overrides,
    ).normalize(request)

    changes = outcomes.count("changed")
    assert result.outcome == ("changed" if changes else "no_change")
    assert result.artifact.read_bytes() == b"original" + b"!" * changes
    assert request.source.read_bytes() == original.read_bytes() == b"original"
    assert "secret" not in result.report.model_dump_json()
    assert workers[0].requests[0] is not request
    assert workers[1].requests[0].source.read_bytes() == b"original" + (b"!" if outcomes[0] == "changed" else b"")
    for worker in workers:
        assert worker.requests[0].declared_encoding == "cp1252"
        assert worker.requests[0].source_format == "custom"
        assert worker.requests[0].output_path != request.output_path or request.output_path is None
    assert workers[0].requests[0].source.password == "secret"
    assert workers[1].requests[0].source.password == (None if outcomes[0] == "changed" else "secret")
    assert result.artifact.password == (None if changes else "secret")
    for index, step in enumerate(result.report.steps):
        saved = outcomes[index] == "changed" and (mode == "folder" or (mode == "overrides" and index == 0))
        assert (step.output_path is not None) == saved
        if saved:
            assert step.output_path.read_bytes() == b"original" + b"!" * outcomes[:index + 1].count("changed")
            if mode == "folder":
                assert step.output_path.name == f"{index + 1:02d}-same.bin"
    assert len(list(folder.iterdir())) == (1 if mode == "folder" and changes else 0)
    assert (tmp_path / "final.bin").exists() == (final_file and bool(changes))
    if not changes:
        assert result.artifact is request.source
    elif final_file:
        assert result.artifact.path == request.output_path
    elif mode == "folder":
        last_changed = max(index for index, outcome in enumerate(outcomes) if outcome == "changed")
        assert result.artifact.path == result.report.steps[last_changed].output_path
    elif mode == "overrides" and outcomes == ("changed", "no_change"):
        assert result.artifact.path == overrides[0]
    else:
        assert result.artifact.path is None
    assert not list(tmp_path.rglob("*.part"))


def test_real_automatic_html_chain_has_readable_named_copies(tmp_path):
    source = tmp_path / "original.html"
    text = '<meta name="author" content="Alice"><script>example</script><p>Café</p>'
    original = text.encode("cp1252")
    source.write_bytes(original)
    final = tmp_path / "final.html"
    result = DocumentNormalizer(intermediate_dir=str(tmp_path)).normalize(
        source=source, declared_encoding="cp1252", output_path=final,
    )
    paths = [step.output_path for step in result.report.steps]
    assert [path.name for path in paths] == [
        "01-strict_text_encoding.html", "02-html_active_content.html", "03-html_metadata.html",
    ]
    assert len({path.parent for path in paths}) == 1
    assert paths[0].read_text(encoding="utf-8") == text
    assert paths[1].read_text(encoding="utf-8") == '<meta name="author" content="Alice"><p>Café</p>'
    assert paths[2].read_text(encoding="utf-8") == "<p>Café</p>"
    assert result.artifact.path == final
    assert final.read_bytes() == paths[-1].read_bytes()
    assert source.read_bytes() == original
    assert [step["output_path"] for step in json.loads(result.report.model_dump_json())["steps"]] == list(map(str, paths))


def test_automatic_csv_skips_unchanged_encoding_copy(tmp_path):
    result = DocumentNormalizer(intermediate_dir=tmp_path).normalize(
        source=b"value\n=1+1\n", source_format="csv", output_path=None,
    )
    first, second = result.report.steps
    assert first.output_path is None
    assert second.output_path.name == "02-csv_formula.csv"
    assert result.artifact.path == second.output_path
    assert second.output_path.read_bytes() == b"value\n'=1+1\n"
    assert list(second.output_path.parent.iterdir()) == [second.output_path]


def test_complete_override_list_takes_precedence_over_folder(tmp_path):
    ignored_folder = tmp_path / "unused-and-absent"
    first = tmp_path / "chosen.html"
    paths = [str(first), None, None]
    normalizer = DocumentNormalizer(intermediate_dir=ignored_folder, step_output_paths=paths)
    paths[0] = tmp_path / "caller-mutated.html"
    result = normalizer.normalize(
        source='<meta name="author" content="Alice"><script>example</script><p>Café</p>'.encode("cp1252"),
        source_format="html", declared_encoding="cp1252", output_path=None,
    )
    assert [step.output_path for step in result.report.steps] == [first, None, None]
    assert result.artifact.path is None
    assert result.artifact.data == "<p>Café</p>".encode("utf-8")
    assert first.exists() and not ignored_folder.exists()
    assert not (tmp_path / "caller-mutated.html").exists()


@pytest.mark.parametrize("count", [0, 1, 3])
def test_all_unchanged_or_empty_chains_leave_no_run_folder(count, tmp_path):
    result = DocumentNormalizer(
        workers=tuple(StorageWorker(outcome="no_change") for _ in range(count)),
        intermediate_dir=tmp_path,
    ).normalize(source=b"original", output_path=tmp_path / "unused")
    assert result.outcome == "no_change"
    assert all(step.output_path is None for step in result.report.steps)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_index", [0, 1, 2])
@pytest.mark.parametrize("exception", [False, True])
def test_failed_steps_keep_only_completed_copies(failure_index, exception, tmp_path):
    error = OSError("disk full") if exception else None
    workers = tuple(
        StorageWorker(str(index), "failed" if index == failure_index else "changed",
                      error=error if index == failure_index else None)
        for index in range(3)
    )
    normalizer = DocumentNormalizer(workers=workers, intermediate_dir=tmp_path)
    if exception:
        with pytest.raises(OSError, match="disk full") as caught:
            normalizer.normalize(source=b"original", output_path=tmp_path / "final.bin")
        assert caught.value is error
    else:
        result = normalizer.normalize(source=b"original", output_path=tmp_path / "final.bin")
        assert result.outcome == "failed" and result.artifact is None
        assert len(result.report.steps) == failure_index + 1
        assert result.report.steps[-1].output_path is None
        assert all(step.output_path.exists() for step in result.report.steps[:-1])
    assert all(not worker.requests for worker in workers[failure_index + 1:])
    runs = list(tmp_path.glob("run-*"))
    assert len(runs) == bool(failure_index)
    assert len(list(tmp_path.rglob("*.bin"))) == failure_index
    assert not (tmp_path / "final.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_missing_source_cleans_empty_run_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        DocumentNormalizer(workers=(StrictTextEncodingNormalizer(),), intermediate_dir=tmp_path).normalize(
            source=tmp_path / "absent.txt", output_path=None,
        )
    assert list(tmp_path.iterdir()) == []


def test_folder_parent_must_exist(tmp_path):
    worker = StorageWorker()
    missing = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        DocumentNormalizer(workers=(worker,), intermediate_dir=missing).normalize(source=b"original", output_path=None)
    assert not worker.requests and not missing.exists()


@pytest.mark.parametrize("invalid", ["", "   ", 123, b"folder", False])
def test_invalid_folder_configuration_is_rejected(invalid):
    with pytest.raises((TypeError, ValueError)):
        DocumentNormalizer(intermediate_dir=invalid)


@pytest.mark.parametrize("invalid", ["file.pdf", Path("file.pdf"), {0: "file.pdf"}, {"file.pdf"}, 123])
def test_override_configuration_requires_an_ordered_list_or_tuple(invalid):
    with pytest.raises(TypeError, match="step_output_paths"):
        DocumentNormalizer(step_output_paths=invalid)


@pytest.mark.parametrize("invalid", ["", " ", 123, b"file", False])
def test_invalid_override_path_is_rejected(invalid):
    with pytest.raises((TypeError, ValueError)):
        DocumentNormalizer(step_output_paths=(invalid,))


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("paths", [(), (None,), (None,) * 4])
def test_wrong_override_count_is_rejected_before_any_output(automatic, paths, tmp_path):
    workers = tuple(StorageWorker() for _ in range(3))
    with pytest.raises(ValueError, match="one entry"):
        DocumentNormalizer(
            workers=None if automatic else workers, intermediate_dir=tmp_path,
            step_output_paths=paths,
        ).normalize(source=b"<p>text</p>", source_format="html", output_path=None)
    assert all(not worker.requests for worker in workers)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("collision", ["same", "parent_alias", "symlink_parent", "source", "final"])
def test_destination_aliases_are_rejected_before_workers(collision, tmp_path):
    source = tmp_path / "original.bin"
    source.write_bytes(b"original")
    destination = tmp_path / "first.bin"
    other = tmp_path / "second.bin"
    final = tmp_path / "final.bin"
    if collision == "same":
        other = destination
    elif collision == "parent_alias":
        (tmp_path / "nested").mkdir()
        other = tmp_path / "nested" / ".." / "first.bin"
    elif collision == "symlink_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        other = alias / "first.bin"
    elif collision == "source":
        other = source
    else:
        other = final
    workers = (StorageWorker(), StorageWorker())
    with pytest.raises(ValueError, match="distinct"):
        DocumentNormalizer(workers=workers, step_output_paths=(destination, other)).normalize(
            source=source, output_path=final,
        )
    assert all(not worker.requests for worker in workers)
    assert source.read_bytes() == b"original" and not destination.exists()


@pytest.mark.parametrize("kind", ["existing", "dangling_symlink", "missing_parent", "file_parent"])
def test_invalid_destinations_are_checked_before_workers(kind, tmp_path):
    second = tmp_path / "second.bin"
    if kind == "existing":
        second.write_bytes(b"KEEP")
        error = FileExistsError
    elif kind == "dangling_symlink":
        second.symlink_to(tmp_path / "absent.bin")
        error = FileExistsError
    elif kind == "missing_parent":
        second = tmp_path / "absent" / "second.bin"
        error = FileNotFoundError
    else:
        parent = tmp_path / "not-a-directory"
        parent.write_bytes(b"KEEP")
        second = parent / "second.bin"
        error = NotADirectoryError
    workers = (StorageWorker(), StorageWorker())
    with pytest.raises(error):
        DocumentNormalizer(workers=workers, step_output_paths=(tmp_path / "first.bin", second)).normalize(
            source=b"original", output_path=None,
        )
    assert all(not worker.requests for worker in workers)
    assert not (tmp_path / "first.bin").exists()
    if kind == "existing":
        assert second.read_bytes() == b"KEEP"


@pytest.mark.parametrize("concurrent", [False, True])
def test_reused_coordinator_creates_separate_runs(concurrent, tmp_path):
    normalizer = DocumentNormalizer(workers=(CsvFormulaSanitizer(),), intermediate_dir=tmp_path)

    def normalize(index):
        return normalizer.normalize(source=f"value\n={index}+1\n".encode(), output_path=None)

    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(normalize, (1, 2)))
    else:
        results = [normalize(1), normalize(2)]
    assert results[0].artifact.path.parent != results[1].artifact.path.parent
    assert [result.artifact.read_bytes() for result in results] == [b"value\n'=1+1\n", b"value\n'=2+1\n"]
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize("name", ["../../escape.pdf", "", "...", "x" * 500, "space / and \\ separators"])
def test_worker_names_cannot_escape_run_folder(name, tmp_path):
    result = DocumentNormalizer(workers=(StorageWorker(name), StorageWorker(name)), intermediate_dir=tmp_path).normalize(
        source=b"original", output_path=None,
    )
    first, second = [step.output_path for step in result.report.steps]
    assert first.parent == second.parent
    assert first.parent.parent == tmp_path
    assert first.name.startswith("01-") and second.name.startswith("02-")
    assert len(first.name) < 100
    assert first.read_bytes() == b"original!" and second.read_bytes() == b"original!!"
    assert [step.worker for step in result.report.steps] == [name, name]


@pytest.mark.parametrize("family", ["docx", "xlsx", "pptx"])
def test_package_names_use_detected_format_not_source_extension(family, tmp_path):
    source = tmp_path / "misleading.txt"
    source.write_bytes(package(parts(family, active=True)))
    result = DocumentNormalizer(
        workers=(OoxmlActiveContentSanitizer(), OoxmlMetadataSanitizer()), intermediate_dir=tmp_path,
    ).normalize(source=source, output_path=None)
    assert result.outcome == "changed"
    assert [step.output_path.suffix for step in result.report.steps] == ["." + family] * 2


@pytest.mark.parametrize("image_format,suffix", [("PNG", ".png"), ("JPEG", ".jpg"), ("TIFF", ".tiff")])
def test_image_names_use_reported_format(image_format, suffix, tmp_path):
    result = DocumentNormalizer(
        workers=(PillowImageOrientationNormalizer(), ImageMetadataSanitizer()), intermediate_dir=tmp_path,
    ).normalize(source=encoded_image(image_format), output_path=None, source_format="txt")
    assert result.outcome == "changed"
    assert [step.output_path.suffix for step in result.report.steps] == [suffix] * 2


def test_image_conversion_and_following_pdf_worker_use_pdf_names(tmp_path):
    result = DocumentNormalizer(
        workers=(PillowImageToPdfConverter(), PdfMetadataSanitizer()), intermediate_dir=tmp_path,
    ).normalize(source=encoded_image(orientation=1), source_format="png", output_path=None)
    assert result.outcome == "changed"
    assert result.report.steps[0].output_path.suffix == ".pdf"
    assert result.artifact.path.suffix == ".pdf"
    with pikepdf.Pdf.open(BytesIO(result.artifact.read_bytes())) as actual:
        assert len(actual.pages) == 1


@pytest.mark.parametrize("converter,suffix", [(DocumentToHtmlConverter, ".html"), (LibreOfficeToPdfConverter, ".pdf")])
def test_native_conversion_names_use_output_format(converter, suffix, fake_native_tools, tmp_path):
    result = DocumentNormalizer(workers=(converter(),), intermediate_dir=tmp_path).normalize(
        source=b"opaque native input", source_format="docx", output_path=None,
    )
    assert result.outcome == "changed"
    assert result.artifact.path.suffix == suffix
    assert result.report.steps[0].output_path == result.artifact.path
    assert len(fake_native_tools) == 1


def test_conversion_format_carries_into_later_encoding_step(fake_native_tools, tmp_path):
    result = DocumentNormalizer(
        workers=(DocumentToHtmlConverter(), StrictTextEncodingNormalizer()), intermediate_dir=tmp_path,
    ).normalize(source=b"input", source_format="docx", target_encoding="utf-16", output_path=None)
    assert [step.output_path.suffix for step in result.report.steps] == [".html", ".html"]
    assert "converted" in result.artifact.read_bytes().decode("utf-16")


def test_unknown_text_format_uses_neutral_extension(tmp_path):
    result = DocumentNormalizer(workers=(StrictTextEncodingNormalizer(),), intermediate_dir=tmp_path).normalize(
        source=b"caf\xe9", declared_encoding="cp1252", output_path=None,
    )
    assert result.artifact.path.suffix == ".bin"
    assert result.artifact.read_bytes() == "café".encode()


def test_destination_created_between_steps_is_not_overwritten(tmp_path):
    second_path = tmp_path / "second.bin"
    first = StorageWorker()
    second = StorageWorker(before=lambda request: second_path.write_bytes(b"KEEP"))
    final = tmp_path / "final.bin"
    with pytest.raises(FileExistsError):
        DocumentNormalizer(workers=(first, second), step_output_paths=(tmp_path / "first.bin", second_path)).normalize(
            source=b"original", output_path=final,
        )
    assert (tmp_path / "first.bin").read_bytes() == b"original!"
    assert second_path.read_bytes() == b"KEEP" and not final.exists()
    assert not list(tmp_path.glob("*.part"))


def test_automatic_named_destination_race_does_not_overwrite(tmp_path):
    def create_collision(request):
        request.output_path.with_suffix(".bin").write_bytes(b"KEEP")

    with pytest.raises(FileExistsError):
        DocumentNormalizer(workers=(StorageWorker(before=create_collision),), intermediate_dir=tmp_path).normalize(
            source=b"original", output_path=None,
        )
    run = next(tmp_path.iterdir())
    assert (run / "01-worker.bin").read_bytes() == b"KEEP"
    assert (run / "01-worker").read_bytes() == b"original!"
    assert not list(run.glob("*.part"))


def test_final_publication_failure_does_not_delete_saved_steps(tmp_path):
    final = tmp_path / "final.bin"
    final.write_bytes(b"KEEP")
    with pytest.raises(FileExistsError):
        DocumentNormalizer(workers=(StorageWorker(),), intermediate_dir=tmp_path).normalize(
            source=b"original", output_path=final,
        )
    assert final.read_bytes() == b"KEEP"
    assert next(tmp_path.glob("run-*")).joinpath("01-worker.bin").read_bytes() == b"original!"


def test_relative_storage_paths_are_bound_at_construction(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    normalizer = DocumentNormalizer(workers=(CsvFormulaSanitizer(),), intermediate_dir="work")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    result = normalizer.normalize(source=b"value\n=1+1\n", output_path=None)
    assert result.artifact.path.parent.parent == folder
