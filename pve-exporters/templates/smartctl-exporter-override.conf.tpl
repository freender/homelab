# Managed by homelab (pve-exporters). Drop-in for the distro
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
#                                  pve-exporters.smartctl_wrapper get the
#                                  wrapper instead (see README).
[Service]
ExecStart=
ExecStart=/usr/bin/smartctl_exporter --web.listen-address=:9633 --smartctl.path={{ SMARTCTL_PATH }} --smartctl.interval=10s --smartctl.powermode-check=standby
