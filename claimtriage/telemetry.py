"""OpenTelemetry tracing (D5). Exports OTLP/HTTP to Jaeger when an endpoint is set."""
from __future__ import annotations

import logging
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from .logging_setup import correlation_id, run_id_var

log = logging.getLogger(__name__)
_initialised = False


def setup_tracing(service: str, endpoint: str = "", extra_exporter: SpanExporter | None = None) -> None:
    global _initialised
    if _initialised:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    trace.set_tracer_provider(provider)
    _initialised = True


def tracer():
    return trace.get_tracer("claimtriage")


@contextmanager
def span(name: str, **attrs):
    """Span that always carries run_id / correlation_id so Jaeger can be searched by them."""
    with tracer().start_as_current_span(name) as sp:
        sp.set_attribute("correlation_id", correlation_id.get())
        sp.set_attribute("run_id", run_id_var.get())
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp


def inject_context() -> dict:
    from opentelemetry.propagate import inject

    carrier: dict = {}
    inject(carrier)
    return carrier


def extract_context(carrier: dict):
    from opentelemetry.propagate import extract

    return extract(carrier or {})
