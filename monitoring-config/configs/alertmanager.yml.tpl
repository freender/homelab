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
        - uptime-kuma-maintenance
      continue: false
    - receiver: mwbot
      mute_time_intervals:
        - uptime-kuma-maintenance
      continue: false

time_intervals:
  - name: uptime-kuma-maintenance
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
          {{ if eq .Status "resolved" }}&#128994;{{ else if eq .CommonLabels.severity "critical" }}&#128308;{{ else if eq .CommonLabels.severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}
          {{ range .Alerts }}
          Severity: {{ .Labels.severity }}
          {{ if .Labels.host }}Host: {{ .Labels.host }}{{ end }}
          {{ if .Labels.name }}Container: {{ .Labels.name }}{{ end }}
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
          {{ if eq .Status "resolved" }}&#128994;{{ else if eq .CommonLabels.severity "critical" }}&#128308;{{ else if eq .CommonLabels.severity "warning" }}&#128993;{{ else }}&#128309;{{ end }} [{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}
          {{ range .Alerts }}
          Severity: {{ .Labels.severity }}
          {{ if .Labels.host }}Host: {{ .Labels.host }}{{ end }}
          {{ if .Labels.name }}Container: {{ .Labels.name }}{{ end }}
          {{ .Annotations.summary }}
          {{ if .Annotations.description }}{{ .Annotations.description }}{{ end }}
          {{ end }}
