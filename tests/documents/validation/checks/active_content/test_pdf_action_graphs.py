from types import SimpleNamespace

import pikepdf
import pytest

from raggae.documents.validation import ActiveContentPolicy, PdfActiveContentProbe
from raggae.documents.validation.checks.active_content import pdf as pdf_module
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.fixture
def bounded_metadata_reads(monkeypatch):
    """A broken cycle guard should fail, not leave pytest in an infinite loop."""
    original_get = pikepdf.Object.get
    reads = []

    def get(value, *args, **kwargs):
        reads.append(args[0])
        if len(reads) > 10_000:
            pytest.fail("Action graph inspection exceeded its bounded metadata reads")
        return original_get(value, *args, **kwargs)

    monkeypatch.setattr(pikepdf.Object, "get", get)
    return reads


@pytest.mark.parametrize("shape", ["self", "two-nodes", "array"])
def test_real_indirect_cycles_terminate_and_keep_all_categories(active_pdf_factory, bounded_metadata_reads, shape):
    def customize(document):
        root = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://evil.test/"))
        if shape == "self":
            root.Next = root
        else:
            launch = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program"))
            launch.Next = root
            root.Next = launch if shape == "two-nodes" else pikepdf.Array([root, launch, launch])
        document.Root.OpenAction = root

    path = active_pdf_factory(customize=customize)
    outcome = PdfActiveContentProbe().check(path)
    expected = (Code.AUTO_ACTION, Code.REMOTE_REFERENCE) if shape == "self" else (
        Code.AUTO_ACTION, Code.LAUNCH_ACTION, Code.REMOTE_REFERENCE)
    assert outcome.codes == expected
    assert dict(outcome.findings[-1].detail) == {"count": 1, "sample": "https://evil.test/"}
    assert len(bounded_metadata_reads) < 100


def test_real_shared_action_array_is_scanned_once_per_chain(active_pdf_factory, bounded_metadata_reads):
    def customize(document):
        shared = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://evil.test/"))
        launch = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program", Next=shared))
        js = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="test", Next=shared))
        document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.Named, N=pikepdf.Name.NextPage,
                                                    Next=pikepdf.Array([launch, js, shared, launch]))

    path = active_pdf_factory(customize=customize)
    outcome = PdfActiveContentProbe().check(path)
    assert outcome.codes == (Code.AUTO_ACTION, Code.JAVASCRIPT, Code.LAUNCH_ACTION, Code.REMOTE_REFERENCE)
    assert outcome.findings[-1].detail["count"] == 1
    assert len(bounded_metadata_reads) < 100


def test_real_long_indirect_chain_does_not_use_python_recursion(active_pdf_factory, bounded_metadata_reads):
    def customize(document):
        current = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program"))
        for _ in range(1_300):
            current = document.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Named, N=pikepdf.Name.NextPage, Next=current))
        document.Root.OpenAction = current

    path = active_pdf_factory(customize=customize)
    assert PdfActiveContentProbe().check(path).codes == (Code.AUTO_ACTION, Code.LAUNCH_ACTION)
    assert len(bounded_metadata_reads) < 5_000


@pytest.mark.parametrize("indirect", [False, True])
def test_annotation_additional_actions_are_inspected(active_pdf_factory, indirect):
    def customize(document):
        launch = pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program")
        if indirect:
            launch = document.make_indirect(launch)
        document.pages[0].obj.Annots = pikepdf.Array([
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                               Rect=pikepdf.Array([0, 0, 10, 10]), AA=pikepdf.Dictionary(E=launch))
        ])

    path = active_pdf_factory(customize=customize)
    assert PdfActiveContentProbe().check(path).codes == (Code.AUTO_ACTION, Code.LAUNCH_ACTION)


def test_allowlist_cannot_hide_launch_in_a_chain(active_pdf_factory):
    def customize(document):
        document.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.URI, URI="https://allowed.test/",
            Next=pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program"))

    path = active_pdf_factory(customize=customize)
    policy = ActiveContentPolicy(allowed_reference_hosts={"allowed.test"})
    assert PdfActiveContentProbe(policy).check(path).codes == (Code.AUTO_ACTION, Code.LAUNCH_ACTION)


@pytest.mark.parametrize("shape", ["self", "two-nodes", "array"])
def test_python_mapping_cycles_are_safe_and_document_closes(monkeypatch, missing_path, shape):
    class CountingAction(dict):
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            assert self.calls < 20, "Direct action cycle was not stopped"
            return super().get(*args, **kwargs)

    root = CountingAction({"/S": "/URI", "/URI": "https://evil.test/"})
    launch = CountingAction({"/S": "/Launch", "/F": "program", "/Next": root})
    root["/Next"] = root if shape == "self" else launch if shape == "two-nodes" else [root, launch, launch]

    class Document:
        Root = {"/OpenAction": root}
        pages = []
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=lambda *a, **k: document))
    outcome = PdfActiveContentProbe().check(missing_path)
    assert document.closed
    expected = (Code.AUTO_ACTION, Code.REMOTE_REFERENCE) if shape == "self" else (
        Code.AUTO_ACTION, Code.LAUNCH_ACTION, Code.REMOTE_REFERENCE)
    assert outcome.codes == expected


@pytest.mark.parametrize("following", [None, 7, "not an action", [None, 7, "not an action"]])
def test_non_action_chain_values_are_not_executed_or_classified(active_pdf_factory, following):
    def customize(document):
        action = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="test")
        if following is not None:
            action.Next = pikepdf.Array(following) if isinstance(following, list) else following
        document.Root.OpenAction = action

    path = active_pdf_factory(customize=customize)
    assert PdfActiveContentProbe().check(path).codes == (Code.AUTO_ACTION, Code.JAVASCRIPT)
