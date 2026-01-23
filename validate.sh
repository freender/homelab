#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source lib/print.sh
source lib/common.sh

print_header "Homelab Validation"

print_action "ShellCheck"
if command -v shellcheck &>/dev/null; then
    find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +
    print_ok "ShellCheck passed"
else
    print_warn "shellcheck not installed, skipping"
fi

print_action "YAML Syntax"
if [[ -f hosts.conf ]]; then
    yq eval '.' hosts.conf >/dev/null
    print_ok "hosts.conf valid"
else
    print_warn "hosts.conf missing"
fi

print_action "Dry-run Modules"
for module in */deploy.sh; do
    dir=$(dirname "$module")
    print_sub "$dir"
    if ./$module --dry-run all >/dev/null 2>&1; then
        print_ok "OK"
    else
        print_warn "Issues"
    fi
done

print_header "Validation Complete"
