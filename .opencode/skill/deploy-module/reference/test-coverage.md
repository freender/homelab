# Test coverage map

Read this when adding or updating tests, or when judging whether an area is
actually covered. Not needed for a routine module edit.

**Read coverage numbers carefully.** The full suite reports ~83%, but roughly a
quarter of that (~24 points) comes from `test_dry_run_all_modules.py`, which
asserts only `exit_code == 0`. Excluding it, assertion-backed coverage is ~59%.
A module can be "covered" and still render semantically wrong output. When
judging whether an area needs tests, use the assertion-backed number:

```bash
COVERAGE_FILE=/tmp/cov_nosmoke .venv/bin/python -m pytest tests/ \
    --ignore=tests/test_dry_run_all_modules.py --cov=src/homelab --cov-report=term
```

Use a separate `COVERAGE_FILE` and an explicit `--cov-report=term`: reusing the
repo's `.coverage` (which `validate` has already written from the full suite) or
passing an empty `--cov-report=` will print the *previous* run's totals and make
the smoke test look like it contributes nothing.

Re-measure rather than trusting the figures above — they move with every coverage
commit, and this paragraph has been stale before.

## Cross-cutting

| Test | Covers |
| --- | --- |
| `tests/test_dry_run_all_modules.py` | Parametrized offline dry-run of every registered module against the real `hosts.conf` (`execute_module(name, "all", True, False)` under `HOMELAB_OFFLINE=1`). This is what `homelab validate` relies on for its per-module dry-run gate — it no longer has its own for-loop. A new module is covered automatically via `MODULES`/`ordered_modules()`; no per-module addition needed. **Smoke only** — it proves a module does not raise, never that its output is correct. Do not treat a module as tested because this passes. |
| `tests/test_render_golden.py` | Golden renders for the **network-critical** modules — `pve-postinstall`, `pve-interface-pinning`, `pve-gpu-passthrough`, `pve-autoinstall`, `keepalived`. A bad render is only discovered after a reboot on a host you can no longer reach. Renders against the real `hosts.conf`, so it also catches inventory drift, and asserts no unsubstituted Jinja placeholders survive. The `keepalived` block is different in kind: its assertions are **cross-host invariants** (shared VRID, unique priorities, symmetric self-excluding unicast peer lists, agreed VIP, `dev` matching `interface`, agreed `advert_int`, per-host healthcheck), because a split-brain VIP is invisible to any single host's own validation. |
| `tests/test_hosts.py`, `tests/test_cli_validate.py` | Inventory parsing and the validate command. |
| `tests/test_build_and_templates.py`, `tests/test_module_fallbacks.py` | Build/render plumbing and module fallback (offline `.example` secret) behavior. |
| `tests/test_leak_check.py`, `tests/test_env_example_check.py` | The public-repo leak check and `.env.example` placeholder check (see `AGENTS.md` § Public Repo Boundary). |
| `tests/test_ssh_helpers.py` | `HostConnection` / staging helpers. |

## `lib/utils.sh` — runs as root on every host

| Test | Covers |
| --- | --- |
| `tests/test_safety_regressions.py` | The **systemd** helpers: `retire_systemd_unit`, `homelab_apply_pause`, `homelab_reload_and_clear_failed`, `homelab_recover_failed_units`, `homelab_mask_unwanted_service`, plus assorted footgun regressions (strict boolean normalizers, unknown-host rejection, tmpfs staging). Harness: `run_utils_snippet` (bash function stub) and `run_recover_snippet` (real on-PATH stub, needed because `timeout` execs the binary and bypasses a shell function). |
| `tests/test_utils_file_helpers.py` | The **file-installation** helpers: `file_needs_update`, `copy_if_changed`, `install_if_changed`, the `backup_and_*` variants, `backup_config`, `prune_backup_history`, `load_file_map`/`mapped_dest`/`mapped_mode`, `install_file_map`, `install_build_file_validated`, `require_env`/`require_file`/`require_dir`, `ensure_timer_state`. Includes a cross-language contract test pinning `module_support.write_file_map` (Python writer) to `load_file_map` (bash reader) — they share no schema, and a delimiter change on either side breaks every module at deploy time. Also holds the regression for the 0=changed / 2=error distinction: these helpers must never report a failed `cp`/`install` as a successful change, because installers feed that status into `homelab_reload_and_clear_failed`. |

## Module-specific

