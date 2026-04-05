#!/bin/bash

if ! command -v print_sub >/dev/null 2>&1; then
    print_sub() { echo "    $*"; }
fi

if ! command -v print_ok >/dev/null 2>&1; then
    print_ok() { echo "    ✓ $*"; }
fi

remove_snap_docker() {
    if ! command -v snap >/dev/null 2>&1; then
        return 0
    fi

    if ! snap list docker >/dev/null 2>&1; then
        return 0
    fi

    print_sub "Removing snap docker before installing Docker CE..."
    snap stop docker >/dev/null 2>&1 || true
    snap disable docker >/dev/null 2>&1 || true
    snap remove --purge docker
    print_ok "Removed snap docker"
}

ensure_docker_installed() {
    local install_reason=""
    local installer_path
    local install_args=""
    local has_docker_repo=false

    remove_snap_docker

    if [[ -f /etc/apt/sources.list.d/docker.list ]] || [[ -f /etc/apt/sources.list.d/docker.sources ]]; then
        has_docker_repo=true
    fi

    if ! command -v docker >/dev/null 2>&1; then
        install_reason="Docker CE not installed"
    elif [[ "$has_docker_repo" != "true" ]]; then
        install_reason="Docker apt source missing"
        install_args="--setup-repo"
    fi

    if [[ -z "$install_reason" ]]; then
        print_sub "Docker already installed"
        return 0
    fi

    print_sub "$install_reason; running Docker convenience installer..."
    apt-get update -qq
    apt-get install -y -q ca-certificates curl

    installer_path="$(mktemp /tmp/get-docker.XXXXXX.sh)"
    trap 'rm -f "$installer_path"' RETURN
    curl -fsSL https://get.docker.com -o "$installer_path"
    sh "$installer_path" $install_args

    if [[ -n "$install_args" ]]; then
        print_ok "Docker apt source configured"
    else
        print_ok "Docker CE installed"
    fi
}
