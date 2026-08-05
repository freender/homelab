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
