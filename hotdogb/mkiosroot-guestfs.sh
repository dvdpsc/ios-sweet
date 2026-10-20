#!/bin/bash
# tools/mkiosroot.sh + tools/sweet-root-tweaks.py for hosts without HFS+ in the
# kernel (WSL2). Same steps and result, different plumbing:
#   1. apfs-fuse the three IPSW images, rsync -aH them into a STAGING dir (ext4)
#   2. apply mkiosroot.sh's additions + sweet-root-tweaks.py to the staging dir
#   3. mkfs.hfsplus -s -J, then libguestfs (Ubuntu kernel in a KVM appliance)
#      tar-in the staging dir; fsck_hfs -fy / -n; img2simg
# Runs inside the hotdogb-rootbuild Docker image (Dockerfile in PORTING_NOTES).
#
#   docker run --rm --device /dev/fuse --device /dev/kvm --cap-add SYS_ADMIN \
#     --security-opt apparmor:unconfined -v ~/ios-root:/w -v $REPO:/repo:ro hotdogb-rootbuild \
#     bash /repo/hotdogb/mkiosroot-guestfs.sh /w/ipsw/OS.dmg /w/ipsw/SYSOS.dmg /w/ipsw/APPOS.dmg /w/out/ios16-root.img [16G]
set -euo pipefail
[[ $# -ge 4 ]] || { sed -n '2,13p' "$0"; exit 2; }
os=$1 sysos=$2 appos=$3 out=$4 size=${5:-16G}
[[ ! -e $out ]] || { echo "refusing to overwrite $out" >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(dirname "$out")/work stage=$(dirname "$out")/stage
mkdir -p "$work" && rm -rf "$stage" && mkdir -p "$stage"
mounts=()
trap 'for m in "${mounts[@]}"; do fusermount3 -u "$m" 2>/dev/null || umount "$m" 2>/dev/null || true; done' EXIT

# apfs-fuse splits options on ',' and only parses UDIF when the name ends in .dmg
mnt() { ln -sf "$1" "$work/$2.dmg"; mkdir -p "$work/$2"; apfs-fuse -o allow_other "$work/$2.dmg" "$work/$2"; mounts+=("$work/$2"); }
mnt "$os" os; mnt "$sysos" sysos; mnt "$appos" appos

copy() { echo "copying $1 -> ${2#"$stage"}"; mkdir -p "$2"; rsync -aH --numeric-ids "$1/" "$2/"; }
copy "$work/os/root" "$stage"
copy "$work/sysos/root" "$stage/private/preboot/Cryptexes/OS"
copy "$work/appos/root" "$stage/private/preboot/Cryptexes/App"
for m in "${mounts[@]}"; do fusermount3 -u "$m" || umount "$m"; done; mounts=()

# ---- mkiosroot.sh additions (same as upstream, on the staging dir) ----
dsc=private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld
mkdir -p "$stage/System/Library/Caches/com.apple.dyld"
for f in "$stage/$dsc"/*; do ln "$f" "$stage/System/Library/Caches/com.apple.dyld/${f##*/}"; done
for c in OS App; do
	(cd "$stage/private/preboot/Cryptexes/$c" && find . ! -type d ! -path './System/Library/Caches/com.apple.dyld/*') |
	while IFS= read -r f; do
		f=${f#./}
		if [[ ! -e "$stage/$f" && ! -L "$stage/$f" ]]; then
			mkdir -p "$stage/$(dirname "$f")"
			src="$stage/private/preboot/Cryptexes/$c/$f"
			if [[ -L "$src" ]]; then cp -P "$src" "$stage/$f"; else ln "$src" "$stage/$f"; fi
		fi
	done
done
db="$stage/private/var/db"
install -d -o 272 -g 272 -m 0751 "$db/diagnostics"
install -d -o 272 -g 272 -m 0755 "$db/uuidtext" "$db/diagnostics/timesync"
for d in diagnostics uuidtext diagnostics/timesync; do install -o 272 -g 272 -m 0644 /dev/null "$db/$d/.migrated"; done
install -d -m 0700 -o 0 -g 0 "$stage/private/var/keybags"
printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
	'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	'<plist version="1.0"><dict><key>KeyBagKeys</key><data>U1dFRVQ=</data></dict></plist>' |
	install -m 0600 -o 0 -g 0 /dev/stdin "$stage/private/var/keybags/systembag.kb"

# ---- bring-up tweaks (upstream script, path based) ----
python3 "$repo/tools/sweet-root-tweaks.py" "$stage"

# Linux hfsplus cannot hard-link symlinks: give every multiply-linked symlink its own inode.
find "$stage" -type l -links +1 -print0 | while IFS= read -r -d '' l; do cp -P "$l" "$l.sw$$" && mv -f "$l.sw$$" "$l"; done
du -sh --apparent-size "$stage"

# ---- HFS+ image ----
truncate -s "$size" "$out"
mkfs.hfsplus -s -J -v iOSRoot "$out"
tar -C "$stage" --numeric-owner -cf - . | guestfish -a "$out" -- run : mount-options force /dev/sda / : \
	tar-in - / : df-h : umount-all
fsck.hfsplus -f -y "$out" | grep -v 'Illegal name' || true
fsck.hfsplus -f -n "$out"
img2simg "$out" "${out%.img}.simg"
sha256sum "$out" "${out%.img}.simg" | tee "${out%.img}.sha256"
