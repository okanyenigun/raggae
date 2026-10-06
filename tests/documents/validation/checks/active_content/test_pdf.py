from types import SimpleNamespace
import urllib.request

import pikepdf
import pytest

from raggae.documents.validation import ActiveContentPolicy, PdfActiveContentProbe
from raggae.documents.validation.checks.active_content import pdf as pdf_module
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


REMOTE = "https://evil.test/document"


def annotation(*, subtype="Link", action=None):
    value = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name("/" + subtype),
                              Rect=pikepdf.Array([0, 0, 10, 10]))
    if action is not None:
        value.A = action
    return value


def install_feature(document, feature):
    js = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('test')"))
    launch = pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("test-program"))
    goto = pikepdf.Dictionary(S=pikepdf.Name.GoTo, D=pikepdf.Array([document.pages[0].obj, pikepdf.Name.Fit]))
    if feature == "catalog_js":
        document.Root.Names = pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(Names=pikepdf.Array(["startup", js])))
    elif feature == "catalog_embedded":
        payload = document.make_stream(b"tiny attachment")
        payload.Type = pikepdf.Name.EmbeddedFile
        file_spec = pikepdf.Dictionary(Type=pikepdf.Name.Filespec, F="note.txt", EF=pikepdf.Dictionary(F=payload))
        document.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array(["note.txt", file_spec])))
    elif feature == "open_js":
        document.Root.OpenAction = js
    elif feature == "open_launch":
        document.Root.OpenAction = launch
    elif feature == "catalog_aa":
        document.Root.AA = pikepdf.Dictionary(WC=goto)
    elif feature == "page_aa":
        document.pages[0].obj.AA = pikepdf.Dictionary(O=goto)
    else:
        subtypes = {"annotation_embedded": "FileAttachment", "annotation_rich_media": "RichMedia"}
        actions = {"annotation_js": js, "annotation_launch": launch,
                   "annotation_uri": pikepdf.Dictionary(S=pikepdf.Name.URI, URI=REMOTE),
                   "annotation_remote_goto": pikepdf.Dictionary(S=pikepdf.Name.GoToR, F=REMOTE)}
        document.pages[0].obj.Annots = pikepdf.Array([annotation(subtype=subtypes.get(feature, "Link"),
                                                               action=actions.get(feature))])


@pytest.mark.parametrize("format_name", [None, "pdf", " .PDF "])
@pytest.mark.parametrize("as_string", [False, True])
def test_clean_pdf_has_no_active_findings_or_invented_facts(active_pdf_factory, format_name, as_string):
    path = active_pdf_factory()
    before = path.read_bytes()
    outcome = PdfActiveContentProbe().check(str(path) if as_string else path, format_name)
    assert outcome.codes == ()
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("feature,codes", [
    ("catalog_js", (Code.JAVASCRIPT,)), ("catalog_embedded", (Code.EMBEDDED_FILE,)),
    ("open_js", (Code.AUTO_ACTION, Code.JAVASCRIPT)),
    ("open_launch", (Code.AUTO_ACTION, Code.LAUNCH_ACTION)),
    ("catalog_aa", (Code.AUTO_ACTION,)), ("page_aa", (Code.AUTO_ACTION,)),
    ("annotation_embedded", (Code.EMBEDDED_FILE,)), ("annotation_rich_media", (Code.RICH_MEDIA,)),
    ("annotation_js", (Code.JAVASCRIPT,)), ("annotation_launch", (Code.LAUNCH_ACTION,)),
    ("annotation_uri", (Code.REMOTE_REFERENCE,)), ("annotation_remote_goto", (Code.REMOTE_REFERENCE,)),
])
@pytest.mark.parametrize("indirect", [False, True])
def test_real_catalog_and_annotation_features(active_pdf_factory, feature, codes, indirect):
    def customize(document):
        install_feature(document, feature)
        if indirect:
            annotations = document.pages[0].obj.get("/Annots")
            if annotations:
                for index, value in enumerate(annotations):
                    annotations[index] = document.make_indirect(value)
            if "/OpenAction" in document.Root:
                document.Root.OpenAction = document.make_indirect(document.Root.OpenAction)

    path = active_pdf_factory(customize=customize)
    before = path.read_bytes()
    outcome = PdfActiveContentProbe().check(path, "pdf")
    assert outcome.codes == codes
    assert dict(outcome.facts) == {}
    if Code.REMOTE_REFERENCE in codes:
        assert dict(outcome.findings[-1].detail) == {"count": 1, "sample": REMOTE}
    assert path.read_bytes() == before


