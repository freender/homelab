from __future__ import annotations

import shutil
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


def test_real_repo_placement_is_consistent() -> None:
    for host in STACK_HOSTS:
        docker_stacks.check_placement(ROOT, host)
    docker_stacks.check_shared_orphans(ROOT)


# --- VM scratch placement: appdata excludes must not sit on a VM's appdata ----


@pytest.fixture
def vm_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`vm` is a qemu guest, `ct` an LXC; both run the same two stacks."""
    _write(tmp_path / "backup-excludes" / "appdata.txt", "/app/cache\n/*/logs\n# note\n")
    _write(
        tmp_path / "docker-stacks" / "stacks" / "app" / "compose.yml.j2",
        "services:\n"
        "  app-{{ HOST }}:\n"
        "    volumes:\n"
        "      - /mnt/cache/appdata/app/config:/config\n"
        "      - /mnt/scratch/app/cache:/cache\n",
    )
    _write(
        tmp_path / "docker-stacks" / "stacks" / "web" / "compose.yml.j2",
        "services:\n  web-{{ HOST }}:\n    image: busybox\n",
    )
    kinds = {"vm": "qemu", "ct": "lxc"}

    class Registry:
        def list_hosts(self, feature: str | None = None) -> list[str]:
            return sorted(kinds)

        def get(self, host: str, key: str, default: object = None) -> object:
            if key == "docker-stacks.stacks":
                return ["app", "web"]
            if key == "pve-guest.kind":
                return kinds[host]
            return default

    monkeypatch.setattr(docker_stacks, "default_registry", lambda _root: Registry())
    return tmp_path


def _add_volume(repo: Path, volume: str) -> None:
    path = repo / "docker-stacks" / "stacks" / "web" / "compose.yml.j2"
    path.write_text(
        "services:\n"
        "  web-{{ HOST }}:\n"
        "    volumes:\n"
        f"      - {volume}\n",
        encoding="utf-8",
    )


def test_vm_scratch_clean_repo_passes(vm_repo: Path) -> None:
    docker_stacks.check_vm_scratch_placement(vm_repo, ["vm", "ct"], known=frozenset())


@pytest.mark.parametrize(
    "volume",
    [
        "/mnt/cache/appdata/app/cache:/cache",
        "/mnt/cache/appdata/app/cache/thumbs:/thumbs:ro",
        "/mnt/cache/appdata/web/logs:/logs",
        "/mnt/cache/appdata/web/logs/:/logs",
        "/mnt/cache/appdata/web/./logs:/logs",
    ],
)
def test_vm_volume_inside_an_exclude_fails(vm_repo: Path, volume: str) -> None:
    _add_volume(vm_repo, volume)
    with pytest.raises(ValueError, match=r"vm web/web-vm: /mnt/cache/appdata/") as error:
        docker_stacks.check_vm_scratch_placement(vm_repo, ["vm", "ct"], known=frozenset())
    assert "/mnt/scratch/<stack>" in str(error.value)
    assert "ct " not in str(error.value), "an LXC's exclude-path already covers it"


def test_vm_long_syntax_bind_is_checked(vm_repo: Path) -> None:
    path = vm_repo / "docker-stacks" / "stacks" / "web" / "compose.yml.j2"
    path.write_text(
        "services:\n"
        "  web-{{ HOST }}:\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: /mnt/cache/appdata/web/logs\n"
        "        target: /logs\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="web/web-vm: /mnt/cache/appdata/web/logs"):
        docker_stacks.check_vm_scratch_placement(vm_repo, ["vm"], known=frozenset())


@pytest.mark.parametrize(
    "volume",
    [
        # An exclude *inside* the volume is not a match: Rule A, homelab-ops#157.
        "/mnt/cache/appdata/app:/app",
        "/mnt/cache/appdata:/appdata",
        # Glob covers one segment only.
        "/mnt/cache/appdata/logs:/logs",
        "/mnt/cache/appdata/web/logsarchive:/logs",
        # Outside the appdata root, or a named volume.
        "/mnt/scratch/web/logs:/logs",
        "/mnt/cache/appdata-old/app/cache:/cache",
        "weblogs:/logs",
    ],
)
def test_vm_volume_not_inside_an_exclude_passes(vm_repo: Path, volume: str) -> None:
    _add_volume(vm_repo, volume)
    docker_stacks.check_vm_scratch_placement(vm_repo, ["vm"], known=frozenset())


def test_vm_scratch_honours_appdata_root(
    vm_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_stacks, "appdata_root_for", lambda _root, _host: "/srv/appdata/")
    _add_volume(vm_repo, "/srv/appdata/web/logs:/logs")
    with pytest.raises(ValueError, match="web/web-vm: /srv/appdata/web/logs"):
        docker_stacks.check_vm_scratch_placement(vm_repo, ["vm"], known=frozenset())


def test_known_violation_passes_with_a_warning(
    vm_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_volume(vm_repo, "/mnt/cache/appdata/web/logs:/logs")
    known = frozenset({("vm", "web", "web-vm", "/mnt/cache/appdata/web/logs")})
    docker_stacks.check_vm_scratch_placement(vm_repo, ["vm"], known=known)
    assert "1 known VM appdata-exclude mount(s) pending" in capsys.readouterr().out


def test_fixed_known_violation_fails_until_removed(vm_repo: Path) -> None:
    """The list may only shrink: a stale entry would re-admit the mount silently."""
    known = frozenset({("vm", "web", "web-vm", "/mnt/cache/appdata/web/logs")})
    with pytest.raises(ValueError, match="no longer violate: vm web/web-vm"):
        docker_stacks.check_vm_scratch_placement(vm_repo, ["vm"], known=known)


def test_known_violation_for_an_unchecked_host_is_not_stale(vm_repo: Path) -> None:
    """`./deploy docker-stacks ct` validates ct only; vm's entries are not judged."""
    known = frozenset({("vm", "web", "web-vm", "/mnt/cache/appdata/web/logs")})
    docker_stacks.check_vm_scratch_placement(vm_repo, ["ct"], known=known)


def test_no_vm_host_skips_loading_the_profile(vm_repo: Path) -> None:
    (vm_repo / "backup-excludes" / "appdata.txt").unlink()
    docker_stacks.check_vm_scratch_placement(vm_repo, ["ct"], known=frozenset())


def test_real_repo_known_scratch_violations_are_exact() -> None:
    """KNOWN_SCRATCH_VIOLATIONS matches the real tree in both directions."""
    docker_stacks.check_vm_scratch_placement(ROOT, list(STACK_HOSTS))
