#!/bin/bash
set -e

curl -s -o /dev/null -w %{http_code} --resolve {{ HEALTHCHECK_HOST }}:443:127.0.0.1 {{ HEALTHCHECK_URL }} | grep -q 200
