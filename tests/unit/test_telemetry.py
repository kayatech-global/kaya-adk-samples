"""The `otlp` telemetry provider sends the trace and ADK's own spans to one exporter, also when ADK set the global provider first."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from retail_kyc_onboarding._engine.factories.telemetry import OtlpTelemetry


def test_otlp_joins_the_provider_adk_installed_first(monkeypatch):
    adk = TracerProvider()  # what get_fast_api_app installs before the engine is built
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: adk)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: (_ for _ in ()).throw(AssertionError("OpenTelemetry would ignore it")))
    telemetry = OtlpTelemetry({"service_name": "kyc"})
    telemetry.exporter = InMemorySpanExporter()
    (sink,) = telemetry.sinks()
    sink.emit({"event": "step.start", "node": "assess_kyc_risk", "prompt": "secret text"})
    adk.get_tracer("google.adk").start_span("invoke_node").end()
    spans = {s.name: dict(s.attributes or {}) for s in telemetry.exporter.get_finished_spans()}
    assert spans.keys() == {"flow.step.start", "invoke_node"}
    assert spans["flow.step.start"] == {"flow.node": "assess_kyc_risk"}  # no content unless capture_content


def test_otlp_installs_its_own_provider_when_none_is_set(monkeypatch):
    installed = []
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.ProxyTracerProvider())
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    telemetry = OtlpTelemetry({"service_name": "kyc"})
    telemetry.exporter = InMemorySpanExporter()
    (sink,) = telemetry.sinks()
    sink.emit({"event": "run.start"})
    assert len(installed) == 1 and installed[0].resource.attributes["service.name"] == "kyc"
    assert [s.name for s in telemetry.exporter.get_finished_spans()] == ["flow.run.start"]
