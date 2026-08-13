# docker-stacks

Repo-managed Docker Compose stack definitions, deployed to the appdata root on the
Docker hosts.

## Scope

This module owns `compose.yml` and nothing else.

- **Stack name == directory name**, in the repo and on the host. `docker compose`
  derives the project name from the directory, so the name stays identical to the
  one `start.sh` already creates and no containers are orphaned by a rename.
- **Runtime `.env` files stay host-local.** They are never written, removed, or read
  for their values. Deploy-time secrets belong in `secrets/`; per-app runtime
  variables belong on the host, where a reboot or the update timer can still start
  the stack without reaching 1Password.
- **`traefik.yml`, `fileConfig.yml` and `acme.json` are not managed here.** They
  carry the public route domain and the route map, and are excluded on purpose.

## Layout

**One directory per application**, named for the application. How a stack is defined is
a file-naming detail *inside* that directory; where it runs is `hosts.conf`'s business
and is not encoded in the tree at all.

```
docker-stacks/
  stacks/<stack>/compose.yml.j2       # uniform: rendered once per declaring host
  stacks/<stack>/<host>.yml           # host-specific: copied verbatim
  scripts/install.sh                  # remote installer
  build/<host>/env                    # generated (gitignored)
  build/<host>/stacks/<stack>/        # generated: assembled staging tree
```

So `stacks/plex/` is always where Plex lives, whether it runs on one host or three —
you never have to know which host owns it to find it.

```
stacks/exporters/compose.yml.j2       # one file, three hosts
stacks/traefik/{tower,helm,neo}.yml   # three files, genuinely different
stacks/plex/tower.yml                 # single host
```

A stack is **one form or the other, never a mix** — a mix would mean some hosts follow
the template while others silently do not. At deploy time both forms are assembled into
`build/<host>/stacks/`, and only that tree is staged; the installer never learns which
form a stack came from.

## Placement lives in hosts.conf

`hosts.conf` is the canonical answer to *"which host runs `<app>`"*. Each host's
`docker-stacks.stacks` list declares its stacks; the directory tree is the payload
for that answer, not the source of it.

```bash
homelab hosts stacks --stack plex     # plex   tower
homelab hosts stacks --host neo       # every stack neo runs
```

`validate` fails on drift in **six** directions:

| Situation | Why it fails |
| --- | --- |
| Declared, defined nowhere | Inventory claims a stack that would deploy nothing |
| `<host>.yml` for a stack that host does not declare | Renaming a file would relocate a service with no inventory change to review |
| Stack mixes `compose.yml.j2` with `<host>.yml` | Some hosts would follow the template and others silently not |
| Filename is not `compose.yml.j2` or `<host>.yml` | `towerr.yml` would sit in the tree deploying to nothing |
| Empty stack directory | Declares an application with no definition |
| Stack nobody declares | Dead code |

### Template or per-host?

Use `compose.yml.j2` when every host running the stack wants byte-identical config
apart from its own name. `{% if HOST == ... %}` is rejected by a test: a conditional
means the stack is not host-invariant, so split it into `<host>.yml` files instead.

Currently templated: `exporters`, `vmagent`, `geoipupdate`, `hawser`, `cloudflared`.

`traefik` and `alloy` stay per-host because their differences are **role asymmetry,
not drift**:

- **tower is the traefik-sync source.** It runs ACME and mounts `acme.json`
  read-write; helm and neo are clients and mount it `:ro`. Do not "normalize" this.
- **traefik-kop is a directional mesh** — each host pushes to the *other* hosts'
  redis, so `REDIS_ADDR` and the kop config path are genuinely per-host.
- **alloy** differs by real mounts (neo has no `geoipupdate` stack, so no geoip
  mount) and by an explicit `--server.http.listen-addr` that tower does not carry.

### Known divergences, deliberately not normalized

These survived the convention pass because verifying them needs the live hosts, not
the repo. They are called out in comments at the point they occur:

- `socket-proxy-helm` alone publishes `2375` and runs `privileged: true`; neo serves
  the same swarm endpoints with neither.
- `socket-proxy-tower` sets `POST: 1` where helm and neo set `POST: 0`, and omits the
  swarm read variables.
- `crowdsec-tower` runs as root with a plugin `chown` entrypoint; helm and neo run it
  as `1000:1000`.
- `traefik` on helm and neo mounts `docker.sock` directly as well as using the socket
  proxy; tower does not.

## Host gate

Enabled by the `docker-stacks` feature in `hosts.conf`, which is **deliberately
separate from `docker`**. The `docker` feature also covers `cinci`, `cottonwood` and
`ghost` — two third-party networks and a work laptop, whose compose files must not be
repo-managed or published. Enabling `docker` must never imply `docker-stacks`.

Optional per-host keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `stacks` | — | Required. Stack names this host runs |
| `appdata_root` | `/mnt/cache/appdata` | Where stack directories live |
| `apply_changed` | `true` | Run `docker compose up -d` for stacks whose compose changed |

## Behaviour

0. Assemble `build/<host>/stacks/` — render each `compose.yml.j2` with `HOST`, copy each
   `<host>.yml` verbatim.
1. Refuse any stack whose appdata directory is missing, or that needs an undefined
   `${VAR}`, or that renames away a still-running container (see below).
