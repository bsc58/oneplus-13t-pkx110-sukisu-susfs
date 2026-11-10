#!/usr/bin/env python3
"""Consistency check for the static keys (jump labels) of a linked arm64 vmlinux.

Why this exists: KernelSU/SUSFS code is glued together from two source trees that only agree
through hand-written `extern` declarations. If one side declares `extern struct static_key_true x`
and tests it with static_branch_likely(), while the other side defines `bool x`, nothing fails at
build time (no LTO, separate translation units). At boot jump_label_init() then treats a 1-byte
bool as a 16-byte static key and writes a pointer over the neighbouring variables.

The linked kernel shows such mistakes. Every __jump_table entry carries the address of the code
site and of its key (low two bits of the key are flags). Two things are checked:

 1. placement - the key address is 8-byte aligned and lies completely inside one data object of
    at least 16 bytes (sizeof(struct static_key)). This catches a key that is really a bool/int.
 2. initial state - the instruction the compiler put at the code site (NOP or B) must be the one
    the kernel expects for the key's initial value: jump = enabled XOR branch-flag, exactly the
    rule of jump_label_type(). This catches a key declared as static_key_true but defined FALSE
    (or the reverse), and a key address that decodes to something else.

Limits: this cannot prove that an object of 16 bytes or more that passes both tests was meant
to be a static key, and it only looks at vmlinux, not at loadable modules.

  statickeys.py <vmlinux> [name-of-key-that-must-exist ...]
Exit status 1 if anything is inconsistent.
"""
import bisect
import mmap
import struct
import sys

KEY_SIZE = 16            # sizeof(struct static_key) on arm64: atomic_t + padding + pointer
NOP = 0xD503201F


def elf_symbols(mm):
    if mm[:4] != b"\x7fELF" or mm[4] != 2 or mm[5] != 1:
        raise ValueError("not a little-endian ELF64 file")
    shoff = struct.unpack_from("<Q", mm, 0x28)[0]
    shentsize, shnum, _shstrndx = struct.unpack_from("<HHH", mm, 0x3A)
    # (name, type, flags, addr, offset, size, link, info, addralign, entsize)
    secs = [struct.unpack_from("<IIQQQQIIQQ", mm, shoff + i * shentsize) for i in range(shnum)]
    symtab = next((s for s in secs if s[1] == 2), None)
    if symtab is None:
        raise ValueError("no .symtab (stripped vmlinux?)")
    strtab = secs[symtab[6]]
    strs = mm[strtab[4]:strtab[4] + strtab[5]]
    syms = []
    for n_off, info, _other, shndx, value, size in struct.iter_unpack("<IBBHQQ", mm[symtab[4]:symtab[4] + symtab[5]]):
        if not n_off:
            continue
        name = strs[n_off:strs.index(b"\0", n_off)].decode(errors="replace")
        syms.append((value, size, name, info & 0xF, shndx))
    return secs, syms


