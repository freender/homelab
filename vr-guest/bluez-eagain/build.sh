#!/bin/bash
# Runs inside BLUEZ_IMAGE (vr_guest.py), started by vr-guest's bluez stage. Builds bluetoothd as terra-bluez-5.87+1.git8750129efca8-2 does, plus patches 0100 (EAGAIN drop) and 0101 (Supremium sniff mode, RealSupremium/bluez fadd645).
set -euo pipefail
dnf -y -q install dbus-devel glib2-devel libell-devel libical-devel make readline-devel json-c-devel \
  systemd-devel systemd cups-devel libtool automake autoconf python3-docutils python3-pygments \
  gcc curl patch redhat-rpm-config annobin-plugin-gcc >/dev/null
cd /build
mkdir -p /work/bluez && cd /work/bluez
curl -sL https://github.com/bluez/bluez/archive/8750129efca8.tar.gz | tar xz --strip-components=1
for p in /patches/[0-9]*.patch; do echo "apply $(basename "$p")"; patch -p1 -F3 -s < "$p"; done
autoreconf -if > /build/autoreconf.log 2>&1
CFLAGS="$(rpm --eval %optflags)"
LDFLAGS="$(rpm --eval %build_ldflags)"
export CFLAGS LDFLAGS
./configure -q \
  --prefix=/usr --sysconfdir=/etc --localstatedir=/var --libdir=/usr/lib64 --libexecdir=/usr/libexec \
  --enable-tools --enable-library --enable-external-ell --disable-optimization --enable-deprecated \
  --enable-sixaxis --enable-cups --enable-nfc --enable-mesh --enable-hid2hci --enable-testing \
  --enable-experimental --enable-bap --enable-bass --enable-mcp --enable-micp --enable-csip --enable-vcp \
  --with-systemdsystemunitdir=/usr/lib/systemd/system --with-systemduserunitdir=/usr/lib/systemd/user \
  > /build/configure.log 2>&1 || { tail -40 config.log; exit 1; }
make -j8 > /build/make.log 2>&1 || { grep -E "error" /build/make.log | head -20; exit 1; }
install -m0755 src/bluetoothd /build/bluetoothd
/build/bluetoothd --version
