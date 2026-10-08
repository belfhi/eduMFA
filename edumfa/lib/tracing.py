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
import socket
import ssl
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


_NETWORK_INSTRUMENTED = False


def instrument_network_tracing():
    """
    Wrap the standard-library DNS, TCP and TLS functions so that their timings
    show up as OpenTelemetry spans.

    Libraries like ldap3 resolve hostnames with ``socket.getaddrinfo``, open
    TCP connections with ``socket.socket.connect`` and perform TLS handshakes
    with ``ssl.SSLContext.wrap_socket``. None of these are covered by the usual
    OpenTelemetry auto-instrumentation, so DNS and TLS latency is otherwise
    invisible in traces.

    This function patches those functions so that each call is wrapped in a
    span (``dns.getaddrinfo``, ``net.tcp.connect`` and ``tls.handshake``). It is
    idempotent and a no-op if tracing is not enabled. The patches are only
    applied once per process.

    Because these are very low-level functions, the spans are only created when
    there is an active span (i.e. the call happens inside a traced request).
    Otherwise the original function is called directly.
    """
    global _NETWORK_INSTRUMENTED
    if _NETWORK_INSTRUMENTED or not tracing_enabled():
        return
    _NETWORK_INSTRUMENTED = True

    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_wrap_socket = ssl.SSLContext.wrap_socket

    def getaddrinfo(*args, **kwargs):
        host = args[0] if args else kwargs.get("host")
        with _network_span("dns.getaddrinfo", {"net.peer.name": host}) as span:
            addrs = original_getaddrinfo(*args, **kwargs)
            if span is not None:
                span.set_attribute("net.addr.count", len(addrs))
            return addrs

    def connect(self_socket, address, *args, **kwargs):
        host = None
        port = None
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
        attrs = {"net.peer.name": str(host) if host else None}
        if port is not None:
            attrs["net.peer.port"] = port
        with _network_span("net.tcp.connect", attrs):
            return original_connect(self_socket, address, *args, **kwargs)

    def wrap_socket(self_context, sock, *args, **kwargs):
        server_hostname = kwargs.get("server_hostname")
        do_handshake = kwargs.get("do_handshake_on_connect", True)
        attrs = {"net.peer.name": server_hostname}
        # The time of the TLS handshake is what we are interested in. If the
        # caller defers the handshake, the span covers only the wrapping.
        attrs["tls.deferred_handshake"] = not do_handshake
        with _network_span("tls.handshake", attrs):
            return original_wrap_socket(self_context, sock, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    ssl.SSLContext.wrap_socket = wrap_socket
    log.info("Instrumented DNS/TCP/TLS for OpenTelemetry tracing.")


@contextmanager
def _network_span(name, attributes):
    """
    Like :func:`trace_span`, but only creates a span if there is already an
    active (i.e. recording) span. This avoids flooding the traces with DNS/TCP
    lookups that happen outside of a request.

    Yields the span or ``None`` if no span was created.
    """
    current = None
    if _OTEL_AVAILABLE:
        try:
            current = trace.get_current_span()
        except Exception:  # pragma: no cover - defensive
            current = None
    if current is None or not current.is_recording():
        yield None
        return
    with trace_span(name, attributes) as span:
        yield span
