global:
  # PVE posts one-shot events straight to /api/v2/alerts without an endsAt, so this
  # is how long a Proxmox notification stays visible in the alert list and in MWBot
  # before it self-resolves. vmalert always sends an explicit endsAt, so raising this
  # does not delay resolution of metric-based alerts.
  resolve_timeout: 12h

route:
  receiver: mwbot
  group_by:
    - alertname
    - host
    - name
    - severity
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    # Dead-man's switch, first so nothing else can claim it. The Watchdog alert
    # (vmalert-rules/configs/watchdog.yml) always fires; this route turns that
    # into a heartbeat to an external healthchecks.io check, which alerts from
    # outside the homelab if the heartbeat stops.
    #
    # This is the only alerting path that survives helm dying. Everything else
    # in this file is delivered *by* the stack it is monitoring: VictoriaMetrics,
    # vmalert and Alertmanager all run on helm, so a helm outage silences the
    # entire alerting system, including the NodeDown that would have named it.
    #
    # repeat_interval is a floor on the ping rate, not the rate itself.
    # Alertmanager only re-notifies on group_interval ticks, and with
    # repeat_interval == group_interval == 1m the tick one minute later is
    # marginally too early to satisfy the interval, so it fires on the one after.
    # Measured cadence is therefore a ping every 2 minutes, not every minute
    # (4 pings over 390s, steady 120s gaps, 2026-08-16). Lowering group_interval
    # is what would tighten it; there is no need here.
    #
    # What matters is that the rate stays comfortably below the external check's
    # period, or the check goes DOWN between pings and UP on the next one
    # forever -- 5m pings against a 1m period did exactly that on 2026-08-15,
    # and a switch that cries wolf gets muted, which is the same as not having
    # one.
    #
    # The check is configured period 5m / grace 5m, so it alerts after 10
    # minutes of silence. At a 2m cadence that is about five pings of headroom.
    # Detection speed is owned by period and grace; the ping rate only decides
    # how many consecutive failures it takes to trip.
    #
    # Deliberately NOT muted by scheduled-maintenance. A mute window here would
    # stop the heartbeat and report an outage that is not happening -- the exact
    # false positive that gets a dead-man's switch disabled and forgotten.
    - receiver: deadmanswitch
      matchers:
        - alertname="Watchdog"
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 1m
      continue: false
    # Proxmox notifications are discrete events, not conditions: they never resolve
    # themselves, so suppress resolved notices and do not re-notify. They are also
    # deliberately not muted by the maintenance interval, because a dropped
    # Proxmox error would never be re-sent.
    #
    # source=~"pve|pbs" because xur (PBS) now posts here too, using the same
    # webhook shape as the PVE nodes. It previously called the Telegram API
    # directly, which meant PBS backup failures could not be silenced by MWBot,
    # ignored the maintenance windows, were never deduplicated, and required a
    # bot token on the backup server. The trade-off is that helm is now a single
    # point of failure for PBS alerting as well -- one more argument for a
    # dead-man's switch on this stack.
    #
    # PDM (arc) is deliberately not listed: Proxmox Datacenter Manager 1.1.7 has
    # no notification subsystem at all (no notification CLI, no
    # notifications.cfg), so there is nothing to repoint. arc's health has to
    # come from node_exporter/NodeDown instead, which means adding it to
    # scrape.yml.
    #
    # jobid/datastore are in group_by so that two different PBS jobs failing do
    # not collapse into one notification; they are simply empty for PVE alerts.
    - receiver: proxmox
      matchers:
        - source=~"pve|pbs"
      group_by:
        - alertname
        - host
        - name
        - vmid
        - jobid
        - datastore
      repeat_interval: 24h
      continue: false
    - receiver: plex-requests
      matchers:
        - name=~"plex|seerr"
      mute_time_intervals:
        - scheduled-maintenance
      continue: false
    # A degraded pool is a slow-moving hardware condition, not an incident that
    # changes between notifications: once ZfsPoolUnhealthy fires, the only thing
    # that clears it is physically replacing a disk and waiting out a resilver,
    # which is days of lead time. At the parent's 4h repeat that paged 6 times a
    # day with identical content, which is the pressure that makes someone
    # silence the alert outright -- and this rule is deliberately the *only* ZFS
    # health signal (see the ZfsPoolDeviceErrors note in vmalert's
    # zfs-pools.yml), so silencing it is a total blind spot on a pool that may
    # have no redundancy left. Daily re-notification keeps the pool visible for
    # the whole replacement window at a volume worth leaving unsilenced.
    # Scoped to this alert, not all of alertgroup="zfs-pools": the capacity and
    # metrics-missing rules have different urgency and should keep the default.
    - receiver: mwbot
      matchers:
        - alertname="ZfsPoolUnhealthy"
      repeat_interval: 24h
      mute_time_intervals:
        - scheduled-maintenance
      continue: false
    - receiver: mwbot
      mute_time_intervals:
        - scheduled-maintenance
      continue: false