| Test | Covers |
| --- | --- |
| `tests/test_zfs_normalize.py` | `zfs_automation/normalize.py` — validators, dataset-path helpers, snapshot plans and templates, migratable-LXC groups, dynamic-LXC source resolution, `source_private_keys` path confinement, `known_host_refresh` validation. Uses a real `HostRegistry` over a temp `hosts.conf`. This is where to add coverage for anything that turns `hosts.conf` into typed plans. |
| `tests/test_zfs_replication_pause.py` | Pause semantics — per-job `paused` vs `enabled: false` in `zfs-automation`. Imports `normalize_replication_config` from the package's `__init__.py` re-export, not `.replication` directly — keep that export if you touch it. |
| `tests/test_docker_stacks.py`, `tests/test_docker_stacks_installer.py`, `tests/test_docker_start.py` | `docker-stacks` orchestration and its remote installer, and the `docker` module's `start.sh`. |
| `tests/test_docker.py` | The `docker` module's file map and ported installer: helper-script modes, update-timer on/off, failed-run recovery on redeploy. |
| `tests/test_monitoring_config.py`, `tests/test_vmalert_rules.py` | Monitoring config rendering and vmalert rule validity. |
| `tests/test_disk_label_exporter.py`, `tests/test_hba_exporter.py`, `tests/test_reboot_exporter.py` | The three `metrics-exporters` textfile collectors (naming, label identity, behavior). |
| `tests/test_pbs_client_backup.py`, `tests/test_pve_backup.py`, `tests/test_pve_http_boot.py`, `tests/test_base_packages.py` | Module-specific behavior. |
| `tests/test_pve_notifications.py`, `tests/test_pve_notifications_installer.py` | Plan normalization and the env it renders; the ported installer against an in-memory model of `/cluster/notifications` — converged config writes nothing, `set` removes properties the module no longer sets, stale routes go only after the new one exists, the Telegram token is shredded on every exit and never echoed in an error. |
| `tests/test_ubuntu_setup_installer.py` | The ported `ubuntu-setup` installer: every refusal (staged sudoers, env, timezone, deploy user) before any write, a converged host touched not at all, the NIC-rule fallback MAC, sshd drop-in rollback, and the *effective* `sshd -T` config checked against the drop-in, since an earlier `sshd_config.d` file wins. |
| `tests/test_pbs_client_backup_installer.py` | The ported `pbs-client-backup` installer: every refusal (env keys, flag typos, host type, staged credentials and keyfile, Ubuntu suite and vendored keyring, PVE client, `zfs`) before any write, a converged host touched not at all, the keyfile install/purge/leave-alone split, and a changed definition clearing the failed record without starting a backup. |
| `tests/test_apt_upgrade.py` | `apt-upgrade`, the single apt mechanism for the fleet since `apt-security-updates` was archived. Pins `auto_reboot` against live inventory (only the offsite hosts opt in) and `SUPPORTED_TYPES` against every host declaring the feature. |

If a new module can take a host off the network or off SSH — or can desynchronize
a cross-host quorum, VIP, or failover group — it belongs in the golden-render set.

## Known thin spots

Modules with no dedicated test, carried only by the dry-run smoke test:
`disk_spindown` and the three `pve_*_patch` wrappers.
(`apt_upgrade`, `ssh_config`, `vmalert_rules`, `pve_upgrade`, `monitoring_config`,
`pve_postinstall_webhook`, `apcupsd`, `docker`, `pve_interface_pinning`, `pve_notifications` and `ubuntu_setup` have left this list as they were ported — porting is currently the
main way coverage arrives. `wsl_conf` has installer tests in
`test_homelab_install.py` but still no dedicated file.)
`zfs_automation/{access,render,staging}.py` are likewise largely unasserted (7% /
11% / 37% assertion-backed). `op_secrets.py` and `ssh.py` are no longer thin —
commit `6273be2` took them to 71% / 72%. Prefer adding to these over re-covering
well-tested areas.
The ~1,940 lines of still-unported `scripts/install.sh` have no execution
coverage at all — ShellCheck only. A port moves a module's installer into
in-process tests that assert behaviour rather than grepping the script for a
string. **It does not move it into the coverage report or the CRAP gate:** tests
load `<module>/scripts/install.py` by file path under an ad-hoc module name, and
pytest's `--cov` names only `homelab` and `homelab_install`, so no ported
installer is measured. Measure one by hand with
`coverage run --include='*/<module>/scripts/install.py' -m pytest --no-cov tests/test_<module>.py`.
