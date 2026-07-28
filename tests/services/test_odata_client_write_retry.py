"""Retry policy for OData write calls.

The posting chain (Unpost → Post) and PATCH are idempotent in 1C, so a 502/503
from an overloaded gateway must be retried instead of aborting an export.
Document creation is NOT idempotent: retrying after the request may have
reached 1C would create a duplicate document.
"""

import io
import socket
import urllib.error
from email.message import Message

import pytest

import app.services.odata_client as odata_client
from app.services.odata_client import OData1CClient


class _FakeResponse:
    def __init__(self, body: bytes = b'{"Ref_Key": "x"}', content_type: str = "application/json"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://1c/doc", code, "Service Unavailable", headers, io.BytesIO(b"busy")
    )


@pytest.fixture
def sent(monkeypatch):
    """Record every urlopen call and never sleep for real."""
    calls: list[dict] = []
    slept: list[float] = []

    monkeypatch.setattr(odata_client.time, "sleep", lambda s: slept.append(s))
    calls_ref = {"calls": calls, "slept": slept, "responses": []}

    def _urlopen(request, timeout=None):
        calls.append({
            "method": request.get_method(),
            "url": request.full_url,
            "data": request.data,
            "timeout": timeout,
        })
        outcome = calls_ref["responses"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(odata_client.urllib.request, "urlopen", _urlopen)
    return calls_ref


def _client() -> OData1CClient:
    return OData1CClient("http://1c/odata", username="u", password="p")


def test_post_operation_retries_gateway_errors(sent):
    sent["responses"] = [_http_error(503), _http_error(502), _FakeResponse()]
    out = _client().post_operation("Document_X(guid'1')/Post?PostingModeOperational=true")
    assert out == {"Ref_Key": "x"}
    assert len(sent["calls"]) == 3
    assert sent["slept"] == [1.0, 2.0]          # exponential backoff
    assert all(c["timeout"] == 60 for c in sent["calls"])


def test_post_operation_honours_retry_after(sent):
    sent["responses"] = [_http_error(429, retry_after="5"), _FakeResponse()]
    _client().post_operation("Document_X(guid'1')/Unpost")
    assert sent["slept"] == [5.0]


def test_post_operation_gives_up_after_retries_and_reports_http_error(sent):
    sent["responses"] = [_http_error(503) for _ in range(4)]
    with pytest.raises(urllib.error.URLError, match="HTTP Error 503"):
        _client().post_operation("Document_X(guid'1')/Post", retries=3)
    assert len(sent["calls"]) == 4


def test_post_operation_does_not_retry_business_errors(sent):
    """500 from 1C is a failed operation, not congestion — repeating is pointless."""
    sent["responses"] = [_http_error(500)]
    with pytest.raises(urllib.error.URLError, match="HTTP Error 500"):
        _client().post_operation("Document_X(guid'1')/Post")
    assert len(sent["calls"]) == 1


def test_post_operation_retries_network_errors(sent):
    sent["responses"] = [urllib.error.URLError(socket.timeout("read timeout")), _FakeResponse()]
    _client().post_operation("Document_X(guid'1')/Post")
    assert len(sent["calls"]) == 2


def test_patch_retries_gateway_errors_and_keeps_payload(sent):
    sent["responses"] = [_http_error(504), _FakeResponse()]
    _client().patch("Document_X(guid'1')", {"Дата": "2026-07-28"})
    assert len(sent["calls"]) == 2
    assert sent["calls"][0]["method"] == "PATCH"
    assert sent["calls"][0]["data"] == sent["calls"][1]["data"]


def test_document_create_is_never_retried_after_the_request_left(sent):
    """A 5xx may still have created the document — a retry would duplicate it."""
    sent["responses"] = [_http_error(503)]
    with pytest.raises(urllib.error.URLError, match="HTTP Error 503"):
        _client().post("Document_ЗаказНаПроизводство", {"Номер": "1"})
    assert len(sent["calls"]) == 1


def test_document_create_is_not_retried_on_ambiguous_network_errors(sent):
    """A reset/timeout after the body was sent could mean 1C already recorded it."""
    sent["responses"] = [urllib.error.URLError(socket.timeout("timed out"))]
    with pytest.raises(urllib.error.URLError):
        _client().post("Document_ЗаказНаПроизводство", {"Номер": "1"})
    assert len(sent["calls"]) == 1


def test_document_create_retries_pre_send_connection_failures(sent):
    """Connection refused happens before the body is sent — no duplicate risk."""
    sent["responses"] = [
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        _FakeResponse(),
    ]
    out = _client().post("Document_ЗаказНаПроизводство", {"Номер": "1"})
    assert out == {"Ref_Key": "x"}
    assert len(sent["calls"]) == 2


def test_document_create_retries_dns_failures(sent):
    sent["responses"] = [urllib.error.URLError(socket.gaierror("name resolution")), _FakeResponse()]
    _client().post("Document_ЗаказНаПроизводство", {"Номер": "1"})
    assert len(sent["calls"]) == 2


def test_all_write_calls_pass_an_explicit_timeout(sent):
    sent["responses"] = [_FakeResponse(), _FakeResponse(), _FakeResponse()]
    client = _client()
    client.post("Document_X", {"a": 1}, timeout=17)
    client.patch("Document_X(guid'1')", {"a": 1}, timeout=18)
    client.post_operation("Document_X(guid'1')/Post", timeout=19)
    assert [c["timeout"] for c in sent["calls"]] == [17, 18, 19]


def test_write_sends_auth_header_on_every_attempt(sent):
    sent["responses"] = [_http_error(503), _FakeResponse()]
    _client().post_operation("Document_X(guid'1')/Post")
    assert len(sent["calls"]) == 2
