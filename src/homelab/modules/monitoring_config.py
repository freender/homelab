from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, normalize_bool, run_module_deploy
from ..output import print_sub
from ..ssh import build_files, diff_many
from ..templates import render_template_text
from .metrics_exporters import exporter_flags

MODULE_DIR = "monitoring-config"
REMOTE_ROOT = "/tmp/homelab-monitoring-config"
REMOTE_SCRAPE_CONFIG = "/mnt/cache/appdata/vmagent/scrape.yml"
REMOTE_ALERTMANAGER_CONFIG = "/mnt/cache/appdata/alertmanager/alertmanager.yml.tpl"
CONFIG_FILES = ("alertmanager.yml.tpl",)
SCRAPE_TEMPLATE = "scrape.yml.j2"
SCRAPE_CONFIG_NAME = "scrape.yml"
EXPORTER_FEATURE = "metrics-exporters"
VECTOR_HOST_FEATURE = "vector-host"

# Exporter ports, one per host-derived scrape job.
NODE_EXPORTER_PORT = 9100
SMARTCTL_EXPORTER_PORT = 9633
APCUPSD_EXPORTER_PORT = 9162
IGPU_EXPORTER_PORT = 9400
VECTOR_HOST_PORT = 9598


@dataclass(frozen=True)
class ScrapeTarget:
    host: str
    address: str


def scrape_name(registry, host: str) -> str:
    """`metrics-exporters.scrape_host` overrides `config.hostname` for a host whose
    SSH address is not the name it should be scraped by (arc's is a bare IP)."""
    return str(
        registry.get(host, f"{EXPORTER_FEATURE}.scrape_host", None)
        or registry.get(host, "config.hostname")
    )


def scrape_targets(root: Path) -> dict[str, list[ScrapeTarget]]:
    """The host-derived scrape jobs, from the hosts each owning module deploys to.

    Which exporters a host runs is decided once, by `exporter_flags`, so the scrape
    config cannot disagree with what metrics-exporters actually installed.
    """
    registry = default_registry(root)
    jobs: dict[str, list[ScrapeTarget]] = {
        "pve-node": [],
        "pve-smartctl": [],
        "apcupsd": [],
        "intel-gpu": [],
        "vector-host": [],
    }
    for host in registry.list_hosts(EXPORTER_FEATURE):
        name = scrape_name(registry, host)
        flags = exporter_flags(root, host)
        jobs["pve-node"].append(ScrapeTarget(host, f"{name}:{NODE_EXPORTER_PORT}"))
        if not flags.lxc_guest:
            jobs["pve-smartctl"].append(ScrapeTarget(host, f"{name}:{SMARTCTL_EXPORTER_PORT}"))
        if flags.has_apcupsd:
            jobs["apcupsd"].append(ScrapeTarget(host, f"{name}:{APCUPSD_EXPORTER_PORT}"))
        if flags.has_igpu:
            jobs["intel-gpu"].append(ScrapeTarget(host, f"{name}:{IGPU_EXPORTER_PORT}"))
    for host in registry.list_hosts(VECTOR_HOST_FEATURE):
        name = scrape_name(registry, host)
        jobs["vector-host"].append(ScrapeTarget(host, f"{name}:{VECTOR_HOST_PORT}"))
    return jobs


def scrape_template(root: Path) -> Path:
    return root / MODULE_DIR / "templates" / SCRAPE_TEMPLATE


def render_scrape_config(root: Path) -> str:
    return render_template_text(scrape_template(root), targets=scrape_targets(root))


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        MODULE_DIR,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    actual_files = tuple(sorted(path.name for path in configs_dir.iterdir() if path.is_file()))
    if actual_files != CONFIG_FILES:
        raise ValueError(
            f"{MODULE_DIR} configs must be exactly {', '.join(CONFIG_FILES)}; "
            f"found {', '.join(actual_files) or 'none'}"
        )

    for required in (root / MODULE_DIR / "scripts" / "install.py", scrape_template(root)):
        if not required.is_file():
            raise ValueError(f"missing required file: {required}")

    alertmanager_template = (configs_dir / "alertmanager.yml.tpl").read_text(encoding="utf-8")
    # The installer substitutes globally, so a chat ID may back more than one receiver
    # (the private chat serves both the default and the Proxmox receiver). Only the
    # absence of a placeholder is a real error.
    # __HEALTHCHECK_URL__ backs the dead-man's switch. It is a placeholder rather
    # than a literal for the same reason the chat IDs are: the ping URL is a
    # capability that would let anyone forge a healthy homelab, and this repo is
    # public. Requiring it here means the switch cannot be silently dropped from
    # the template.
    for placeholder in ("__TELEGRAM_CHATID__", "__TELEGRAM_CHATID_PLEX__", "__HEALTHCHECK_URL__"):
        if placeholder not in alertmanager_template:
            raise ValueError(f"alertmanager template must contain {placeholder}")


def alertmanager_enabled(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, f"{MODULE_DIR}.alertmanager", None),
        False,
        f"{MODULE_DIR}.alertmanager must be true or false for {host}",
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    scrape_config = build_dir / SCRAPE_CONFIG_NAME
    scrape_config.write_text(render_scrape_config(root), encoding="utf-8")

    manages_alertmanager = alertmanager_enabled(root, host)
    # The destinations go down the wire rather than being repeated in the
    # installer: this module diffs against them before staging, so a second copy
    # on the host could report a diff for one file and install another.
    # ALERTMANAGER_DEST is sent even where Alertmanager is not managed, so the
    # env file has one shape and `env.require` can check it unconditionally.
    write_env_file(
        build_dir / "env",
        {
            "ALERTMANAGER_ENABLED": "true" if manages_alertmanager else "false",
            "ALERTMANAGER_DEST": REMOTE_ALERTMANAGER_CONFIG,
            "VMAGENT_CONTAINER": f"vmagent-{host}",
            "VMAGENT_DEST": REMOTE_SCRAPE_CONFIG,
        },
    )

    connection = connection_for_host(root, host)
    diff_pairs = [(scrape_config, REMOTE_SCRAPE_CONFIG)]
    if manages_alertmanager:
        diff_pairs.append(
            (configs_dir / "alertmanager.yml.tpl", REMOTE_ALERTMANAGER_CONFIG)
        )
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy {MODULE_DIR} to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (configs_dir, f"{REMOTE_ROOT}/configs"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.py",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "configs", "scripts", "src"),
    )
