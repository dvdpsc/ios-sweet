#!/usr/bin/env python3
"""Apply the OnePlus 7T (hotdogb, SM8150) changes to an ios-sweet checkout.

Usage (from anywhere):
    python3 hotdogb/apply-hotdogb-port.py .   (repo root; LF line endings)

- Idempotent: already-applied edits are skipped, so it is safe to re-run.
- Stops with an error if an expected original string is missing (upstream changed).
- Only "config" changes: constants, the apps-watchdog disable and an optional
  framebuffer colour test. No driver code. Sources for every value: PORTING_NOTES.md.
- Patches are read/written as latin-1 (some xnu patches are not valid UTF-8).

Deliberately NOT changed yet (see PORTING_NOTES.md):
- RAM for XNU (PHYS_BASE/MEM_SIZE/HIGH_*): needs the phone's live DT memory node.
- GIC, timer INTIDs, UFS base, PS_HOLD, FB base, 1080x2400: identical to sweet.
- compatible strings: 'qcom,kryo470' and 'xiaomi,sweet' are used for IOKit matching.
"""
import os
import sys

root = sys.argv[1] if len(sys.argv) > 1 else '.'

EDITS = {
    # ---- boot shim -----------------------------------------------------------
    'shim/shim.c': [
        ('0x146aa65cUL\t/* qcom,msm-imem restart_reason@65c */',
         '0x146bf65cUL\t/* hotdogb: sm8150.dtsi qcom,msm-imem@146bf000 + restart_reason@65c */'),
        ('#define PSHOLD\t\t\t0x0c264000UL',
         '#define PSHOLD\t\t\t0x0c264000UL\n'
         '#define WDT_BASE\t\t0x17c10000UL\t/* sm8150.dtsi qcom,wdt@17c10000; EN is +0x08 (as on the V60) */'),
        ('\tstruct mmu_info mi;\n\n\t/* An xnu payload in the ramdisk means: boot it (MMU stays off). */',
         '\tstruct mmu_info mi;\n\n'
         '\t/*\n'
         '\t * hotdogb: ABL may leave the apps watchdog armed (the V60 ABL does: reset\n'
         '\t * after ~1 min). Disable it first, as Linux does on suspend.\n'
         '\t */\n'
         '\twr32(WDT_BASE + 0x08, 0);\n'
         '\t__asm__ volatile("dsb sy" ::: "memory");\n\n'
         '#ifdef HOTDOGB_FBTEST\n'
         '\t/*\n'
         '\t * hotdogb test1: does writing the cont_splash framebuffer change what the\n'
         '\t * command-mode panel shows? Red, green, blue, 3 s each. If the screen\n'
         '\t * never changes, the panel needs an MDP/DSI kick per frame.\n'
         '\t */\n'
         '\tstage = "fbtest";\n'
         '\tcon_clear(RED);\n'
         '\tdelay_ms(3000);\n'
         '\tcon_clear(GREEN);\n'
         '\tdelay_ms(3000);\n'
         '\tcon_clear(BLUE);\n'
         '\tdelay_ms(3000);\n'
         '#endif\n\n'
         '\t/* An xnu payload in the ramdisk means: boot it (MMU stays off). */'),
        ('con_str("SHIM", "sweet milestone 4: xnu loader");',
         'con_str("SHIM", "hotdogb: xnu loader / ufs probe");'),
        # test2 (-DHOTDOGB_TEST2): skip the UFS probe and run sweet's milestone 3
        # (MMU 16K, PSCI, GIC + timer IRQ); keep the result on screen 2 min.
        ('\t/* No payload: probe the internal UFS while the MMU is still off. */\n\tstage = "ufs";\n'
         '\ttest_ufs();\n\tcon_skip();\n',
         '#ifdef HOTDOGB_SMP\n'
         '\t/* smp test: PSCI CPU_ON for MPIDR 0x100-0x700, MMU off (no cache coherency issues). */\n'
         '\tcon_hex2("PSCI", "ver=", smc(PSCI_VERSION, 0, 0, 0), "on=", smc(PSCI_FEATURES, PSCI_CPU_ON, 0, 0));\n'
         '\ttest_smp();\n'
         '\tcon_hex2("GICR1", "grp0=", sec_probe[0], "grp1=", sec_probe[1]);\n'
         '\tcon_hex2("GICR1", "prio=", sec_probe[2], "ok=", sec_probe[3]);\n'
         '\tcon_skip();\n'
         '\tcon_str("SMP", "done, reset in ~2 min");\n'
         '\tdelay_ms(120000);\n'
         '\treset(20);\n'
         '#endif\n'
         '#ifndef HOTDOGB_TEST2\n'
         '\t/* No payload: probe the internal UFS while the MMU is still off. */\n\tstage = "ufs";\n'
         '\ttest_ufs();\n\tcon_skip();\n'),
        ('\tdelay_ms(180000);\n\treset(20);\n',
         '\tdelay_ms(180000);\n\treset(20);\n#endif\n'),
        ('\ttest_gic();\n\n\tstage = "done";\n\treset(40);',
         '\ttest_gic();\n\n\tstage = "done";\n'
         '#ifdef HOTDOGB_TEST2\n\tdelay_ms(80000);\t/* time to photograph */\n#endif\n'
         '\treset(40);'),
    ],
    'shim/build.sh': [
        ('# Build the shim and wrap it in a boot.img v2 that mirrors the stock PixelOS header.',
         '# Build the shim and wrap it in a boot.img v2.\n'
         '# hotdogb: DTB=<dtb extracted from the stock OnePlus boot.img> EXTRA_CFLAGS=-DHOTDOGB_FBTEST\n'
         '# Without mkbootimg (CI), NO_BOOTIMG=1 stops after out/Image; pack it with port/pack-boot.py.'),
        ('CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra"',
         'CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra ${EXTRA_CFLAGS:-}"'),
        ('truncate -s "$size" out/Image\n',
         'truncate -s "$size" out/Image\n'
         'if [ -n "${NO_BOOTIMG:-}" ]; then ls -l out/Image; exit 0; fi\n'),
        ('--dtb ../dtb/boot_unpacked/dtb', '--dtb "${DTB:-../dtb/boot_unpacked/dtb}"'),
    ],
    # ---- Apple Device Tree generator -----------------------------------------
    'tools/mkadt.py': [
        ('FB, FB_SIZE = 0x9c000000, 0x1700000',
         'FB, FB_SIZE = 0x9c000000, 0x2400000   # hotdogb: sm8150.dtsi cont_splash_region (36 MiB)'),
        ("('clock-frequency', u32(2304000000 if core >= 6 else 1804800000)),",
         "# hotdogb SM8150+ (descriptive only): 4x A55 1.785 GHz, 3x gold 2.42 GHz, 1x prime 2.96 GHz\n"
         "            ('clock-frequency', u32(2956800000 if core == 7 else 2419200000 if core >= 4 else 1785600000)),"),
        ('# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)\nPHYS_BASE = 0xb0000000\n',
         '# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)\n'
         '# hotdogb live FDT memory node: <0x80000000 0x3bb00000>, <0xc0000000 0xc0000000>,\n'
         '# <0x1_80000000 0x1_00000000>: hole at 0xbbb00000-0xc0000000, so sweet\'s\n'
         '# 0xb0000000 base would cross it. Fixed carve-outs all end below 0xace00000.\n'
         'PHYS_BASE = 0xc0000000\n'),
        ('# carve-out is in the low bank and the dynamic pools are limited below 4G.\n',
         '# carve-out is in the low bank and the dynamic pools are limited below 4G.\n'
         '# hotdogb: 0x1_00000000-0x1_fcc00000 lies inside the live 0xc0000000-0x2_80000000\n'
         '# RAM run, no reserved-memory above 4G: sweet values kept (up to 6G available).\n'),
    ],
}


def main():
    changed = 0
    for rel, pairs in EDITS.items():
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            sys.exit(f'[error] missing file: {path} (pass the ios-sweet checkout root)')
        with open(path, encoding='latin-1', newline='') as f:
            s = f.read()
        for old, new in pairs:
            if new in s:
                continue  # already applied
            if s.count(old) == 1:
                s = s.replace(old, new)
                changed += 1
            else:
                sys.exit(f'[error] {rel}: expected original text not found (or not unique):\n  {old!r}')
        with open(path, 'w', encoding='latin-1', newline='') as f:
            f.write(s)
    print(f'done: {changed} edits applied (already-applied ones skipped)')


if __name__ == '__main__':
    main()
