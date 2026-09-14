#!/usr/bin/env python3
"""Remote installer for the monitoring-config module (freender/homelab-ops#30).

Two config files, installed only after the container that will consume them has
agreed to parse them. The shape is the one `vmalert-rules` established -- validate
against the pinned image, install only if the bytes differ, bounce only if they
did -- with one asymmetry worth knowing before reading:

* **vmagent is reloaded.** It re-reads `scrape.yml` over its own HTTP endpoint, so
  a reload keeps every in-flight scrape and the target state behind it.
* **Alertmanager is recreated.** What this module installs is a `.tpl`, rendered
  into the real config by the container's entrypoint at start, so a running
  Alertmanager has no way to pick up a changed template. There is no reload that
  would work, which is why the expensive option is the correct one here.

Either way the bounce happens **only when the file changed** -- recreating
Alertmanager drops the in-memory state of every active alert and would re-fire
pending ones on each deploy.

The destination paths arrive in the env file rather than being constants here.
The orchestrator already owns them, because it diffs against them before staging;
two copies would let the deploy report a diff for one file and install another.
`compose.yml` is derived from the Alertmanager destination for the same reason --
it is the compose file *of that directory*, and pinning it separately would let
the two drift onto different appdata paths.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from homelab_install import env, files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ALERTMANAGER_CONTAINER = "alertmanager"
COMPOSE_NAME = "compose.yml"
CONFIG_MODE = "644"
SCRAPE_CONFIG_NAME = "scrape.yml"
ALERTMANAGER_TEMPLATE_NAME = "alertmanager.yml.tpl"
VMAGENT_RELOAD_URL = "http://127.0.0.1:8429/-/reload"

# Dummy values, substituted only so `amtool` has something syntactically valid to
# parse. amtool checks chat-id and URL *syntax* and never connects, so no real
# value is needed -- and the real healthcheck URL is a capability that would let
# anyone forge a healthy homelab, so it must not appear in this public repo
# (AGENTS.md). The template ships placeholders for exactly that reason.
VALIDATION_SUBSTITUTIONS = {
    "__TELEGRAM_CHATID__": "123456",
    "__TELEGRAM_CHATID_PLEX__": "654321",
    "__HEALTHCHECK_URL__": "https://example.net/ping/validation",
}

# Indirection point for tests, matching packages.py / systemd.py.
_run = subprocess.run


def _docker(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a docker command. `capture=False` lets the child write straight to the
    deploy log, which is what `docker compose up` wants and what a validation run
    does not -- a successful validation should be silent."""
    return _run(["docker", *args], check=False, capture_output=capture, text=True)


def _report(result: subprocess.CompletedProcess) -> None:
    """Surface a failed container's own diagnosis.

    Without it the operator gets "validation failed" and no way to find the bad
    line; vmagent and amtool both name the offending file and offset.
    """
    log.sub((result.stderr or result.stdout or "").strip() or "<no output>")


def container_image(container: str) -> str:
    """The image tag the named container is currently running.

    Doubles as the existence check the bash spent a separate `docker inspect` on.
    The image matters: validating against a different version could accept syntax
    the running container rejects, which would defeat the point of validating.
    """
    result = _docker("inspect", "--format", "{{.Config.Image}}", container)
    if result.returncode != 0:
        raise InstallError(f"container is missing: {container}")

    image = (result.stdout or "").strip()
    if not image:
        raise InstallError(f"could not read the image of {container}")
    return image


def require_staged(path: Path, label: str) -> Path:
    """Fail before the validation run when the staged source is absent.

    `files.install_from` would catch this too, but only after the docker run --
    and a missing source there means a partial upload, which is worth naming as
    such rather than surfacing as a container error.
    """
    if not path.is_file():
        raise InstallError(f"missing staged {label}: {path}")
    return path


def require_live(dest: str, label: str) -> Path:
    """Refuse to install when the destination file is not already present.

    This is a mount check, not a bootstrap check. These paths live under
    `/mnt/cache/appdata`; if that mount is missing, the write would succeed
    against the empty directory underneath it and the container would keep
    reading its old config while the deploy reported success.
    """
    path = Path(dest)
    if not path.is_file():
        raise InstallError(f"missing {label}: {dest}")
    return path


def validate_scrape_config(image: str, src: Path) -> None:
    """Parse the staged scrape config with the vmagent image itself."""
    log.sub("Validating staged vmagent scrape config...")
    result = _docker(
        "run",
        "--rm",
        "-v",
        f"{src}:/etc/vmagent/scrape.yml:ro",
        image,
        "-promscrape.config=/etc/vmagent/scrape.yml",
        "-promscrape.config.dryRun",
    )
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"vmagent rejected the staged scrape config (exit {result.returncode})")


