import pikepdf
from pathlib import Path
from .policy import PdfLimits, ResourceFinding
from ..utils import require_path, applicability, unreadable, make_finding
from ...schemas.result import CheckOutcome, FactValue, Finding


class PdfResourceProbe:
    """
    Page count, page dimensions and embedded image dimensions, read from the
    document's own structure. Nothing is rendered and no image is decoded.

    The page count is checked first, so a document declaring fifty thousand pages
    is refused before anything walks fifty thousand pages.
    """

    def __init__(self, policy: PdfLimits | None = None) -> None:
        self._policy = policy or PdfLimits()

    def check(
        self,
        path: Path,
        detected_format: str | None = None,
        password: str | None = None,
    ) -> CheckOutcome:
        target = require_path(path)

        skipped = applicability(
            self.handles, detected_format, self.name, ResourceFinding.NOT_APPLICABLE
        )
        if skipped:
            return CheckOutcome(findings=tuple(skipped))

        try:
            document = pikepdf.Pdf.open(
                target, password=password or "", attempt_recovery=False
            )
        except Exception as error:
            return CheckOutcome(
                findings=(unreadable(ResourceFinding.UNREADABLE, target, error),)
            )

        with document:
            page_count = len(document.pages)
            facts: dict[str, FactValue] = {"page_count": page_count}

            over_limit = self._check_page_count(page_count)
            if over_limit:
                # Refuse before walking the pages we just refused.
                return CheckOutcome(findings=tuple(over_limit), facts=facts)

            try:
                findings = self._check_pages(document)
            except (OSError, pikepdf.PdfError, TypeError, ValueError) as error:
                # Missing inspection results are not evidence of safe dimensions.
                # Keep only the page count already established successfully.
                return CheckOutcome(
                    findings=(unreadable(ResourceFinding.UNREADABLE, target, error),),
                    facts=facts,
                )
            return CheckOutcome(findings=tuple(findings), facts=facts)

    @property
    def name(self) -> str:
        return "validation_resource_limits_pdf"

    @property
    def handles(self) -> frozenset[str]:
        return frozenset({"pdf"})

    @property
    def policy(self) -> PdfLimits:
        return self._policy

    def _check_page_count(self, page_count: int) -> list[Finding]:
        limit = self._policy.max_pages
        if page_count <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.TOO_MANY_PAGES,
                "Document declares more pages than the limit allows.",
                limit=limit,
                observed=page_count,
            )
        ]

    def _check_pages(self, document: pikepdf.Pdf) -> list[Finding]:
        findings: list[Finding] = []
        page_reported = False
        image_reported = False

        for number, page in enumerate(document.pages, start=1):
            if not page_reported:
                oversized = self._check_page_size(page, number)
                if oversized:
                    findings += oversized
                    page_reported = True
            if not image_reported:
                oversized = self._check_page_images(page, number)
                if oversized:
                    findings += oversized
                    image_reported = True
            if page_reported and image_reported:
                break
        return findings

    def _check_page_size(self, page: object, number: int) -> list[Finding]:
        box = getattr(page, "mediabox", None)
        if box is None:
            return []
        try:
            left, bottom, right, top = (float(value) for value in box)
        except (TypeError, ValueError):
            return []

        longest = max(abs(right - left), abs(top - bottom))
        limit = self._policy.max_page_points
        if longest <= limit:
            return []
        return [
            make_finding(
                ResourceFinding.PAGE_TOO_LARGE,
                f"Page {number} declares an edge longer than the limit allows.",
                limit=limit,
                observed=longest,
                page=number,
            )
        ]

    def _check_page_images(self, page: object, number: int) -> list[Finding]:
        images = dict(page.get_images())  # type: ignore[attr-defined]

        limit = self._policy.max_image_pixels
        for image in images.values():
            width = int(getattr(image, "Width", 0) or 0)
            height = int(getattr(image, "Height", 0) or 0)
            pixels = width * height
            if pixels > limit:
                return [
                    make_finding(
                        ResourceFinding.IMAGE_TOO_LARGE,
                        f"An image on page {number} declares more pixels than "
                        "the limit allows.",
                        limit=limit,
                        observed=pixels,
                        page=number,
                    )
                ]
        return []
