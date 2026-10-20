#!/bin/bash
# 8 MB HFS+ test root (/sbin/launchd = sweet-init, /dev) without HFS+ in the host kernel:
# the WSL2 kernel has no hfsplus, so libguestfs boots Ubuntu's kernel in a tiny KVM appliance.
#   hotdogb/mkhfs-test.sh research/sweet-init research/root-hfs.img
set -euo pipefail
in=$(realpath "$1"); out=$(realpath -m "$2"); dir=$(dirname "$out")
docker run --rm --device /dev/kvm -v "$(dirname "$in")":/in:ro -v "$dir":/out ubuntu:24.04 bash -c "
set -e; export DEBIAN_FRONTEND=noninteractive LIBGUESTFS_BACKEND=direct
apt-get update -qq >/dev/null; apt-get install -y -qq hfsprogs libguestfs-tools linux-image-generic >/dev/null 2>&1
dd if=/dev/zero of=/out/$(basename "$out") bs=1M count=8 status=none
mkfs.hfsplus -v SweetRoot /out/$(basename "$out")
guestfish -a /out/$(basename "$out") <<G
run
mount /dev/sda /
mkdir /sbin
mkdir /dev
upload /in/$(basename "$in") /sbin/launchd
chmod 0755 /sbin/launchd
G
fsck.hfsplus -n /out/$(basename "$out") | tail -1"
