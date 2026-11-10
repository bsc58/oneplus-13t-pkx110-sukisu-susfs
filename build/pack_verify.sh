#!/usr/bin/env bash
# Package boot.img from the build output, verify it OFFLINE against facts read from the phone and,
# only if every check passes, (re)create release/. Nothing here talks to a device.
#   usage: sudo bash pack_verify.sh      (after build_13t.sh)
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$HOME/op13t}"
DIST="$WORK/dist"
OUTD="${OUTD:-$HERE/out}"
REL="${REL:-$HERE/release}"
NAME="${BOOT_NAME:-boot_sukisu_13t.img}"   # not ${NAME}: WSL pre-sets NAME to the Windows host name
for tool in unpack_bootimg pahole python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' not found - run build_13t.sh deps"; exit 1; }
done
mkdir -p "$OUTD"
# Start clean: an earlier release/ or an earlier passing report must not survive a run that fails.
rm -rf "$REL"
rm -f "$OUTD"/AnyKernel3_*.zip "$OUTD/$NAME" "$OUTD/VERIFY_REPORT.txt" "$OUTD/SHA256SUMS.txt"

cp "$DIST/Image" "$DIST/kernel.config" "$DIST/BUILD_INFO.txt" "$OUTD/"
cp "$DIST"/AnyKernel3_*.zip "$OUTD/"
gzip -9 -n -c "$DIST/vmlinux.symvers" > "$OUTD/vmlinux.symvers.gz"
gzip -9 -n -c "$DIST/System.map" > "$OUTD/System.map.gz"

echo "== repack: stock boot_b.img + new kernel"
python3 "$HERE/tools/bootimg.py" repack "$HERE/stock/boot_b.img" "$OUTD/$NAME" --kernel "$DIST/Image"

rc=0
echo "== independent parse with AOSP unpack_bootimg (Ubuntu package mkbootimg)"
t=$(mktemp -d)
unpack_bootimg --boot_img "$OUTD/$NAME" --out "$t" | sed 's/^/  /'
if cmp "$t/kernel" "$DIST/Image"; then
  echo "  kernel extracted by unpack_bootimg == built Image"
else
  echo "  FAIL: unpack_bootimg extracts a different kernel"; rc=1
fi
rm -rf "$t"

echo "== offline verification"
python3 "$HERE/tools/verify_build.py" --dist "$DIST" --device "$HERE/device" --vmlinux "$WORK/out/vmlinux" \
  --stock-boot "$HERE/stock/boot_b.img" --boot "$OUTD/$NAME" \
  --init-boot-stock "$HERE/stock/init_boot_a.img" --init-boot-current "$HERE/stock/init_boot_b.img" \
  --report "$OUTD/VERIFY_REPORT.txt" || rc=1
python3 "$HERE/tools/config_diff.py" "$HERE/stock/stock_config.gz" "$DIST/kernel.config" > "$OUTD/CONFIG_DIFF_vs_stock.txt"

echo "== real struct layouts (BTF) of the new kernel vs the stock kernel"
python3 "$HERE/tools/btf_layout.py" "$HERE/stock/boot_b.img" "$DIST/Image" --report "$OUTD/BTF_LAYOUT_vs_stock.txt" > /dev/null
sed -n '1,3p' "$OUTD/BTF_LAYOUT_vs_stock.txt"
grep -E 'key structs with a real layout difference|MEMBERS MOVED|SIZE DIFFERS' "$OUTD/BTF_LAYOUT_vs_stock.txt" || true
( cd "$OUTD" && sha256sum "$NAME" Image AnyKernel3_*.zip > SHA256SUMS.txt )

if [ "$rc" -ne 0 ]; then
  echo "== VERIFICATION FAILED: no release/ was created (see $OUTD/VERIFY_REPORT.txt)"
  exit "$rc"
fi

echo "== all checks passed: assembling $REL"
mkdir -p "$REL/flash" "$REL/rollback"
cp "$OUTD/$NAME" "$REL/flash/$NAME"                                      # new kernel            -> boot
cp "$HERE/stock/init_boot_a.img" "$REL/flash/init_boot_stock.img"         # stock, no KernelSU LKM -> init_boot
cp "$HERE/stock/boot_b.img" "$REL/rollback/boot_stock.img"                # stock kernel
cp "$HERE/stock/init_boot_b.img" "$REL/rollback/init_boot_kernelsu_lkm_current.img"   # today's init_boot (KernelSU LKM)
cp "$HERE/stock/init_boot_a.img" "$REL/rollback/init_boot_stock.img"
cp "$OUTD"/AnyKernel3_*.zip "$REL/"
( cd "$REL" && sha256sum flash/* rollback/* AnyKernel3_*.zip > SHA256SUMS.txt && cat SHA256SUMS.txt )
