import os
import threading
import time
from flask import Flask, jsonify, render_template
from opentelemetry.trace import StatusCode

# --- OpenTelemetry setup ---
# FlaskInstrumentor auto-instruments every HTTP route (method, path, status,
# latency) with zero changes to the route handlers. Manual child spans below
# capture the resource-stressor operations as distinct spans within each trace.
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor

_otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")
_provider = TracerProvider(
    resource=Resource.create({"service.name": "observability-stressor"})
)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{_otel_endpoint}/v1/traces"))
)
trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer(__name__)
# --- End OpenTelemetry setup ---

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

# --- State ---
_cpu_target_pct: int = 0  # 0-100; read by the duty-cycle thread
_memory_blocks: list[bytearray] = []
_lock = threading.Lock()

MEMORY_CHUNK_MB = 256
CPU_STEP = 10          # percent changed per button press
DUTY_CYCLE_S = 0.1     # length of each duty-cycle window in seconds


def _cpu_duty_cycle() -> None:
    """Single thread: busy for target_pct% of each DUTY_CYCLE_S window."""
    global _cpu_target_pct
    while True:
        pct = _cpu_target_pct
        if pct <= 0:
            time.sleep(DUTY_CYCLE_S)
            continue
        burn_s = DUTY_CYCLE_S * pct / 100
        sleep_s = DUTY_CYCLE_S - burn_s
        deadline = time.perf_counter() + burn_s
        while time.perf_counter() < deadline:
            pass
        if sleep_s > 0:
            time.sleep(sleep_s)


_cpu_worker = threading.Thread(target=_cpu_duty_cycle, daemon=True)
_cpu_worker.start()


# --- Routes ---

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/cpu/add")
def cpu_add():
    global _cpu_target_pct
    # Manual child span: records the CPU target mutation and resulting percentage
    # as span attributes, making it visible as a distinct operation in traces.
    with _tracer.start_as_current_span("cpu.add") as span:
        with _lock:
            _cpu_target_pct = min(100, _cpu_target_pct + CPU_STEP)
            pct = _cpu_target_pct
        span.set_attribute("cpu.target_percent", pct)
    return jsonify(status="ok", cpu_percent=pct)


@app.post("/api/cpu/release")
def cpu_release():
    global _cpu_target_pct
    # Manual child span: mirrors cpu.add — same reasoning.
    with _tracer.start_as_current_span("cpu.release") as span:
        with _lock:
            _cpu_target_pct = max(0, _cpu_target_pct - CPU_STEP)
            pct = _cpu_target_pct
        span.set_attribute("cpu.target_percent", pct)
    return jsonify(status="ok", cpu_percent=pct)


@app.post("/api/memory/add")
def memory_add():
    # Manual child span: records allocation size and running total so memory
    # growth is visible as span attributes without adding separate metrics.
    with _tracer.start_as_current_span("memory.add") as span:
        block = bytearray(MEMORY_CHUNK_MB * 1024 * 1024)
        with _lock:
            _memory_blocks.append(block)
            total_mb = len(_memory_blocks) * MEMORY_CHUNK_MB
        span.set_attribute("memory.chunk_mb", MEMORY_CHUNK_MB)
        span.set_attribute("memory.total_mb", total_mb)
    return jsonify(status="ok", memory_mb=total_mb)


@app.post("/api/memory/release")
def memory_release():
    # Manual child span: mirrors memory.add — same reasoning.
    with _tracer.start_as_current_span("memory.release") as span:
        with _lock:
            if _memory_blocks:
                _memory_blocks.pop()
            total_mb = len(_memory_blocks) * MEMORY_CHUNK_MB
        span.set_attribute("memory.total_mb", total_mb)
    return jsonify(status="ok", memory_mb=total_mb)


@app.post("/api/error/400")
def trigger_400():
    # Target the root HTTP span created by FlaskInstrumentor directly.
    # FlaskInstrumentor only auto-promotes 5xx to STATUS_CODE_ERROR, not 4xx,
    # so we set it manually here. A child span with ERROR status leaves the
    # root span UNSET, which means Prometheus span-metrics and Grafana Error
    # Rate queries (status_code="STATUS_CODE_ERROR") won't match this route.
    span = trace.get_current_span()
    span.record_exception(ValueError("Simulated 400 Bad Request"))
    span.set_status(StatusCode.ERROR, "Simulated 400 Bad Request")
    return jsonify(status="error", message="Simulated bad request"), 400


@app.post("/api/error/500")
def trigger_500():
    # Same pattern as trigger_400. FlaskInstrumentor would auto-set ERROR for
    # 5xx after the response, but we set it early so the exception event is
    # also recorded on the root span (not just the status code).
    span = trace.get_current_span()
    span.record_exception(RuntimeError("Simulated 500 Internal Server Error"))
    span.set_status(StatusCode.ERROR, "Simulated 500 Internal Server Error")
    return jsonify(status="error", message="Simulated internal server error"), 500


@app.get("/api/status")
def status():
    return jsonify(cpu_percent=_cpu_pct(), memory_mb=_memory_mb())


def _cpu_pct() -> int:
    with _lock:
        return _cpu_target_pct


def _memory_mb() -> int:
    with _lock:
        return len(_memory_blocks) * MEMORY_CHUNK_MB


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
