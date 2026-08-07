[Unit]
Description=Re-arm Proxmox HA after coordinated UPS shutdown
Wants=corosync.service pve-cluster.service pve-ha-crm.service
After=corosync.service pve-cluster.service pve-ha-crm.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/homelab-ha-rearm
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