# A host that is down cannot be diagnosed through its own exporters, and every
# rule that depends on them fires at once when it goes: ace's 45 minute outage
# on 2026-08-13 produced NicMissing for nic0 and nic1 plus
# SystemdUnitMetricsMissing, none of which said "the node is off". NodeDown now
# names that condition directly, so everything else about the same host is
# redundant while it holds.
#
# equal: [host] is what keeps this safe. Alertmanager only inhibits a target
# whose `host` label matches the firing NodeDown exactly, so alerts carrying no
# host label at all -- the fleet-wide absence rules such as ZfsPoolMetricsMissing
# and SmartMetricsMissing -- are never suppressed by one host going down.
#
# This does not replace the per-rule host-up gates on NicMissing and
# DockerMetricsTargetDown. Inhibition only applies once NodeDown is firing at
# the 10 minute mark, and both of those alerts would already have notified
# before then; the gates stop them from ever entering the pending state during
# an outage, and this rule cleans up everything slower than they are.
inhibit_rules:
  - source_matchers:
      - alertname=~"NodeDown|NodeDownOffsite"
    target_matchers:
      - alertname!~"NodeDown|NodeDownOffsite"
    equal:
      - host

  # osiris is standalone, not part of the ace/bray/clovis HA cluster (verified
  # 2026-08-15 via `ha-manager status` / `pvesh get /cluster/ha/resources`: CT
  # 101/104/106/107/108 are HA-managed with fencing armed and watchdog active
  # on ace/bray/clovis; xur and deepstone are absent from that resource list).
  # If osiris goes down, xur and deepstone have no failover target and are
  # guaranteed down for the same duration -- their own NodeDown firing after
  # osiris's is 100% redundant, not new information, so it is inhibited here.
  #
  # This is deliberately NOT extended to ace/bray/clovis and their HA guests
  # (tower, helm, neo, riven, arc). A normal fence-and-relocate finishes well
  # inside NodeDown's 10m `for:`, so the guest's own NodeDown typically never
  # fires during routine HA recovery -- there is no duplicate to suppress. If
  # it does fire after the node's already did, that means HA did not save the
  # guest in time (quorum loss, contested fencing, stuck migration), which is
  # exactly the case that should still page. Blanket-suppressing it the same
  # way as osiris would hide that failure mode.
  - source_matchers:
      - alertname="NodeDown"
      - host="osiris"
    target_matchers:
      - alertname=~"NodeDown|NodeDownOffsite"
      - host=~"xur|deepstone"

