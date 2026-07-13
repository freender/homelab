#!/usr/bin/env bash
set -euo pipefail

DRV_NAME=r8152
DRV_VERSION=${DRV_VERSION:-2.21.4}
REPO_URL=${REPO_URL:-https://github.com/wget/realtek-r8152-linux.git}
REPO_REF=${REPO_REF:-master}
STATE_DIR=/var/lib/homelab/pve-realtek-r8152-dkms
SRC_DIR=${STATE_DIR}/src
STATUS_FILE=${STATE_DIR}/status
DKMS_SRC=/usr/src/${DRV_NAME}-${DRV_VERSION}
AUTOINSTALL_SCRIPT=/usr/local/sbin/homelab-r8152-pve-dkms-autoinstall
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-r8152-pve-dkms-autoinstall
BLACKLIST_FILE=/etc/modprobe.d/99-rtl815x-usb-blacklist.conf
INITRAMFS_MODULES=/etc/initramfs-tools/modules

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

write_status() {
  local state=$1
  local commit="unknown"

  if [[ -d ${SRC_DIR}/.git ]]; then
    commit=$(git -C "${SRC_DIR}" rev-parse HEAD 2>/dev/null || printf 'unknown')
  fi

  {
    printf 'state=%s\n' "${state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'repo_url=%s\n' "${REPO_URL}"
    printf 'repo_ref=%s\n' "${REPO_REF}"
    printf 'repo_commit=%s\n' "${commit}"
    printf 'driver=%s\n' "${DRV_NAME}"
    printf 'driver_version=%s\n' "${DRV_VERSION}"
    dkms status -m "${DRV_NAME}" -v "${DRV_VERSION}" 2>/dev/null || true
  } > "${STATUS_FILE}"
}

live_driver_state() {
  local iface driver bad_driver=0 saw_r8152=0

  for iface_path in /sys/class/net/*; do
    [[ -e ${iface_path} ]] || continue
    iface=$(basename "${iface_path}")
    driver=$(ethtool -i "${iface}" 2>/dev/null | awk '/^driver:/ { print $2; exit }' || true)
    case "${driver}" in
      r8152) saw_r8152=1 ;;
      cdc_ncm|cdc_ether|r8153_ecm) bad_driver=1 ;;
    esac
  done

  if [[ ${bad_driver} -eq 1 ]]; then
    printf 'reboot_required\n'
  elif [[ ${saw_r8152} -eq 1 ]]; then
    printf 'active\n'
  else
    printf 'unknown\n'
  fi
}

install_packages() {
  apt-get update
  apt-get install -y git dkms build-essential libdw1 libelf1
}

install_kernel_headers() {
  local module_dir kernel_version

  for module_dir in /lib/modules/*; do
    [[ -d ${module_dir} ]] || continue
    kernel_version=$(basename "${module_dir}")

    case "${kernel_version}" in
      *-pve) ;;
      *) continue ;;
    esac

    [[ -e ${module_dir}/build ]] && continue

    apt-get install -y "proxmox-headers-${kernel_version}" || \
      apt-get install -y "pve-headers-${kernel_version}" || \
      echo "warning: headers not available for ${kernel_version}" >&2
  done
}

fetch_driver_source() {
  local checkout_ref=${REPO_REF}

  mkdir -p "${STATE_DIR}"

  if [[ -d ${SRC_DIR}/.git ]]; then
    git -C "${SRC_DIR}" fetch --prune origin
  else
    rm -rf "${SRC_DIR}"
    git clone "${REPO_URL}" "${SRC_DIR}"
  fi

  if git -C "${SRC_DIR}" rev-parse --verify --quiet "origin/${REPO_REF}" >/dev/null; then
    checkout_ref="origin/${REPO_REF}"
  fi

  git -C "${SRC_DIR}" checkout --detach "${checkout_ref}"
}

stage_dkms_source() {
  rm -rf "${DKMS_SRC}"
  mkdir -p "${DKMS_SRC}"
  cp "${SRC_DIR}/Makefile" "${SRC_DIR}/compatibility.h" "${SRC_DIR}/r8152.c" "${DKMS_SRC}/"

  cat > "${DKMS_SRC}/dkms.conf" <<EOF
PACKAGE_NAME="${DRV_NAME}"
PACKAGE_VERSION="${DRV_VERSION}"
PROCS_NUM=\`nproc\`
[ \$PROCS_NUM -gt 16 ] && PROCS_NUM=16
MAKE="'make' -j\$PROCS_NUM modules KERNELDIR=/lib/modules/\${kernelver}/build"
CLEAN="'make' clean"
BUILT_MODULE_NAME[0]="r8152"
BUILT_MODULE_LOCATION[0]="."
DEST_MODULE_LOCATION[0]="/updates"
AUTOINSTALL="yes"
EOF
}

install_dkms_module() {
  local module_dir kernel_version result=0

  dkms add -m "${DRV_NAME}" -v "${DRV_VERSION}" 2>/tmp/homelab-r8152-dkms-add.err || {
    if ! grep -q "DKMS tree already contains" /tmp/homelab-r8152-dkms-add.err; then
      cat /tmp/homelab-r8152-dkms-add.err >&2
      return 1
    fi
  }
  rm -f /tmp/homelab-r8152-dkms-add.err

  dkms build -m "${DRV_NAME}" -v "${DRV_VERSION}"

  for module_dir in /lib/modules/*; do
    [[ -d ${module_dir} ]] || continue
    kernel_version=$(basename "${module_dir}")

    case "${kernel_version}" in
      *-pve) ;;
      *) continue ;;
    esac

    if [[ ! -e ${module_dir}/build ]]; then
      echo "warning: skipping ${kernel_version}; headers missing" >&2
      result=1
      continue
    fi

    dkms install --force -m "${DRV_NAME}" -v "${DRV_VERSION}" -k "${kernel_version}" || result=1
  done

  return "${result}"
}

install_runtime_config() {
  install -m 0644 "${SRC_DIR}/50-usb-realtek-net.rules" /etc/udev/rules.d/50-usb-realtek-net.rules
  udevadm control --reload-rules

  cat > "${BLACKLIST_FILE}" <<'EOF'
# Prevent generic/alternate drivers from binding RTL815x USB NICs.
blacklist cdc_ncm
blacklist cdc_ether
blacklist r8153_ecm
install cdc_ncm /bin/false
install cdc_ether /bin/false
install r8153_ecm /bin/false
EOF
  chmod 0644 "${BLACKLIST_FILE}"

  touch "${INITRAMFS_MODULES}"
  if ! grep -qE '^\s*r8152(\s|$)' "${INITRAMFS_MODULES}"; then
    printf 'r8152\n' >> "${INITRAMFS_MODULES}"
  fi

  update-initramfs -u -k all
}

install_pve_autoinstall_hook() {
  cat > "${AUTOINSTALL_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOCK_DIR=/run/homelab-r8152-pve-dkms-autoinstall.lock

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  exit 0
fi

cleanup() {
  rmdir "${LOCK_DIR}" 2>/dev/null || true
}
trap cleanup EXIT INT HUP TERM

command -v dkms >/dev/null 2>&1 || exit 0

missing_headers=()
for module_dir in /lib/modules/*; do
  [[ -d ${module_dir} ]] || continue
  kernel_version=$(basename "${module_dir}")

  case "${kernel_version}" in
    *-pve) ;;
    *) continue ;;
  esac

  [[ -e ${module_dir}/build ]] || missing_headers+=("${kernel_version}")
done

if [[ ${#missing_headers[@]} -gt 0 ]]; then
  apt-get update

  for kernel_version in "${missing_headers[@]}"; do
    [[ -e /lib/modules/${kernel_version}/build ]] && continue
    apt-get install -y "proxmox-headers-${kernel_version}" || \
      apt-get install -y "pve-headers-${kernel_version}" || \
      true
  done
fi

for module_dir in /lib/modules/*; do
  [[ -d ${module_dir} ]] || continue
  kernel_version=$(basename "${module_dir}")

  case "${kernel_version}" in
    *-pve) ;;
    *) continue ;;
  esac

  [[ -e ${module_dir}/build ]] || continue
  dkms autoinstall -k "${kernel_version}" || true
  update-initramfs -u -k "${kernel_version}" || true
done
EOF
  chmod 0755 "${AUTOINSTALL_SCRIPT}"

  cat > "${APT_HOOK}" <<EOF
DPkg::Post-Invoke {
  "if command -v systemd-run >/dev/null 2>&1 && [ -x ${AUTOINSTALL_SCRIPT} ]; then systemd-run --unit=homelab-r8152-pve-dkms-autoinstall --on-active=30s ${AUTOINSTALL_SCRIPT} >/dev/null 2>&1 || true; fi";
};
EOF
  chmod 0644 "${APT_HOOK}"
}

mkdir -p "${STATE_DIR}"
install_packages
install_kernel_headers
fetch_driver_source
stage_dkms_source

result=0
install_dkms_module || result=1

# The blacklist hard-disables the generic RTL815x drivers (install ... /bin/false).
# Only ever establish it when the r8152 DKMS module actually built for every -pve
# kernel; otherwise the next boot into an unbuilt kernel has neither r8152 nor a
# generic fallback, and the host comes back with no network. On failure, tear any
# stale blacklist back down so the generic drivers can bind.
if [[ ${result} -eq 0 ]]; then
  install_runtime_config
else
  echo "error: r8152 DKMS build failed for at least one -pve kernel; refusing to blacklist the generic drivers" >&2
  if [[ -e ${BLACKLIST_FILE} ]]; then
    rm -f "${BLACKLIST_FILE}"
    update-initramfs -u -k all
    echo "warning: removed stale ${BLACKLIST_FILE} so cdc_ncm/cdc_ether/r8153_ecm can bind the NIC" >&2
  fi
fi

install_pve_autoinstall_hook

driver_state=$(live_driver_state)

if [[ ${driver_state} == reboot_required ]]; then
  echo "warning: Realtek USB NIC is still bound to a generic driver; reboot and rerun deploy" >&2
  write_status reboot_required
elif [[ ${result} -eq 0 ]]; then
  write_status installed
else
  write_status partial
fi

cat "${STATUS_FILE}"
exit "${result}"
