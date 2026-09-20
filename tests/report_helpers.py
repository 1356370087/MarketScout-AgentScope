"""Domain report tests replace model IO at the native ReportContext boundary."""

from open_deep_research.report.assembly import ReportContext


def patch_report_model(monkeypatch, invoke):
    async def write(self, messages, *, span_name):
        return await invoke(None, messages, self.config, span_name=span_name,
                            agent_role="lead", model_name=self.configurable.final_report_model)

    async def structured(self, schema, messages, *, span_name):
        return await write(self, messages, span_name=span_name)

    monkeypatch.setattr(ReportContext, "invoke_writer_with_output_recovery", write)
    monkeypatch.setattr(ReportContext, "invoke_structured_with_fallback", structured)
