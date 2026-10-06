from io import BytesIO

import pytest


class TrackedStream:
    """A real file descriptor with observable, optionally controlled reads."""

    def __init__(self, path, *, payload=None, read_error=None):
        self.raw = path.open("rb")
        self.content = BytesIO(payload) if payload is not None else self.raw
        self.read_error = read_error
        self.read_sizes = []

    def fileno(self):
        return self.raw.fileno()

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.content.read(size)

    @property
    def closed(self):
        return self.raw.closed

    def close(self):
        self.content.close()
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def tracked_stream_factory():
    streams = []

    def make_stream(path, **options):
        stream = TrackedStream(path, **options)
        streams.append(stream)
        return stream

    yield make_stream
    for stream in streams:
        stream.close()
