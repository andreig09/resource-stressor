# Observability POC — Resource Stressor

A minimal Flask app for **evaluating observability platforms**. It lets you manually drive CPU and RAM consumption from a browser UI or REST API, then observe how a monitoring stack reacts — metrics, traces, dashboards, and all.

The bundled stack (Docker Compose) ships a fully wired observability environment:

| Service | URL | Purpose |
|---|---|---|
| **Stressor** | http://localhost:5000 | Resource stressor UI and API |
| **Grafana** | http://localhost:3000 | Pre-built dashboards |
| **Prometheus** | http://localhost:9090 | Metrics store |
| **Tempo** | http://localhost:3200 | Distributed trace backend |
| **cAdvisor** | http://localhost:8080 | Container metrics source |
| **Node Exporter** | http://localhost:9100 | Host metrics source |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the Compose plugin (`docker compose version`)
- Ports 5000, 3000, 9090, 3200, 4317, 4318, 8080, 9100 available on your machine

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/<your-username>/observability-poc.git
cd observability-poc

# 2. Build and start the full stack
docker compose up --build -d

# 3. Wait ~10 seconds for all services to initialize, then open:
#    Stressor UI   → http://localhost:5000
#    Grafana       → http://localhost:3000  (admin / admin)
```

To stop and clean up volumes:

```bash
docker compose down -v
```

---

## Using the Stressor

Open http://localhost:5000. You'll see three control cards:

### CPU Load
- **+ 10%** — raises the CPU target by 10% (each click adds a 10-point increment up to 100%)
- **− 10%** — lowers the CPU target by 10%

A background thread runs a duty-cycle burn loop, spinning for the target percentage of each 100 ms window. This produces a steady, adjustable CPU signal that monitoring tools can track.

### RAM Pressure
- **+ Alloc 256 MB** — allocates a 256 MB block of memory
- **− Release** — frees the last 256 MB block

Each block is a Python `bytearray`, so the allocation is real and shows up in both container and process memory metrics.

### Error Triggers
- **Trigger 400** — fires an HTTP 400 response with a recorded span error
- **Trigger 500** — fires an HTTP 500 response with a recorded span error

Both errors set `STATUS_CODE_ERROR` on the root OpenTelemetry span so they appear in Tempo traces and in the error-rate panels in Grafana.

---

## Exploring the Grafana Dashboards

Log in at http://localhost:3000 with **admin / admin** (local dev default — do not expose this to the internet).

Navigate to **Dashboards** in the left sidebar. Two dashboards are pre-provisioned:

### Infrastructure Overview

Panels powered by **cAdvisor** (container-level) and **Node Exporter** (host-level):

- **Container CPU usage** — CPU utilization per container over time
- **Container memory usage** — resident set size per container
- **Host CPU / Memory** — aggregate host metrics (Docker Desktop VM on macOS)

A **Container** dropdown at the top auto-discovers all running containers. Select the `observability-stressor` container (or identify it by watching which container's memory jumps when you click **+ Alloc 256 MB** — see the macOS note below).

**Suggested exercise:** start at 0% CPU, click **+ 10%** several times, and watch the container CPU panel respond in real time. Then allocate a few RAM blocks and observe the memory panel.

### Distributed Tracing

Panels powered by **Tempo** (traces) and derived span metrics in **Prometheus**:

- **Request Rate** — requests per second across all routes
- **P99 / P95 / P50 Latency** — response-time percentiles
- **Error Rate** — percentage of requests with `STATUS_CODE_ERROR`
- **Recent Traces** table — lists individual traces with service, operation, duration, and status

Click any row in the **Recent Traces** table to open the full trace waterfall in Tempo. Each API call has a root HTTP span (auto-instrumented by `FlaskInstrumentor`) and a manual child span (`cpu.add`, `memory.release`, etc.) carrying operation-specific attributes.

**Suggested exercise:** trigger a few 500 errors, then filter the Recent Traces table by `status = error` to find the affected traces. Drill into a trace to see the exception event on the span.

#### Trace → Metrics navigation

In Grafana, metric data points that were emitted alongside a trace carry an embedded trace ID. Clicking a data point in a metric panel can jump directly to the corresponding trace in Tempo. This requires no manual correlation — Tempo's metrics generator remote-writes span-derived metrics (RED metrics) to Prometheus and embeds the exemplar trace IDs.

---

## REST API

All endpoints return JSON. The UI uses these internally; you can also call them directly with `curl`.

```bash
# Raise CPU by 10%
curl -X POST http://localhost:5000/api/cpu/add

# Lower CPU by 10%
curl -X POST http://localhost:5000/api/cpu/release

# Allocate 256 MB
curl -X POST http://localhost:5000/api/memory/add

# Free 256 MB
curl -X POST http://localhost:5000/api/memory/release

# Trigger an HTTP 400 error (with error trace)
curl -X POST http://localhost:5000/api/error/400

# Trigger an HTTP 500 error (with error trace)
curl -X POST http://localhost:5000/api/error/500

# Check current state
curl http://localhost:5000/api/status
# → {"cpu_percent": 30, "memory_mb": 512}
```

---

## Simulating Resource Constraints

The stressor container in `docker-compose.yml` has resource limits pre-configured (currently active):

```yaml
deploy:
  resources:
    limits:
      cpus: "2"
      memory: 2g
```

Adjust or comment out these values to simulate constrained environments. After changing the file, restart the stack:

```bash
docker compose up --build -d
```

---

## Architecture

```
Browser / curl
     │
     ▼
Flask app (Gunicorn, 1 worker, 8 threads)
     │ HTTP routes auto-instrumented by FlaskInstrumentor
     │ Manual child spans for stressor operations
     │
     ├─ OTLP HTTP ──► Tempo :4318
     │                   │
     │                   ├─ stores traces
     │                   └─ metrics generator ──► Prometheus :9090 (RED metrics + exemplars)
     │
Prometheus :9090
     │ ◄── cAdvisor :8080 (container metrics)
     │ ◄── Node Exporter :9100 (host metrics)
     │ ◄── Prometheus self-scrape
     │
Grafana :3000
     ├─ datasource: Prometheus
     └─ datasource: Tempo (trace search + exemplar drill-through)
```

---

## macOS / Docker Desktop Note

cAdvisor exposes containers by their 64-character container ID in the `id` label (e.g., `/docker/<hash>`) rather than by name. This is a Docker Desktop VM isolation limitation. The **Container** dropdown in Grafana lists all available IDs. To find the stressor container, use Docker Desktop to get the Container's ID or use any of the apps functionalities to modify container's resource consumption and watch which container ID's resources are modified in Grafana.

Node Exporter metrics reflect the **Docker Desktop Linux VM's** resources (bounded by Docker Desktop → Settings → Resources), not the Mac's physical hardware.

---

## Project Structure

```
.
├── app.py                        # Flask app + OpenTelemetry instrumentation
├── Dockerfile                    # Single-container build (Gunicorn)
├── docker-compose.yml            # Full observability stack
├── requirements.txt
├── templates/
│   └── index.html                # Browser UI
├── prometheus/
│   └── prometheus.yml            # Scrape config
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   ├── prometheus.yml    # Prometheus datasource
│       │   └── tempo.yml         # Tempo datasource
│       └── dashboards/
│           ├── infrastructure.json
│           └── tracing.json
└── tempo/
    └── tempo.yml                 # Tempo 3.x config (OTLP + metrics generator)
```
