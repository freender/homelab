# vmalert Rules

Repo-managed VictoriaMetrics alert rules for the monitoring stack on `helm`.
The module owns every active `*.yml` file in
`/mnt/cache/appdata/vmalert/rules`; disabled historical files are deliberately
left outside that set. It validates the staged rules with vmalert before any
live file changes and restarts vmalert only when a rule changes.

```bash
./deploy --dry-run vmalert-rules helm
./deploy vmalert-rules helm
```

Alertmanager routing and runtime notification credentials are not managed by
this module.

`reboot.yml` and `apt-updates.yml` use `for:` durations measured in hours and
days rather than minutes, because a pending kernel or an unapplied update is a
scheduling reminder, not an incident. That is safe here: vmalert runs with
`-remoteWrite.url` and `-remoteRead.url`, so pending-alert start times persist
to VictoriaMetrics as `ALERTS_FOR_STATE` and are restored on restart — a rules
redeploy does not reset them. `for:` also only resets when the expression stops
matching entirely, so a changing count (a new CVE landing) does not restart the
clock; only actually patching does.

The same `ALERTS_FOR_STATE` persistence is what makes the `for: 6h` on the
three `*FillingUp` forecast rules (`node-resources.yml`, `docker.yml`,
`zfs-pools.yml`) usable: a trend that has to hold for six hours would otherwise
be reset by every rules redeploy and never fire.

Those three are deliberately band-limited so they cannot double-report against
the threshold rules they front — each one stops where its
`*LowSpace`/`*SpaceHigh` counterpart begins. `node-resources.yml` covers memory
and filesystem for every host `docker.yml` does not, and deliberately excludes
ZFS mountpoints that share a pool's free space, since those are already covered
once per pool by `ZfsPoolSpaceHigh` rather than once per dataset.

`RebootRequired`'s metric comes from `metrics-exporters`'
`reboot-textfile-exporter` and exists on bare metal only.
`ProxmoxUpdatesAvailable` is a weekly digest rather than an alert — see the
`repeat_interval: 168h` route in `monitoring-config`.
