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
- **`env-examples/<stack>.env.example` documents the keys, not the values.** It
  lives outside `stacks/` on purpose -- `stacks/<stack>/` is strictly `compose.yml.j2`
  or `<host>.yml`, enforced by `check_stack_tree`, so a doc-only file cannot sit next
  to the compose it describes. Every value in these files must be a placeholder,
  not a real one; `./validate` enforces that with `check_env_example_placeholders`
  (`src/homelab/cli.py`).
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
  env-examples/<stack>.env.example    # doc-only: keys the host-local .env needs
  scripts/install.py                  # remote installer
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

Currently templated: `exporters`, `hawser`, `cloudflared`.

`vmagent` and `geoipupdate` went per-host when helm, a VM, moved their regenerable
data to a scratch disk. That disk is retired and the data is back under appdata, so
they could be templated again.

`traefik` and `alloy` stay per-host because their differences are **role asymmetry,
not drift**:

- **helm is the only ACME issuer.** It mounts `acme.json` read-write and runs
  `traefik-cert-exporter-helm`, which publishes the cert into the Sentinel Redis.
  tower and neo run no resolver and mount no `acme.json`. Do not "normalize" this.
- **Each host runs its own `traefik-kv-<host>` / `traefik-sentinel-<host>` /
  `traefik-kop-<host>`** around one Sentinel-managed Redis (homelab-ops#318). The
  differences between hosts are the container names and the one-time seed: tower's kv
  seeds as master and helm/neo seed `replicaof traefik-kv-tower`. After that, Sentinel
  owns the role.
- **alloy** differs by real mounts (neo has no `geoipupdate` stack, so no geoip
  mount) and by an explicit `--server.http.listen-addr` that tower does not carry.

### Known divergences, deliberately not normalized

These survived the convention pass because verifying them needs the live hosts, not
the repo. They are called out in comments at the point they occur:

- `crowdsec-tower` runs as root with a plugin `chown` entrypoint; helm and neo run it
  as `1000:1000`.

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
2. Copy each `compose.yml` when content differs (`files.install_from`), honouring `FORCE_UPDATE`.
3. For each stack that actually changed, run `docker compose up -d` in its directory.
   Unchanged stacks are never touched, so an edit to one stack does not restart 47 others.
4. Report stacks present on the host but absent from the repo. They are **never
   removed** — repo coverage is intentionally partial.

## Renaming a container

Every multi-host service carries the `-<host>` suffix. Renaming one is a migration,
not an edit.

**A rename changes the Compose *service key*, not just `container_name`.** Compose
identifies containers by the `com.docker.compose.service` label, so the old
container becomes an orphan of the project. `docker compose up -d` does not remove
orphans. It starts the new container *alongside* the old one. Two crowdsec LAPI
instances would share one SQLite database, and an orphaned cloudflared would keep
serving the tunnel as an unmanaged extra connector.

The installer refuses to let this happen. Before copying anything, it compares the
running containers' service labels against the incoming definition. If any service
has been renamed away, it **skips the stack, leaves the host copy untouched, and
fails the deploy** with the exact `docker rm -f` to run. Remove the old container,
then deploy. Take the `traefik` hosts one at a time, never `all`, and start with one
that does not hold the keepalived VIP.

**Alert rules move with the container.** `vmalert-rules/configs/*-containers.yml`
key on exact container names, so a rename that misses them produces a permanent
false "container missing" alert *and* leaves the new container unmonitored.
`test_alert_rules_track_container_renames` fails the build if a rename forgets. The
`cinci` and `cottonwood` copies of `crowdsec`/`cloudflared`/`geoipupdate` are
third-party hosts and deliberately keep their unsuffixed names.

**Crowdsec keeps a plain `crowdsec` alias.** Traefik reaches Crowdsec through
`crowdsecLapiHost: crowdsec:8080` in `fileConfig.yml`. This module does not manage
that file, and fileconfig-sync writes it *unchanged* to tower, helm and neo. One
shared name must resolve to the **local** LAPI on all three hosts, so each crowdsec
container keeps `aliases: [crowdsec]` on `net.internal` (a per-host bridge) and is
deliberately **not** attached to `net_overlay`. On the overlay, that name would
resolve across the swarm and a host could stream decisions from another host's
LAPI. The alias also makes a botched rename silent, because old and new both answer
to `crowdsec`. `test_crowdsec_is_reachable_as_plain_crowdsec_on_every_host` enforces
all three properties.

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
