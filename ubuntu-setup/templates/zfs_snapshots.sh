#!/bin/bash

set -euo pipefail

exec /usr/sbin/sanoid --configdir=/etc/sanoid --cron --verbose
