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


def test_plan_env_renders_every_installer_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_for(
        monkeypatch,
        {
            "pve-notifications.target": "alertmanager",
            "pve-notifications.alertmanager_url": "http://helm.freender.internal:9093",
            "pve-notifications.remove_matchers": ["backup-errors", "telegram-matcher"],
        },
    )

    assert pve_notifications.plan_env(plan) == {
        "NOTIFY_TARGET": "alertmanager",
        "ALERTMANAGER_URL": "http://helm.freender.internal:9093",
        "ALERTMANAGER_SEVERITY": "critical",
        "ALERTMANAGER_ALERTNAME": "ProxmoxNotification",
        "TARGET_NAME": "Alertmanager",
        "MATCHER_NAME": "alertmanager-matcher",
        "MATCHER_COMMENT": "Route notifications to Alertmanager",
        "DISABLE_MAIL_TO_ROOT": "true",
        "DISABLE_DEFAULT_MATCHER": "true",
        "MATCH_SEVERITY": "error",
        "REMOVE_MATCHERS": "backup-errors telegram-matcher",
        "REMOVE_WEBHOOK_TARGETS": "telegram",
    }


def test_an_empty_severity_list_renders_empty_rather_than_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_for(monkeypatch, {"pve-notifications.match_severity": []})

    assert pve_notifications.plan_env(plan)["MATCH_SEVERITY"] == ""


@pytest.mark.parametrize("key", ["match_severity", "remove_matchers", "remove_webhook_targets"])
def test_a_list_entry_with_whitespace_is_refused(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    # The installer splits these on whitespace, so "backup errors" would become
    # two names on the host -- and a stale-matcher delete against the wrong one.
    with pytest.raises(ValueError, match="must not contain whitespace"):
        plan_for(monkeypatch, {f"pve-notifications.{key}": ["backup errors"]})
