#!/usr/bin/env python3
"""Offline KMI check: can the phone's existing kernel modules load on a newly built kernel?

With CONFIG_MODVERSIONS the kernel refuses to load a module if any symbol it imports
is missing from the kernel or has a different CRC. This script reproduces that check
offline, using the .ko files copied from the phone and the symvers file of a new build.

  extract  <out.json> <dir-or-tar> [<dir-or-tar> ...]
      Read every .ko, record what it imports (__versions: name + CRC) and exports.

  compare  <modules.json> <vmlinux.symvers|Module.symvers> [--loaded proc_modules.txt]
           [--stock-ksymtab kallsyms_ksymtab.txt] [--report out.txt]
      Report imports the new kernel cannot satisfy (missing symbol or CRC mismatch).
"""
import hashlib
import io
import json
import os
import struct
import sys
import tarfile


def _sections(data):
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not a little-endian ELF64 file")
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    secs = []
    for i in range(shnum):
        name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from(
            "<IIQQQQIIQQ", data, shoff + i * shentsize)
        secs.append({"name_off": name, "type": typ, "off": off, "size": size, "link": link})
    strtab = secs[shstrndx]
    for s in secs:
        end = data.index(b"\0", strtab["off"] + s["name_off"])
        s["name"] = data[strtab["off"] + s["name_off"]:end].decode()
    return secs


def parse_ko(data):
    secs = _sections(data)
    imports, exports, info = {}, {}, {}
    extra = {"import_ns": [], "this_module_size": None}
    und, gpl = {}, set()
    for s in secs:
        if s["name"] == "__versions":
            for o in range(s["off"], s["off"] + s["size"], 64):
                crc = struct.unpack_from("<Q", data, o)[0] & 0xFFFFFFFF
                name = data[o + 8:o + 64].split(b"\0")[0].decode()
                if name:
                    imports[name] = crc
        elif s["name"] == ".modinfo":
            for kv in data[s["off"]:s["off"] + s["size"]].split(b"\0"):
                if b"=" in kv:
                    k, v = kv.split(b"=", 1)
                    if k in (b"name", b"vermagic", b"scmversion", b"license"):
                        info.setdefault(k.decode(), v.decode(errors="replace"))
                    elif k == b"import_ns":
                        extra["import_ns"].append(v.decode(errors="replace"))
        elif s["name"] == ".gnu.linkonce.this_module":
            extra["this_module_size"] = s["size"]   # == sizeof(struct module) of the kernel it was built for
    for s in secs:
        if s["type"] != 2:  # SHT_SYMTAB
            continue
        st = secs[s["link"]]
        crcs, ksyms = {}, {}
        for o in range(s["off"], s["off"] + s["size"], 24):
            n_off, st_info, _other, shndx, value, _size = struct.unpack_from("<IBBHQQ", data, o)
            end = data.index(b"\0", st["off"] + n_off)
            name = data[st["off"] + n_off:end]
            if shndx == 0 and name:
                und[name.decode()] = st_info >> 4   # symbol binding: 1 = global, 2 = weak
            elif name.startswith(b"__crc_"):
                if shndx == 0xFFF1:
                    crc = value & 0xFFFFFFFF
                elif 0 < shndx < len(secs):
                    crc = struct.unpack_from("<I", data, secs[shndx]["off"] + value)[0]
                else:
                    continue
                crcs[name[6:].decode()] = crc
            elif name.startswith(b"__ksymtab_") and not name.startswith(b"__ksymtab_strings"):
                ksyms[name[10:].decode()] = secs[shndx]["name"] if 0 < shndx < len(secs) else ""
        for k, sec in ksyms.items():
            exports[k] = crcs.get(k)
            if "gpl" in sec:   # EXPORT_SYMBOL_GPL entries live in the __ksymtab_gpl section
                gpl.add(k)
    # Symbols the loader must resolve although they carry no __versions entry (weak ones may stay unresolved).
    extra["und_noversion"] = sorted(n for n, b in und.items() if b == 1 and n not in imports)
    extra["exports_gpl"] = sorted(gpl)
    extra["signed"] = data.endswith(b"~Module signature appended~\n")
    extra["sha256"] = hashlib.sha256(data).hexdigest()
    return {"imports": imports, "exports": exports, **info, **extra}


def iter_ko(path):
    if os.path.isdir(path):
        for root, _d, files in os.walk(path):
            for f in files:
                if f.endswith(".ko"):
                    p = os.path.join(root, f)
                    with open(p, "rb") as fh:
                        yield p.replace("\\", "/"), fh.read()
    else:
        with tarfile.open(path) as tf:
            for m in tf:
                if m.isfile() and m.name.endswith(".ko"):
                    yield m.name, tf.extractfile(m).read()


