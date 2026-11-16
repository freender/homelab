"""solar-gpu-metrics: the NVIDIA GPU and VR-session textfile exporter on solar (vr-guest).

The metric names are the Homelab Dashboard's GPU panels; renaming one blanks them.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "vr-guest" / "files" / "libexec" / "solar-gpu-metrics"

# A real line from solar's 3080 Ti at idle, in QUERY order.
IDLE = (
    "0, NVIDIA GeForce RTX 3080 Ti, 620.70.06, 94.02.71.40.14, 3, 14, 0, 0, 1055, 12288, "
    "40, 0, 20.33, 350.00, 210, 405, 1, 3, 8, 16, P8, 0x0000000000000001"
)


@pytest.fixture
def gpu():
    loader = importlib.machinery.SourceFileLoader("solar_gpu_metrics", str(SCRIPT))
    spec = importlib.util.spec_from_loader("solar_gpu_metrics", loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def values(body: str) -> dict[str, str]:
    """`name{labels}` -> value for every sample line."""
    return dict(line.rsplit(" ", 1) for line in body.splitlines() if not line.startswith("#"))


def test_parse_sample_maps_the_query_in_order(gpu) -> None:
    sample = gpu.parse_sample(IDLE + "\n")

    assert sample["name"] == "NVIDIA GeForce RTX 3080 Ti"
    assert sample["power.draw.instant"] == "20.33"
    assert sample["clocks_event_reasons.active"] == "0x0000000000000001"


@pytest.mark.parametrize("line", ["", "0, 1, 2", IDLE + ", extra"])
def test_parse_sample_refuses_a_partial_line(gpu, line: str) -> None:
    assert gpu.parse_sample(line) is None


def test_render_scales_units_and_labels_the_gpu(gpu) -> None:
    body = values(gpu.render(gpu.parse_sample(IDLE), 31.5, set(), False))

    assert body["nvidia_gpu_up"] == "1"
    assert body['nvidia_gpu_utilization_percent{gpu="0"}'] == "3"
    assert body['nvidia_gpu_memory_used_bytes{gpu="0"}'] == str(1055 * 1048576)
    assert body['nvidia_gpu_graphics_clock_hertz{gpu="0"}'] == "210000000"
    assert body['nvidia_gpu_power_draw_watts{gpu="0"}'] == "20.33"
    assert body['nvidia_gpu_power_draw_max_watts{gpu="0"}'] == "31.5"
    assert body['nvidia_gpu_power_limit_watts{gpu="0"}'] == "350"
    assert body['nvidia_gpu_pcie_link_width{gpu="0"}'] == "8"
    assert body['nvidia_gpu_pcie_link_width_max{gpu="0"}'] == "16"
    assert body['nvidia_gpu_pstate{gpu="0"}'] == "8"
    assert body[
        'nvidia_gpu_info{gpu="0",name="NVIDIA GeForce RTX 3080 Ti",'
        'driver="620.70.06",vbios="94.02.71.40.14"}'
    ] == "1"


def test_render_skips_fields_the_driver_reports_as_na(gpu) -> None:
    sample = gpu.parse_sample(IDLE)
    sample["fan.speed"] = "[N/A]"
    sample["pstate"] = "[N/A]"

    body = values(gpu.render(sample, None, set(), False))

    assert not any(key.startswith(("nvidia_gpu_fan_speed", "nvidia_gpu_pstate",
                                   "nvidia_gpu_power_draw_max")) for key in body)
    assert body['nvidia_gpu_temperature_celsius{gpu="0"}'] == "40"


@pytest.mark.parametrize(("mask", "held"), [
    ("0x0000000000000001", {"gpu_idle"}),
    ("0x0000000000000024", {"sw_power_cap", "sw_thermal_slowdown"}),
    ("0x00000000000000C8", {"hw_slowdown", "hw_thermal_slowdown", "hw_power_brake_slowdown"}),
    ("0x0", set()),
])
def test_clock_event_reasons_decode_the_nvml_bits(gpu, mask: str, held: set[str]) -> None:
    body = values("\n".join(gpu.clock_reason_metrics("0", mask)))

    assert {key.split('reason="')[1].rstrip('"}') for key, value in body.items()
            if value == "1"} == held
    assert len(body) == len(gpu.CLOCK_REASONS)


def test_an_unreadable_reason_mask_publishes_nothing(gpu) -> None:
    assert gpu.clock_reason_metrics("0", "[N/A]") == []


def test_without_a_sample_only_up_and_the_session_are_written(gpu) -> None:
    body = values(gpu.render(None, None, {"vrserver"}, True))

    assert body == {
        "nvidia_gpu_up": "0",
        'homelab_solar_process_running{process="vrserver"}': "1",
        'homelab_solar_process_running{process="vrcompositor"}': "0",
        "homelab_solar_headset_connected": "1",
    }


def test_sampler_goes_stale_and_keeps_the_power_peak_of_its_window(gpu) -> None:
    sampler = gpu.Sampler()
    for at, watts in ((0, "300.0"), (10, "120.0"), (16, "90.0")):
        sample = gpu.parse_sample(IDLE)
        sample["power.draw.instant"] = watts
        sampler.add(sample, at)

    # 300 W at t=0 has left the 15 s window by t=16.
    assert sampler.current(16)[1] == 120.0
    assert sampler.current(16 + gpu.STALE_SECONDS + 1) == (None, None)


def test_sampler_without_samples(gpu) -> None:
    assert gpu.Sampler().current(0) == (None, None)


def test_running_reads_comm_and_ignores_vanished_pids(gpu, tmp_path) -> None:
    for pid, comm in (("10", "vrserver\n"), ("11", "bash\n"), ("12", "vrcompositor\n")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(comm)
    (tmp_path / "13").mkdir()
    (tmp_path / "13" / "comm").mkdir()  # reading it raises, like a pid that exited
    (tmp_path / "self").mkdir()

    assert gpu.running(tmp_path) == {"vrserver", "vrcompositor"}


@pytest.mark.parametrize(("devices", "connected"), [
    ({"1-1": ("054c", "0cde"), "1-2": ("8087", "0026")}, True),
    ({"1-1": ("054c", "0ce6"), "1-2": ("8087", "0026")}, False),
    ({"1-1": ("054c", None)}, False),
])
def test_headset_connected(gpu, tmp_path, devices, connected: bool) -> None:
    for name, (vendor, product) in devices.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "idVendor").write_text(f"{vendor}\n")
        if product is not None:
            (tmp_path / name / "idProduct").write_text(f"{product}\n")

    assert gpu.headset_connected(tmp_path) is connected


def test_publish_replaces_the_file_atomically(gpu, tmp_path) -> None:
    target = tmp_path / "solar-gpu.prom"
    target.write_text("old\n")

    gpu.publish("nvidia_gpu_up 1\n", target)

    assert target.read_text() == "nvidia_gpu_up 1\n"
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert [path.name for path in tmp_path.iterdir()] == ["solar-gpu.prom"]
