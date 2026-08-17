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

`reboot.yml` is the one rule set whose `for:` is measured in hours rather than
minutes: a pending kernel is a scheduling reminder, not an incident, so
`RebootRequired` waits 24h to avoid firing while the daily `apt-upgrade` run is
still in progress. A vmalert restart resets that timer, which is an accepted
tradeoff for a non-urgent signal. Its source metric comes from
`metrics-exporters`' `reboot-textfile-exporter` and exists on bare metal only.
