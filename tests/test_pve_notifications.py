from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import pve_notifications


class FakeRegistry:
    """Minimal stand-in for HostRegistry that serves one feature config."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, host: str, key: str, default: object = None) -> object:
        return self._values.get(key, default)


def plan_for(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> dict[str, object]:
    monkeypatch.setattr(
        pve_notifications,
        "default_registry",
        lambda root: FakeRegistry(values),
    )
    return pve_notifications.normalize_plan(Path("/nonexistent"), "osiris")


def test_alertmanager_target_uses_its_own_names(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_for(
        monkeypatch,
        {
            "pve-notifications.target": "alertmanager",
            "pve-notifications.alertmanager_url": "http://helm.freender.internal:9093",
        },
    )

    assert plan["notify_target"] == "alertmanager"
    assert plan["target_name"] == "Alertmanager"
    assert plan["matcher_name"] == "alertmanager-matcher"
    assert plan["alertmanager_severity"] == "critical"
    assert plan["alertmanager_alertname"] == "ProxmoxNotification"


def test_telegram_remains_the_default_target(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_for(monkeypatch, {})

    assert plan["notify_target"] == "telegram"
    assert plan["target_name"] == "Telegram"
    assert plan["matcher_name"] == "telegram-matcher"


def test_alertmanager_target_requires_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="alertmanager_url is required"):
        plan_for(monkeypatch, {"pve-notifications.target": "alertmanager"})


def test_alertmanager_url_must_be_http(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="must be an http"):
        plan_for(
            monkeypatch,
            {
                "pve-notifications.target": "alertmanager",
                "pve-notifications.alertmanager_url": "helm.freender.internal:9093",
            },
        )


def test_unknown_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="target must be one of"):
        plan_for(monkeypatch, {"pve-notifications.target": "gotify"})


def test_write_plan_emits_the_target_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = plan_for(
        monkeypatch,
        {
            "pve-notifications.target": "alertmanager",
            "pve-notifications.alertmanager_url": "http://helm.freender.internal:9093",
        },
    )
    destination = tmp_path / "notification-plan.conf"

    pve_notifications.write_plan(destination, plan)
    content = destination.read_text(encoding="utf-8")

    assert "NOTIFY_TARGET='alertmanager'" in content
    assert "ALERTMANAGER_URL='http://helm.freender.internal:9093'" in content
    assert "ALERTMANAGER_SEVERITY='critical'" in content
    assert "ALERTMANAGER_ALERTNAME='ProxmoxNotification'" in content
