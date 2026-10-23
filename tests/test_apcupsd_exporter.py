"""Behavioural tests for metrics-exporters' apcupsd-exporter.

Two vmagents scrape every exporter. apcupsd's NIS server stalls for seconds at a
time while its USB poll holds the UPS lock, so what matters is that concurrent
scrapes share one apcaccess call rather than each queueing another NIS client,
and that apcaccess is given long enough to give up on its own instead of being
killed mid-request (freender/homelab-ops#228).
"""

from __future__ import annotations

import importlib.util
import threading
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "apcupsd-exporter.py"

STATUS = """\
APC      : 001,036,0886
HOSTNAME : bray
UPSNAME  : bray-ups
MODEL    : Back-UPS NS 1500M2
STATUS   : ONLINE
LINEV    : 121.0 Volts
LOADPCT  : 14.0 Percent
BCHARGE  : 100.0 Percent
TIMELEFT : 38.1 Minutes
SERIALNO : 3B1944X20420
END APC  : 2026-10-22 20:09:02 -0400
"""


def load_exporter(monkeypatch: pytest.MonkeyPatch, **env: str) -> ModuleType:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("apcupsd_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_apcaccess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delay: float = 0, fail: bool = False
) -> Path:
    """Put an apcaccess on PATH that logs each call, so tests can count NIS clients."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (tmp_path / "status").write_text(STATUS, encoding="utf-8")
    script = bin_dir / "apcaccess"
    script.write_text(
        f"""#!/bin/bash
echo call >> "{calls}"
sleep {delay}
{"exit 1" if fail else f'cat "{tmp_path / "status"}"'}
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return calls


def call_count(calls: Path) -> int:
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


def concurrently(fn, n: int) -> list:
    results: list = [None] * n
    start = threading.Barrier(n)

    def run(i: int) -> None:
        start.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - the test inspects what each caller saw
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


def test_timeout_defaults_above_apcaccess_own_give_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APCUPSD_EXPORTER_TIMEOUT", raising=False)
    # 5 s connect + 15 s read is apcaccess's own worst case; killing it sooner
    # leaves a parked NIS thread behind.
    assert load_exporter(monkeypatch).APCACCESS_TIMEOUT > 20


def test_timeout_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_exporter(monkeypatch, APCUPSD_EXPORTER_TIMEOUT="7.5").APCACCESS_TIMEOUT == 7.5


def test_concurrent_callers_share_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = load_exporter(monkeypatch)
    release = threading.Event()
    calls = []

    def slow() -> str:
        calls.append(1)
        release.wait(5)
        return "status"

    flight = exporter.SingleFlight(slow)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    assert concurrently(flight, 3) == ["status"] * 3
    assert len(calls) == 1


def test_a_failed_call_is_shared_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = load_exporter(monkeypatch)
    release = threading.Event()
    outcomes = iter([RuntimeError("apcaccess wedged"), "status"])

    def flaky() -> str:
        release.wait(5)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    flight = exporter.SingleFlight(flaky)
    threading.Timer(0.3, release.set).start()
    results = concurrently(flight, 2)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert flight() == "status"


def serve(exporter: ModuleType):
    server = exporter.ThreadingHTTPServer(("127.0.0.1", 0), exporter.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}{exporter.PATH}"


def scrape(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.status == 200
        return response.read().decode()


def test_two_overlapping_scrapes_make_one_apcaccess_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_apcaccess(tmp_path, monkeypatch, delay=0.5)
    exporter = load_exporter(
        monkeypatch, APCUPSD_EXPORTER_UPS_HOST="bray", APCUPSD_EXPORTER_UPS_NAME="bray-ups"
    )
    server, url = serve(exporter)
    try:
        bodies = concurrently(lambda: scrape(url), 2)
    finally:
        server.shutdown()
    assert call_count(calls) == 1
    for body in bodies:
        assert 'apcupsd_up{host="bray",ups_name="bray-ups"} 1' in body
        assert "apcupsd_battery_charge_percent" in body


def test_a_failing_apcaccess_still_answers_200_with_up_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_apcaccess(tmp_path, monkeypatch, fail=True)
    exporter = load_exporter(monkeypatch, APCUPSD_EXPORTER_UPS_HOST="bray")
    server, url = serve(exporter)
    try:
        body = scrape(url)
    finally:
        server.shutdown()
    assert 'apcupsd_up{host="bray"} 0' in body
    assert "apcupsd_battery_charge_percent" not in body