def cmd_extract(out, sources):
    mods, files = {}, 0
    for src in sources:
        for name, data in iter_ko(src):
            try:
                rec = parse_ko(data)
            except Exception as e:  # keep going, but say so
                print(f"WARN: cannot parse {name}: {e}", file=sys.stderr)
                continue
            rec["path"] = name
            key = (rec.get("name") or os.path.basename(name)[:-3]).replace("-", "_")
            prev = mods.get(key)   # the same module often sits in several partitions; remember if copies differ
            rec["copies"] = (prev["copies"] if prev else 0) + 1
            rec["copies_differ"] = bool(prev) and (prev["copies_differ"] or prev["sha256"] != rec["sha256"])
            mods[key] = rec
            files += 1
    with open(out, "w") as fh:
        json.dump(mods, fh)
    n_imp = len({s for m in mods.values() for s in m["imports"]})
    print(f"{files} .ko files, {len(mods)} distinct modules, {n_imp} distinct imported symbols, "
          f"{sum(1 for m in mods.values() if m['copies_differ'])} names with differing copies -> {out}")


def cmd_compare(mod_json, symvers, loaded=None, stock_ksymtab=None, report=None):
    mods = json.load(open(mod_json))
    kernel = {}
    for line in open(symvers):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 3 and p[2] == "vmlinux":
            kernel[p[1]] = int(p[0], 16)
    mod_exports = {}
    for name, m in mods.items():
        for s, crc in m["exports"].items():
            mod_exports[s] = (name, crc)
    loaded_set = None
    if loaded:
        loaded_set = {l.split()[0].replace("-", "_") for l in open(loaded) if l.strip()}
    stock = None
    if stock_ksymtab:
        stock = set()
        for l in open(stock_ksymtab, errors="replace"):
            p = l.split()
            if len(p) == 3 and p[2].startswith("__ksymtab_"):
                stock.add(p[2][10:])

    out = io.StringIO()
    bad_mods, missing, mismatch = {}, {}, {}
    for name, m in sorted(mods.items()):
        for s, crc in m["imports"].items():
            if s == "module_layout" and s not in kernel:
                continue
            if s in kernel:
                if kernel[s] != crc:
                    mismatch.setdefault(s, (crc, kernel[s], []))[2].append(name)
                    bad_mods.setdefault(name, []).append(f"CRC {s}")
            elif s in mod_exports:
                pass  # provided by another module on the phone
            else:
                missing.setdefault(s, []).append(name)
                bad_mods.setdefault(name, []).append(f"MISSING {s}")

    total = len(mods)
    print(f"kernel exports (vmlinux): {len(kernel)}", file=out)
    print(f"modules checked: {total}", file=out)
    if stock is not None:
        gone = sorted(stock - set(kernel))
        print(f"stock vmlinux exports: {len(stock)}; absent from new kernel: {len(gone)}", file=out)
        used = {s for m in mods.values() for s in m["imports"]}
        gone_used = [s for s in gone if s in used]
        print(f"  ...of which actually imported by a phone module: {len(gone_used)}", file=out)
    print(f"symbols with CRC mismatch: {len(mismatch)}", file=out)
    print(f"symbols missing: {len(missing)}", file=out)
    print(f"modules that would FAIL to load: {len(bad_mods)} / {total}", file=out)
    if loaded_set is not None:
        bl = sorted(n for n in bad_mods if n in loaded_set)
        print(f"  ...of which currently loaded on the phone: {len(bl)}", file=out)
    print("", file=out)
    for s, (want, have, users) in sorted(mismatch.items(), key=lambda kv: -len(kv[1][2])):
        print(f"CRC   {s}: modules want 0x{want:08x}, kernel has 0x{have:08x} ({len(users)} modules, e.g. {users[0]})", file=out)
    for s, users in sorted(missing.items(), key=lambda kv: -len(kv[1])):
        print(f"MISS  {s}: needed by {len(users)} modules, e.g. {', '.join(users[:4])}", file=out)
    print("", file=out)
    for name, why in sorted(bad_mods.items()):
        tag = ""
        if loaded_set is not None:
            tag = " [loaded]" if name in loaded_set else " [not loaded]"
        print(f"FAIL  {name}{tag}: {len(why)} problem(s): {', '.join(why[:5])}", file=out)
    text = out.getvalue()
    if report:
        with open(report, "w") as fh:
            fh.write(text)
    head = text.split("\n\n", 1)[0]
    print(head)
    return 0 if not bad_mods else 1


def main(argv):
    if len(argv) >= 4 and argv[1] == "extract":
        cmd_extract(argv[2], argv[3:])
        return 0
    if len(argv) >= 4 and argv[1] == "compare":
        opts = {"loaded": None, "stock_ksymtab": None, "report": None}
        rest = argv[4:]
        i = 0
        while i < len(rest):
            key = rest[i].lstrip("-").replace("-", "_")
            if key not in opts or i + 1 >= len(rest):
                print(__doc__)
                return 2
            opts[key] = rest[i + 1]
            i += 2
        return cmd_compare(argv[2], argv[3], **opts)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
