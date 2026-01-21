## apcupsd.conf v1.1 ##
## apcupsd.conf - ${HOST} (Master)
## USB connection to UPS on ${HOST}
## Triggers coordinated shutdown of cluster

UPSNAME ${UPSNAME}
UPSCABLE usb
UPSTYPE usb
DEVICE

# Network Information Server (NIS) - allows slaves to monitor
NISIP ${NISIP}
NISPORT 3551
NETSERVER on

# Shutdown thresholds
BATTERYLEVEL 20
MINUTES 10
TIMEOUT 0

# Timing
ONBATTERYDELAY 30
ANNOY 300
ANNOYDELAY 60
NOLOGON disable
KILLDELAY 60

# Paths
PWRFAILDIR /etc/apcupsd
NOLOGINDIR /etc
STATTIME 0
STATFILE /var/log/apcupsd.status
LOGSTATS off
DATATIME 0
