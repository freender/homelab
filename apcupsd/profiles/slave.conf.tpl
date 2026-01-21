## apcupsd.conf v1.1 ##
## apcupsd.conf - ${HOST} (Slave)
## Network client monitoring master UPS via NIS
## Shutdown triggered by master via SSH

UPSNAME ${UPSNAME}
UPSCABLE ether
UPSTYPE net
DEVICE ${DEVICE}

# NIS disabled on slave (only monitors master)
NISIP ${NISIP}
NISPORT 3551
NETSERVER off

# No local shutdown thresholds - master controls shutdown
BATTERYLEVEL 0
MINUTES 0
TIMEOUT 0

# Timing
ONBATTERYDELAY 6
ANNOY 300
ANNOYDELAY 60
NOLOGON disable
KILLDELAY 0

# Paths
PWRFAILDIR /etc/apcupsd
NOLOGINDIR /etc
STATTIME 0
STATFILE /var/log/apcupsd.status
LOGSTATS off
DATATIME 0
