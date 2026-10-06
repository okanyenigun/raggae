import pikepdf
import pytest

from raggae.documents.validation import PdfActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("location,codes", [
    ("catalog-aa", (Code.AUTO_ACTION, Code.LAUNCH_ACTION)),
    ("page-aa", (Code.AUTO_ACTION, Code.JAVASCRIPT)),
    ("next", (Code.AUTO_ACTION, Code.LAUNCH_ACTION, Code.REMOTE_REFERENCE)),
])
@pytest.mark.parametrize("indirect", [False, True])
def test_nested_action_type_is_reported_not_just_its_container(active_pdf_factory, location, codes, indirect):
    def customize(document):
        launch = pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("test-program"))
        js = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('test')"))
        if indirect:
            launch = document.make_indirect(launch)
            js = document.make_indirect(js)
        if location == "catalog-aa":
            document.Root.AA = pikepdf.Dictionary(WC=launch)
        elif location == "page-aa":
            document.pages[0].obj.AA = pikepdf.Dictionary(O=js)
        else:
            document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://evil.test/", Next=launch)

    path = active_pdf_factory(customize=customize)
    before = path.read_bytes()
    with pikepdf.Pdf.open(path, attempt_recovery=False) as document:
        if location == "catalog-aa":
            assert str(document.Root.AA.WC.S) == "/Launch"
        elif location == "page-aa":
            assert str(document.pages[0].obj.AA.O.S) == "/JavaScript"
        else:
            assert str(document.Root.OpenAction.Next.S) == "/Launch"
    outcome = PdfActiveContentProbe().check(path, "pdf")
    assert path.read_bytes() == before
    assert outcome.codes == codes
