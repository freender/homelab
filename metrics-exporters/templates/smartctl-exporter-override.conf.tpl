# Managed by homelab (metrics-exporters). Drop-in for the distro
# prometheus-smartctl-exporter unit, whose packaged ExecStart takes no
# arguments at all. ExecStart must be cleared first: it is a non-list
# directive, so an unreset second assignment would be a startup error.
#
# All four flags below are the upstream defaults except --smartctl.interval;
# they are stated explicitly rather than relied on so a future upstream default
# change cannot silently alter behaviour here:
#   --smartctl.interval=10s        matches the 10s scrape_interval of the
#                                  pve-smartctl job in vmagent/scrape.yml on
#                                  helm (upstream default is 60s).
#   --smartctl.powermode-check     load-bearing next to the disk-spindown
#                                  module: "standby" makes the exporter skip
#                                  (not wake) a spun-down disk.
#   --web.listen-address=:9633     the port vmagent scrapes.
#   --smartctl.path                normally /usr/sbin/smartctl; hosts setting
#                                  metrics-exporters.smartctl_wrapper get the
#                                  wrapper instead (see README).
#
# ExecStartPre holds the exporter back until the disk list stops changing,
# because it registers its metric descriptors once at startup but keeps
# rescanning for devices afterwards -- a disk that appears late makes /metrics
# return HTTP 500 for every disk until the next restart. See the script's header
# for the incident this comes from. Unlike ExecStart it is a list directive with
# no packaged value, so it needs no reset line. TimeoutStartSec must exceed the
# script's own timeout (180s) or systemd kills the wait before it can give up
# gracefully; the packaged unit inherits the 90s default, which is too low.
[Service]
TimeoutStartSec=300
ExecStartPre=/usr/local/bin/homelab-smartctl-wait-devices {{ SMARTCTL_PATH }}
ExecStart=
ExecStart=/usr/bin/smartctl_exporter --web.listen-address=:9633 --smartctl.path={{ SMARTCTL_PATH }} --smartctl.interval=10s --smartctl.powermode-check=standby