def reload_vmagent(container: str) -> None:
    """Ask vmagent to re-read its scrape config over its own HTTP endpoint."""
    log.sub(f"Reloading {container}...")
    result = _docker("exec", container, "wget", "-qO-", "--post-data=", VMAGENT_RELOAD_URL)
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"failed to reload {container} (exit {result.returncode})")
    log.ok(f"{container} reloaded")


def render_for_validation(template: Path, destination: Path) -> None:
    """Substitute the deploy-time placeholders so amtool has a parseable file."""
    text = template.read_text(encoding="utf-8")
    for placeholder, value in VALIDATION_SUBSTITUTIONS.items():
        text = text.replace(placeholder, value)
    destination.write_text(text, encoding="utf-8")


def validate_alertmanager_config(image: str, template: Path) -> None:
    """Check the staged template with the running Alertmanager's own amtool."""
    log.sub("Validating staged Alertmanager config...")
    with tempfile.TemporaryDirectory() as staging_dir:
        staging = Path(staging_dir)
        config = staging / "alertmanager.yml"
        render_for_validation(template, config)

        # The template points at bot-token files, and amtool resolves them while
        # parsing. The contents are never sent anywhere by `check-config`, so a
        # single byte is enough -- these are placeholders, not the real tokens.
        tokens = ("telegram_token", "telegram_token_plex")
        for name in tokens:
            (staging / name).write_text("x", encoding="utf-8")

        mounts: list[str] = ["-v", f"{config}:/config/alertmanager.yml:ro"]
        for name in tokens:
            mounts += ["-v", f"{staging / name}:/tmp/{name}:ro"]

        result = _docker(
            "run",
            "--rm",
            *mounts,
            "--entrypoint",
            "/bin/amtool",
            image,
            "check-config",
            "/config/alertmanager.yml",
        )

    if result.returncode != 0:
        _report(result)
        raise InstallError(
            f"amtool rejected the staged Alertmanager config (exit {result.returncode})"
        )


def recreate_alertmanager(compose: Path) -> None:
    """Recreate the container, then check what it actually rendered.

    The second step is the one that matters: `check-config` above ran against a
    template with dummy substitutions, so it proves the *shape* is valid and says
    nothing about the real secrets the entrypoint injects. Running amtool inside
    the new container checks the file Alertmanager is really using.
    """
    log.sub("Recreating Alertmanager to render the updated template...")
    result = _docker(
        "compose",
        "-f",
        str(compose),
        "up",
        "-d",
        "--force-recreate",
        ALERTMANAGER_CONTAINER,
        capture=False,
    )
    if result.returncode != 0:
        raise InstallError(
            f"failed to recreate {ALERTMANAGER_CONTAINER} (exit {result.returncode})"
        )

    verify = _docker(
        "exec", ALERTMANAGER_CONTAINER, "/bin/amtool", "check-config", "/tmp/alertmanager.yml"
    )
    if verify.returncode != 0:
        _report(verify)
        raise InstallError("the recreated Alertmanager rendered an invalid config")
    log.ok("Alertmanager recreated")


def install_scrape_config(ctx: InstallContext, dest: str, container: str) -> None:
    src = require_staged(ctx.script_dir / "configs" / SCRAPE_CONFIG_NAME, "vmagent scrape config")
    require_live(dest, "live vmagent scrape config")

    validate_scrape_config(container_image(container), src)

    if files.install_from(ctx, src, dest, CONFIG_MODE):
        reload_vmagent(container)
    else:
        log.ok("vmagent scrape config unchanged")


def install_alertmanager_template(ctx: InstallContext, dest: str) -> None:
    src = require_staged(
        ctx.script_dir / "configs" / ALERTMANAGER_TEMPLATE_NAME, "Alertmanager config template"
    )
    require_live(dest, "live Alertmanager config template")
    compose = require_live(str(Path(dest).parent / COMPOSE_NAME), "Alertmanager compose file")

    validate_alertmanager_config(container_image(ALERTMANAGER_CONTAINER), src)

    if files.install_from(ctx, src, dest, CONFIG_MODE):
        recreate_alertmanager(compose)
    else:
        log.ok("Alertmanager config template unchanged")


def install(ctx: InstallContext) -> None:
    log.header("Monitoring Config")

    env.require(ctx, "VMAGENT_CONTAINER", "VMAGENT_DEST", "ALERTMANAGER_DEST")
    install_scrape_config(ctx, ctx.env["VMAGENT_DEST"], ctx.env["VMAGENT_CONTAINER"])

    # Only helm runs Alertmanager; neo scrapes and forwards. The flag is read
    # after vmagent is done so a host that does not manage Alertmanager still
    # converges its scrape config.
    if env.flag(ctx, "ALERTMANAGER_ENABLED"):
        install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])


if __name__ == "__main__":
    run(install, "Monitoring Config")
