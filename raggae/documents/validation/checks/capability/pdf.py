from collections.abc import Mapping, Sequence
from pathlib import Path
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import IndirectObject, NullObject
from .policy import CapabilityPolicy, CapabilityFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome, Finding, FactValue


class _ResourceInspectionError(ValueError):
    """Image resource metadata is malformed or could not be inspected."""


class PdfCapabilityProbe:
    """
    Extracts text from the sampled pages and reports how much came out.

    The only probe here, because PDF is the only format whose answer is not
    already known: an image is pixels and a `.txt` is text, but a PDF can look
    like text and not be.

    A scanned page is rarely empty — a stray header or page number survives — so
    "has text" requires nonempty text meeting the policy's character floor.
    """

    def __init__(self, policy: CapabilityPolicy | None = None) -> None:
        self._policy = policy or CapabilityPolicy()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)
        skipped = applicability(
            self.handles, detected_format, self.name, CapabilityFinding.NOT_APPLICABLE
        )
        if skipped:
            return CheckOutcome(findings=tuple(skipped))

        try:
            reader = self._open(target, password)
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(CapabilityFinding.UNREADABLE, target, error),)
            )

        sampled = reader.pages[: self._policy.sampled_pages]
        characters = 0
        pages_with_text = 0
        pages_with_images = 0

        for page in sampled:
            length = self._text_length(page)
            characters += length
            if length > 0 and length >= self._policy.min_characters_per_page:
                pages_with_text += 1
            try:
                has_image = self._has_image(page)
            except _ResourceInspectionError as error:
                return CheckOutcome(
                    findings=(unreadable(CapabilityFinding.UNREADABLE, target, error),)
                )
            if has_image:
                pages_with_images += 1

        has_text = pages_with_text > 0
        facts: dict[str, FactValue] = {
            "has_extractable_text": has_text,
            "pages_sampled": len(sampled),
            "pages_with_text": pages_with_text,
            "characters_sampled": characters,
        }
        return CheckOutcome(
            findings=tuple(self._check_empty(sampled, has_text, pages_with_images)),
            facts=facts,
        )

    @property
    def name(self) -> str:
        return "validation_capability_pdf"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"pdf"})

    @property
    def policy(self) -> CapabilityPolicy:
        return self._policy

    def _open(self, path: Path, password: str | None) -> PdfReader:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise ValueError("document is encrypted and the password was not accepted")
        return reader

    @staticmethod
    def _text_length(page: object) -> int:
        """
        Characters of real text on a page, ignoring whitespace.

        A page of blank lines extracts as a long string of newlines, which is not
        text by any measure that matters.
        """
        try:
            extracted = page.extract_text() or ""  # type: ignore[attr-defined]
        except Exception:
            return 0
        return len("".join(extracted.split()))

    @staticmethod
    def _has_image(page: object) -> bool:
        """
        Whether the page draws an image, read from its resources.

        Inspect image and form dictionaries, including indirect references, without
        decoding pixels. Track visited objects so reused or cyclic forms terminate.
        Missing/null dictionaries are empty; inspection failures are not absence.
        """
        def resolve(value: object) -> object:
            if isinstance(value, IndirectObject):
                resolved = value.get_object()
                if resolved is None:
                    raise _ResourceInspectionError("PDF resource reference cannot be resolved.")
                return resolved
            return value

        def dictionary(value: object, label: str, *, optional: bool = False) -> Mapping:
            resolved = resolve(value)
            if optional and (resolved is None or isinstance(resolved, NullObject)):
                return {}
            if not isinstance(resolved, Mapping):
                raise _ResourceInspectionError(f"PDF {label} must be a dictionary.")
            return resolved

        try:
            resources = dictionary(page.get("/Resources"), "resources", optional=True)  # type: ignore[attr-defined]
            xobjects = dictionary(resources.get("/XObject"), "XObjects", optional=True)
            pending = list(xobjects.values())
            visited: dict[int, object] = {}
            while pending:
                xobject = dictionary(pending.pop(), "XObject")
                identity = id(xobject)
                if identity in visited:
                    continue
                visited[identity] = xobject
                subtype = str(xobject.get("/Subtype", ""))
                if subtype == "/Image":
                    return True
                if subtype == "/Form":
                    resources = dictionary(xobject.get("/Resources"), "form resources", optional=True)
                    nested = dictionary(resources.get("/XObject"), "form XObjects", optional=True)
                    pending.extend(nested.values())
            return False
        except _ResourceInspectionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, PdfReadError) as error:
            raise _ResourceInspectionError("PDF image resources could not be inspected.") from error

    def _check_empty(
        self, sampled: Sequence[object], has_text: bool, pages_with_images: int
    ) -> list[Finding]:
        """
        Report a document with neither text nor pictures of text.

        No text plus images is a scan, which is ordinary and needs OCR. No text
        and no images is a document with nothing in it, and saying so here beats
        an empty entry appearing in the index later.
        """
        if has_text or pages_with_images:
            return []
        return [
            make_finding(
                CapabilityFinding.EMPTY_DOCUMENT,
                "Sampled pages hold neither text nor images.",
                pages_sampled=len(sampled),
            )
        ]
