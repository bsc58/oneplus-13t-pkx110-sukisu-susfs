#!/usr/bin/env python3
"""Compare real struct layouts of two kernels using the BTF embedded in their Images.

  btf_layout.py <stock Image or boot.img> <new Image or boot.img> [--show name ...] [--report file]

CONFIG_MODVERSIONS CRCs only cover what genksyms sees; BTF records the layout the compiler
actually produced (sizes, member offsets). For every struct/union name present in both
kernels this prints whether size and member offsets agree. Differences in kernel-private
structs between two sublevels are normal; differences in structs that vendor modules
dereference (task_struct, rq, ...) are not.
"""
import struct
import sys

KIND = ["void", "int", "ptr", "array", "struct", "union", "enum", "fwd", "typedef", "volatile",
        "const", "restrict", "func", "func_proto", "var", "datasec", "float", "decl_tag",
        "type_tag", "enum64"]

# structs that out-of-tree (vendor) modules commonly dereference
KEY = ["task_struct", "thread_info", "sched_entity", "sched_rt_entity", "sched_dl_entity", "sched_avg",
       "rq", "cfs_rq", "rt_rq", "dl_rq", "root_domain", "sched_domain", "sched_group", "task_group",
       "mm_struct", "vm_area_struct", "file", "inode", "dentry", "super_block", "mount", "vfsmount",
       "address_space", "page", "folio", "zone", "pglist_data", "mem_cgroup", "cgroup", "css_set",
       "cred", "signal_struct", "sighand_struct", "nsproxy", "pid", "module", "device", "device_driver",
       "bus_type", "kobject", "kset", "class", "platform_device", "dev_pm_info", "wakeup_source",
       "sk_buff", "sock", "net_device", "net", "socket", "request_queue", "gendisk", "bio", "request",
       "block_device", "cpufreq_policy", "cpuidle_device", "irq_desc", "irq_data", "timer_list",
       "hrtimer", "work_struct", "delayed_work", "workqueue_struct", "kthread_worker", "mutex",
       "rw_semaphore", "spinlock", "raw_spinlock", "wait_queue_head", "completion", "list_head",
       "binder_proc", "binder_thread", "binder_node", "binder_transaction", "psi_group", "uclamp_se",
       "clk_hw", "clk_init_data", "regulator_dev", "regulator_desc", "thermal_zone_device",
       "power_supply", "usb_gadget", "usb_device", "scsi_device", "scsi_cmnd", "Scsi_Host", "ufs_hba",
       "mmc_host", "i2c_client", "i2c_adapter", "spi_device", "input_dev", "drm_device", "dma_buf",
       "iommu_domain", "icc_node", "icc_provider", "of_phandle_args", "device_node", "fwnode_handle",
       "perf_event", "bpf_prog", "seq_file", "kernfs_node", "proc_dir_entry", "user_struct",
       "hmbird_entity", "sched_ext_entity", "scx_entity", "hmbird_ops", "hmbird_rq", "scx_rq"]


def find_btf(blob):
    best = None
    pos = 0
    while True:
        i = blob.find(b"\x9f\xeb\x01", pos)
        if i < 0:
            break
        pos = i + 1
        if i + 24 > len(blob):
            break
        _magic, _ver, _flags, hdr_len, type_off, type_len, str_off, str_len = struct.unpack_from("<HBBIIIII", blob, i)
        if hdr_len != 24 or type_off != 0 or str_off < type_len or type_len < 100000:
            continue
        end = i + hdr_len + str_off + str_len
        if end > len(blob) or blob[i + hdr_len + str_off:i + hdr_len + str_off + 1] != b"\0":
            continue
        if best is None or type_len > best[1]:
            best = (blob[i:end], type_len)
    if best is None:
        raise SystemExit("no BTF blob found (kernel built without CONFIG_DEBUG_INFO_BTF?)")
    return best[0]


def kernel_of(path):
    data = open(path, "rb").read()
    if data[:8] == b"ANDROID!":
        ks = struct.unpack_from("<I", data, 8)[0]
        return data[4096:4096 + ks]
    return data


