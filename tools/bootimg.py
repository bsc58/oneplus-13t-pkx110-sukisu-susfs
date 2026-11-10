#!/usr/bin/env python3
"""Small, dependency-free boot image helper (Android boot header v3/v4, 4096-byte pages).

  info   <img>
  repack <stock.img> <out.img> [--kernel Image] [--ramdisk ramdisk.cpio.lz4]
      Copy the stock image, swap the kernel and/or ramdisk, keep every other header
      field, and carry the AVB footer + vbmeta blob over the same way magiskboot does
      (blob moved behind the new payload, footer offsets updated, file padded back to
      the partition size). The stored hash is NOT re-signed: this is for an unlocked
      bootloader only.
  unpatch-ksu-lkm <patched_init_boot.img> <out.img>
      Undo a KernelSU LKM patch of init_boot: drop /init (ksuinit) and /kernelsu.ko,
      rename /init.real back to /init. Needs the `lz4` command for recompression.
"""
import gzip
import os
import re
import struct
import subprocess
import sys

PAGE = 4096


def align(n, a=PAGE):
    return (n + a - 1) // a * a


def parse(img):
    if img[:8] != b"ANDROID!":
        raise SystemExit("not an Android boot image")
    ks, rs, osv, hsize = struct.unpack_from("<4I", img, 8)
    hv = struct.unpack_from("<I", img, 40)[0]
    if hv not in (3, 4):
        raise SystemExit(f"unsupported boot header version {hv}")
    sig = struct.unpack_from("<I", img, 1580)[0] if hv == 4 else 0
    k_off = PAGE
    r_off = k_off + align(ks)
    s_off = r_off + align(rs)
    info = {"hv": hv, "hsize": hsize, "osv": osv, "kernel": img[k_off:k_off + ks],
            "ramdisk": img[r_off:r_off + rs], "sig": img[s_off:s_off + sig],
            "payload_end": s_off + align(sig), "size": len(img), "avb": None}
    if img[-64:-60] == b"AVBf":
        maj, _min, orig, voff, vsize = struct.unpack_from(">IIQQQ", img, len(img) - 60)
        blob = img[voff:voff + vsize]
        # Same sanity check as magiskboot: the footer must point at a real vbmeta blob ("AVB0").
        if maj != 1 or voff + vsize > len(img) - 64 or blob[:4] != b"AVB0":
            raise SystemExit("AVB footer found, but it does not point at a valid vbmeta blob "
                             "(truncated or corrupt image?); refusing")
        info["avb"] = {"orig": orig, "voff": voff, "vsize": vsize, "blob": blob, "footer": img[-64:]}
    return info


