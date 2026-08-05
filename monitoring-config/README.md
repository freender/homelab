# Monitoring Config

Repo-managed configuration shared by the VictoriaMetrics monitoring stack:

- One canonical `scrape.yml`, deployed to `vmagent-helm` and `vmagent-neo`.
- The secret-free Alertmanager template, deployed only to the host with
  `monitoring-config.alertmanager: true`.

The module does not manage Docker Compose files, vmagent queue data, Alertmanager
notification credentials, or vmalert rules. Telegram tokens and chat IDs remain
in Alertmanager's host-local `.env`; the Compose entrypoint substitutes the chat
ID placeholders and writes token files into the container at startup.

```bash
./deploy --dry-run monitoring-config all
./deploy monitoring-config all
```

The installer validates each staged file with the image used by its running
container before making changes. vmagent reloads in place after a scrape change;
Alertmanager is force-recreated after a template change so its host-local secrets
are rendered into the runtime config.
