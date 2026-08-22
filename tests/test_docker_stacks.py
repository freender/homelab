from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.modules import docker_stacks

ROOT = Path(__file__).resolve().parents[1]
STACK_HOSTS = ("tower", "helm", "neo")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal repo: one shared stack on two hosts, one per-host stack.

    Layout is one directory per application; how it is defined is a file-naming
    detail inside that directory.
    """
    _write(
        tmp_path / "docker-stacks" / "stacks" / "shared-app" / "compose.yml.j2",
        "services:\n  shared-app-{{ HOST }}:\n    image: busybox\n",
    )
    _write(
        tmp_path / "docker-stacks" / "stacks" / "solo" / "alpha.yml",
        "services:\n  solo-alpha:\n    image: busybox\n",
    )

    declared = {
        "alpha": ["shared-app", "solo"],
        "beta": ["shared-app"],
    }

    class Registry:
        def list_hosts(self, feature: str | None = None) -> list[str]:
            return sorted(declared)

        def get(self, host: str, key: str, default: object = None) -> object:
            if key == "docker-stacks.stacks":
                return declared.get(host, [])
            return default

    monkeypatch.setattr(docker_stacks, "default_registry", lambda _root: Registry())
    return tmp_path


# --- placement: hosts.conf is canonical, and cannot drift from the tree -------


def test_declared_stack_defined_nowhere_fails(fake_repo: Path) -> None:
    shutil.rmtree(fake_repo / "docker-stacks" / "stacks" / "shared-app")
    with pytest.raises(ValueError, match="declares stack 'shared-app' but"):
        docker_stacks.check_placement(fake_repo, "alpha")


def test_undeclared_host_file_fails(fake_repo: Path) -> None:
    """Renaming a file between hosts must not relocate a service silently."""
    _write(fake_repo / "docker-stacks" / "stacks" / "rogue" / "alpha.yml", "services: {}\n")
    with pytest.raises(ValueError, match="not declared in hosts.conf: rogue"):
        docker_stacks.check_placement(fake_repo, "alpha")


def test_stack_mixing_template_and_host_file_fails(fake_repo: Path) -> None:
    """A mix means some hosts follow the template and others silently do not."""
    _write(fake_repo / "docker-stacks" / "stacks" / "shared-app" / "alpha.yml", "services: {}\n")
    with pytest.raises(ValueError, match="mixes compose.yml.j2 with per-host"):
        docker_stacks.check_stack_tree(fake_repo)


def test_unknown_host_filename_fails(fake_repo: Path) -> None:
    """`towerr.yml` must not sit in the tree deploying to nothing."""
    _write(fake_repo / "docker-stacks" / "stacks" / "solo" / "towerr.yml", "services: {}\n")
    with pytest.raises(ValueError, match="expected compose.yml.j2 or <host>.yml"):
        docker_stacks.check_stack_tree(fake_repo)


def test_empty_stack_directory_fails(fake_repo: Path) -> None:
    (fake_repo / "docker-stacks" / "stacks" / "hollow").mkdir(parents=True)
    with pytest.raises(ValueError, match="empty stack directory"):
        docker_stacks.check_stack_tree(fake_repo)


def test_stack_no_host_declares_fails(fake_repo: Path) -> None:
    _write(fake_repo / "docker-stacks" / "stacks" / "orphan" / "compose.yml.j2", "services: {}\n")
    with pytest.raises(ValueError, match="declared by no host"):
        docker_stacks.check_shared_orphans(fake_repo)


def test_clean_repo_passes(fake_repo: Path) -> None:
    docker_stacks.check_stack_tree(fake_repo)
    for host in ("alpha", "beta"):
        docker_stacks.check_placement(fake_repo, host)
    docker_stacks.check_shared_orphans(fake_repo)


# --- assembly ----------------------------------------------------------------


def test_assemble_renders_shared_and_copies_host(fake_repo: Path) -> None:
    out = fake_repo / "out"
    origins = docker_stacks.assemble_stacks(fake_repo, "alpha", ["shared-app", "solo"], out)

    assert origins == {"shared-app": "shared", "solo": "host"}
    rendered = (out / "shared-app" / "compose.yml").read_text()
    assert "shared-app-alpha:" in rendered
    assert "{{" not in rendered
    assert (out / "solo" / "compose.yml").read_text() == (
        fake_repo / "docker-stacks" / "stacks" / "solo" / "alpha.yml"
    ).read_text()


def test_shared_template_renders_per_host(fake_repo: Path) -> None:
    """Same template, different hosts, no cross-contamination."""
    for host in ("alpha", "beta"):
        out = fake_repo / f"out-{host}"
        docker_stacks.assemble_stacks(fake_repo, host, ["shared-app"], out)
        data = yaml.safe_load((out / "shared-app" / "compose.yml").read_text())
        assert list(data["services"]) == [f"shared-app-{host}"]


# --- the real repo -----------------------------------------------------------


def test_every_real_per_host_file_is_valid_yaml() -> None:
    """Compose's parser is spec-compliant; invalid escapes break `compose up`."""
    for path in sorted(docker_stacks.stacks_root(ROOT).rglob("*.yml")):
        if path.name == docker_stacks.TEMPLATE_NAME:
            continue
        yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_shared_template_renders_for_every_host(tmp_path: Path) -> None:
    from homelab.templates import render_template

    for stack in docker_stacks.shared_stacks(ROOT):
        template = docker_stacks.stack_dir(ROOT, stack) / docker_stacks.TEMPLATE_NAME
        for host in STACK_HOSTS:
            out = tmp_path / host / stack / "compose.yml"
            render_template(template, out, HOST=host)
            data = yaml.safe_load(out.read_text())
            assert data.get("services"), f"{stack} rendered no services for {host}"


