#!/usr/bin/env python3

import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer


PORT = int(os.getenv("APCUPSD_EXPORTER_PORT", "9162"))
PATH = os.getenv("APCUPSD_EXPORTER_PATH", "/metrics")
UPS_NAME = os.getenv("APCUPSD_EXPORTER_UPS_NAME", "")
UPS_HOST = os.getenv("APCUPSD_EXPORTER_UPS_HOST", "")
UPS_SERIAL = os.getenv("APCUPSD_EXPORTER_UPS_SERIAL", "")

GAUGES = {
    "LINEV": ("apcupsd_line_volts", "line voltage"),
    "LOADPCT": ("apcupsd_load_percent", "load percent"),
    "BCHARGE": ("apcupsd_battery_charge_percent", "battery charge percent"),
    "TIMELEFT": ("apcupsd_time_left_minutes", "time left minutes"),
    "BATTV": ("apcupsd_battery_volts", "battery voltage"),
    "NOMPOWER": ("apcupsd_nominal_power_watts", "nominal power watts"),
}


def esc(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_status():
    out = subprocess.check_output(["apcaccess", "status"], text=True, timeout=10)
    values = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def metric_value(raw):
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", raw)
    if not match:
        return None
    return match.group(0)


def render_labels(labels):
    return ",".join(f'{k}="{esc(v)}"' for k, v in labels.items() if v)


def identity_labels():
    # Only the labels that are known without a working apcaccess call, so
    # apcupsd_up keeps one stable series identity across success and failure.
    # These come from the deploy-time env (see pve_exporters.py), not the probe.
    return {
        "host": UPS_HOST,
        "ups_name": UPS_NAME,
        "serial": UPS_SERIAL,
    }


def build_metrics():
    values = parse_status()
    # STATUS is deliberately NOT part of the common label set: it flips on every
    # ONBATTERY/ONLINE transition, and carrying it on the gauges would give each
    # transition a brand-new series, breaking exactly the trend continuity the
    # battery-degradation and runtime alerts depend on. It rides on
    # apcupsd_status alone, which is what an info-style metric is for.
    base_labels = {
        "host": UPS_HOST or values.get("HOSTNAME", ""),
        "ups_name": UPS_NAME or values.get("UPSNAME", ""),
        "serial": UPS_SERIAL or values.get("SERIALNO", ""),
        "model": values.get("MODEL", ""),
    }
    label_text = render_labels(base_labels)
    status_text = render_labels({**base_labels, "status": values.get("STATUS", "")})

    lines = []
    for key, (metric, help_text) in GAUGES.items():
        value = metric_value(values.get(key, ""))
        if value is None:
            continue
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric}{{{label_text}}} {value}")

    lines.append("# HELP apcupsd_status UPS status as labeled gauge")
    lines.append("# TYPE apcupsd_status gauge")
    lines.append(f"apcupsd_status{{{status_text}}} 1")
    lines.append("# HELP apcupsd_up Whether apcupsd exporter scrape succeeded")
    lines.append("# TYPE apcupsd_up gauge")
    lines.append(f"apcupsd_up{{{render_labels(identity_labels())}}} 1")
    return "\n".join(lines) + "\n"


def build_failure_metrics():
    return (
        "# HELP apcupsd_up Whether apcupsd exporter scrape succeeded\n"
        "# TYPE apcupsd_up gauge\n"
        f"apcupsd_up{{{render_labels(identity_labels())}}} 0\n"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != PATH:
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = build_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            # Answer 200, not 500: a 5xx makes the scraper discard the body, so the
            # apcupsd_up 0 this path exists to publish never actually landed in
            # VictoriaMetrics. Serving it as a normal payload is what lets
            # UpsExporterDown fire on a dead apcupsd rather than only on a dead
            # exporter process.
            body = (build_failure_metrics() + f"# scrape_error {exc}\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
