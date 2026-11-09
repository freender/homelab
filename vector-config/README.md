# Vector Config

Repo-managed `vector.yaml` for the Vector log shipper on every host that runs the
`vector` Docker stack (tower, helm, neo). It replaced `alloy-config`
(homelab-ops#362, archived under `archive/retired-modules/`). Two templates are
rendered per host:

- `templates/vector.yaml.j2`: the config.
- `templates/vector.test.yaml.j2`: `vector test` unit tests for its transforms.

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

## What it ships

| Stream | Labels (stream fields) | Source |
| --- | --- | --- |
| Docker containers | `job=docker`, `host`, `container_name` | `docker_logs` |
| Traefik access | `job=traefik-access`, `host`, `container_name=traefik` | `traefik-<host>` lines containing `"ClientHost"` |
| Traefik runtime | `job=traefik-runtime`, `host`, `container_name=traefik` | other `traefik-<host>` lines |
| `/var/log/*log` | `job=varlogs`, `host` | `file`, `read_from: end` |

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
