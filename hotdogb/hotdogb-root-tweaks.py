#!/usr/bin/env python3
"""OnePlus 7T additions to tools/sweet-root-tweaks.py (run after it).

  hotdogb-root-tweaks.py ROOT          ROOT = staging dir or a launchd.plist path

launchd.plist: remove the jobs that crash-loop on the 7T (first UFS boot log,
logs/boot-ufs2.txt: 125 relaunches each in ~25 min, 501 xpcproxy execs):
  com.apple.bluetoothd    no Bluetooth hardware/driver for iOS
  com.apple.fairplayd.H2  no Secure Enclave
Same mechanism sweet uses for CommCenter. Idempotent.
"""
import os
import plistlib
import sys

REMOVE = ('com.apple.bluetoothd', 'com.apple.fairplayd.H2')


def launchd(path):
    st = os.stat(path)
    with open(path, 'rb') as f:
        plist = plistlib.load(f)
    jobs = plist['LaunchDaemons']
    removed = [k for k, j in jobs.items() if j.get('Label') in REMOVE]
    for k in removed:
        del jobs[k]
    with open(path, 'wb') as f:
        plistlib.dump(plist, f, fmt=plistlib.FMT_BINARY)
    os.chown(path, st.st_uid, st.st_gid)
    os.chmod(path, st.st_mode & 0o7777)
    print(f'launchd.plist: removed {len(removed)} jobs {sorted(j.split("/")[-1] for j in removed)}, {len(jobs)} left')


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    p = sys.argv[1]
    launchd(p if p.endswith('.plist') else os.path.join(p, 'System/Library/xpc/launchd.plist'))


if __name__ == '__main__':
    main()