2. Copy each `compose.yml` when content differs (`copy_if_changed`), honouring `FORCE_UPDATE`.
3. For each stack that actually changed, run `docker compose up -d` in its directory.
   Unchanged stacks are never touched, so an edit to one stack does not restart 47 others.
4. Report stacks present on the host but absent from the repo. They are **never
   removed** — repo coverage is intentionally partial.

## Pending one-time rename migration

Eight containers are renamed by the current definitions so that every multi-host
service carries the `-<host>` suffix, and every kop instance names its target:

| Host | Old container | New container | Stack |
| --- | --- | --- | --- |
| tower | `geoipupdate` | `geoipupdate-tower` | geoipupdate |
| tower | `cloudflared` | `cloudflared-tower` | cloudflared |
| tower | `traefik-kop-tower` | `traefik-kop-tower-to-helm` | traefik |
| helm | `geoipupdate` | `geoipupdate-helm` | geoipupdate |
| helm | `cloudflared` | `cloudflared-helm` | cloudflared |
| helm | `crowdsec` | `crowdsec-helm` | traefik |
| helm | `traefik-kop-helm` | `traefik-kop-helm-to-tower` | traefik |
| neo | `crowdsec` | `crowdsec-neo` | traefik |

**The `crowdsec` rename is only safe because of the alias.** Traefik reaches
Crowdsec through `crowdsecLapiHost: crowdsec:8080` in `fileConfig.yml` — a file
this module does not manage, which traefik-sync copies *unchanged* from tower to
helm and neo. One shared name must resolve to the **local** LAPI on all three
hosts, so each crowdsec container keeps `aliases: [crowdsec]` on `net.internal`
(a per-host bridge) and is deliberately **not** attached to `net_overlay` — on the
overlay that name would resolve across the swarm mesh and a host could stream
decisions from another host's LAPI. tower has run exactly this pattern
(`crowdsec-tower` + alias) in production all along, which is what makes the helm
and neo renames safe. `test_crowdsec_is_reachable_as_plain_crowdsec_on_every_host`
enforces all three properties.

**Alert rules move with the container.** `vmalert-rules/configs/*-containers.yml`
key on exact container names, so a rename that misses them produces a permanent
false "container missing" alert *and* leaves the new container unmonitored. All
eight are already updated; `test_alert_rules_track_container_renames` fails the
build if a future rename forgets. The `cinci` and `cottonwood` copies of
`crowdsec`/`cloudflared`/`geoipupdate` are third-party hosts and deliberately keep
their unsuffixed names.

**A rename changes the Compose *service key*, not just `container_name`.** Compose
identifies containers by the `com.docker.compose.service` label, so the old
container becomes an orphan of the project — and `docker compose up -d` does not
remove orphans, it starts the new container *alongside* the old one. Two crowdsec
LAPI instances would share one SQLite database; two cloudflared connectors would
serve one tunnel.

The installer refuses to let this happen: before copying anything it compares the
running containers' service labels against the incoming definition, and if any
service has been renamed away it **skips the stack, leaves the host copy untouched,
and fails the deploy** with the exact `docker rm -f` to run. The migration cannot be
half-done by accident.

### Runbook

Do the standalone stacks first — they are single-container and prove the flow:

```bash
ssh tower 'docker rm -f geoipupdate cloudflared'
./deploy docker-stacks tower        # recreates them suffixed
```

`traefik` is the one to be careful with. Its compose changed for every service, so
the deploy restarts that host's whole Traefik stack, including the container
answering the keepalived VIP:

```bash
# 1. confirm which host currently holds the VIP, and start with one that does not
# 2. on that host only:
ssh helm 'docker rm -f crowdsec traefik-kop-helm'
./deploy docker-stacks helm
# tower additionally:   docker rm -f traefik-kop-tower
# neo additionally:     docker rm -f crowdsec
# 3. verify routing and cert sync before moving to the next host
```

Never run `./deploy docker-stacks all` for this migration — take the Traefik hosts
one at a time.

## The appdata directory must already exist

This module owns `compose.yml` and nothing else — a stack's `.env`, config and data are
host-local and were never in the repo. So a declared stack whose
`/mnt/cache/appdata/<stack>/` is missing does **not** mean "new stack"; it means the stack
is declared on a host holding none of its state, which is almost always a placement moved
in `hosts.conf` without the data being moved with it.

The installer refuses: it skips the stack, **creates nothing**, and fails the deploy.
`mkdir -p` would fabricate an empty directory and start containers against no config —
which looks like a successful deploy.

Onboarding a genuinely new stack is therefore an explicit act: create the directory and
its `.env` on the host, then deploy.

## Undefined-variable guard

Before writing anything, the installer checks every `${VAR}` a compose file
references against the stack's `.env` and the current environment. If one is
undefined it **skips that stack, leaves the host copy untouched, and fails the
deploy**.

This exists because `docker compose` does not treat an undefined variable as an
error — it substitutes empty and continues, which silently turns
``Host(`app.${DOMAIN}`)`` into ``Host(`app.`)`` and unroutes the service. Since this
module does not manage `.env`, that failure would otherwise be introduced by a
perfectly valid-looking compose change.

Variables carrying their own default (`${VAR:-value}`) are ignored by the check.