def test_shared_stacks_carry_no_host_conditionals() -> None:
    """A conditional means the stack is not actually host-invariant; it should be
    split into <host>.yml files instead. This keeps the template form honest."""
    for stack in docker_stacks.shared_stacks(ROOT):
        text = (docker_stacks.stack_dir(ROOT, stack) / docker_stacks.TEMPLATE_NAME).read_text(
            encoding="utf-8"
        )
        assert "{% if" not in text, f"{stack} has a host conditional"


def test_real_stack_tree_is_structurally_valid() -> None:
    docker_stacks.check_stack_tree(ROOT)


# Alert rules for a managed host that intentionally target a container this repo
# does not define. Empty today: every alerted container on tower/helm/neo is
# repo-managed. Adding an entry must be a deliberate act, because the whole point
# of this test is that a container rename cannot silently orphan its alert.
UNMANAGED_ALERT_TARGETS: set[tuple[str, str]] = set()


def test_alert_rules_track_container_renames(tmp_path: Path) -> None:
    """A rename that misses vmalert gives a permanent false 'missing' critical
    AND leaves the new container unmonitored. Bind the two together here."""
    import re

    defined: dict[str, set[str]] = {}
    for host in STACK_HOSTS:
        out = tmp_path / host
        docker_stacks.assemble_stacks(ROOT, host, docker_stacks.host_stacks(ROOT, host), out)
        names: set[str] = set()
        for compose in out.rglob("compose.yml"):
            document = yaml.safe_load(compose.read_text()) or {}
            for service, body in (document.get("services") or {}).items():
                names.add((body or {}).get("container_name") or service)
        defined[host] = names

    unmatched = set()
    for rules in sorted((ROOT / "vmalert-rules" / "configs").glob("*-containers.yml")):
        pattern = r'host="(' + "|".join(STACK_HOSTS) + r')", name="([^"]+)"'
        for match in re.finditer(pattern, rules.read_text(encoding="utf-8")):
            host, name = match.group(1), match.group(2)
            if name not in defined[host] and (host, name) not in UNMANAGED_ALERT_TARGETS:
                unmatched.add(f"{rules.name}: {host}/{name}")

    assert not unmatched, (
        "alert rule(s) reference containers no compose file defines: "
        + ", ".join(sorted(unmatched))
    )


def test_alertmanager_compose_substitutes_every_template_placeholder() -> None:
    """The Alertmanager template and this compose file are owned by two different
    modules (`monitoring-config` renders the template, `docker-stacks` renders the
    compose that substitutes into it), so nothing but this test binds them.

    A placeholder with no matching substitution does not fail the deploy: it
    survives into the running config as a literal `__NAME__`. For
    `__HEALTHCHECK_URL__` that silently produces an unreachable webhook target,
    which kills the dead-man's switch -- the one alerting path that survives helm
    dying -- while every other alert keeps working and nothing looks wrong. That
    is exactly how it drifted before: the render logic existed only on helm and
    was missing from this repo for two days.

    The `:?` guard is asserted alongside it so a missing host-local .env value
    fails the container start loudly instead of substituting empty.
    """
    import re

    template = (ROOT / "monitoring-config" / "configs" / "alertmanager.yml.tpl").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-stacks" / "stacks" / "alertmanager" / "helm.yml").read_text(
        encoding="utf-8"
    )

    placeholders = set(re.findall(r"__[A-Z][A-Z0-9_]*__", template))
    assert placeholders, "alertmanager template carries no placeholders; contract changed"

    # `-e "s<delim>__NAME__` -- the delimiter varies because the URL contains slashes.
    unsubstituted = sorted(
        name for name in placeholders if not re.search(r'-e "s(.)' + re.escape(name), compose)
    )
    assert not unsubstituted, (
        "alertmanager template placeholder(s) with no compose substitution, so they "
        "survive into the running config: " + ", ".join(unsubstituted)
    )

    unguarded = sorted(name for name in placeholders if f"{name.strip('_')}:?" not in compose)
    assert not unguarded, (
        "alertmanager compose substitutes placeholder(s) without a `:?` guard, so a "
        "missing .env value renders empty instead of failing: " + ", ".join(unguarded)
    )


