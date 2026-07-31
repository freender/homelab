#!/usr/bin/env python3
"""Intel GPU Prometheus exporter, backed by intel_gpu_top -J.

Replaces the third-party Go exporter (mike1808/igpu-exporter), which had to be
compiled from a pinned git revision on every host: upstream publishes no release
artifacts and it is packaged nowhere, so keeping it meant installing the whole
golang-go toolchain on PVE nodes and doing a build-from-source at deploy time.
This script is a thin wrapper over intel_gpu_top, which is already installed
(intel-gpu-tools) because that exporter shelled out to it anyway.

Metric names, HELP strings, TYPEs and the `engine` label values are deliberately
byte-identical to the Go exporter's output so this is a drop-in replacement: the
`intel-gpu` scrape job in vmagent/scrape.yml on helm and the
igpu_engines_busy_percent panels in Grafana keep working with no changes.

`intel_gpu_top -J` streams samples continuously -- a `[` followed by
concatenated JSON objects with no separating commas -- so a reader thread keeps
the newest sample and /metrics renders whatever is current. Sampling once per
scrape is not viable: intel_gpu_top's first sample is a ~0ms warm-up with every
counter zeroed, so a one-shot invocation would report a permanently idle GPU
regardless of real load.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.getenv("PORT", "9400"))
METRICS_PATH = os.getenv("IGPU_EXPORTER_PATH", "/metrics")
REFRESH_PERIOD_MS = int(os.getenv("REFRESH_PERIOD_MS", "1000"))
DEVICE = os.getenv("DEVICE", "").strip()

# Restart delay after intel_gpu_top exits. The reader supervises its own child
# rather than letting the process die so a transient failure (e.g. the i915
# device briefly unavailable) does not turn into a systemd restart loop.
RESTART_DELAY_SECONDS = 5

# A sample older than this means intel_gpu_top is alive but no longer producing;
# treat it as down rather than serving indefinitely-stale utilisation figures.
STALE_AFTER_SECONDS = max(10.0, (REFRESH_PERIOD_MS / 1000.0) * 5)

# metric name -> (HELP text, JSON section, key within that section)
SCALAR_METRICS = (
    ("igpu_period", "Period ms", "period", "duration"),
    ("igpu_frequency_requested", "Frequency requested MHz", "frequency", "requested"),
    ("igpu_frequency_actual", "Frequency actual MHz", "frequency", "actual"),
    ("igpu_interrupts", "Interrupts/s", "interrupts", "count"),
    ("igpu_rc6", "RC6 %", "rc6", "value"),
    ("igpu_power_gpu", "GPU power W", "power", "GPU"),
    ("igpu_power_package", "Package power W", "power", "Package"),
    ("igpu_imc_bandwidth_reads", "IMC reads MiB/s", "imc-bandwidth", "reads"),
    ("igpu_imc_bandwidth_writes", "IMC writes MiB/s", "imc-bandwidth", "writes"),
)

# metric name -> (HELP text, key within each engines.<name> object)
ENGINE_METRICS = (
    ("igpu_engines_busy_percent", "Engine busy utilisation %", "busy"),
    ("igpu_engines_sema_percent", "Engine sema utilisation %", "sema"),
    ("igpu_engines_wait_percent", "Engine wait utilisation %", "wait"),
)

_lock = threading.Lock()
_sample = None
_sample_at = 0.0


def esc(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def store_sample(sample):
    global _sample, _sample_at
    with _lock:
        _sample = sample
        _sample_at = time.monotonic()


def clear_sample():
    global _sample, _sample_at
    with _lock:
        _sample = None
        _sample_at = 0.0


def latest_sample():
    with _lock:
        if _sample is None:
            return None
        if time.monotonic() - _sample_at > STALE_AFTER_SECONDS:
            return None
        return _sample


def intel_gpu_top_command():
    command = ["intel_gpu_top", "-J", "-s", str(REFRESH_PERIOD_MS), "-o", "-"]
    if DEVICE:
        command += ["-d", DEVICE]
    return command


def stream_samples():
    """Yield each JSON sample object as intel_gpu_top emits it."""
    # Binary pipe on purpose: TextIOWrapper has no read1(), and a plain text
    # read(n) blocks until n characters arrive, which would hold samples back
    # until the buffer happened to fill.
    process = subprocess.Popen(
        intel_gpu_top_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    decoder = json.JSONDecoder()
    buffer = ""
    try:
        while True:
            chunk = process.stdout.read1(65536)
            if not chunk:
                return
            buffer += chunk.decode("utf-8", "replace")
            while True:
                # Trim the stream's array framing: a leading '[' on the first
                # sample, and stray whitespace/commas between objects.
                buffer = buffer.lstrip().lstrip("[,").lstrip()
                if not buffer:
                    break
                try:
                    sample, end = decoder.raw_decode(buffer)
                except ValueError:
                    break  # incomplete object; wait for more bytes
                buffer = buffer[end:]
                yield sample
    finally:
        process.stdout.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def reader_loop():
    while True:
        try:
            for sample in stream_samples():
                store_sample(sample)
        except Exception:
            pass
        # Drop the last sample so /metrics reports down instead of serving the
        # final pre-crash reading while the child is being respawned.
        clear_sample()
        time.sleep(RESTART_DELAY_SECONDS)


def render_metrics():
    sample = latest_sample()
    lines = []

    lines.append("# HELP igpu_up Whether a current intel_gpu_top sample is available")
    lines.append("# TYPE igpu_up gauge")
    lines.append(f"igpu_up {1 if sample else 0}")
    if sample is None:
        # Deliberately no gauges here: emitting the last known values would keep
        # dashboards showing plausible utilisation for a GPU we are no longer
        # measuring. Absence plus igpu_up 0 is unambiguous.
        return "\n".join(lines) + "\n"

    for metric, help_text, section, key in SCALAR_METRICS:
        value = (sample.get(section) or {}).get(key)
        if not isinstance(value, (int, float)):
            continue
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {value}")

    engines = sample.get("engines") or {}
    for metric, help_text, key in ENGINE_METRICS:
        rendered = []
        for engine_name, engine in sorted(engines.items()):
            value = (engine or {}).get(key)
            if not isinstance(value, (int, float)):
                continue
            rendered.append(f'{metric}{{engine="{esc(engine_name)}"}} {value}')
        if not rendered:
            continue
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        lines.extend(rendered)

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != METRICS_PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = render_metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    threading.Thread(target=reader_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
