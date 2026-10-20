#!/bin/bash
# Build a RAM-bootable XNU image for the OnePlus 7T (hotdogb).
#
#   hotdogb/mkxnu-boot.sh KERNEL SHIM_IMAGE STOCK_BOOT_IMG OUT.img ["extra boot-args"]
#
#   KERNEL          kernel.development.sweet from the xnu CI artifact
#   SHIM_IMAGE      shim-xnu.Image from the shim CI artifact (or shim/out-xnu/Image)
#   STOCK_BOOT_IMG  the phone's own boot.img (backup/boot_b.img): header + DTB are reused
#
# Safe by default: no ufsrw/rootrw (sweet_ufs refuses every write), sweetlog=0
# (no console log at the end of userdata), Redmi-only drivers off, 1 CPU.
# Writing to UFS needs ALLOW_UFS_WRITES=1 and an explicit decision (PORTING_NOTES).
# RAMDISK=<image> boots a RAM-only root (rd=md0) instead of userdata (rd=ufsroot0).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
[ $# -ge 4 ] || { sed -n 2,12p "$0"; exit 1; }
kernel=$1 shim=$2 stock=$3 out=$4 extra=${5:-}

args="debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress wdt=-1"
root=ufsroot0; [ -n "${RAMDISK:-}" ] && root=md0
log=sweetlog=0; [ "${ALLOW_UFS_WRITES:-}" = 1 ] && log=""   # writes allowed: keep the console log (last 4 MiB of userdata)
args+=" rd=$root $log sweettouch=0 sweetkeys=0 sweetusb=0 sweetbattery=0"
args+=" $extra"
if [[ "$args" =~ (ufsrw|rootrw|sweetlog=1) ]] && [ "${ALLOW_UFS_WRITES:-}" != 1 ] || { [ "${ALLOW_UFS_WRITES:-}" = 1 ] && [ -n "${RAMDISK:-}" ]; }; then
	echo "refusing: boot-args would allow UFS writes ($args)" >&2
	exit 1
fi

tmp=$(mktemp -d)
python3 "$here/tools/mkadt.py" "$tmp/hotdogb.adt" --highmem
python3 "$here/tools/mkpayload.py" "$kernel" "$tmp/hotdogb.adt" "$tmp/payload.bin" --cmdline "$args" ${RAMDISK:+--ramdisk "$RAMDISK"}
python3 "$here/hotdogb/pack-boot.py" --stock "$stock" --kernel "$shim" --ramdisk "$tmp/payload.bin" --out "$out"
echo "boot-args: $args"
rm -rf "$tmp"
