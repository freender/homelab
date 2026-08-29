"""Guards for the pve-upgrade deploy-time gate.

pve-upgrade is the one module whose deploy action *is* the mutation: it runs
`apt-get dist-upgrade` on the target rather than converging config. Two things
keep that from happening unintentionally, and both are invisible on the host
once they regress -- the upgrade simply happens:

1. It is excluded from `deploy all`, so "deploy everything" cannot dist-upgrade
   the cluster out of order and without the runbook's preflight.
2. A live deploy refuses without --confirm-upgrade.

Exclusion is asserted against the real registry rather than a fixture because
the failure mode is a registry edit, not a call-site one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.cli import execute_module
from homelab.deploy import DeploySession
from homelab.modules import (
    MODULE_ORDER,
    MODULES,
    all_registered_modules,
    ordered_modules,
    pve_upgrade,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The flag is passed via the environment, so a leaked value from another
    # test (or the ambient shell) would make the refusal tests pass vacuously.
    monkeypatch.delenv(pve_upgrade.CONFIRM_ENV, raising=False)
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")


def test_pve_upgrade_is_excluded_from_deploy_all() -> None:
    assert "pve-upgrade" not in ordered_modules()


def test_pve_upgrade_is_still_registered_and_reachable() -> None:
    # Excluded from `all`, not retired: it must stay deployable by explicit name.
    assert "pve-upgrade" in MODULES
    assert "pve-upgrade" in all_registered_modules()


def test_exclusion_survives_being_added_back_to_module_order() -> None:
    # The regression this pins: ordered_modules() appends anything missing from
    # MODULE_ORDER as an "extra", so removing the entry is not what excludes it.
    # Only include_in_all does. Someone re-adding the name must not re-enable it.
    assert "pve-upgrade" not in MODULE_ORDER
    assert MODULES["pve-upgrade"].include_in_all is False


def test_every_other_module_is_still_in_deploy_all() -> None:
    # Keep the exclusion a deliberate one-off: a new module must not quietly
    # inherit include_in_all=False and stop deploying.
    excluded = [name for name in all_registered_modules() if not MODULES[name].include_in_all]
    assert excluded == ["pve-upgrade"]


def test_live_deploy_refuses_without_confirmation() -> None:
    # execute_module converts the module's ValueError into a non-zero exit.
    exit_code = execute_module("pve-upgrade", "all", False, False)
    assert exit_code == 1


def test_live_deploy_raises_with_actionable_message() -> None:
    session = DeploySession("PVE/PBS/PDM Upgrade")
    with pytest.raises(ValueError) as excinfo:
        pve_upgrade.deploy(ROOT, "all", False, False, session)

    message = str(excinfo.value)
    assert "--confirm-upgrade" in message
    assert "README" in message


def test_dry_run_is_not_gated() -> None:
    # Dry-run changes nothing and is what validate's per-module smoke test runs;
    # gating it would break that without making anything safer.
    exit_code = execute_module("pve-upgrade", "all", True, False)
    assert exit_code == 0


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_confirmation_accepts_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(pve_upgrade.CONFIRM_ENV, value)
    assert pve_upgrade.confirmed() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_confirmation_rejects_other_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(pve_upgrade.CONFIRM_ENV, value)
    assert pve_upgrade.confirmed() is False
