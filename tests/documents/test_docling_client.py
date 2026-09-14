"""Docling Serve client behaviour with a mocked transport."""

from __future__ import annotations

import httpx
import pytest

from open_deep_research.documents import docling
from open_deep_research.documents.parsers import DocumentParseError
from open_deep_research.documents.settings import DocumentSettings

SETTINGS = DocumentSettings(
    docling_base_url="http://docling.test",
    docling_api_key="sk-test",
    docling_timeout_seconds=5.0,
    docling_poll_seconds=0.0,
)


def _install(monkeypatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def fake_client(_settings, _timeout):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler),
            base_url="http://docling.test",
            headers={"X-API-Key": "sk-test"},
        )

    monkeypatch.setattr(docling, "_client", fake_client)
    return requests


def test_settings_detect_configured_endpoint():
    assert DocumentSettings(docling_base_url="http://x").docling_configured
    assert not DocumentSettings(docling_base_url="").docling_configured


@pytest.mark.asyncio
async def test_convert_reports_task_and_returns_result(monkeypatch, tmp_path):
    polls = {"count": 0}
    notified: list[str] = []
    sample = tmp_path / "a.pdf"
    sample.write_bytes(b"%PDF-1.4 fake")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convert/file/async":
            return httpx.Response(200, json={"task_id": "t-1"})
        if request.url.path == "/v1/status/poll/t-1":
            polls["count"] += 1
            if polls["count"] == 1:
                return httpx.Response(200, json={"task_status": "pending"})
            return httpx.Response(200, json={"task_status": "success"})
        if request.url.path == "/v1/result/t-1":
            return httpx.Response(
                200,
                json={"status": "success", "document": {"json_content": {"items": []}}},
            )
        return httpx.Response(404)

    requests = _install(monkeypatch, handler)

    async def on_submitted(task_id: str) -> None:
        notified.append(task_id)

    task_id, result = await docling.convert(
        sample, "a.pdf", "application/pdf", SETTINGS, on_submitted=on_submitted
    )

    assert task_id == "t-1" and result["status"] == "success"
    assert notified == ["t-1"]  # persisted right after submit, before the poll loop
    submit = next(r for r in requests if r.url.path == "/v1/convert/file/async")
    assert submit.headers["x-api-key"] == "sk-test"
    assert b"to_formats" in submit.content and b"json" in submit.content


@pytest.mark.asyncio
async def test_convert_maps_upstream_failure(monkeypatch, tmp_path):
    sample = tmp_path / "a.pdf"
    sample.write_bytes(b"%PDF-1.4")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convert/file/async":
            return httpx.Response(200, json={"task_id": "t-2"})
        return httpx.Response(
            200,
            json={
                "task_status": "failure",
                "failure": {"category": "CONVERSION_BACKEND_ERROR"},
            },
        )

    _install(monkeypatch, handler)
    with pytest.raises(DocumentParseError) as exc:
        await docling.convert(sample, "a.pdf", "application/pdf", SETTINGS)
    assert str(exc.value).startswith("document_docling_task_failed:")


@pytest.mark.asyncio
async def test_resume_resubmits_once_when_task_lost(monkeypatch, tmp_path):
    submissions = {"count": 0}
    sample = tmp_path / "a.pdf"
    sample.write_bytes(b"%PDF-1.4")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/convert/file/async":
            submissions["count"] += 1
            return httpx.Response(200, json={"task_id": "t-2"})
        if path.startswith("/v1/status/poll/t-1") or path.startswith("/v1/result/t-1"):
            return httpx.Response(404)  # the persisted task vanished
        return httpx.Response(200, json={"task_status": "success"})

    _install(monkeypatch, handler)
    task_id, _result = await docling.convert(
        sample, "a.pdf", "application/pdf", SETTINGS, resume_task_id="t-1"
    )
    assert submissions["count"] == 1  # exactly one resubmission
    assert task_id == "t-2"


@pytest.mark.asyncio
async def test_submit_maps_unauthorized(monkeypatch, tmp_path):
    sample = tmp_path / "a.pdf"
    sample.write_bytes(b"%PDF-1.4")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _install(monkeypatch, handler)
    with pytest.raises(DocumentParseError) as exc:
        await docling.submit_file(sample, "a.pdf", "application/pdf", SETTINGS)
    assert str(exc.value) == "document_docling_unauthorized"
