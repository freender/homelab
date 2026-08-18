#!/usr/bin/env bash
# install.sh - Ensure the baseline package set is installed on an apt host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE_PACKAGES=${BASE_PACKAGES:-}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

if [[ ${EUID} -ne 0 ]]; then
    print_error "must run as root"
    exit 1
fi

print_header "Base Packages"

if [[ -z "${BASE_PACKAGES// /}" ]]; then
    print_error "BASE_PACKAGES is empty; refusing to run"
    exit 1
fi

# Word splitting is intentional: BASE_PACKAGES arrives as a space-separated list
# rendered by the orchestrator.
# shellcheck disable=SC2206
requested=($BASE_PACKAGES)

missing=()
for package in "${requested[@]}"; do
    if ! dpkg -s "$package" >/dev/null 2>&1; then
        missing+=("$package")
    fi
done

if [[ ${#missing[@]} -eq 0 ]]; then
    print_sub "All baseline packages already installed: ${requested[*]}"
    print_header "Base Packages Complete"
    exit 0
fi

print_action "Installing missing packages: ${missing[*]}"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"

# Re-check rather than trusting the apt exit status: a package that resolves but
# fails to configure would otherwise be reported as installed.
still_missing=()
for package in "${missing[@]}"; do
    if ! dpkg -s "$package" >/dev/null 2>&1; then
        still_missing+=("$package")
    fi
done

if [[ ${#still_missing[@]} -gt 0 ]]; then
    print_error "packages still missing after install: ${still_missing[*]}"
    exit 1
fi

print_ok "Installed: ${missing[*]}"
print_header "Base Packages Complete"