class Btf:
    def __init__(self, raw):
        _m, _v, _f, hdr_len, type_off, type_len, str_off, str_len = struct.unpack_from("<HBBIIIII", raw, 0)
        self.strs = raw[hdr_len + str_off:hdr_len + str_off + str_len]
        self.types = [None]
        p = hdr_len + type_off
        end = p + type_len
        while p < end:
            name_off, info, size_type = struct.unpack_from("<III", raw, p)
            p += 12
            kind, vlen, kflag = (info >> 24) & 0x1F, info & 0xFFFF, info >> 31
            t = {"kind": kind, "name": self.s(name_off), "st": size_type}
            if kind == 1:
                p += 4
            elif kind == 3:
                t["elem"], _idx, t["nelems"] = struct.unpack_from("<III", raw, p)
                p += 12
            elif kind in (4, 5):
                mem = []
                for _ in range(vlen):
                    n, ty, off = struct.unpack_from("<III", raw, p)
                    p += 12
                    bits = (off >> 24) if kflag else 0
                    mem.append((self.s(n), ty, off & 0xFFFFFF if kflag else off, bits))
                t["members"] = mem
            elif kind == 6:
                p += 8 * vlen
            elif kind == 13:
                p += 8 * vlen
            elif kind in (14, 17):
                p += 4
            elif kind in (15, 19):
                p += 12 * vlen
            self.types.append(t)

    def s(self, off):
        return self.strs[off:self.strs.index(b"\0", off)].decode(errors="replace")

    def tname(self, tid, depth=0):
        if tid == 0:
            return "void"
        t = self.types[tid]
        k = t["kind"]
        if depth > 6:
            return "..."
        if k in (8,):  # typedef: resolve so that renamed typedefs do not show up as changes
            return self.tname(t["st"], depth + 1)
        if k in (9, 10, 11, 18):
            return self.tname(t["st"], depth + 1)
        if k == 2:
            return "*" + self.tname(t["st"], depth + 1)
        if k == 3:
            return f"{self.tname(t['elem'], depth + 1)}[{t['nelems']}]"
        if k in (4, 5, 6, 7, 19):
            return f"{KIND[k]} {t['name'] or '(anon)'}"
        if k == 13:
            return "func"
        return t["name"] or KIND[k]

    def layouts(self):
        out = {}
        for t in self.types[1:]:
            if t["kind"] in (4, 5) and t["name"]:
                lay = (t["st"], tuple((n, off, bits, self.tname(ty)) for n, ty, off, bits in t["members"]))
                out.setdefault(f"{KIND[t['kind']]} {t['name']}", set()).add(lay)
        return out


def describe(lay):
    size, members = lay
    lines = [f"    size {size}"]
    for n, off, bits, ty in members:
        lines.append(f"    +{off // 8:<5}{'.' + str(off % 8) if off % 8 else '':<3} {ty} {n}{':' + str(bits) if bits else ''}")
    return lines


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    show, report = [], None
    rest = argv[3:]
    i = 0
    while i < len(rest):
        if rest[i] == "--report":
            report = rest[i + 1]
            i += 2
        elif rest[i] == "--show":
            i += 1
            while i < len(rest) and not rest[i].startswith("--"):
                show.append(rest[i])
                i += 1
        else:
            i += 1
    a = Btf(find_btf(kernel_of(argv[1]))).layouts()
    b = Btf(find_btf(kernel_of(argv[2]))).layouts()
    common = sorted(set(a) & set(b))
    same = [n for n in common if a[n] == b[n]]
    # "offsets" = sizes and member offsets agree although a member was renamed/retyped (e.g. a KABI
    # reserved slot put to use)
    shape = lambda lays: {(s, tuple((off, bits) for _n, off, bits, _t in m)) for s, m in lays}
    renamed = [n for n in common if a[n] != b[n] and shape(a[n]) == shape(b[n])]
    sizes = lambda lays: {s for s, _m in lays}
    resized = [n for n in common if sizes(a[n]) != sizes(b[n])]
    moved = [n for n in common if a[n] != b[n] and n not in renamed and n not in resized]
    out = []
    out.append(f"named structs/unions: stock {len(a)}, new {len(b)}, common {len(common)}")
    out.append(f"identical layout: {len(same)}   same offsets, member renamed/retyped: {len(renamed)}   "
               f"same size, members moved: {len(moved)}   different size: {len(resized)}")
    out.append(f"only in stock: {len(set(a) - set(b))}   only in new: {len(set(b) - set(a))}")
    out.append("")
    out.append("== key structs (dereferenced by vendor modules)")
    bad_key = 0
    for k in KEY:
        for kind in ("struct", "union"):
            n = f"{kind} {k}"
            if n in a and n in b:
                st = "identical" if n in same else "offsets identical (member renamed/retyped)" if n in renamed \
                    else "MEMBERS MOVED" if n in moved else "SIZE DIFFERS"
                if n in moved or n in resized:
                    bad_key += 1
                out.append(f"  {n}: {st}  (size {sorted(sizes(a[n]))} -> {sorted(sizes(b[n]))})")
            elif n in a or n in b:
                out.append(f"  {n}: only in {'stock' if n in a else 'new'}")
    out.append(f"key structs with a real layout difference: {bad_key}")
    out.append("")
    out.append("== same offsets, member renamed/retyped: " + ", ".join(renamed))
    out.append("")
    out.append("== same size, members moved: " + ", ".join(moved))
    out.append("")
    out.append("== different size: " + ", ".join(f"{n} {sorted(sizes(a[n]))}->{sorted(sizes(b[n]))}" for n in resized))
    for name in show:
        for kind in ("struct", "union"):
            n = f"{kind} {name}"
            if n in a or n in b:
                out.append("")
                out.append(f"== {n}")
                for label, db in (("stock", a), ("new", b)):
                    for lay in sorted(db.get(n, []), key=lambda x: x[0]):
                        out.append(f"  [{label}]")
                        out.extend(describe(lay))
    text = "\n".join(out)
    if report:
        with open(report, "w") as fh:
            fh.write(text + "\n")
    print(text[:6000] + ("\n... (see report file for the rest)" if len(text) > 6000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