# These windows mute the expected churn from the repo's own scheduled jobs in
# hosts.conf, so they are load-bearing despite predating the retirement of
# Uptime Kuma (which they were originally named after):
#   02:00 -- docker `update_schedule`, container image updates
#   08:00-08:06 -- `apt-upgrade` schedules, staggered across hosts
# Deleting them restores nightly alert noise during both windows. Keep them in
# step with those schedules rather than removing them.
time_intervals:
  - name: scheduled-maintenance
    time_intervals:
      - times:
          - start_time: "02:00"
            end_time: "02:10"
        location: America/New_York
      - times:
          - start_time: "08:00"
            end_time: "08:10"
        location: America/New_York

receivers:
  - name: mwbot
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: __TELEGRAM_CHATID__
        send_resolved: true
        message: |-
          {{ if eq .Status "resolved" }}&#128994;{{ else if eq .CommonLabels.severity "critical" }}&#128308;{{ else if eq .CommonLabels.severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}{{ if .CommonLabels.name }}: {{ .CommonLabels.name }}{{ end }}
          {{ range .Alerts }}
          Severity: {{ .Labels.severity }}
          {{ if .Labels.host }}Host: {{ .Labels.host }}{{ end }}
          {{ if .Labels.name }}Name: {{ .Labels.name }}{{ end }}
          {{ .Annotations.summary }}
          {{ if .Annotations.description }}{{ .Annotations.description }}{{ end }}
          {{ end }}

  # Shared by PVE and PBS. pve_severity keeps its name rather than becoming
  # proxmox_severity: the label is set by the webhook body on each sending host,
  # and the four PVE nodes' notification configs are host-local and not
  # repo-managed, so renaming it would mean hand-editing all of them for a
  # cosmetic gain. PBS sets the same label for the same reason.
  - name: proxmox
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: __TELEGRAM_CHATID__
        send_resolved: false
        message: |-
          {{ if eq .CommonLabels.pve_severity "error" }}&#128308;{{ else if eq .CommonLabels.pve_severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [{{ if .CommonLabels.source }}{{ .CommonLabels.source | toUpper }}{{ else }}PROXMOX{{ end }}] {{ .CommonLabels.name }}
          {{ range .Alerts }}
          {{ if .Labels.host }}Node: {{ .Labels.host }}{{ end }}
          {{ if .Labels.vmid }}Guest: {{ .Labels.vmid }}{{ end }}
          {{ if .Labels.datastore }}Datastore: {{ .Labels.datastore }}{{ end }}
          {{ if .Labels.jobid }}Job: {{ .Labels.jobid }}{{ end }}
          {{ .Annotations.summary }}
          {{ if .Annotations.description }}{{ reReplaceAll "(?s)(.{700}).*" "$1 [...]" .Annotations.description }}{{ end }}
          {{ end }}

  # The ping URL is a capability: anyone holding it can report the homelab as
  # healthy, which would mask a real outage. This repo is public, so the URL is
  # never written here -- it is substituted from HEALTHCHECK_URL in the
  # host-local .env on helm, exactly like the Telegram tokens.
  #
  # send_resolved is false because the alert never resolves; a resolved
  # notification would only be sent if the switch itself were removed.
  - name: deadmanswitch
    webhook_configs:
      - url: __HEALTHCHECK_URL__
        send_resolved: false

  - name: plex-requests
    telegram_configs:
      - bot_token_file: /tmp/telegram_token_plex
        chat_id: __TELEGRAM_CHATID_PLEX__
        send_resolved: true
        message: |-
          {{ if eq .Status "resolved" }}&#128994;{{ else if eq .CommonLabels.severity "critical" }}&#128308;{{ else if eq .CommonLabels.severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}{{ if .CommonLabels.name }}: {{ .CommonLabels.name }}{{ end }}
          {{ range .Alerts }}
          Severity: {{ .Labels.severity }}
          {{ if .Labels.host }}Host: {{ .Labels.host }}{{ end }}
          {{ if .Labels.name }}Container: {{ .Labels.name }}{{ end }}
          {{ .Annotations.summary }}
          {{ if .Annotations.description }}{{ .Annotations.description }}{{ end }}
          {{ end }}
