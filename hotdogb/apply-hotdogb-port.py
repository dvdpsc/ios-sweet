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
        ('/* ---- main ---- */',
         '#ifdef HOTDOGB_KEYS\n/*\n * hotdogb test-keys: read-only SPMI observer reads (as sweet patch 0060), no PMIC writes.\n * Live DT: arbiter core 0xc440000, obsrvr 0xe600000, cnfg 0xc40a000; power = PM8150 PON\n * KPDPWR (PPID 0x008, RT_STS 0x0810 bit0, RESIN bit1); vol+ = PM8150 GPIO6 (PPID 0x0c5,\n * 0xc510 bit0, active low); vol- = PM8150 GPIO7 (PPID 0x0c6, 0xc610 bit0, active low).\n */\n#define SPMI_CORE\t0x0c440000UL\n#define SPMI_OBS\t0x0e600000UL\n#define SPMI_CNFG\t0x0c40a000UL\n#define NO_APID\t\t0xffffU\n\nstatic uint32_t spmi_status;\n\nstatic uint16_t spmi_apid(uint16_t ppid, uint32_t count)\n{\n\tuint16_t found = NO_APID;\n\tfor (uint32_t i = 0; i < count; i++) {\n\t\tuint32_t map = rd32(SPMI_CORE + 0x900 + 4 * i);\n\t\tif (map == 0 || ((map >> 8) & 0xfff) != ppid)\n\t\t\tcontinue;\n\t\tif (found == NO_APID || (rd32(SPMI_CNFG + 0x700 + 4 * i) & 7) == 0)\n\t\t\tfound = i;\n\t}\n\treturn found;\n}\n\nstatic int spmi_read(uint16_t apid, uint16_t addr, uint8_t *out)\n{\n\tif (apid == NO_APID)\n\t\treturn 0;\n\tuintptr_t ch = SPMI_OBS + 0x80UL * apid;\n\t__asm__ volatile("dmb oshst" ::: "memory");\n\twr32(ch, (1U << 27) | ((uint32_t)(addr & 0xff) << 4));\t/* EXT_READL, 1 byte */\n\t__asm__ volatile("dsb osh" ::: "memory");\n\tuint64_t end = now() + rsr("cntfrq_el0") / 100;\t\t/* 10 ms */\n\twhile (now() < end) {\n\t\tspmi_status = rd32(ch + 0x08);\n\t\tif (spmi_status & 1) {\n\t\t\tif (spmi_status & 0x0e)\n\t\t\t\treturn 0;\n\t\t\t*out = rd32(ch + 0x18);\n\t\t\treturn 1;\n\t\t}\n\t}\n\treturn 0;\n}\n\nstatic void keys_header(uint32_t ver, uint32_t count, const uint16_t *apid)\n{\n\tcon_clear(RED);\n\tcon_str("SHIM", "hotdogb test-keys (read-only SPMI)");\n\tcon_hex2("ARB", "ver=", ver, "apids=", count);\n\tcon_hex2("APID", "pon=", apid[0], "gp6=", apid[1]);\n\tcon_hex2("APID", "gp7=", apid[2], 0, 0);\n\tstatic const uint16_t base[2] = { 0xc500, 0xc600 };\n\tfor (int g = 0; g < 2; g++) {\n\t\tuint8_t st = 0, mode = 0, en = 0;\n\t\tint a = spmi_read(apid[g + 1], base[g] + 0x05, &st);\n\t\tint b = spmi_read(apid[g + 1], base[g] + 0x40, &mode);\n\t\tint c = spmi_read(apid[g + 1], base[g] + 0x46, &en);\n\t\tcon_hex2(g ? "GPIO7" : "GPIO6", "sub/mode=", (a << 31) | (st << 8) | mode,\n\t\t\t "en/ok=", (en << 8) | (b << 1) | c);\n\t}\n\tcon_str("KEYS", "press power / vol+ / vol- (2 min)");\n\tcon_skip();\n}\n\nstatic void test_keys(void)\n{\n\tstage = "keys";\n\tuint32_t ver = rd32(SPMI_CORE), count = rd32(SPMI_CORE + 4) & 0x7ff;\n\tuint16_t apid[3] = { NO_APID, NO_APID, NO_APID };\n\tif (ver >= 0x50000000U && ver < 0x70000000U && count && count <= 512) {\n\t\tapid[0] = spmi_apid(0x008, count);\n\t\tapid[1] = spmi_apid(0x0c5, count);\n\t\tapid[2] = spmi_apid(0x0c6, count);\n\t}\n\tkeys_header(ver, count, apid);\n\tuint32_t last = 0xffffffff;\n\tint events = 0;\n\tuint64_t end = now() + rsr("cntfrq_el0") * 120;\n\twhile (now() < end) {\n\t\tuint8_t pon = 0, g6 = 0, g7 = 0;\n\t\tint ok = spmi_read(apid[0], 0x0810, &pon) | spmi_read(apid[1], 0xc510, &g6) << 1 |\n\t\t\t spmi_read(apid[2], 0xc610, &g7) << 2;\n\t\t/* pressed bits: 1 power, 2 vol+, 4 vol-, 8 resin; ok in bits 8-10 */\n\t\tuint32_t keys = (pon & 1) | (!(g6 & 1) && (ok & 2)) << 1 | (!(g7 & 1) && (ok & 4)) << 2 |\n\t\t\t\t((pon >> 1) & 1) << 3 | ok << 8;\n\t\tif (keys != last) {\n\t\t\tlast = keys;\n\t\t\tif (++events > 18) {\n\t\t\t\tkeys_header(ver, count, apid);\n\t\t\t\tevents = 1;\n\t\t\t}\n\t\t\tcon_hex2("KEY", "pwr/v+/v-/rs=", ((keys & 1) << 12) | ((keys >> 1 & 1) << 8) |\n\t\t\t\t ((keys >> 2 & 1) << 4) | (keys >> 3 & 1), "raw=", (pon << 16) | (g6 << 8) | g7);\n\t\t}\n\t\tdelay_ms(20);\n\t}\n}\n#endif\n\n/* ---- main ---- */'),
        ('#ifdef HOTDOGB_SMP\n',
         '#ifdef HOTDOGB_KEYS\n\ttest_keys();\n\treset(20);\n#endif\n#ifdef HOTDOGB_SMP\n'),
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
    # ---- keys (shim test-keys verified on the phone): vol+ PM8150 GPIO6, vol- PM8150 GPIO7 ----
    'xnu/patches/0060-sweet-keys-touch-drivers.patch': [
        ('+++ b/bsd/dev/sweet_keys.c\n@@ -0,0 +1,182 @@', '+++ b/bsd/dev/sweet_keys.c\n@@ -0,0 +1,185 @@'),
        ('+ * See hw-report.md: volume-up is PM6150L SID 4, NOT PM6150 SID 0.',
         '+ * hotdogb: vol+ = PM8150 GPIO6, vol- = PM8150 GPIO7 (SID 0), power = PON KPDPWR.'),
        ('+#define KEYS_GPIO_RT     0xc110u\n', '+#define KEYS_GPIO_RT     0xc510u\n+#define KEYS_GPIO7_RT    0xc610u\n'),
        ('+static uint16_t keys_pon_apid = KEYS_NO_APID, keys_gpio_apid = KEYS_NO_APID;',
         '+static uint16_t keys_pon_apid = KEYS_NO_APID, keys_gpio_apid = KEYS_NO_APID, keys_gpio7_apid = KEYS_NO_APID;'),
        ('+\t\tbool gok = keys_read(keys_gpio_apid, KEYS_GPIO_RT, &gpio);\n+\t\tint now[3] = {!!(pon & 1u), !(gpio & 1u), !!(pon & 2u)};',
         '+\t\tbool gok = keys_read(keys_gpio_apid, KEYS_GPIO_RT, &gpio);\n'
         '+\t\tuint8_t g7 = 0; bool g7ok = keys_read(keys_gpio7_apid, KEYS_GPIO7_RT, &g7);\n'
         '+\t\tint now[3] = {!!(pon & 1u), !(gpio & 1u), !(g7 & 1u)};'),
        ('+\t\t\tif ((k == 1 ? gok : pok) && now[k] != state[k]) {',
         '+\t\t\tif ((k == 1 ? gok : k == 2 ? g7ok : pok) && now[k] != state[k]) {'),
        ('+\tkeys_gpio_apid = keys_find_apid(0x4c1, count);\n',
         '+\tkeys_gpio_apid = keys_find_apid(0x0c5, count);\n+\tkeys_gpio7_apid = keys_find_apid(0x0c6, count);\n'),
        ('+\tbool mok = keys_read(keys_gpio_apid, 0xc140, &mode);', '+\tbool mok = keys_read(keys_gpio_apid, 0xc540, &mode);'),
        ('+\tbool eok = keys_read(keys_gpio_apid, 0xc146, &en);', '+\tbool eok = keys_read(keys_gpio_apid, 0xc546, &en);'),
        ('+\tbool sok = keys_read(keys_gpio_apid, 0xc105, &subtype);', '+\tbool sok = keys_read(keys_gpio_apid, 0xc505, &subtype);'),
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
