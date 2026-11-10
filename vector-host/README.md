# Vector Host

Host-native Vector that ships the systemd journal to VictoriaLogs on helm, for
hosts with no `vector` Docker stack (homelab-ops#374, #377). The Docker hosts
(tower, helm, neo) ship theirs from `vector-config.journald`; `validate` refuses
both on one host.

```bash
./deploy --dry-run vector-host all
./deploy vector-host arc
```

## What it installs

| File | Destination |
| --- | --- |
| `configs/datadog.asc` | `/etc/apt/keyrings/datadog.asc` |
| apt source (rendered in Python) | `/etc/apt/sources.list.d/vector.list` |
| `vector-config/configs/journalctl-catchup` | `/usr/local/bin/journalctl-catchup` |
| `templates/vector-override.conf.j2` | `/etc/systemd/system/vector.service.d/homelab.conf` |
| `templates/vector.yaml.j2` | `/etc/vector/homelab.yaml` |

- **Package.** `vector` from Datadog's apt repo
  (`deb … https://apt.vector.dev/ stable vector-0`), unpinned, so `apt-upgrade`
  keeps it current (homelab-ops#376). The `vector-0` component holds back a 1.x
  release until the source line is edited. `--no-install-recommends` skips
  `datadog-signing-keys`, because the keyring is vendored. The keyring has two
  keys: `C0962C7D`, which signs the repo today and expires in 2028, and the
  `F14F620E` master key. Each upgrade restarts Vector, which loses nothing.
- **Config.** The journald source, transform, sink and unit tests come from the
  macros in `vector-config/templates/journald.yaml.j2`. That gives the same shape
  the Docker hosts ship, and that the retired journal-upload shipped: stream fields
  `_MACHINE_ID,_HOSTNAME,_SYSTEMD_UNIT`, `level` from `PRIORITY`, and no `__*`
  fields. The endpoint is the VictoriaLogs Elasticsearch endpoint on the host
  that runs the `victoria-logs` stack. Internal metrics go out on `:9598`.
  Vector's own `vector.service` lines are shipped too.
- **Drop-in.** `StartLimitIntervalSec=0`, `RestartSec=30s`, and
  `ExecStartPre=vector validate --skip-healthchecks`, so a helm outage at boot
  cannot leave the unit failed for good (homelab-ops#373).
  `VECTOR_CONFIG=/etc/vector/homelab.yaml` keeps the config out of the package's
  `/etc/vector/vector.yaml` conffile. `VECTOR_THREADS=2` keeps a journald-only
  pipeline from starting one worker per CPU on the PVE nodes.

`vector-host.shadow: true` sends the journal to tenant `AccountID=1`, so it can
run beside another shipper on tenant 0; no host uses it since the journal-upload
cutover (homelab-ops#378). To read shadow data, query with
`curl -H 'AccountID: 1' …`.

## Installer

1. Keyring and source, then `vector` if it is missing. A fresh install neither
   enables nor starts the unit.
2. Refuses to continue unless `vector` is in `systemd-journal`. The postinst
   adds it there, and without it the source would read only Vector's own lines.
3. Installs the wrapper, then runs `vector validate --no-environment` and
   `vector test` against the staged config. A failed check installs nothing
   else.
4. Installs the drop-in and config, then enables, restarts or starts the unit
   as needed.
5. After any (re)start, it logs a `vector-host` marker with a nonce and waits
   up to 30 s for VictoriaLogs to return it from the tenant the config writes
   to. The journald checkpoint is not proof: it advances once a batch reaches
   the disk buffer, even with VictoriaLogs down.

## Alerts

The `vector` scrape job picks up every `vector-host` host (monitoring-config).
`VectorLogsDiscarded` and `VectorDeliveryFailing` cover these hosts as they cover
the Docker hosts. `VectorDockerSourceDead` skips `install="host"` targets, since
those hosts have no `docker_logs` source. A unit that stays failed shows up in
the failed-systemd-unit alert.

## Known limits

- The first start ships from that moment on (`since_now: true`). After that the
  checkpoint carries it across restarts and reboots, including the previous
  boot's tail, which the wrapper restores (homelab-ops#376).
- Vector uses about 75–100 MB RSS, compared with about 4 MB for the retired
  journal-upload.
