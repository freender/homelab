# Alloy Config

Repo-managed `config.alloy` for the Grafana Alloy log shipper on every host that
runs the `alloy` Docker stack (tower, helm, neo). One template,
`templates/config.alloy.j2`, rendered per host with:

- `HOST` — the host name, used for the `host` label and the host's own
  `traefik-<host>` container.
- `GEOIP` — true when the host also declares the `geoipupdate` stack, since the
  GeoLite2 ASN database only exists where that stack writes it. Derived from
  `docker-stacks` placement, not set separately.

Alloy ships Docker container logs (`job=docker`), Traefik access and runtime logs
(`job=traefik-access`, `job=traefik-runtime`), and `/var/log/*log`
(`job=varlogs`) to VictoriaLogs on helm. Every stream carries `host`.

```bash
./deploy --dry-run alloy-config all
./deploy alloy-config all
```

The module owns `config.alloy` and nothing else. The compose file belongs to
`docker-stacks`, and `validate` fails if a host runs the `alloy` stack without
this feature, or enables the feature without the stack.

The installer validates the rendered file with `alloy validate` in the image the
running `alloy-<host>` container uses. It writes the file in place, because it is
a single-file bind mount and a rename would leave the container on the old inode.
It reloads with SIGHUP only when the content changed, then waits for Alloy to log
`config reloaded`. A `failed to reload config` line, or silence for 15 s, fails
the deploy, because Alloy keeps running the old config when a reload is rejected.