def write_atomic(path, data):
    """Never leave a truncated image behind: write next to the target, then rename over it."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def kind(b):
    if not b:
        return "none"
    if b[:2] == b"\x1f\x8b":
        return "gzip"
    if b[:4] == b"\x02\x21\x4c\x18":
        return "lz4-legacy"
    if b[56:60] == b"ARMd":
        return "raw arm64 Image"
    if b[:6] == b"070701":
        return "cpio"
    return "unknown(" + b[:4].hex() + ")"


def avb_props(blob):
    out = {}
    for m in re.finditer(rb"(com\.android\.build\.[a-z_.]+)\0([^\0]{1,120})\0", blob):
        out[m.group(1).decode()] = m.group(2).decode(errors="replace")
    return out


def cmd_info(path):
    img = open(path, "rb").read()
    i = parse(img)
    osv = i["osv"]
    print(f"file size        : {len(img)}")
    print(f"header version   : {i['hv']} (header_size {i['hsize']})")
    print(f"header os_version: {(osv >> 25) & 0x7f}.{(osv >> 18) & 0x7f}.{(osv >> 11) & 0x7f}"
          f"  patch {((osv >> 4) & 0x7f) + 2000}-{osv & 0xf:02d}")
    k = i["kernel"]
    print(f"kernel           : {len(k)} bytes, {kind(k)}")
    if k[56:60] == b"ARMd":
        flags = struct.unpack_from("<Q", k, 24)[0]
        page = {1: "4K", 2: "16K", 3: "64K"}.get((flags >> 1) & 3, "unspecified")
        print(f"  arm64 page size: {page}  (image_size {struct.unpack_from('<Q', k, 16)[0]})")
        m = re.search(rb"Linux version [^\n]{0,160}", k)
        if m:
            print("  " + m.group(0).decode(errors="replace"))
    print(f"ramdisk          : {len(i['ramdisk'])} bytes, {kind(i['ramdisk'])}")
    print(f"boot signature   : {len(i['sig'])} bytes")
    a = i["avb"]
    if a:
        print(f"AVB footer       : original_image_size {a['orig']}, vbmeta at {a['voff']} (+{a['vsize']})")
        for k_, v in avb_props(a["blob"]).items():
            print(f"  {k_} = {v}")
    else:
        print("AVB footer       : none")


def build(stock, kernel=None, ramdisk=None):
    i = parse(stock)
    k = kernel if kernel is not None else i["kernel"]
    r = ramdisk if ramdisk is not None else i["ramdisk"]
    hdr = bytearray(stock[:PAGE])
    struct.pack_into("<II", hdr, 8, len(k), len(r))
    out = bytearray(hdr)
    for part in (k, r, i["sig"]):
        out += part
        out += b"\0" * (align(len(out)) - len(out))
    total = len(out)
    a = i["avb"]
    if a:
        if total + a["vsize"] + 64 > i["size"]:
            raise SystemExit(f"new image does not fit: payload {total} + vbmeta {a['vsize']} "
                             f"+ footer > partition {i['size']}")
        out += a["blob"]
        out += b"\0" * (i["size"] - len(out))
        footer = bytearray(a["footer"])
        struct.pack_into(">QQ", footer, 12, total, total)
        out[-64:] = footer
    elif total > i["size"]:
        raise SystemExit("new image is larger than the stock image")
    else:
        out += b"\0" * (i["size"] - len(out))
    return bytes(out)


def lz4_block(src, out):
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]
                i += 1
                ll += b
                if b != 255:
                    break
        out += src[i:i + ll]
        i += ll
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            chunk = bytes(out[start:start + off])
            out += (chunk * (ml // off + 1))[:ml]


def decompress(b):
    if b[:4] == b"\x02\x21\x4c\x18":
        out, i = bytearray(), 4
        while i + 4 <= len(b):
            sz = struct.unpack_from("<I", b, i)[0]
            if sz == 0x184C2102:
                i += 4
                continue
            if sz == 0 or i + 4 + sz > len(b):
                break
            i += 4
            lz4_block(b[i:i + sz], out)
            i += sz
        return bytes(out)
    if b[:2] == b"\x1f\x8b":
        return gzip.decompress(b)
    if b[:6] == b"070701":
        return b
    raise SystemExit("unsupported ramdisk compression")


def cpio_iter(d):
    i = 0
    while i + 110 <= len(d) and d[i:i + 6] == b"070701":
        f = [int(d[i + 6 + k * 8:i + 14 + k * 8], 16) for k in range(13)]
        fsize, nsize = f[6], f[11]
        name = d[i + 110:i + 110 + nsize - 1]
        ds = (i + 110 + nsize + 3) & ~3
        yield name, f, d[ds:ds + fsize]
        i = (ds + fsize + 3) & ~3
        if name == b"TRAILER!!!":
            break


def cpio_entry(name, f, data):
    f = list(f)
    f[6], f[11] = len(data), len(name) + 1
    h = b"070701" + b"".join(b"%08X" % v for v in f) + name + b"\0"
    h += b"\0" * ((4 - len(h) % 4) % 4)
    return h + data + b"\0" * ((4 - len(data) % 4) % 4)


def cmd_unpatch(src, dst):
    stock = open(src, "rb").read()
    i = parse(stock)
    fmt = kind(i["ramdisk"])
    raw = decompress(i["ramdisk"])
    entries = list(cpio_iter(raw))
    names = [e[0] for e in entries]
    if b"init.real" not in names or b"kernelsu.ko" not in names:
        raise SystemExit("this ramdisk does not look KernelSU-LKM patched (no init.real / kernelsu.ko)")
    out = bytearray()
    for name, f, data in entries:
        if name in (b"init", b"kernelsu.ko"):
            print(f"  drop   /{name.decode()} ({len(data)} bytes)")
            continue
        if name == b"init.real":
            print(f"  rename /init.real -> /init ({len(data)} bytes, mode {f[1]:o})")
            name = b"init"
        out += cpio_entry(name, f, data)
    out += b"\0" * ((512 - len(out) % 512) % 512)
    if fmt == "lz4-legacy":
        packed = subprocess.run(["lz4", "-l", "-12", "--favor-decSpeed", "-c"], input=bytes(out),
                                stdout=subprocess.PIPE, check=True).stdout
    elif fmt == "gzip":
        packed = gzip.compress(bytes(out), 9, mtime=0)
    else:
        packed = bytes(out)
    write_atomic(dst, build(stock, ramdisk=packed))
    print(f"  ramdisk {len(i['ramdisk'])} -> {len(packed)} bytes ({fmt}); wrote {dst}")


def ramdisk_entries(img, undo_ksu_lkm=False):
    """{name: (mode, uid, gid, data)} of an init_boot/boot ramdisk; optionally with a KernelSU LKM
    patch undone (drop /init = ksuinit and /kernelsu.ko, rename /init.real back to /init)."""
    out = {}
    for name, f, data in cpio_iter(decompress(parse(img)["ramdisk"])):
        if undo_ksu_lkm:
            if name in (b"init", b"kernelsu.ko"):
                continue
            if name == b"init.real":
                name = b"init"
        out[name.decode(errors="replace")] = (f[1], f[2], f[3], bytes(data))
    return out


def main(argv):
    if len(argv) == 3 and argv[1] == "info":
        cmd_info(argv[2])
    elif len(argv) >= 4 and argv[1] == "repack":
        rest, opts = argv[4:], {}
        if len(rest) % 2:
            raise SystemExit("repack: every option needs a value")
        for key, val in zip(rest[0::2], rest[1::2]):
            if key not in ("--kernel", "--ramdisk") or key in opts:
                raise SystemExit(f"repack: unknown or repeated option {key}")
            opts[key] = val
        if not opts:
            raise SystemExit("repack: nothing to change, give --kernel and/or --ramdisk")
        stock = open(argv[2], "rb").read()
        kernel = open(opts["--kernel"], "rb").read() if "--kernel" in opts else None
        ramdisk = open(opts["--ramdisk"], "rb").read() if "--ramdisk" in opts else None
        if kernel is not None and kind(parse(stock)["kernel"]) != kind(kernel):
            raise SystemExit(f"kernel format differs from stock ({kind(parse(stock)['kernel'])} "
                             f"vs {kind(kernel)}); refusing")
        write_atomic(argv[3], build(stock, kernel, ramdisk))
        cmd_info(argv[3])
    elif len(argv) == 4 and argv[1] == "unpatch-ksu-lkm":
        cmd_unpatch(argv[2], argv[3])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
