"""Internal encoding errors, distinct from source and destination I/O errors."""


class InvalidTextEncoding(Exception):
    """Separate decoder errors from exceptions raised by caller-owned streams."""


class InvalidTargetEncoding(Exception):
    """Separate encoder errors from exceptions raised by destination streams."""
