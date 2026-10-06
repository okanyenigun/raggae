from types import SimpleNamespace

from pypdf.generic import IndirectObject
import pytest

from raggae.documents.validation import PdfCapabilityProbe


class ObservedObject(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inspections = 0

    def get(self, key, default=None):
        if key == "/Subtype":
            self.inspections += 1
            if self.inspections > 1:
                pytest.fail("A reused/cyclic object must not be inspected again.")
        return super().get(key, default)

    def get_data(self):
        pytest.fail("Image/form streams must not be decoded for metadata discovery.")


@pytest.mark.parametrize("cycle_size", [1, 2])
@pytest.mark.parametrize("with_image", [False, True])
@pytest.mark.parametrize("indirect", [False, True])
def test_cyclic_forms_terminate_and_still_find_reachable_image(cycle_size, with_image, indirect):
    forms = [ObservedObject({"/Subtype": "/Form"}) for _ in range(cycle_size)]
    image = ObservedObject({"/Subtype": "/Image"})
    registry = {index + 1: form for index, form in enumerate(forms)}
    registry[99] = image
    reader = SimpleNamespace(get_object=lambda reference: registry[reference.idnum])

    def reference(index):
        return IndirectObject(index, 0, reader) if indirect else registry[index]

    for index, form in enumerate(forms):
        objects = {}
        if with_image and index == 0:
            objects["/Im"] = reference(99)
        # LIFO traversal sees the cycle before the queued image.
        objects["/Loop"] = reference((index + 1) % cycle_size + 1)
        form["/Resources"] = {"/XObject": objects}
    page = {"/Resources": {"/XObject": {"/Fm": reference(1)}}}
    assert PdfCapabilityProbe._has_image(page) is with_image
    assert [form.inspections for form in forms] == [1] * cycle_size
    assert image.inspections == int(with_image)


@pytest.mark.parametrize("with_image", [False, True])
def test_shared_form_aliases_are_inspected_once(with_image):
    image = ObservedObject({"/Subtype": "/Image"})
    shared = ObservedObject({"/Subtype": "/Form", "/Resources": {
        "/XObject": {"/Im": image} if with_image else {}}})
    page = {"/Resources": {"/XObject": {f"/Alias{index}": shared for index in range(50)}}}
    assert PdfCapabilityProbe._has_image(page) is with_image
    assert shared.inspections == 1
    assert image.inspections == int(with_image)


@pytest.mark.parametrize("with_image", [False, True])
def test_deep_form_graph_does_not_use_python_recursion(with_image):
    image = ObservedObject({"/Subtype": "/Image"})
    objects = {"/Im": image} if with_image else {}
    forms = []
    for _ in range(1500):
        form = ObservedObject({"/Subtype": "/Form", "/Resources": {"/XObject": objects}})
        forms.append(form)
        objects = {"/Fm": form}
    page = {"/Resources": {"/XObject": objects}}
    assert PdfCapabilityProbe._has_image(page) is with_image
    assert all(form.inspections == 1 for form in forms)
    assert image.inspections == int(with_image)


@pytest.mark.parametrize("form", [{"/Subtype": "/Form"},
                                 {"/Subtype": "/Form", "/Resources": None},
                                 {"/Subtype": "/Form", "/Resources": {}},
                                 {"/Subtype": "/Form", "/Resources": {"/XObject": {}}}])
def test_empty_form_is_not_itself_an_image(form):
    assert PdfCapabilityProbe._has_image({"/Resources": {"/XObject": {"/Fm": form}}}) is False
