"""Remote PaddleOCR transport and response parsing tests."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_deep_research.documents.parsers import (
    DocumentParseError,
    _ocr_image,
    _remote_ocr,
    _texts_from_ocr_result,
)
from open_deep_research.documents.settings import DocumentSettings


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://ocr.test/ocr")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("request failed", request=request, response=response)

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    response: _FakeResponse
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def _settings(tmp_path: Path) -> DocumentSettings:
    return DocumentSettings(
        storage_dir=tmp_path,
        ocr_mode="remote",
        ocr_url="http://ocr.test/ocr",
        ocr_api_key="secret",
        ocr_timeout_seconds=17,
    )


def test_remote_ocr_sends_json_base64_and_bearer_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(
        {
            "logId": "run-1",
            "result": {
                "ocrResults": [
                    {"prunedResult": {"rec_texts": ["内部结论"], "rec_scores": [0.98]}}
                ],
                "dataInfo": {"width": 10, "height": 10, "type": "image"},
            },
            "errorCode": 0,
            "errorMsg": "Success",
        }
    )
    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", _FakeClient)

    result = _remote_ocr(b"png-bytes", _settings(tmp_path))

    call = _FakeClient.calls[0]
    assert call["url"] == "http://ocr.test/ocr"
    assert call["headers"] == {"Authorization": "Bearer secret"}
    assert call["json"]["file"] == base64.b64encode(b"png-bytes").decode("ascii")
    assert call["json"]["fileType"] == 0
    assert call["json"]["visualize"] is False
    assert result["result"]["ocrResults"][0]["prunedResult"]["rec_texts"] == ["内部结论"]


def test_remote_response_is_parsed_from_paddle_nested_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.response = _FakeResponse(
        {
            "result": {
                "ocrResults": [
                    {
                        "prunedResult": {
                            "rec_texts": ["第一行", "第二行"],
                            "rec_scores": [0.91, 0.88],
                        }
                    }
                ]
            },
            "errorCode": 0,
        }
    )
    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", _FakeClient)

    text, confidence = _ocr_image(b"png-bytes", _settings(tmp_path))

    assert text == "第一行\n第二行"
    assert confidence == pytest.approx(0.895)


def test_remote_error_code_is_exposed_as_deterministic_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.response = _FakeResponse(
        {"logId": "run-2", "errorCode": 1002, "errorMsg": "bad input"}
    )
    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", _FakeClient)

    with pytest.raises(DocumentParseError, match="document_ocr_remote_error_1002"):
        _remote_ocr(b"png-bytes", _settings(tmp_path))


def test_remote_http_failure_and_timeout_have_distinct_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.response = _FakeResponse({"errorCode": 0}, status_code=503)
    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", _FakeClient)
    with pytest.raises(DocumentParseError, match="document_ocr_remote_http_503"):
        _remote_ocr(b"png-bytes", _settings(tmp_path))

    class TimeoutClient(_FakeClient):
        def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            request = httpx.Request("POST", url)
            raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", TimeoutClient)
    with pytest.raises(DocumentParseError, match="document_ocr_remote_timeout"):
        _remote_ocr(b"png-bytes", _settings(tmp_path))


def test_remote_ocr_reads_path_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse({"result": {"ocrResults": []}, "errorCode": 0})
    monkeypatch.setattr("open_deep_research.documents.parsers.httpx.Client", _FakeClient)
    image = tmp_path / "image.png"
    image.write_bytes(b"file-bytes")

    _remote_ocr(str(image), _settings(tmp_path))

    assert _FakeClient.calls[0]["json"]["file"] == base64.b64encode(
        b"file-bytes"
    ).decode("ascii")


def test_text_extractor_supports_legacy_line_objects() -> None:
    assert _texts_from_ocr_result(
        [{"text": "legacy", "confidence": 0.75}]
    ) == [("legacy", 0.75)]