def test_crowdsec_is_reachable_as_plain_crowdsec_on_every_host(tmp_path: Path) -> None:
    """Traefik reaches Crowdsec via `crowdsecLapiHost: crowdsec:8080` in
    fileConfig.yml -- a file this module does not manage, which traefik-sync
    copies unchanged from tower to helm and neo. One shared name must therefore
    resolve to the LOCAL LAPI on all three hosts:

      * the container is host-suffixed, so it needs the `crowdsec` alias;
      * the alias must live on net.internal, a per-host bridge;
      * it must NOT be on net_overlay, or the name would resolve across the
        swarm mesh and a host could stream decisions from another host's LAPI.
    """
    for host in STACK_HOSTS:
        out = tmp_path / host
        docker_stacks.assemble_stacks(ROOT, host, ["traefik"], out)
        document = yaml.safe_load((out / "traefik" / "compose.yml").read_text())

        internal = document["networks"]["net.internal"]
        assert not internal.get("external"), f"{host}: net.internal must be a host-local bridge"

        services = [name for name in document["services"] if name.startswith("crowdsec")]
        assert len(services) == 1, f"{host}: expected exactly one crowdsec service"
        networks = document["services"][services[0]]["networks"]

        assert "net_overlay" not in networks, (
            f"{host}: crowdsec must not join net_overlay -- 'crowdsec' would "
            "resolve across the swarm mesh"
        )
        aliases = (networks.get("net.internal") or {}).get("aliases") or []
        assert "crowdsec" in aliases, (
            f"{host}: crowdsec needs the 'crowdsec' alias on net.internal; the "
            "shared fileConfig.yml hardcodes that name"
        )


# --- installer guards (run the real install.sh against a fake docker) ---------


def _installer_sandbox(tmp_path: Path, stacks: dict[str, bool]) -> tuple[Path, Path]:
    """Stage install.sh with `stacks` mapping stack name -> appdata dir exists."""
    sandbox = tmp_path / "sbx"
    for sub in ("lib", "scripts", "bin", "appdata", "build/testhost/stacks"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for lib in ("utils.sh", "print.sh"):
        shutil.copy(ROOT / "lib" / lib, sandbox / "lib" / lib)
    shutil.copy(ROOT / "docker-stacks" / "scripts" / "install.sh", sandbox / "scripts")

    (sandbox / "build" / "testhost" / "env").write_text(
        f"APPDATA_ROOT={sandbox}/appdata\nAPPLY_CHANGED=true\nMANAGED_STACK_COUNT={len(stacks)}\n"
    )
    for stack, dir_exists in stacks.items():
        _write(
            sandbox / "build" / "testhost" / "stacks" / stack / "compose.yml",
            f"services:\n  {stack}:\n    image: busybox\n",
        )
        if dir_exists:
            (sandbox / "appdata" / stack).mkdir(parents=True, exist_ok=True)

    docker = sandbox / "bin" / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        '[[ "$1" == "compose" && "$*" == *"config --services"* ]] && { echo "$3"; exit 0; }\n'
        '[[ "$1" == "ps" ]] && exit 0\n'
        '[[ "$1" == "compose" && "$*" == *"up -d"* ]] && { echo up >> "$0.log"; exit 0; }\n'
        "exit 0\n"
    )
    docker.chmod(0o755)
    return sandbox, docker


def _run_installer(sandbox: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{sandbox / 'bin'}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", str(sandbox / "scripts" / "install.sh"), "testhost"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_installer_refuses_stack_with_no_appdata_directory(tmp_path: Path) -> None:
    """A declared stack with no appdata dir means its config/.env/data are not on
    this host -- almost always a placement moved without the data. Creating the
    directory would start containers against no config."""
    sandbox, _ = _installer_sandbox(tmp_path, {"ghost": False})
    result = _run_installer(sandbox)

    assert "does not exist on this host" in result.stdout
    assert not (sandbox / "appdata" / "ghost").exists(), "installer created the directory"
    assert "skipped=1" in result.stdout
    assert result.returncode != 0


def test_installer_still_applies_healthy_stacks_alongside_a_refused_one(tmp_path: Path) -> None:
    sandbox, docker = _installer_sandbox(tmp_path, {"ghost": False, "real": True})
    result = _run_installer(sandbox)

    assert "applied=1" in result.stdout and "skipped=1" in result.stdout
    assert (sandbox / "appdata" / "real" / "compose.yml").is_file()
    assert not (sandbox / "appdata" / "ghost").exists()


def test_installer_is_idempotent_when_nothing_changed(tmp_path: Path) -> None:
    sandbox, docker = _installer_sandbox(tmp_path, {"real": True})
    assert _run_installer(sandbox).returncode == 0
    second = _run_installer(sandbox)
    assert "changed=0" in second.stdout and "applied=0" in second.stdout
    assert second.returncode == 0


def test_real_repo_placement_is_consistent() -> None:
    for host in STACK_HOSTS:
        docker_stacks.check_placement(ROOT, host)
    docker_stacks.check_shared_orphans(ROOT)
