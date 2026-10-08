# License:  AGPLv3
# This file is part of eduMFA. eduMFA is a fork of privacyIDEA which was forked from LinOTP.
# Copyright (c) 2024 eduMFA Project-Team
#
# This code is free software; you can redistribute it and/or
# modify it under the terms of the GNU AFFERO GENERAL PUBLIC LICENSE
# License as published by the Free Software Foundation; either
# version 3 of the License, or any later version.
#
# This code is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU AFFERO GENERAL PUBLIC LICENSE for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
"""Helpers for adding application-level OpenTelemetry spans.

The OpenTelemetry API is a hard dependency of eduMFA, but tracing is only
configured in the container entrypoint.  To make sure that importing and using
this module never breaks the application (e.g. in tests or in deployments
without an exporter), all functions degrade gracefully to no-ops if the SDK is
not initialized or if OpenTelemetry is not importable.

Usage::

    from edumfa.lib.tracing import trace_span

    with trace_span("auth.check_webi_user") as span:
        ...
        span.set_attribute("edumfa.role", role)

The functions are intentionally cheap when tracing is disabled: a single
dictionary lookup decides whether anything needs to be done.
"""

import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

try:  # pragma: no cover - trivial import guard
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - opentelemetry is a hard dependency
    trace = None
    Status = None
    StatusCode = None
    _OTEL_AVAILABLE = False

_TRACER_NAME = "edumfa"


def get_tracer():
    """Return the eduMFA tracer, or ``None`` if tracing is unavailable."""
    if not _OTEL_AVAILABLE:
        return None
    return trace.get_tracer(_TRACER_NAME)


def tracing_enabled() -> bool:
    """
    Return whether a real tracer provider is configured.

    This makes it easy to skip expensive attribute computation when no
    exporter is set up (the default ``ProxyTracerProvider`` is a no-op).
    """
    if not _OTEL_AVAILABLE:
        return False
    try:
        provider = trace.get_tracer_provider()
    except Exception:  # pragma: no cover - defensive
        return False
    return hasattr(provider, "add_span_processor")


@contextmanager
def trace_span(name, attributes=None, record_exception=True):
    """
    Context manager that wraps the enclosed block in an OpenTelemetry span.

    If tracing is not configured this is a cheap no-op that yields a span-like
    object supporting ``set_attribute`` so that callers do not need to care.

    :param name: the span name
    :param attributes: optional dict of span attributes
    :param record_exception: whether to mark the span as errored on exceptions
    """
    if not tracing_enabled():
        yield _NoOpSpan()
        return

    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)
        try:
            yield span
        except Exception as e:
            if record_exception:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR))
            raise


class _NoOpSpan:
    """Minimal stand-in for a span when tracing is disabled."""

    def set_attribute(self, key, value):
        return self

    def add_event(self, name, attributes=None):
        return self

    def record_exception(self, exception):
        return self

    def set_status(self, status):
        return self
