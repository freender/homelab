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
    # Proxmox notifications are discrete events, not conditions: they never resolve
    # themselves, so suppress resolved notices and do not re-notify. They are also
    # deliberately not muted by the maintenance interval, because a dropped PVE
    # error would never be re-sent.
    - receiver: pve
      matchers:
        - source="pve"
      group_by:
        - alertname
        - host
        - name
        - vmid
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

  - name: pve
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: __TELEGRAM_CHATID__
        send_resolved: false
        message: |-
          {{ if eq .CommonLabels.pve_severity "error" }}&#128308;{{ else if eq .CommonLabels.pve_severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [PROXMOX] {{ .CommonLabels.name }}
          {{ range .Alerts }}
          {{ if .Labels.host }}Node: {{ .Labels.host }}{{ end }}
          {{ if .Labels.vmid }}Guest: {{ .Labels.vmid }}{{ end }}
          {{ .Annotations.summary }}
          {{ if .Annotations.description }}{{ reReplaceAll "(?s)(.{700}).*" "$1 [...]" .Annotations.description }}{{ end }}
          {{ end }}

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
