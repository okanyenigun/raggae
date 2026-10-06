import pikepdf
import pytest

from raggae.documents.validation import PdfActiveContentProbe
from raggae.documents.validation.checks.active_content.policy import ActiveContentFinding as Code


@pytest.mark.parametrize("indirect", [False, True])
def test_valid_initial_page_destination_is_not_an_action_dictionary(active_pdf_factory, indirect):
    def customize(document):
        destination = pikepdf.Array([document.pages[0].obj, pikepdf.Name.Fit])
        document.Root.OpenAction = document.make_indirect(destination) if indirect else destination

    path = active_pdf_factory(customize=customize)
    before = path.read_bytes()
    # Verify the saved fixture is an ordinary explicit destination, not an action.
    with pikepdf.Pdf.open(path, attempt_recovery=False) as document:
        destination = document.Root.OpenAction
        assert len(destination) == 2
        assert destination[0] == document.pages[0].obj
        assert str(destination[1]) == "/Fit"
    outcome = PdfActiveContentProbe().check(path, "pdf")
    assert outcome.codes == (Code.AUTO_ACTION,)
    assert dict(outcome.facts) == {}
    assert path.read_bytes() == before
