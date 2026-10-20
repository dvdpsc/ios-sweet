/*
 * Milestone 2: one boot answers the questions that decide the XNU port.
 *   - CPU feature registers (16K granule, GIC sysreg interface)
 *   - PSCI/SMCCC version and which PSCI calls the Qualcomm TZ offers
 *   - SMP: PSCI CPU_ON for the 7 secondaries, each reports its MPIDR
 *   - GICv3 Group 1 IRQ from the virtual timer (INTID 19 on Qualcomm)
 * Any unexpected exception paints a blue panic screen.
 */
#include <stdint.h>
#include "console.h"
#include "mmu.h"
#include "loader.h"
#include "ufs.h"

#define IMEM_RESTART_REASON	0x146bf65cUL	/* hotdogb: sm8150.dtsi qcom,msm-imem@146bf000 + restart_reason@65c */
#define RESTART_BOOTLOADER	0x77665500U
#define PSHOLD			0x0c264000UL
#define WDT_BASE		0x17c10000UL	/* sm8150.dtsi qcom,wdt@17c10000; EN is +0x08 (as on the V60) */

#define GICD_BASE		0x17a00000UL
#define GICR_BASE		0x17a60000UL
#define GICR_SIZE		0x100000UL
#define GICR_PE_SIZE		0x20000UL
#define GICR_TYPER		0x08
#define GICR_WAKER		0x14
#define GICR_SGI		0x10000
#define GICR_IGROUPR0		(GICR_SGI + 0x080)
#define GICR_ISENABLER0		(GICR_SGI + 0x100)
#define GICR_ICENABLER0		(GICR_SGI + 0x180)
#define GICR_IPRIORITYR		(GICR_SGI + 0x400)
#define GICR_ISPENDR0		(GICR_SGI + 0x200)
#define GICR_ISACTIVER0		(GICR_SGI + 0x300)
#define GICR_ICFGR1		(GICR_SGI + 0xc04)
#define VTIMER_INTID		19	/* DT: <GIC_PPI 3 ...>, not the usual 27 */
#define PTIMER_INTID		18	/* DT: <GIC_PPI 2 ...> */
#define SGI_INTID		1

#define PSCI_VERSION		0x84000000UL
#define PSCI_CPU_SUSPEND	0xc4000001UL
#define PSCI_CPU_OFF		0x84000002UL
#define PSCI_CPU_ON		0xc4000003UL
#define PSCI_MIGRATE_INFO_TYPE	0x84000006UL
#define PSCI_SYSTEM_RESET	0x84000009UL
#define PSCI_FEATURES		0x8400000aUL
#define PSCI_SYSTEM_SUSPEND	0xc400000eUL
#define PSCI_SYSTEM_RESET2	0xc4000012UL
#define SMCCC_VERSION		0x80000000UL

#define NR_CPUS			8

#define rsr(r) ({ uint64_t _v; __asm__ volatile("mrs %0, " r : "=r"(_v)); _v; })
#define wsr(r, v) __asm__ volatile("msr " r ", %0" : : "r"((uint64_t)(v)))
#define isb() __asm__ volatile("isb" ::: "memory")

