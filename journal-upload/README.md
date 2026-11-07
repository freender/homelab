# Journal Upload

Ships each host's systemd journal to VictoriaLogs with Debian's
`systemd-journal-upload` (package `systemd-journal-remote`). Debian 13 has no
rsyslog, so the journal is the only place host, kernel, ZFS, PBS and failed
`homelab-*` unit messages exist; Alloy ships container logs only.

The module renders one drop-in, `/etc/systemd/journal-upload.conf.d/homelab.conf`:

```ini
[Upload]
URL=http://<victoria-logs host>:9428/insert/journald
```

The host is not configured separately. It is the single host whose
`docker-stacks` declares the `victoria-logs` stack, and `validate` fails unless
exactly one host does. The uploader appends `/upload` itself.

```bash
./deploy --dry-run journal-upload all
./deploy journal-upload all
```

## Querying

On systemd 257 the uploader cannot send custom headers (`VL-Stream-Fields` and
the tenant headers need v258), so journal entries carry no `job` or `host`
label. They keep VictoriaLogs' journald defaults: stream fields `_MACHINE_ID`,
`_HOSTNAME`, `_SYSTEMD_UNIT`, a `level` derived from `PRIORITY`, and no `__*`
metadata fields. `_HOSTNAME` is the short host name, the same value Alloy puts in
`host`.

```logsql
{_HOSTNAME=~".+"}                       # every journal stream
{_HOSTNAME="ace"} level:error           # one host's errors
{_HOSTNAME=~".+", _SYSTEMD_UNIT="pvescheduler.service"}
```

## First start and delivery check

With no state file, the uploader sends the whole journal, and that backlog
overruns VictoriaLogs' `-journald.maxRequestSize`. Before the unit's first start,
the installer seeds the uploader's own state file with the journal's newest
cursor, so shipping begins from the deploy onward. The unit runs with
`DynamicUser=yes`, so the file is written to
`/var/lib/private/systemd/journal-upload/state`.

Whenever the installer (re)starts the unit, it waits up to 30 s for that cursor
to move. The uploader rewrites the file only after VictoriaLogs accepts a batch,
so an unchanged cursor fails the deploy even though the unit reports active.

## Hosts not shipping

- `deepstone` is not enabled yet (tracked in homelab-ops).
- `cinci` and `cottonwood` are offsite third-party networks.