def analyse(path, must_exist=()):
    """Returns (entries, misplaced, wrong_state, per-required-key info)."""
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        secs, syms = elf_symbols(mm)
        alloc = sorted((s[3], s[5], s[4], s[1]) for s in secs if s[3] and s[5] and (s[2] & 2))   # SHF_ALLOC
        alloc_addr = [s[0] for s in alloc]

        def read_u32(addr):
            i = bisect.bisect_right(alloc_addr, addr) - 1
            if i < 0:
                return None
            a0, size, off, typ = alloc[i]
            if not (a0 <= addr and addr + 4 <= a0 + size):
                return None
            return 0 if typ == 8 else struct.unpack_from("<I", mm, off + addr - a0)[0]   # .bss reads as zero

        by_name = {s[2]: s[0] for s in syms}
        start, stop = by_name["__start___jump_table"], by_name["__stop___jump_table"]
        sec = next(s for s in secs if s[1] != 8 and s[3] <= start < s[3] + s[5])
        off = sec[4] + start - sec[3]
        ents = []
        for i in range((stop - start) // 16):   # struct jump_entry { s32 code; s32 target; long key; } - all relative
            ea = start + i * 16
            code, _target, key = struct.unpack_from("<iiq", mm, off + i * 16)
            raw = (ea + 8 + key) & 0xFFFFFFFFFFFFFFFF
            e = {"code": (ea + code) & 0xFFFFFFFFFFFFFFFF, "key": raw & ~3, "branch": raw & 1, "raw": raw}
            e["insn"], e["enabled"] = read_u32(e["code"]), read_u32(e["key"])
            ents.append(e)
        mm.close()

    objs = sorted((v, sz, n) for v, sz, n, typ, shndx in syms if typ == 1 and sz and shndx)   # STT_OBJECT
    obj_addr = [o[0] for o in objs]
    funcs = sorted((v, sz, n) for v, sz, n, typ, shndx in syms if typ == 2 and shndx)         # STT_FUNC
    func_addr = [f[0] for f in funcs]

    def container(addr):
        i = bisect.bisect_right(obj_addr, addr) - 1
        for j in range(i, max(i - 16, -1), -1):   # a few steps back: aliases / nested objects
            v, sz, _n = objs[j]
            if v <= addr and addr + KEY_SIZE <= v + sz:
                return objs[j]
        return None

    def nearest(addr):
        i = bisect.bisect_right(obj_addr, addr) - 1
        return objs[i] if i >= 0 else None

    def where(addr):
        i = bisect.bisect_right(func_addr, addr) - 1
        return f"{funcs[i][2]}+0x{addr - funcs[i][0]:x}" if i >= 0 else hex(addr)

    misplaced, wrong_state, seen = [], [], {}
    for e in ents:
        e["site"] = where(e["code"])
        if e["key"] not in seen:
            c = container(e["key"])
            seen[e["key"]] = c if (c and e["key"] % 8 == 0) else None
        near = nearest(e["key"])
        e["lands_in"] = f"{near[2]} (size {near[1]})" if near else "?"
        if seen[e["key"]] is None:
            misplaced.append(e)
        if e["insn"] is None or e["enabled"] is None:
            wrong_state.append({**e, "why": "code site or key not readable"})
        else:
            want_jump = bool(e["enabled"]) ^ bool(e["branch"])        # jump_label_type()
            is_jump = (e["insn"] >> 26) == 0x05                        # unconditional B
            if (want_jump and not is_jump) or (not want_jump and e["insn"] != NOP):
                wrong_state.append({**e, "why": f"key value {e['enabled']}, branch flag {e['branch']}: kernel expects "
                                                f"{'B' if want_jump else 'NOP'}, code has 0x{e['insn']:08x}"})
    required = {}
    for name in must_exist:
        hit = [(v, sz) for v, sz, n in objs if n == name]
        if not hit:
            required[name] = None
            continue
        v, sz = hit[0]
        uses = [e for e in ents if e["key"] == v]
        required[name] = {"size": sz, "uses": [e["site"] for e in uses]}
    return ents, misplaced, wrong_state, required


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    ents, misplaced, wrong_state, required = analyse(argv[1], argv[2:])
    print(f"{len(ents)} jump-table entries, {len({e['key'] for e in ents})} distinct keys; "
          f"{len(misplaced)} not placed in a real key, {len(wrong_state)} with a wrong initial instruction")
    for e in misplaced:
        print(f"  MISPLACED {e['site']}: key 0x{e['key']:x} (raw 0x{e['raw']:x}) lands in {e['lands_in']}")
    for e in wrong_state[:20]:
        print(f"  WRONG STATE {e['site']} (key in {e['lands_in']}): {e['why']}")
    bad = bool(misplaced or wrong_state)
    for name, r in required.items():
        if r is None:
            print(f"  MISSING data symbol {name}")
            bad = True
        else:
            print(f"  {name}: size {r['size']}, used at {', '.join(r['uses']) or 'nowhere'}")
            bad = bad or r["size"] != KEY_SIZE or not r["uses"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
