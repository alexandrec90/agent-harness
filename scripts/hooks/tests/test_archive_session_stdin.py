"""Unit tests for archive-session.py's UTF-8 stdin payload reader."""

import io
import sys

import pytest
from conftest import load_module

arch = load_module("scripts/hooks/archive-session.py")


class _FakeStdin:
    """stdin stand-in exposing .buffer/.read() for read_stdin_payload()."""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def read(self):
        return self.buffer.read().decode("utf-8", "surrogateescape")


def test_read_stdin_payload_parses_utf8(monkeypatch):
    payload = {"transcript_path": "/x/sesión.jsonl", "cwd": "/repo"}
    import json

    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(payload).encode("utf-8")))
    assert arch.read_stdin_payload() == payload


def test_read_stdin_payload_tolerates_undecodable_byte(monkeypatch):
    # Byte 0x9d (the '\udc9d' that crashed the stop hook) sits inside a JSON string
    # value; decoding must not raise and the surrounding structure must still parse.
    raw = b'{"transcript_path": "/x/a\x9d.jsonl", "cwd": "/repo"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(raw))
    result = arch.read_stdin_payload()
    assert result["cwd"] == "/repo"
    assert result["transcript_path"].encode("utf-8", "surrogateescape") == b"/x/a\x9d.jsonl"


def test_read_stdin_payload_raises_on_garbage(monkeypatch):
    # main() wraps this in a broad try/except that logs and exits 0, so a parse
    # failure here is contained rather than crashing the hook.
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"not json"))
    with pytest.raises(ValueError):
        arch.read_stdin_payload()
