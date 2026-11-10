#!/usr/bin/env python3
"""Offline verification of a freshly built kernel against facts read from the phone.

Nothing here talks to a device. Every check prints PASS / FAIL / INFO; the exit status is
non-zero if any check FAILs. A check that cannot be carried out (missing input, missing tool)
counts as a FAIL - nothing is skipped quietly.

  verify_build.py --dist <dir with Image, kernel.config, vmlinux.symvers, System.map,
                          gki_module_unprotected.h, gki_module_protected_exports.h>
                  --device <dir with modules_all.json, proc_modules.txt, kallsyms_names.txt,
                            module_facts.json, kmi/, system_dlkm_find_order.txt,
                            system_dlkm_blocklist.txt, vendor_dlkm_modules_load.txt,
                            vendor_dlkm_modules_blocklist.txt>
                  --vmlinux <vmlinux with symbols and BTF>
                  --stock-boot stock/boot_b.img --boot out/boot.img
                  [--init-boot-stock stock/init_boot_a.img --init-boot-current stock/init_boot_b.img]
                  [--report out/VERIFY_REPORT.txt]
"""
import argparse
import glob
import importlib.util
import json
import mmap
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True   # do not leave __pycache__ next to the tools
# include/linux/license.h: license_is_gpl_compatible()
GPL_OK = {"GPL", "GPL v2", "GPL and additional rights", "Dual BSD/GPL", "Dual MIT/GPL", "Dual MPL/GPL"}
# static keys shared between the SUSFS hooks in the core kernel and the KernelSU driver
KSU_STATIC_KEYS = ("ksu_su_compat_enabled", "ksu_is_init_rc_hook_enabled", "ksu_is_input_hook_enabled",
                   "is_first_zygote", "susfs_is_sdcard_android_data_not_decrypted")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Report:
    def __init__(self):
        self.lines, self.fails = [], 0

    def add(self, status, text):
        if status == "FAIL":
            self.fails += 1
        line = f"[{status}] {text}"
        self.lines.append(line)
        print(line)

    def check(self, ok, text, detail=""):
        self.add("PASS" if ok else "FAIL", text + (f" ({detail})" if detail else ""))
        return ok

    def section(self, title):
        self.lines.append("")
        self.lines.append(f"== {title}")
        print(f"\n== {title}")


def read_config(path):
    cfg = {}
    for line in open(path, errors="replace"):
        m = re.match(r"^(CONFIG_\w+)=(.*)$", line.rstrip("\n"))
        if m:
            cfg[m.group(1)] = m.group(2)
    return cfg


def some(items, n=8):
    items = sorted(items)
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--vmlinux", required=True)
    ap.add_argument("--stock-boot", required=True)
    ap.add_argument("--boot", required=True)
    ap.add_argument("--init-boot-stock", required=True)
    ap.add_argument("--init-boot-current", required=True)
    ap.add_argument("--report")
    a = ap.parse_args()
    rep = Report()
    try:
        run_checks(a, rep)
    except Exception as e:   # a crash must end in a failing report, never leave an older passing one behind
        rep.add("FAIL", f"verification aborted: {type(e).__name__}: {e}")
    rep.section("Result")
    rep.add("INFO" if rep.fails == 0 else "FAIL",
            "all checks passed" if rep.fails == 0 else f"{rep.fails} check(s) failed")
    if a.report:
        with open(a.report, "w") as fh:
            fh.write("\n".join(rep.lines).lstrip("\n") + "\n")
    return 1 if rep.fails else 0


