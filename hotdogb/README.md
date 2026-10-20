# OnePlus 7T (hotdogb, SM8150) port of ios-sweet

Branch `hotdogb`. The project diary (values, sources, open questions) lives outside
this repo in `PORTING_NOTES.md` / `STATUS.md`.

| File | What |
| --- | --- |
| `apply-hotdogb-port.py` | Re-applies the config-only changes (IMEM, watchdog, FB size, CPU clocks) to a fresh ios-sweet checkout. Idempotent. |
| `mkxnu-boot.sh` | CI kernel + shim + stock boot.img → RAM-bootable XNU image with safe boot-args (no UFS writes). |
| `mkhfs-test.sh` | 8 MB HFS+ RAM root with sweet-init (libguestfs in Docker, WSL has no hfsplus). Boot with `RAMDISK=` in mkxnu-boot.sh. |
| `pack-boot.py` | Packs a shim `Image` into a boot.img v2 using the header + DTB of the phone's stock `boot.img`. Pure Python. |

## Shim builds (CI `shim.yml`, or locally)

    NO_BOOTIMG=1 EXTRA_CFLAGS=-DHOTDOGB_FBTEST shim/build.sh   # test1: RGB colour test, then read-only UFS/GPT probe
    NO_BOOTIMG=1 shim/build.sh                                   # loader for an xnu payload

    python3 hotdogb/pack-boot.py --stock backup/boot_a.img --kernel shim/out/Image --out shim-test1.img
    fastboot boot shim-test1.img        # RAM only. Never `fastboot flash`.

Without a payload the shim: disables the apps watchdog, (test1) shows red/green/blue for 3 s each,
probes UFS read-only (no SCSI WRITE exists in `shim/ufs.c`), keeps the result on screen ~3 min,
then reboots to the bootloader.

## Not changed yet
- RAM given to XNU (`tools/mkadt.py` PHYS_BASE/MEM_SIZE/HIGH_*): needs the live DT.
- Keys, touch (Synaptics S3706), USB: new drivers, boot with `sweettouch=0 sweetkeys=0 sweetusb=0`.