static inline uint32_t rd32(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline uint64_t rd64(uintptr_t a) { return *(volatile uint64_t *)a; }
static inline void wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline void wr8(uintptr_t a, uint8_t v) { *(volatile uint8_t *)a = v; }

extern char secondary_entry[];

static const char *stage;	/* set at runtime: no absolute pointers in data */
static volatile uint64_t sec_mpidr[NR_CPUS];
static volatile uint64_t sec_probe[4];	/* CPU1 redistributor write test */
static volatile uint32_t irq_count, irq_other, irq_last;
static volatile int irq_src;

/* ICC_SGI1R_EL1 value targeting the calling core only. */
static uint64_t sgi_self(unsigned id)
{
	uint64_t m = rsr("mpidr_el1");
	return ((m >> 16) & 0xff) << 32 |	/* Aff2 */
	       ((m >> 8) & 0xff) << 16 |	/* Aff1 */
	       (uint64_t)id << 24 |
	       (1U << (m & 0xf));		/* target list: Aff0 */
}

static uint64_t smc(uint64_t fn, uint64_t a1, uint64_t a2, uint64_t a3)
{
	register uint64_t x0 __asm__("x0") = fn;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = a3;
	__asm__ volatile("smc #0"
			 : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3)
			 :
			 : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
			   "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	return x0;
}

static uint64_t now(void)
{
	isb();
	return rsr("cntpct_el0");
}

static void delay_ms(unsigned ms)
{
	uint64_t end = now() + rsr("cntfrq_el0") / 1000 * ms;
	while (now() < end)
		;
}

static void __attribute__((noreturn)) reset(unsigned after_s)
{
	con_skip();
	con_str("RESET", after_s > 20 ? "in 40s" : "in 20s");
	delay_ms(after_s * 1000);
	wr32(IMEM_RESTART_REASON, RESTART_BOOTLOADER);
	__asm__ volatile("dsb sy" ::: "memory");
	smc(PSCI_SYSTEM_RESET, 0, 0, 0);
	wr32(PSHOLD, 0);
	for (;;)
		__asm__ volatile("wfe");
}

/* ---- exceptions ---- */

static void irq_handler(void)
{
	uint64_t iar = rsr("icc_iar1_el1");
	uint32_t id = iar & 0xffffff;

	if (id >= 1020 && id <= 1023)
		return;	/* spurious */
	irq_last = id;
	if (id == (uint32_t)irq_src)
		irq_count++;
	else
		irq_other++;
	if (id == VTIMER_INTID)
		wsr("cntv_tval_el0", rsr("cntfrq_el0") / 100);	/* 10 ms */
	else if (id == PTIMER_INTID)
		wsr("cntp_tval_el0", rsr("cntfrq_el0") / 100);
	wsr("icc_eoir1_el1", iar);
	isb();
}

void exc_handler(uint64_t slot, uint64_t *regs)
{
	if (slot == 5) {	/* IRQ from current EL, SPx */
		irq_handler();
		return;
	}
	con_clear(BLUE);
	switch (slot & 3) {
	case 0: con_str("PANIC", "SYNC"); break;
	case 1: con_str("PANIC", "IRQ"); break;
	case 2: con_str("PANIC", "FIQ"); break;
	default: con_str("PANIC", "SERROR"); break;
	}
	con_hex2("SLOT", "n=", slot, "cpu=", rsr("mpidr_el1"));
	con_str("STAGE", stage);
	con_hex("ESR", rsr("esr_el1"));
	con_hex("ELR", rsr("elr_el1"));
	con_hex("FAR", rsr("far_el1"));
	con_hex("SPSR", rsr("spsr_el1"));
	con_hex("X0", regs[0]);
	con_hex("X1", regs[1]);
	con_hex("X30", regs[29]);	/* stashed lr is just above the frame */
	reset(40);
}

/* ---- SMP ---- */

static uintptr_t find_gicr(void);

void secondary_main(uint64_t idx)
{
	sec_mpidr[idx] = rsr("mpidr_el1") | (1ULL << 63);
	if (idx == 1) {
		uintptr_t r = find_gicr();
		if (r) {
			sec_probe[0] = rd32(r + GICR_IGROUPR0);
			wr32(r + GICR_IGROUPR0, 0xffffffff);
			sec_probe[1] = rd32(r + GICR_IGROUPR0);
			wr8(r + GICR_IPRIORITYR + 19, 0xa0);
			sec_probe[2] = rd32(r + GICR_IPRIORITYR + 16);
		}
		sec_probe[3] = r ? 1 : 0xdead;
	}
	__asm__ volatile("dsb sy" ::: "memory");
	smc(PSCI_CPU_OFF, 0, 0, 0);
}

static void __attribute__((unused)) test_smp(void)
{
	stage = "smp";
	uint64_t self = rsr("mpidr_el1") & 0xffffff;
	int64_t ret[NR_CPUS];

	for (int i = 1; i < NR_CPUS; i++)
		ret[i] = smc(PSCI_CPU_ON, (uint64_t)i << 8, (uintptr_t)secondary_entry, i);
	delay_ms(500);
	for (int i = 1; i < NR_CPUS; i++) {
		char label[] = "CPU0";
		label[3] = '0' + i;
		if (((uint64_t)i << 8) == self)
			con_str(label, "is boot cpu");
		else
			con_hex2(label, "ret=", (uint32_t)ret[i], "mpidr=",
				 (uint32_t)sec_mpidr[i] | (sec_mpidr[i] ? 0 : 0xdead0000));
	}
}

/* ---- GIC + timer ---- */

static uintptr_t find_gicr(void)
{
	uint64_t aff = rsr("mpidr_el1");
	uint32_t want = (aff & 0xffffff) | ((aff >> 8) & 0xff000000);

	for (uintptr_t off = 0; off < GICR_SIZE; off += GICR_PE_SIZE) {
		uint64_t typer = rd64(GICR_BASE + off + GICR_TYPER);
		if ((uint32_t)(typer >> 32) == want)
			return GICR_BASE + off;
		if (typer & (1 << 4))	/* Last */
			break;
	}
	return 0;
}

static void gicd_wait_rwp(void)
{
	for (int i = 0; i < 1000000 && (rd32(GICD_BASE) & (1U << 31)); i++)
		;
}

/* Run one interrupt source for ~300 ms and report what the GIC saw. */
static void irq_window(const char *label, uintptr_t r, int src)
{
	uint64_t f = rsr("cntfrq_el0");
	uint32_t pend = 0, act = 0, isr = 0, hppi = 0x3ff, ist = 0;

	irq_count = irq_other = 0;
	irq_last = 0x3ff;
	irq_src = src;
	if (src == VTIMER_INTID) {
		wsr("cntv_tval_el0", f / 100);
		wsr("cntv_ctl_el0", 1);
	} else if (src == PTIMER_INTID) {
		wsr("cntp_tval_el0", f / 100);
		wsr("cntp_ctl_el0", 1);
	}
	isb();
	/* sample state with IRQs still masked, after the first period */
	uint64_t t = now() + f / 50;
	while (now() < t)
		;
	if (src == SGI_INTID)
		wsr("icc_sgi1r_el1", sgi_self(SGI_INTID));
	isb();
	ist = src == PTIMER_INTID ? rsr("cntp_ctl_el0") : rsr("cntv_ctl_el0");
	pend = rd32(r + GICR_ISPENDR0);
	act = rd32(r + GICR_ISACTIVER0);
	isr = rsr("isr_el1");
	hppi = rsr("icc_hppir1_el1");

	__asm__ volatile("msr daifclr, #2" ::: "memory");
	t = now() + f * 3 / 10;
	/* SGI: one already sent above, send 9 more 10 ms apart -> expect 10 */
	uint64_t next = now() + f / 100;
	for (int sent = 1; now() < t;)
		if (src == SGI_INTID && sent < 10 && now() >= next) {
			wsr("icc_sgi1r_el1", sgi_self(SGI_INTID));
			isb();
			sent++;
			next += f / 100;
		}
	__asm__ volatile("msr daifset, #2" ::: "memory");
	wsr("cntv_ctl_el0", 0);
	wsr("cntp_ctl_el0", 0);
	isb();

	con_hex2(label, "irqs=", irq_count, "other=", irq_other);
	con_hex2(" pre", "pend=", pend, "act=", act);
	con_hex2(" pre", "isr=", isr, "hppi=", hppi);
	con_hex2(" pre", "tctl=", ist, "last=", irq_last);
}

static void test_gic(void)
{
	stage = "gic sre";
	uint64_t sre = rsr("icc_sre_el1");
	if (!(sre & 1)) {
		wsr("icc_sre_el1", sre | 1);
		isb();
	}
	if (!(rsr("icc_sre_el1") & 1)) {
		con_str("GIC", "no sysreg iface, skip");
		return;
	}

	stage = "gic dist";
	uint32_t dctlr = rd32(GICD_BASE);
	/* Non-secure view (DS=0): bit0 EnableGrp1, bit1 EnableGrp1A, bit4 ARE_NS; Linux writes 0x13 */
	wr32(GICD_BASE, dctlr | 0x13);
	gicd_wait_rwp();
	con_hex2("GICD", "was=", dctlr, "now=", rd32(GICD_BASE));

	stage = "gic redist";
	uintptr_t r = find_gicr();
	if (!r) {
		con_str("GICR", "not found");
		return;
	}
	uint32_t waker = rd32(r + GICR_WAKER);
	if (waker & 2) {
		wr32(r + GICR_WAKER, waker & ~2U);
		for (int i = 0; i < 100000 && (rd32(r + GICR_WAKER) & 4); i++)
			;
	}
	con_hex2("GICR", "base=", r, "waker=", rd32(r + GICR_WAKER));

	stage = "gic config";
	uint32_t grp = rd32(r + GICR_IGROUPR0);
	wr32(r + GICR_IGROUPR0, 0xffffffff);	/* NS view: secure bits are WI */
	uint32_t en_before = rd32(r + GICR_ISENABLER0);
	uint32_t pri_before = rd32(r + GICR_IPRIORITYR + 16);	/* INTIDs 16..19 */
	for (int id = 0; id < 32; id++)
		wr8(r + GICR_IPRIORITYR + id, 0xa0);
	wr32(r + GICR_ISENABLER0, (1U << VTIMER_INTID) | (1U << PTIMER_INTID) | (1U << SGI_INTID));
	con_hex2("ENABLE", "was=", en_before, "now=", rd32(r + GICR_ISENABLER0));
	(void)grp;
	(void)pri_before;

	uint64_t ctlr = rsr("icc_ctlr_el1");
	wsr("icc_ctlr_el1", ctlr & ~2ULL);	/* EOImode 0: EOIR also deactivates */
	wsr("icc_pmr_el1", 0xf0);
	wsr("icc_bpr1_el1", 0);
	wsr("icc_igrpen1_el1", 1);
	isb();
	con_hex2("ICC", "ctlr=", ctlr, "grpen1=", rsr("icc_igrpen1_el1"));

	stage = "gic vtimer";
	irq_window("VTIMER", r, VTIMER_INTID);
	stage = "gic sgi";
	irq_window("SGI1", r, SGI_INTID);
	wr32(r + GICR_ICENABLER0, 0xffffffff);
	isb();
}

#ifdef HOTDOGB_KEYS
/*
 * hotdogb test-keys: read-only SPMI observer reads (as sweet patch 0060), no PMIC writes.
 * Live DT: arbiter core 0xc440000, obsrvr 0xe600000, cnfg 0xc40a000; power = PM8150 PON
 * KPDPWR (PPID 0x008, RT_STS 0x0810 bit0, RESIN bit1); vol+ = PM8150 GPIO6 (PPID 0x0c5,
 * 0xc510 bit0, active low); vol- = PM8150 GPIO7 (PPID 0x0c6, 0xc610 bit0, active low).
 */
#define SPMI_CORE	0x0c440000UL
#define SPMI_OBS	0x0e600000UL
#define SPMI_CNFG	0x0c40a000UL
#define NO_APID		0xffffU

static uint32_t spmi_status;

static uint16_t spmi_apid(uint16_t ppid, uint32_t count)
{
	uint16_t found = NO_APID;
	for (uint32_t i = 0; i < count; i++) {
		uint32_t map = rd32(SPMI_CORE + 0x900 + 4 * i);
		if (map == 0 || ((map >> 8) & 0xfff) != ppid)
			continue;
		if (found == NO_APID || (rd32(SPMI_CNFG + 0x700 + 4 * i) & 7) == 0)
			found = i;
	}
	return found;
}

static int spmi_read(uint16_t apid, uint16_t addr, uint8_t *out)
{
	if (apid == NO_APID)
		return 0;
	uintptr_t ch = SPMI_OBS + 0x80UL * apid;
	__asm__ volatile("dmb oshst" ::: "memory");
	wr32(ch, (1U << 27) | ((uint32_t)(addr & 0xff) << 4));	/* EXT_READL, 1 byte */
	__asm__ volatile("dsb osh" ::: "memory");
	uint64_t end = now() + rsr("cntfrq_el0") / 100;		/* 10 ms */
	while (now() < end) {
		spmi_status = rd32(ch + 0x08);
		if (spmi_status & 1) {
			if (spmi_status & 0x0e)
				return 0;
			*out = rd32(ch + 0x18);
			return 1;
		}
	}
	return 0;
}

static void keys_header(uint32_t ver, uint32_t count, const uint16_t *apid)
{
	con_clear(RED);
	con_str("SHIM", "hotdogb test-keys (read-only SPMI)");
	con_hex2("ARB", "ver=", ver, "apids=", count);
	con_hex2("APID", "pon=", apid[0], "gp6=", apid[1]);
	con_hex2("APID", "gp7=", apid[2], 0, 0);
	static const uint16_t base[2] = { 0xc500, 0xc600 };
	for (int g = 0; g < 2; g++) {
		uint8_t st = 0, mode = 0, en = 0;
		int a = spmi_read(apid[g + 1], base[g] + 0x05, &st);
		int b = spmi_read(apid[g + 1], base[g] + 0x40, &mode);
		int c = spmi_read(apid[g + 1], base[g] + 0x46, &en);
		con_hex2(g ? "GPIO7" : "GPIO6", "sub/mode=", (a << 31) | (st << 8) | mode,
			 "en/ok=", (en << 8) | (b << 1) | c);
	}
	con_str("KEYS", "press power / vol+ / vol- (2 min)");
	con_skip();
}

static void test_keys(void)
{
	stage = "keys";
	uint32_t ver = rd32(SPMI_CORE), count = rd32(SPMI_CORE + 4) & 0x7ff;
	uint16_t apid[3] = { NO_APID, NO_APID, NO_APID };
	if (ver >= 0x50000000U && ver < 0x70000000U && count && count <= 512) {
		apid[0] = spmi_apid(0x008, count);
		apid[1] = spmi_apid(0x0c5, count);
		apid[2] = spmi_apid(0x0c6, count);
	}
	keys_header(ver, count, apid);
	uint32_t last = 0xffffffff;
	int events = 0;
	uint64_t end = now() + rsr("cntfrq_el0") * 120;
	while (now() < end) {
		uint8_t pon = 0, g6 = 0, g7 = 0;
		int ok = spmi_read(apid[0], 0x0810, &pon) | spmi_read(apid[1], 0xc510, &g6) << 1 |
			 spmi_read(apid[2], 0xc610, &g7) << 2;
		/* pressed bits: 1 power, 2 vol+, 4 vol-, 8 resin; ok in bits 8-10 */
		uint32_t keys = (pon & 1) | (!(g6 & 1) && (ok & 2)) << 1 | (!(g7 & 1) && (ok & 4)) << 2 |
				((pon >> 1) & 1) << 3 | ok << 8;
		if (keys != last) {
			last = keys;
			if (++events > 18) {
				keys_header(ver, count, apid);
				events = 1;
			}
			con_hex2("KEY", "pwr/v+/v-/rs=", ((keys & 1) << 12) | ((keys >> 1 & 1) << 8) |
				 ((keys >> 2 & 1) << 4) | (keys >> 3 & 1), "raw=", (pon << 16) | (g6 << 8) | g7);
		}
		delay_ms(20);
	}
}
#endif

/* ---- main ---- */

#ifdef HOTDOGB_TOUCH0
/*
 * hotdogb touch power probe: READ ONLY. PM8150 LDO17 (touch AVDD, RPMh ldoa17) over the
 * SPMI observer; TLMM SOUTH pins 54/80 (reset), 55/56 (SDA/SCL qup17), 119 (1.8 V en),
 * 122 (IRQ); GCC QUP wrapper 2 branch clocks; SE17 (0xc80000) only if they are all on
 * (reading an unclocked GENI SE can hang the bus).
 */
#define TLMM_SOUTH	0x03d00000UL
#define GCC_BASE	0x00100000UL
#define SE17_BASE	0x00c80000UL

static void test_touch0(void)
{
	stage = "touch0";
	uint32_t ver = rd32(SPMI_CORE), count = rd32(SPMI_CORE + 4) & 0x7ff;
	con_clear(RED);
	con_str("SHIM", "hotdogb touch power probe (read-only)");
	uint16_t ldo = (ver >= 0x50000000U && count <= 512) ? spmi_apid(0x050, count) : NO_APID;
	uint8_t t = 0, st = 0, sts = 0, vl = 0, vh = 0, en = 0;
	int ok = spmi_read(ldo, 0x5004, &t) | spmi_read(ldo, 0x5005, &st) << 1 |
		 spmi_read(ldo, 0x5008, &sts) << 2 | spmi_read(ldo, 0x5040, &vl) << 3 |
		 spmi_read(ldo, 0x5041, &vh) << 4 | spmi_read(ldo, 0x5046, &en) << 5;
	con_hex2("LDO17", "apid=", ldo, "ok=", ok);
	con_hex2("LDO17", "type/sub=", (t << 8) | st, "sts/en=", (sts << 8) | en);
	con_hex2("LDO17", "mV=", (vh << 8) | vl, 0, 0);
	static const int pins[6] = { 54, 80, 55, 56, 119, 122 };
	for (int i = 0; i < 6; i++) {
		char l[] = "GPIO000";
		l[4] = '0' + pins[i] / 100; l[5] = '0' + pins[i] / 10 % 10; l[6] = '0' + pins[i] % 10;
		con_hex2(l, "cfg=", rd32(TLMM_SOUTH + pins[i] * 0x1000UL), "io=",
			 rd32(TLMM_SOUTH + pins[i] * 0x1000UL + 4));
	}
	con_hex2("GCC", "vote52014=", rd32(GCC_BASE + 0x52014), "vote5200c=", rd32(GCC_BASE + 0x5200c));
	static const uint32_t cbcr[5] = { 0x1e004, 0x1e008, 0x1e00c, 0x1e010, 0x1e144 };
	int all_on = 1;
	for (int i = 0; i < 5; i++) {
		uint32_t v = rd32(GCC_BASE + cbcr[i]);
		if (v >> 31)
			all_on = 0;
		con_hex2("CBCR", "reg=", cbcr[i], "val=", v);
	}
	if (all_on) {
		stage = "se17";
		con_hex2("SE17", "fw=", rd32(SE17_BASE + 0x68), "status=", rd32(SE17_BASE + 0x40));
		con_hex2("SE17", "clksel=", rd32(SE17_BASE + 0x7c), "mclk=", rd32(SE17_BASE + 0x48));
	} else {
		con_str("SE17", "clock off: not read");
	}
	con_skip();
	con_str("TOUCH0", "done, reset in ~2 min");
	delay_ms(120000);
}
#endif

static void __attribute__((unused)) psci_line(const char *label, uint64_t fn)
{
	con_hex2(label, "feat=", smc(PSCI_FEATURES, fn, 0, 0), 0, 0);
}

static uint64_t scratch[8192];	/* 64KB, lives in our (later cacheable) image */

static uint64_t ticks_fill(uint32_t c)
{
	uint64_t t = now();
	con_clear(c);
	return now() - t;
}

static uint64_t ticks_mem(void)
{
	uint64_t t = now();
	for (int pass = 0; pass < 16; pass++)
		for (int i = 0; i < 8192; i++)
			scratch[i] = scratch[i] * 3 + i;
	return now() - t;
}

static uint32_t to_us(uint64_t ticks)
{
	return ticks * 10 / 192;	/* 19.2 MHz */
}

void shim_main(uint64_t dtb)
{
	struct mmu_info mi;

	/*
	 * hotdogb: ABL may leave the apps watchdog armed (the V60 ABL does: reset
	 * after ~1 min). Disable it first, as Linux does on suspend.
	 */
	wr32(WDT_BASE + 0x08, 0);
	__asm__ volatile("dsb sy" ::: "memory");

#ifdef HOTDOGB_FBTEST
	/*
	 * hotdogb test1: does writing the cont_splash framebuffer change what the
	 * command-mode panel shows? Red, green, blue, 3 s each. If the screen
	 * never changes, the panel needs an MDP/DSI kick per frame.
	 */
	stage = "fbtest";
	con_clear(RED);
	delay_ms(3000);
	con_clear(GREEN);
	delay_ms(3000);
	con_clear(BLUE);
	delay_ms(3000);
#endif

	/* An xnu payload in the ramdisk means: boot it (MMU stays off). */
	stage = "loader";
	con_clear(RED);
	con_str("SHIM", "hotdogb: xnu loader / ufs probe");
	con_skip();
	const char *why = loader_boot(dtb);
	con_str("LOADER", why);
	con_skip();

#ifdef HOTDOGB_TOUCH0
	test_touch0();
	reset(20);
#endif
#ifdef HOTDOGB_KEYS
	test_keys();
	reset(20);
#endif
#ifdef HOTDOGB_SMP
	/* smp test: PSCI CPU_ON for MPIDR 0x100-0x700, MMU off (no cache coherency issues). */
	con_hex2("PSCI", "ver=", smc(PSCI_VERSION, 0, 0, 0), "on=", smc(PSCI_FEATURES, PSCI_CPU_ON, 0, 0));
	test_smp();
	con_hex2("GICR1", "grp0=", sec_probe[0], "grp1=", sec_probe[1]);
	con_hex2("GICR1", "prio=", sec_probe[2], "ok=", sec_probe[3]);
	con_skip();
	con_str("SMP", "done, reset in ~2 min");
	delay_ms(120000);
	reset(20);
#endif
#ifndef HOTDOGB_TEST2
	/* No payload: probe the internal UFS while the MMU is still off. */
	stage = "ufs";
	test_ufs();
	con_skip();
	/* Keep the probe results on screen (later tests clear it); reset after 3 min. */
	con_str("UFS", "probe done, reset in ~3 min");
	delay_ms(180000);
	reset(20);
#endif

	stage = "fill off";
	uint64_t fill_off = ticks_fill(RED);
	uint64_t mem_off = ticks_mem();

	stage = "mmu enable";
	int rc = mmu_enable(&mi);

	stage = "fill on";
	uint64_t fill_on = rc ? 0 : ticks_fill(RED);
	uint64_t mem_on = rc ? 0 : ticks_mem();
	if (rc)
		con_clear(RED);

	con_str("SHIM", "sweet milestone 3");
	con_skip();
	con_hex2("DTB", "at=", dtb, "magic=", dtb ? __builtin_bswap32(rd32(dtb)) : 0);
	con_hex2("IMAGE", "lo=", mi.img_lo, "hi=", mi.img_hi);
	con_hex2("MMU", "rc=", rc, "l3=", mi.n_l3);
	con_hex2("TTBR0", "pa=", mi.ttbr, "pa_hi=", mi.ttbr >> 32);
	con_hex("SCTLR0", mi.sctlr_before);
	con_hex("SCTLR1", mi.sctlr_after);
	con_hex("TCR", rsr("tcr_el1"));
	con_hex2("PARANGE", "mmfr0=", rsr("id_aa64mmfr0_el1") & 0xf, "tg16=", (rsr("id_aa64mmfr0_el1") >> 20) & 0xf);
	con_skip();
	con_hex("PAR img", mmu_probe((uintptr_t)shim_main));
	con_hex("PAR fb", mmu_probe(FB_PHYS + 0x1000));
	con_hex("PAR gic", mmu_probe(GICD_BASE));
	con_hex("PAR 12G", mmu_probe(0x300000000UL));
	con_skip();
	con_hex2("FILL us", "off=", to_us(fill_off), "on=", to_us(fill_on));
	con_hex2("MEM us", "off=", to_us(mem_off), "on=", to_us(mem_on));
	con_skip();

	stage = "psci";
	con_hex2("PSCI", "ver=", smc(PSCI_VERSION, 0, 0, 0), "smccc=", smc(SMCCC_VERSION, 0, 0, 0));
	con_skip();
	test_gic();

	stage = "done";
#ifdef HOTDOGB_TEST2
	delay_ms(80000);	/* time to photograph */
#endif
	reset(40);
}