def run_checks(a, rep):
    bootimg = load_tool("bootimg")
    statickeys = load_tool("statickeys")

    image = open(os.path.join(a.dist, "Image"), "rb").read()
    cfg = read_config(os.path.join(a.dist, "kernel.config"))
    facts = json.load(open(os.path.join(a.device, "module_facts.json")))
    mods = json.load(open(os.path.join(a.device, "modules_all.json")))
    loaded = {l.split()[0].replace("-", "_") for l in open(os.path.join(a.device, "proc_modules.txt")) if l.strip()}

    # ------------------------------------------------------------------ Image
    rep.section("Kernel Image")
    rep.check(image[56:60] == b"ARMd", "arm64 Image magic present")
    flags = struct.unpack_from("<Q", image, 24)[0]
    page = {1: "4K", 2: "16K", 3: "64K"}.get((flags >> 1) & 3, "unspecified")
    rep.check(page == "4K", "Image header declares 4K pages", f"flags=0x{flags:x} -> {page}")
    m = re.search(rb"Linux version (\S+) \(([^)]*)\) \(([^\n]{0,140})", image)
    release = m.group(1).decode() if m else "?"
    rep.check(bool(re.match(r"^6\.6\.\d+-android15-8-.*-4k$", release)),
              "kernel release has the android15-6.6 KMI form and the -4k suffix", release)
    rep.add("INFO", "compiler: " + (m.group(3).decode(errors="replace") if m else "?"))
    # vmlinux (used below for struct sizes and static keys) must be the very build the Image came from:
    # the full banner carries the build time stamp.
    banner = re.search(rb"Linux version [^\n]{20,400}\n", image)
    with open(a.vmlinux, "rb") as fh:
        vmm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        same = bool(banner) and vmm.find(banner.group(0)) != -1
        vmm.close()
    rep.check(same, "vmlinux and Image are from the same build (identical version banner incl. build time)",
              banner.group(0).decode(errors="replace").strip()[-40:] if banner else "no banner in Image")
    vm = re.search(rb"6\.6\.\d+\S* (SMP [a-z_ ]*aarch64)", image)
    vflags = vm.group(1).decode() if vm else "?"
    mod_flags = {m_["vermagic"].split(" ", 1)[1] for m_ in mods.values() if " " in m_.get("vermagic", "")}
    rep.check(mod_flags == {vflags}, f"vermagic flags of all {len(mods)} stock modules equal the kernel's",
              f"kernel '{vflags}' vs modules {sorted(mod_flags)}")

    # ------------------------------------------------------------------ config
    rep.section("Kernel configuration")
    want = {"CONFIG_ARM64_4K_PAGES": "y", "CONFIG_KSU": "y", "CONFIG_KSU_SUSFS": "y", "CONFIG_HMBIRD_SCHED": "y",
            "CONFIG_MODVERSIONS": "y", "CONFIG_MODULE_SIG": "y", "CONFIG_MODULE_SIG_PROTECT": "y",
            "CONFIG_LTO_NONE": "y", "CONFIG_CFI_CLANG": "y", "CONFIG_TMPFS_XATTR": "y",
            "CONFIG_TMPFS_POSIX_ACL": "y", "CONFIG_ANDROID_KABI_RESERVE": "y", "CONFIG_IKCONFIG_PROC": "y",
            "CONFIG_ARM64_VA_BITS": "39", "CONFIG_MODULE_ALLOW_BTF_MISMATCH": "y"}
    for k, v in want.items():
        rep.check(cfg.get(k) == v, f"{k}={v}", f"got {cfg.get(k)}")
    for k in ("CONFIG_ARM64_16K_PAGES", "CONFIG_ARM64_64K_PAGES", "CONFIG_MODULE_SIG_FORCE", "CONFIG_KSU_DEBUG",
              "CONFIG_SCHED_CLASS_EXT", "CONFIG_SLIM_SCHED", "CONFIG_MODULE_ALLOW_MISSING_NAMESPACE_IMPORTS"):
        rep.check(k not in cfg, f"{k} is not set", f"got {cfg.get(k)}")
    rep.add("INFO", "SUSFS options: " + " ".join(sorted(k[len("CONFIG_KSU_SUSFS_"):] for k in cfg
                                                        if k.startswith("CONFIG_KSU_SUSFS_"))))
    rep.add("INFO", f"CONFIG_KPM: {cfg.get('CONFIG_KPM', 'not set')}; module names refused by the kernel "
                    f"(CONFIG_DEBLOAT_VENDOR_MODULES): {cfg.get('CONFIG_DEBLOAT_VENDOR_MODULES', 'feature not built')}")

    # ------------------------------------------------------------------ symbol CRCs
    rep.section("Module compatibility (CONFIG_MODVERSIONS)")
    kernel, ktype, kns = {}, {}, {}
    for line in open(os.path.join(a.dist, "vmlinux.symvers")):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 4 and p[2] == "vmlinux":
            kernel[p[1]] = int(p[0], 16)
            ktype[p[1]] = p[3]
            kns[p[1]] = p[4] if len(p) > 4 else ""
    rep.add("INFO", f"new kernel exports {len(kernel)} symbols; phone has {len(mods)} distinct modules "
                    f"({len(loaded & set(mods))} of them loaded)")
    mod_exports = {s for m_ in mods.values() for s in m_["exports"]}

    def needs(m_):   # everything the loader has to resolve for this module
        return [s for s in list(m_["imports"]) + m_.get("und_noversion", []) if s != "module_layout"]

    fact_keys = ("und_noversion", "this_module_size", "license", "import_ns", "copies_differ", "exports_gpl", "vermagic")
    rep.check(len(mods) > 500 and all(all(k in m_ for k in fact_keys) and m_["this_module_size"] for m_ in mods.values()),
              "module facts carry the full set of load-time properties (re-run modcheck.py extract if not)")
    missing, mismatch, bad = {}, {}, {}
    for name, m_ in mods.items():
        for s in needs(m_):
            crc = m_["imports"].get(s)
            if s in kernel:
                if crc is not None and kernel[s] != crc:
                    mismatch.setdefault(s, []).append(name)
                    bad.setdefault(name, []).append(s)
            elif s not in mod_exports:
                missing.setdefault(s, []).append(name)
                bad.setdefault(name, []).append(s)
    needed = {s for m_ in mods.values() for s in needs(m_) if s not in mod_exports}
    rep.check(not missing, f"all {len(needed)} symbols the modules need from vmlinux are exported",
              f"{len(missing)} missing: {some(missing)}" if missing else "")
    rep.check(not mismatch, "every imported symbol has the CRC the modules were built against",
              f"{len(mismatch)} mismatching: {some(mismatch)}" if mismatch else "")
    bad_loaded = sorted(n for n in bad if n in loaded)
    rep.check(not bad, "no stock module would be rejected by the version check",
              f"{len(bad)} modules affected, {len(bad_loaded)} of them loaded today: {some(bad_loaded)}" if bad else "")
    want_layout = int(facts["module_layout_crc"], 16)
    rep.check(kernel.get("module_layout") == want_layout, "module_layout CRC matches (struct module layout)",
              f"kernel 0x{kernel.get('module_layout', 0):08x} vs modules 0x{want_layout:08x}")
    sizes = {m_.get("this_module_size") for m_ in mods.values()}
    try:
        out = subprocess.run(["pahole", "-F", "btf", "-C", "module", a.vmlinux], capture_output=True,
                             text=True, timeout=600).stdout
        sm = re.search(r"/\* size: (\d+),", out)
        size = int(sm.group(1)) if sm else -1
    except Exception as e:   # pahole missing or failed
        size = f"pahole failed: {e}"
    rep.check(sizes == {size}, "sizeof(struct module) equals the this_module size of every stock module",
              f"kernel {size} vs modules {sorted(sizes, key=str)}")

    # ------------------------------------------------------------------ other loader rules
    rep.section("Other load-time rules of the module loader (kernel/module/main.c)")
    differ = [n for n, m_ in mods.items() if m_.get("copies_differ")]
    rep.check(not differ, "copies of the same module in different partitions are identical", some(differ))
    dup = sorted(mod_exports & set(kernel))
    rep.check(not dup, "no stock module exports a symbol that the new vmlinux also exports "
                       "(it exports far more than the trimmed stock kernel)", some(dup))
    nongpl = {n: m_.get("license") for n, m_ in mods.items() if m_.get("license") not in GPL_OK}
    gpl_bad = sorted(n for n in nongpl if any(ktype.get(s, "").endswith("_GPL") for s in needs(mods[n])))
    rep.check(not gpl_bad, "no module with a non-GPL licence needs a GPL-only kernel export",
              f"{len(nongpl)} non-GPL modules; affected: {some(gpl_bad)}" if nongpl else "all licences GPL-compatible")
    ns_bad = sorted({f"{n}:{s}({kns[s]})" for n, m_ in mods.items() for s in needs(m_)
                     if kns.get(s) and kns[s] not in m_.get("import_ns", [])})
    rep.check(not ns_bad, f"every namespaced kernel export in use is imported with MODULE_IMPORT_NS "
                          f"({sum(1 for v in kns.values() if v)} namespaced exports)", some(ns_bad))

    # ------------------------------------------------------------------ unsigned-module rule
    rep.section("GKI unsigned-module rule (a self-built kernel cannot verify stock module signatures)")
    hdr = os.path.join(a.dist, "gki_module_unprotected.h")
    unprot = re.findall(r'^\t"([^"]+)",$', open(hdr).read(), re.M) if os.path.exists(hdr) else []
    rep.check(len(unprot) > 1000 and unprot == sorted(unprot, key=lambda s: s.encode()),
              "the list the kernel enforces (generated gki_module_unprotected.h) is present and sorted for bsearch",
              f"{len(unprot)} symbols")
    unprot = set(unprot)
    kmi = set()
    for f in glob.glob(os.path.join(a.device, "kmi", "abi_gki_aarch64*")):
        if ".stg" in os.path.basename(f):   # the STG ABI dump is not a symbol list
            continue
        for line in open(f, errors="replace"):
            s = line.strip()
            if s and not s.startswith("[") and not s.startswith("#"):
                kmi.add(s)
    refused = {n: [s for s in needs(m_) if s in kernel and s not in unprot] for n, m_ in mods.items()}
    refused = {n: v for n, v in refused.items() if v}
    vendor_refused = sorted(n for n in refused if "system_dlkm" not in mods[n]["path"])
    rep.check(not vendor_refused, "no vendor module imports a kernel symbol outside the unprotected (KMI) list",
              some(vendor_refused))
    refused_loaded = sorted(n for n in refused if n in loaded)
    rep.add("INFO", f"{len(refused)} stock GKI (system_dlkm) modules use protected symbols and will be refused, as on "
                    f"any self-built kernel: {', '.join(sorted(refused))}")
    rep.add("INFO", f"of these, loaded on the phone today: {', '.join(refused_loaded) or 'none'}; "
                    f"modules depending on them: "
                    f"{some(n for n, m_ in mods.items() if any(s in mods[r]['exports'] for r in refused for s in needs(m_))) or 'none'}")
    by_src = {n for n, m_ in mods.items() if any(s in kernel and s not in kmi for s in needs(m_))}
    rep.check(by_src == set(refused), "same result from the symbol lists in the source tree (android/abi_gki_aarch64*)")
    phdr = os.path.join(a.dist, "gki_module_protected_exports.h")
    prot = re.findall(r'^\t"([^"]+)",$', open(phdr).read(), re.M) if os.path.exists(phdr) else None
    rep.check(prot == [], "no symbol is export-protected, so no stock module is refused for what it exports",
              "header missing" if prot is None else f"{len(prot)} protected")

    # ------------------------------------------------------------------ the phone's loader scripts
    # Read from the phone: /vendor/bin/system_dlkm_modprobe.sh and vendor_modprobe.sh build a module list, load the
    # FIRST module synchronously and give up on the whole list if that fails; every other module is loaded by its own
    # background modprobe, so one failing module does not stop the rest. So the first ("gate") module of each list
    # must be loadable, and the modules this kernel refuses must not be gates.
    rep.section("Module loading scripts of the phone (second-stage init)")

    def script_filter(lines, patterns):   # what `grep -w -v -e pat ...` in the scripts lets through
        keep = []
        for line in lines:
            if not any(re.search(r"(?<![0-9A-Za-z_])(?:%s)(?![0-9A-Za-z_])" % p, line) for p in patterns):
                keep.append(line)
        return keep

    def blocklist(name):
        return [mm_.group(1) for mm_ in (re.match(r"\s*blocklist\s+(\S+)", l) for l in open(os.path.join(a.device, name),
                                                                                           errors="replace")) if mm_]

    canon = lambda p: os.path.basename(p.strip())[:-3].replace("-", "_")
    sys_lines = [l.strip() for l in open(os.path.join(a.device, "system_dlkm_find_order.txt")) if l.strip().endswith(".ko")]
    sys_list = [canon(l) for l in script_filter(sys_lines, [b.replace("-", "[-_]") for b in blocklist("system_dlkm_blocklist.txt")]
                                                + ["zram", "zsmalloc"])]
    ven_lines = []
    for l in open(os.path.join(a.device, "vendor_dlkm_modules_load.txt")):
        if l.strip().endswith(".ko") and l.strip() not in ven_lines:
            ven_lines.append(l.strip())
    ven_list = [canon(l) for l in script_filter(ven_lines, [b.replace("-", "_") for b in blocklist("vendor_dlkm_modules_blocklist.txt")])]
    for label, lst in (("system_dlkm", sys_list), ("vendor_dlkm", ven_list)):
        gate = lst[0] if lst else None
        rep.check(bool(gate) and gate in mods and gate not in bad and gate not in refused,
                  f"{label}: the gate module (first of {len(lst)} in the script's list) passes every load-time rule",
                  gate or "empty list")
    unknown = sorted(n for n in sys_list + ven_list if n not in mods)
    rep.check(not unknown, "every module the scripts would load is among the modules that were checked", some(unknown))
    pos = {n: sys_list.index(n) + 1 for n in refused if n in sys_list}
    rep.add("INFO", "refused modules sit at positions " + ", ".join(f"{n}={p}" for n, p in sorted(pos.items(), key=lambda kv: kv[1]))
                    + f" of the system_dlkm list; each is loaded by its own modprobe call, so only these {len(pos)} are missing")

    # ------------------------------------------------------------------ static keys
    rep.section("Static keys shared between the SUSFS hooks and the KernelSU driver (vmlinux)")
    try:
        ents, suspects, wrong_state, required = statickeys.analyse(a.vmlinux, KSU_STATIC_KEYS)
        rep.check(len(ents) > 1000 and not suspects,
                  f"all {len(ents)} static-key uses in vmlinux refer to an 8-aligned data object of at least 16 bytes",
                  "; ".join(f"{s['site']} -> {s['lands_in']}" for s in suspects[:6]))
        rep.check(not wrong_state, "at every use the compiled instruction (NOP or branch) matches the key's initial value, "
                                   "i.e. keys are declared and defined the same way",
                  "; ".join(f"{s['site']}: {s['why']}" for s in wrong_state[:4]))
        for name in KSU_STATIC_KEYS:
            r = required.get(name)
            rep.check(bool(r) and r["size"] == statickeys.KEY_SIZE and bool(r["uses"]),
                      f"{name} is defined as a static key and used as one",
                      "symbol missing" if not r else f"size {r['size']}, {len(r['uses'])} use(s): {some(r['uses'], 4)}")
    except Exception as e:
        rep.add("FAIL", f"static-key check could not run: {e}")

    # ------------------------------------------------------------------ symbols
    rep.section("Built-in features (System.map)")
    names = set()
    for line in open(os.path.join(a.dist, "System.map"), errors="replace"):
        p = line.split()
        if len(p) == 3:
            names.add(p[2])
    ksu = [n for n in names if n.startswith("ksu_")]
    susfs = [n for n in names if n.startswith("susfs_")]
    rep.check(len(ksu) > 50, "KernelSU driver symbols are built in", f"{len(ksu)} ksu_* symbols")
    rep.check(len(susfs) > 20, "SUSFS symbols are built in", f"{len(susfs)} susfs_* symbols")
    fv = re.search(rb"v\d+\.\d+\.\d+[-\w.]*@[\w-]+", image)
    rep.check(bool(fv), "KSU version string present in the Image", fv.group(0).decode() if fv else "not found")
    stock_names = set()
    for line in open(os.path.join(a.device, "kallsyms_names.txt"), errors="replace"):
        p = line.split()
        if len(p) == 2:
            stock_names.add(p[1])
    pat = re.compile(r"hmbird|^slim_|^scx_")
    strip = lambda s: re.sub(r"(\.cfi_jt|\.llvm\.\d+|\.__key(\.\d+)?|\.__already_done(\.\d+)?|\.\d+)$", "", s)
    s_stock = {strip(n) for n in stock_names if pat.search(n)}
    s_new = {strip(n) for n in names if pat.search(n)}
    both = s_stock & s_new
    rep.add("INFO", f"HMBIRD/slim/scx symbols: stock {len(s_stock)}, new {len(s_new)}, common {len(both)}; "
                    f"stock-only {len(s_stock - s_new)}, new-only {len(s_new - s_stock)}")
    rep.check(len(s_stock) > 50 and len(both) >= 0.8 * len(s_stock),
              "HMBIRD scheduler core mirrors the stock kernel's symbol set", f"{len(both)}/{len(s_stock)} stock symbols present")
    if s_stock - s_new:
        rep.add("INFO", "stock-only: " + ", ".join(sorted(s_stock - s_new)[:25]))
    if s_new - s_stock:
        rep.add("INFO", "new-only: " + ", ".join(sorted(s_new - s_stock)[:25]))

    # ------------------------------------------------------------------ boot image
    rep.section("boot.img")
    stock = open(a.stock_boot, "rb").read()
    new = open(a.boot, "rb").read()
    si, ni = bootimg.parse(stock), bootimg.parse(new)
    rep.check(len(new) == len(stock), "image size equals the boot partition size", f"{len(new)}")
    rep.check(ni["kernel"] == image, "kernel inside boot.img is byte-identical to the built Image",
              f"{len(ni['kernel'])} bytes")
    rep.check(ni["hv"] == si["hv"] == 4, "boot header version 4 kept")
    h_s, h_n = bytearray(stock[:4096]), bytearray(new[:4096])
    h_s[8:12] = h_n[8:12] = b"\0\0\0\0"
    rep.check(h_s == h_n, "header identical to stock except kernel_size")
    rep.check(len(ni["ramdisk"]) == 0 and len(ni["sig"]) == 0, "no ramdisk / no boot signature, as in stock")
    ok = ni["avb"] is not None and si["avb"] is not None and ni["avb"]["blob"] == si["avb"]["blob"]
    rep.check(ok, "AVB vbmeta blob carried over unchanged")
    if ok:
        rep.check(ni["avb"]["voff"] == ni["payload_end"] == ni["avb"]["orig"],
                  "AVB footer points right behind the new payload", f"offset {ni['avb']['voff']}")
        props = bootimg.avb_props(ni["avb"]["blob"])
        rep.check(props.get("com.android.build.boot.security_patch") ==
                  bootimg.avb_props(si["avb"]["blob"]).get("com.android.build.boot.security_patch"),
                  "boot security patch level property preserved",
                  props.get("com.android.build.boot.security_patch", "missing"))
        tail = new[ni["avb"]["voff"] + ni["avb"]["vsize"]:-64]
        gap = new[4096 + len(ni["kernel"]):ni["avb"]["voff"]]
        rep.check(not any(tail) and not any(gap), "everything between kernel, vbmeta and footer is zero padding")

    if a.init_boot_stock and a.init_boot_current:
        rep.section("init_boot images used for flashing / rollback")
        s_ent = bootimg.ramdisk_entries(open(a.init_boot_stock, "rb").read())
        cur = open(a.init_boot_current, "rb").read()
        c_raw = bootimg.ramdisk_entries(cur)
        c_ent = bootimg.ramdisk_entries(cur, undo_ksu_lkm=True)
        rep.check("kernelsu.ko" in c_raw and "init.real" in c_raw,
                  "the phone's current init_boot carries the KernelSU LKM (ksuinit + kernelsu.ko)")
        rep.check("kernelsu.ko" not in s_ent and "init.real" not in s_ent and "init" in s_ent,
                  "the init_boot offered as 'stock' carries no KernelSU")
        rep.check(s_ent == c_ent, "it is the same build: identical to the current init_boot with the LKM patch undone",
                  f"{len(s_ent)} ramdisk entries compared by name, mode, owner and content")
        # The current init_boot was written on the phone by ksud's bundled magiskboot. Re-doing that repack with
        # bootimg.py (stock image + the patched ramdisk) must give the very same file.
        stock_ib = open(a.init_boot_stock, "rb").read()
        rep.check(bootimg.build(stock_ib, ramdisk=bootimg.parse(cur)["ramdisk"]) == cur,
                  "bootimg.py's ramdisk repack equals magiskboot's: stock init_boot + patched ramdisk == the phone's init_boot, byte for byte")


if __name__ == "__main__":
    sys.exit(main())
