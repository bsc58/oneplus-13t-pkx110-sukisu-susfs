#!/usr/bin/env python3
"""Compare two kernel .config files (plain or .gz): config_diff.py <stock> <new> [--all]

Prints options whose value differs, options only set in one of them, and a short
summary. Options that only exist because the kernel versions differ are expected;
the point is to make every difference visible so nothing changes unnoticed.
"""
import gzip
import re
import sys


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    cfg = {}
    with opener(path, "rt", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(CONFIG_\w+)=(.*)$", line.rstrip("\n"))
            if m:
                cfg[m.group(1)] = m.group(2)
    return cfg


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    a, b = load(argv[1]), load(argv[2])
    show_all = "--all" in argv
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    only_a = sorted(k for k in a if k not in b)
    only_b = sorted(k for k in b if k not in a)
    print(f"stock: {len(a)} options set   new: {len(b)} options set")
    print(f"same: {sum(1 for k in a if k in b and a[k] == b[k])}   changed: {len(changed)}   "
          f"only in stock: {len(only_a)}   only in new: {len(only_b)}")
    print("\n== value changed (stock -> new)")
    for k in changed:
        print(f"  {k}: {a[k]} -> {b[k]}")
    print("\n== set in stock, not set in new")
    for k in only_a:
        print(f"  {k}={a[k]}")
    print("\n== set in new, not set in stock")
    for k in only_b:
        if show_all or not k.startswith("CONFIG_KSU"):
            print(f"  {k}={b[k]}")
    ksu = [k for k in only_b if k.startswith("CONFIG_KSU")]
    if ksu and not show_all:
        print(f"  (+ {len(ksu)} CONFIG_KSU* options: {' '.join(s[7:] for s in ksu)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
