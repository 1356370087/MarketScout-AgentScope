"""A failed status query must not cancel an independently running paid trial."""
import json
from types import SimpleNamespace

import httpx
import pytest

from tests.as_runtime import run_quality_e2e as runner


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_running_first", [False, True])
async def test_observation_failure_preserves_identity_without_dispatching_cancel(tmp_path, monkeypatch, observed_running_first):
    calls = []

    class Client:
        async def request(self, method, path, **kwargs):
            calls.append((method, path))
            request = httpx.Request(method, "http://test" + path)
            if method == "POST":
                assert path == "/runs"
                return httpx.Response(201, request=request, json={"run_id": "existing-trial"})
            if observed_running_first and calls.count(("GET", path)) == 1:
                return httpx.Response(200, request=request, json={"status": "running", "progress": {}})
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("database temporarily unavailable", request=request, response=response)

    async def no_wait(*args):
        pass

    monkeypatch.setattr(runner.asyncio, "sleep", no_wait)
    case = {"id": "public-topic", "question": "Explain the public topic", "selection": {"mode": "web"}, "allowed_domains": []}
    with pytest.raises(httpx.HTTPStatusError):
        await runner.run_case(Client(), case, SimpleNamespace(output=tmp_path, deadline=60), "source-digest", 100000)
    directory = tmp_path / case["id"]
    assert json.loads((directory / "run.json").read_text())["run_id"] == "existing-trial"
    interrupted = json.loads((directory / "observation-interrupted.json").read_text())
    assert interrupted["last_observed_status"] == ("running" if observed_running_first else None)
    assert [(method, path) for method, path in calls if method != "GET"] == [("POST", "/runs")]


@pytest.mark.asyncio
@pytest.mark.parametrize('gates_enabled', [True, False])
async def test_explicit_ungated_experiment_never_reports_a_quality_gate_pass(tmp_path, gates_enabled):
    markdown = 'A completed report for independent inspection. ' * 4
    requests = []
    class Client:
        async def request(self, method, path, **kwargs):
            requests.append((method, path, kwargs))
            request = httpx.Request(method, 'http://test' + path)
            if method == 'POST' and path == '/runs':
                assert kwargs['json']['configurable']['quality_evaluation_enabled'] is gates_enabled
                assert kwargs['json']['configurable']['report_review_enabled'] is gates_enabled
                return httpx.Response(201, request=request, json={'run_id': 'trial'})
            if path == '/runs/trial':
                return httpx.Response(200, request=request, json={'status': 'completed', 'progress': {}, 'output': {
                    'markdown': markdown, 'completion_status': 'success',
                    'report_review': {'decision': 'pass'} if gates_enabled else {}}})
            if path.endswith('/usage'):
                return httpx.Response(200, request=request, json={'totals': {'calls': {'attempts': 1},
                    'budgets': {'cost_micro_usd': {'settled': 10, 'reserved': 0}}}})
            if path.endswith('/download'):
                return httpx.Response(200, request=request, content=markdown.encode())
            return httpx.Response(200, request=request, json={'publication_id': 'publication', 'status': 'completed'})
    case = {'id': 'topic', 'question': 'A research question', 'selection': {'mode': 'web'},
        'allowed_domains': [], 'required_facts': [], 'negative_controls': [], 'reference_sources': []}
    result = await runner.run_case(Client(), case, SimpleNamespace(output=tmp_path, deadline=60,
        disable_quality_gates=not gates_enabled), 'source-digest', 100000)
    assert result['execution_passed'] and result['published']
    assert result['runtime_quality_passed'] is gates_enabled
    assert result['quality_gates_enabled'] is gates_enabled
    assert result['independent_content_review'] == 'pending'
