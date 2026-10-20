#!/usr/bin/env python3
"""Pack a shim Image into a boot.img v2, reusing the stock OnePlus 7T boot.img header and DTB.

Pure Python (no mkbootimg), runs on Windows, WSL or macOS. Never touches the phone.
The stock boot.img is private device data: keep it in backup/ (git-ignored).

    pack-boot.py --stock backup/boot_a.img --kernel Image --out shim-test1.img
    pack-boot.py --stock backup/boot_a.img --kernel Image --ramdisk payload.bin --out shim-xnu.img
    pack-boot.py --stock backup/boot_a.img --info                # print header, nothing written
    pack-boot.py --stock backup/boot_a.img --extract-dtb 7t.dtb  # for shim/build.sh DTB=

Everything in the stock header is kept (load addresses, os_version/patch level,
cmdline) except the kernel/ramdisk sizes and the SHA1 id, as the V60 port did.
Without --ramdisk an empty gzip'd newc cpio is used (ABL dislikes a zero-size ramdisk).
"""
import argparse
import gzip
import hashlib
import struct
import sys
from pathlib import Path

DTB_MAGIC = b'\xd0\x0d\xfe\xed'


def align(n, page):
    return (n + page - 1) // page * page


def parse(img):
    if img[:8] != b'ANDROID!':
        sys.exit('stock image: no ANDROID! magic')
    ks, ka, rs, ra, ss, sa, ta, page, ver, osv = struct.unpack_from('<10I', img, 8)
    if ver != 2:
        sys.exit(f'stock image: header version {ver}, this tool only handles v2')
    rdo_size, rdo_off, hdr_size = struct.unpack_from('<IQI', img, 1632)
    dtb_size, dtb_addr = struct.unpack_from('<IQ', img, 1648)
    h = dict(kernel_size=ks, kernel_addr=ka, ramdisk_size=rs, ramdisk_addr=ra,
             second_size=ss, second_addr=sa, tags_addr=ta, page_size=page,
             header_version=ver, os_version=osv, header_size=hdr_size,
             recovery_dtbo_size=rdo_size, recovery_dtbo_offset=rdo_off,
             dtb_size=dtb_size, dtb_addr=dtb_addr,
             name=img[48:64].split(b'\0')[0].decode('latin-1'),
             cmdline=(img[64:576].split(b'\0')[0] + img[608:1632].split(b'\0')[0]).decode('latin-1'))
    pos = page + align(ks, page) + align(rs, page) + align(ss, page) + align(rdo_size, page)
    dtb = img[pos:pos + dtb_size]
    if not dtb_size or len(dtb) != dtb_size or dtb[:4] != DTB_MAGIC:
        sys.exit('stock image: missing or invalid DTB section')
    return h, dtb


def os_version_str(v):
    a, b, c = v >> 25, (v >> 18) & 0x7f, (v >> 11) & 0x7f
    y, m = 2000 + ((v >> 4) & 0x7f), v & 0xf
    return f'{a}.{b}.{c} patch {y}-{m:02d}'


def empty_cpio():
    def entry(name, mode):
        n = name.encode() + b'\0'
        hdr = b'070701' + b''.join(b'%08x' % x for x in
                                   (0, mode, 0, 0, 1 if mode else 0, 0, 0, 0, 0, 0, 0, len(n), 0))
        e = hdr + n
        return e + bytes(-len(e) % 4)
    raw = entry('.', 0o40755) + entry('TRAILER!!!', 0)
    raw += bytes(-len(raw) % 512)
    return gzip.compress(raw, 9, mtime=0)


def pack(stock, kernel, ramdisk, dtb, h):
    page = h['page_size']
    if kernel[56:60] != b'ARM\x64':
        sys.exit('kernel: not an arm64 Image (no ARM\\x64 magic at 0x38)')
    if h['second_size'] or h['recovery_dtbo_size']:
        sys.exit('stock image has a second stage or recovery DTBO: unexpected, stopping')
    hdr = bytearray(stock[:page])
    struct.pack_into('<I', hdr, 8, len(kernel))
    struct.pack_into('<I', hdr, 16, len(ramdisk))
    sha = hashlib.sha1()
    for part in (kernel, ramdisk, b'', b'', dtb):  # AOSP mkbootimg v2 id order
        sha.update(part)
        sha.update(struct.pack('<I', len(part)))
    hdr[576:608] = sha.digest() + bytes(12)
    pad = lambda b: b + bytes(align(len(b), page) - len(b))
    return bytes(hdr) + pad(kernel) + pad(ramdisk) + pad(dtb)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stock', required=True, type=Path, help='stock boot.img (v2) from the backup')
    ap.add_argument('--kernel', type=Path, help='shim Image (shim/out/Image)')
    ap.add_argument('--ramdisk', type=Path, help='xnu payload (tools/mkpayload.py); default: empty cpio')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--extract-dtb', type=Path)
    ap.add_argument('--info', action='store_true')
    a = ap.parse_args()

    stock = a.stock.read_bytes()
    h, dtb = parse(stock)
    if a.info or not (a.out or a.extract_dtb):
        for k, v in h.items():
            print(f'{k:22} {hex(v) if isinstance(v, int) else v}')
        print(f'{"os_version (decoded)":22} {os_version_str(h["os_version"])}')
        print(f'{"dtbs in section":22} {dtb.count(DTB_MAGIC)} (magic count, approximate)')
    if a.extract_dtb:
        a.extract_dtb.write_bytes(dtb)
        print(f'{a.extract_dtb}: {len(dtb)} bytes')
    if a.out:
        if not a.kernel:
            sys.exit('--out needs --kernel')
        ramdisk = a.ramdisk.read_bytes() if a.ramdisk else empty_cpio()
        img = pack(stock, a.kernel.read_bytes(), ramdisk, dtb, h)
        a.out.write_bytes(img)
        print(f'{a.out}: {len(img)} bytes  SHA256 {hashlib.sha256(img).hexdigest()}')


if __name__ == '__main__':
    main()
