import os
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from edumfa.app import create_app

sys.stdout = sys.stderr


# Service identification
resource = Resource.create(
    {"service.name": os.getenv("OTEL_SERVICE_NAME", "edumfa")})
provider = TracerProvider(resource=resource)

# Read endpoint from standard OTel env var or custom OTEL_EXPORTER_OTLP_ENDPOINT
otlp_endpoint = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://jaeger.tracing.svc.cluster.local:4317"
)

# Initialize OTLP gRPC Exporter
otlp_exporter = OTLPSpanExporter(
    endpoint=otlp_endpoint,
    insecure=True
)

provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)

# Instrument libraries

SQLAlchemyInstrumentor().instrument()
RequestsInstrumentor().instrument()
URLLib3Instrumentor().instrument()


application = create_app(config_name="production",
                         config_file="/etc/edumfa/edumfa.cfg")
FlaskInstrumentor().instrument_app(application)
