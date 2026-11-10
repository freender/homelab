# Vector Config

Repo-managed `vector.yaml` for the Vector log shipper on every host that runs the
`vector` Docker stack (tower, helm, neo). It replaced `alloy-config`
(homelab-ops#362, archived under `archive/retired-modules/`). Two templates are
rendered per host:

- `templates/vector.yaml.j2`: the config.
- `templates/vector.test.yaml.j2`: `vector test` unit tests for its transforms.

Both templates import their journald source, transform, sink and tests from the
macros in `templates/journald.yaml.j2`. `vector-host` renders the same macros,
so host-native Vector cannot ship a different journal shape.

Both take:

- `HOST`: the host name. Used for the `host` label, the host's own
  `traefik-<host>` container, and the `vector-<host>` self-exclusion.
- `GEOIP`: true when the host also declares the `geoipupdate` stack, since the
  GeoLite2 ASN database only exists where that stack writes it. Derived from
  `docker-stacks` placement, not set separately.
- `SHADOW`: the per-host `vector-config.shadow` key (default `false`). When true,
  both sinks write to VictoriaLogs tenant `AccountID=1`, so a host can run Vector
  beside another shipper without Grafana, `log-spikes` or vmalert (all on tenant
  0) seeing any line twice. The Alloy cutover (homelab-ops#367) removed it from
  every host. To read shadow data, query with `curl -H 'AccountID: 1' …`.
- `JOURNALD`: the per-host `vector-config.journald` key: `false` (default),
  `shadow`, or `true`. It adds a `journald` source that ships the host's systemd
  journal in journal-upload's shape (homelab-ops#374). `shadow` sends only the
  journal to tenant 1, so it can run beside journal-upload. `validate` refuses
  `true` while `journal-upload` is still enabled on the host.

## What it ships

| Stream | Labels (stream fields) | Source |
| --- | --- | --- |
| Docker containers | `job=docker`, `host`, `container_name` | `docker_logs` |
| Traefik access | `job=traefik-access`, `host`, `container_name=traefik` | `traefik-<host>` lines containing `"ClientHost"` |
| Traefik runtime | `job=traefik-runtime`, `host`, `container_name=traefik` | other `traefik-<host>` lines |
| `/var/log/*log` | `job=varlogs`, `host` | `file`, `read_from: end` |
| systemd journal (`journald` hosts) | `_MACHINE_ID`, `_HOSTNAME`, `_SYSTEMD_UNIT` | `journald` via `journalctl-catchup` |

The journal goes to the **Elasticsearch** endpoint instead, because it takes
`_stream_fields`. Every journal field is kept, `level` comes from `PRIORITY`, the
`__*` fields are dropped, and `_time` is journald's receive time
(`__REALTIME_TIMESTAMP`). That is the same shape VictoriaLogs' `/insert/journald`
produces for journal-upload. On neo, every entry checked was byte-identical
(homelab-ops#375). The sink uses acknowledgements, so the journald checkpoint
only advances once a batch is in the disk buffer.

`configs/journalctl-catchup` is installed into `<appdata>/vector/bin/` and set as
`journalctl_path`. It works around two behaviours of systemd 250–257, where
`journalctl --follow` implies `--boot` for the boot ID in
`/proc/sys/kernel/random/boot_id`:

- Docker inside an LXC (neo, tower) sees a different boot ID from the one the
  LXC's journald stamps, so `--follow` matches nothing. The wrapper pins the boot
  of the journal's newest entry instead.
- Resuming from a cursor in a previous boot skips that boot's shutdown tail. The
  wrapper drains without `--follow` first (homelab-ops#376).

The stack mounts `/run/log/journal`, `/etc/machine-id` and the `bin` directory;
`/var/log/journal` comes with `/var/log`. `bin` is a directory mount, so Docker
creates it on first start. Deploy `docker-stacks` before this module; the
installer refuses to run while `bin` is missing.

Everything goes to VictoriaLogs' **Loki** endpoint on helm. That endpoint parses
JSON lines into fields, which is where every Traefik access field the dashboard
reads comes from. Vector passes the line through raw. On GeoIP hosts it only adds
`geoip_autonomous_system_number` and `geoip_autonomous_system_organization`
to the JSON line, so they arrive as plain fields, not stream fields. Blank
container lines are dropped, as Alloy did; VictoriaLogs would otherwise store each
one as `missing _msg field` (homelab-ops#367).

```bash
./deploy --dry-run vector-config all
./deploy vector-config all
```

## Installer

The module owns `vector.yaml` and nothing else. The compose file belongs to
`docker-stacks`, and `validate` fails if a host runs the `vector` stack without
this feature, or enables the feature without the stack.

On every deploy the installer does two checks in the image the running
`vector-<host>` container uses, mounting the GeoIP database the container mounts:
`vector validate --no-environment` on the staged file, then `vector test` with
the rendered tests. The check container is named `vector-<host>-check`, which the
`docker_logs` source excludes by prefix, so its output is not shipped. The file is
written in place, because it is a single-file bind mount. Vector is reloaded with
SIGHUP only when the content changed, and the installer then waits for
`Vector has reloaded.`. A reload error in the log, or silence for 15 s, fails the
deploy, because Vector keeps running the old config when a reload is rejected.

## Known limits

- **`docker_logs` keeps no checkpoint** (upstream vectordotdev/vector#7358). Lines
  written while Vector is down are lost: about the length of the outage minus 1 s
  on a recreate, nothing on a fast restart (homelab-ops#363, #365). A reload only
  rebuilds the components that changed, so ordinary config deploys cost nothing.
  Image bumps cost a short gap, which is why the image is pinned.
- **A new host needs `vector.yaml` before its first start.** The bind mount would
  otherwise create a directory. Create `/mnt/cache/appdata/vector/data` and copy
  the rendered `build/<host>/vector.yaml` into place, then deploy
  `docker-stacks`, then this module.
- **The GeoIP table is read at start and on reload.** A database `geoipupdate`
  replaces is picked up at the next SIGHUP or restart, not by itself. A swap
  plus SIGHUP was checked live on helm (homelab-ops#367).

## Alerts

The scrape job is `vector` (`vector-<host>:9598`, with a keep-list). The rules are
in `vmalert-rules/configs/monitoring-stack.yml`:

- `VectorLogsDiscarded`: unintentional discards, meaning real loss.
- `VectorDeliveryFailing`: pushes to VictoriaLogs have been retrying for 15 min.
  An outage does not raise `component_errors_total`; it shows up here.
- `VectorDockerSourceDead`: Vector is up, but `docker_logs` has delivered nothing
  since it started (2 h). This catches upstream #16806, where a failed first
  container listing is never retried.

The `ImportantContainerMissing` rules for each `vector-<host>` are in
`important-containers.yml`.
