"""Label-identity tests for metrics-exporters' hba-textfile-exporter.

The exporter's labelling is the part that is easy to get wrong and expensive to
notice: a metric series keyed on something that changes when hardware is moved
silently splits a card's history in two, and a series keyed on something that is
not unique makes node_exporter reject the whole textfile.

Both failures were observed in this homelab:

  - clovis's HBA was moved to a different PCIe slot on 2026-08-14. Because the
    series carried a `pci` label, the old series went stale and a new one
    appeared, which read as a second controller that had reached 125C.
  - clovis's card is a cross-flashed clone whose manufacturing NVDATA is
    unprogrammed, so `board_tracer` (serial) is empty. Any scheme keyed on the
    serial would collapse every such card onto one unlabelled series.

So the rule is: metric series are keyed on board+chip (plus the `host` label the
scrape config attaches), and every volatile identifier lives on
homelab_hba_info. `pci` comes back only when board+chip is genuinely ambiguous.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "hba-textfile-exporter.py"


def _load():
    spec = importlib.util.spec_from_file_location("hba_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hba():
    return _load()


def _controller(board="SAS9207-8i", chip="LSISAS2308", pci="0000:07:00.0", **kw):
    base = {
        "driver": "mpt2sas",
        "ioc": "0",
        "scsi_host": "host0",
        "pci": pci,
        "board": board,
        "chip": chip,
        "firmware": "20.00.07.00",
        "sas_address": "0x500605b00a967080",
        "serial": "SV52978426",
    }
    base.update(kw)
    return base


def test_series_labels_exclude_pci(hba):
    """The whole point: a slot move must not change the series identity."""
    slot_a = hba.disambiguate([_controller(pci="0000:04:00.0")])[0]
    slot_b = hba.disambiguate([_controller(pci="0000:02:00.0")])[0]
    assert hba.labels(slot_a, sensor="ioc") == hba.labels(slot_b, sensor="ioc")
    assert "pci" not in hba.labels(slot_a, sensor="ioc")


def test_series_labels_are_board_and_chip(hba):
    controller = hba.disambiguate([_controller()])[0]
    assert hba.labels(controller, sensor="ioc") == (
        'board="SAS9207-8i",chip="LSISAS2308",sensor="ioc"'
    )


def test_distinct_cards_stay_distinct_without_pci(hba):
    """Different hardware must not be merged just because pci was dropped."""
    controllers = hba.disambiguate(
        [
            _controller(board="SAS9207-8i", chip="LSISAS2308", pci="0000:07:00.0"),
            _controller(board="SAS9300-8i", chip="LSISAS3008", pci="0000:04:00.0"),
        ]
    )
    rendered = {hba.labels(c, sensor="ioc") for c in controllers}
    assert len(rendered) == 2
    assert not any("pci" in r for r in rendered)


def test_identical_cards_force_pci_back(hba):
    """Two identical cards in one host would otherwise emit duplicate metrics,
    and node_exporter drops the entire textfile when it sees one."""
    controllers = hba.disambiguate(
        [
            _controller(pci="0000:07:00.0"),
            _controller(pci="0000:04:00.0"),
        ]
    )
    rendered = [hba.labels(c, sensor="ioc") for c in controllers]
    assert len(set(rendered)) == 2, "duplicate label sets would break the textfile"
    assert all("pci=" in r for r in rendered)


def test_info_metric_carries_volatile_identity(hba):
    """pci/sas_address/serial must remain queryable, just not on the series."""
    controller = hba.disambiguate([_controller()])[0]
    rendered = hba.labels(
        controller,
        pci=controller["pci"],
        sas_address=controller["sas_address"],
        serial=controller["serial"],
    )
    assert 'pci="0000:07:00.0"' in rendered
    assert 'sas_address="0x500605b00a967080"' in rendered
    assert 'serial="SV52978426"' in rendered


def test_info_metric_does_not_duplicate_pci_when_ambiguous(hba):
    """When the guard has already added pci, passing it again must overwrite in
    place -- a repeated label name makes the textfile unparseable."""
    controllers = hba.disambiguate([_controller(pci="0000:07:00.0"), _controller()])
    rendered = hba.labels(controllers[0], pci=controllers[0]["pci"])
    assert rendered.count("pci=") == 1


def test_empty_serial_is_kept_as_empty_label(hba):
    """clovis's clone reports no serial, so the exporter must still render the
    label rather than omitting the key -- an omitted key would change the
    rendered line's shape depending on the card.

    Note this is about the *textfile*, not the stored series: Prometheus and
    VictoriaMetrics treat an empty label as absent, so clovis's homelab_hba_info
    simply has no `serial` in VictoriaMetrics. That is expected; do not "fix" it
    by substituting a placeholder, which would make an unprogrammed card look
    like it has a serial.
    """
    controller = hba.disambiguate([_controller(serial="")])[0]
    rendered = hba.labels(controller, serial=controller["serial"])
    assert 'serial=""' in rendered