def test_categories_are_unique_sorted_and_references_aggregated(active_pdf_factory):
    def customize(document):
        document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program")
        for page in document.pages:
            page.obj.Annots = pikepdf.Array([
                annotation(subtype="FileAttachment"), annotation(subtype="RichMedia"),
                annotation(action=pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="test")),
                annotation(action=pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://z.evil.test/")),
                annotation(action=pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://a.evil.test/")),
            ])

    path = active_pdf_factory(customize=customize)
    outcome = PdfActiveContentProbe().check(path)
    assert outcome.codes == (Code.AUTO_ACTION, Code.EMBEDDED_FILE, Code.JAVASCRIPT,
                             Code.LAUNCH_ACTION, Code.RICH_MEDIA, Code.REMOTE_REFERENCE)
    assert dict(outcome.findings[-1].detail) == {"count": 2, "sample": "https://a.evil.test/, https://z.evil.test/"}


def test_allowed_uri_does_not_suppress_launch_or_attachments(active_pdf_factory):
    def customize(document):
        document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.Launch, F="program")
        document.pages[0].obj.Annots = pikepdf.Array([
            annotation(subtype="FileAttachment"),
            annotation(action=pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://cdn.example.com/file")),
        ])

    policy = ActiveContentPolicy(allowed_reference_hosts={"example.com"})
    path = active_pdf_factory(customize=customize)
    assert PdfActiveContentProbe(policy).check(path).codes == (Code.AUTO_ACTION, Code.EMBEDDED_FILE, Code.LAUNCH_ACTION)


def test_attachment_payloads_and_references_are_never_read_or_fetched(active_pdf_factory, monkeypatch, forbidden_operation):
    def customize(document):
        install_feature(document, "catalog_embedded")
        install_feature(document, "annotation_uri")

    path = active_pdf_factory(customize=customize)
    monkeypatch.setattr(pikepdf.Object, "read_bytes", forbidden_operation)
    monkeypatch.setattr(pikepdf.Object, "read_raw_bytes", forbidden_operation)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_operation)
    assert PdfActiveContentProbe().check(path).codes == (Code.EMBEDDED_FILE, Code.REMOTE_REFERENCE)


@pytest.mark.parametrize("password", [None, "", "wrong", "secret"])
def test_password_is_used_only_to_inspect_existing_document(active_pdf_factory, password):
    path = active_pdf_factory(encryption=pikepdf.Encryption(user="secret", owner="owner", R=6))
    before = path.read_bytes()
    outcome = PdfActiveContentProbe().check(path, password=password)
    assert outcome.codes == (() if password == "secret" else (Code.UNREADABLE,))
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("password", [None, "", "secret"])
def test_strict_open_password_forwarding_and_closure(monkeypatch, missing_path, password):
    class Document:
        Root = {}
        pages = []
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

    document = Document()
    calls = []

    def open_document(*args, **kwargs):
        calls.append((args, kwargs))
        return document

    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=open_document))
    assert PdfActiveContentProbe().check(missing_path, password=password).codes == ()
    assert calls == [((missing_path,), {"password": password or "", "attempt_recovery": False})]
    assert document.closed


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_and_corrupt_pdf_are_unreadable(file_factory, missing_path, kind):
    path = missing_path if kind == "missing" else file_factory("broken.pdf", b"not a PDF")
    outcome = PdfActiveContentProbe().check(path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"path": path.name}


@pytest.mark.parametrize("format_name,normalized", [("docx", "docx"), (" .JPG ", "jpeg"), ("", "")])
def test_mismatched_format_never_opens(monkeypatch, missing_path, forbidden_operation, format_name, normalized):
    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=forbidden_operation))
    outcome = PdfActiveContentProbe().check(missing_path, format_name)
    assert outcome.codes == (Code.NOT_APPLICABLE,)
    assert dict(outcome.facts) == {}
    assert dict(outcome.findings[0].detail) == {"detected_format": normalized}


@pytest.mark.parametrize("bad_path", [None, 1, b"document.pdf", object()])
def test_invalid_path_type_is_programming_error(bad_path):
    with pytest.raises(TypeError, match="path must be a Path or str"):
        PdfActiveContentProbe().check(bad_path)


@pytest.mark.parametrize("error_type", [OSError, pikepdf.PdfError, pikepdf.PasswordError])
def test_open_errors_are_unreadable(monkeypatch, missing_path, error_type):
    def fail(*args, **kwargs):
        raise error_type("cannot open document")

    monkeypatch.setattr(pdf_module.pikepdf, "Pdf", SimpleNamespace(open=fail))
    outcome = PdfActiveContentProbe().check(missing_path)
    assert outcome.codes == (Code.UNREADABLE,)
    assert dict(outcome.facts) == {}


def test_repeated_calls_and_policy_are_instance_local(active_pdf_factory):
    policy = ActiveContentPolicy(allowed_reference_hosts={"evil.test"})
    probe = PdfActiveContentProbe(policy)
    remote = active_pdf_factory("remote.pdf", customize=lambda document: install_feature(document, "annotation_uri"))
    clean = active_pdf_factory("clean.pdf")
    assert probe.check(remote).codes == ()
    assert probe.check(clean).codes == ()
    assert PdfActiveContentProbe().check(remote).codes == (Code.REMOTE_REFERENCE,)
    assert probe.policy is policy
    assert probe.name == "validation_active_content_pdf"
    assert probe.handles == frozenset({"pdf"})
