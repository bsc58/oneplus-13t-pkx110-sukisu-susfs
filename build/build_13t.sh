#!/usr/bin/env bash
# OnePlus 13T (PKX110, SM8750 "sun") - GKI android15-6.6, 4K pages
# SukiSU Ultra (builtin branch, SUSFS built in) - ColorOS/OxygenOS flow:
#   ZyCromerZ Clang 19 + plain `make gki_defconfig && make Image` on the common kernel,
#   as done by palazik/actions_oplus_sm8750 (Build Kernel.yml) and WildKernels.
#
# Run inside Linux (WSL2 Ubuntu 24.04 is fine) on a case-sensitive filesystem:
#   sudo bash build_13t.sh            # all steps
#   sudo bash build_13t.sh build      # a single step: deps|fetch|ksu|susfs|hmbird|abi|lkmguard|config|build|collect
#   sudo bash build_13t.sh clean      # optional: drop the object directory before a from-scratch rebuild
# Then: sudo bash pack_verify.sh        # repack boot.img, verify it offline (no device involved), fill release/
set -Eeuo pipefail

# ---------------------------------------------------------------- settings
WORK="${WORK:-$HOME/op13t}"            # must NOT be under /mnt/c (needs a case-sensitive fs)
JOBS="${JOBS:-$(nproc)}"
LTO="${LTO:-none}"                     # stock 13T kernel is built with CONFIG_LTO_NONE=y
CPU_OPT="${CPU_OPT:-0}"                # 1 = add -mcpu=oryon-1 (palazik "cpu_opt"); off = closer to stock
# The stock 13T kernel is an OGKI build with CONFIG_HMBIRD_SCHED=y ("Fengchi" scheduler core).
# OnePlus' open-source tree ships an older SLIM_SCHED/SCHED_CLASS_EXT variant instead, so the
# reference builders (WildKernels "hmbird": true, palazik "fengchi") patch the HMBIRD core back.
# 1 = apply WildKernels' fengchi_OP13T_A15.patch (same config symbol as stock); 0 = raw OSS tree.
HMBIRD="${HMBIRD:-1}"
# 1 = the kernel refuses to load a module named "kernelsu" (a KernelSU LKM left over in init_boot
# can then never run next to the built-in SukiSU); 0 = leave the module loader untouched.
LKM_GUARD="${LKM_GUARD:-1}"
TOOLS_DIR="${TOOLS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../tools" && pwd)}"

# ---------------------------------------------------------------- pinned inputs
# OnePlusOSS/kernel_manifest, branch oneplus/sm8750, file oneplus_13t.xml:
#   kernel_platform/common = android_kernel_common_oneplus_sm8750 @ oneplus/sm8750_v_15.0.2_oneplus_13t
# Pinned to the commit "Synchronize code for OnePlus PKX110_15.0.2.103(CN01)" (Linux 6.6.56),
# the release closest to firmware PKX110_15.0.2.108. The phone's vendor modules carry
# vermagic 6.6.56-android15-8-o-4k, i.e. they were built against this tree.
KERNEL_URL="https://github.com/OnePlusOSS/android_kernel_common_oneplus_sm8750.git"
KERNEL_REF="${KERNEL_REF:-b1fee232c2b5fbf3b4f16d55da53760c0209f59f}"

# SukiSU-Ultra, branch "builtin" (kernel driver with SUSFS integrated). UAPI version 6.
SUKISU_URL="https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
SUKISU_REF="${SUKISU_REF:-1cfa57340590971cba7521ed536da8ebbedc5f0a}"
# Version identity reported to the manager. The upstream Makefile asks the GitHub API for
# these at build time; pinned here so the build is reproducible and matches manager v4.3.0-r2.
SUKISU_TAG="${SUKISU_TAG:-4.3.0-r2}"
SUKISU_MAIN_COMMITS="${SUKISU_MAIN_COMMITS:-3965}"   # 40000 + 3965 - 2815 = 41150

# susfs4ksu, branch gki-android15-6.6, at the state the SukiSU builtin commit above was
# synced against (SUSFS v2.3.0; later commits change both kernel and driver sides).
SUSFS_URL="https://gitlab.com/simonpunk/susfs4ksu.git"
SUSFS_REF="${SUSFS_REF:-eb162d1a32d1aec256899c9d9cc054599c9abea3}"

# WildKernels helper that adapts older OnePlus android15-6.6 trees so the SUSFS patch applies.
WILD_URL="https://github.com/WildKernels/OnePlus_KernelSU_SUSFS.git"
WILD_REF="${WILD_REF:-10f9741ee0bbff5adf69f3260019c7ec5ba63be1}"
WILDP_URL="https://github.com/WildKernels/kernel_patches.git"
WILDP_REF="${WILDP_REF:-b6690c6c66a05f873faf460daaaac1c84de9d720}"

AK3_URL="https://github.com/palazik/AnyKernel3.git"
AK3_REF="${AK3_REF:-fcb882bee47bba5efd4076f8d5c2a00609c533fd}"

CLANG_URL="https://github.com/ZyCromerZ/Clang/releases/download/19.0.0git-20240723-release/Clang-19.0.0git-20240723.tar.gz"

# ---------------------------------------------------------------- layout
COMMON="$WORK/kernel_platform/common"
OUT="$WORK/out"
CLANG="${CLANG:-$WORK/zyc-clang}"
DIST="$WORK/dist"
LOCALVER="-android15-8-g${KERNEL_REF:0:12}-sukisu-4k"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

fetch_commit() {  # dir url sha
  local dir=$1 url=$2 sha=$3
  if [ ! -d "$dir/.git" ]; then
    mkdir -p "$dir"; git -C "$dir" init -q; git -C "$dir" remote add origin "$url"
  fi
  if ! git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
    git -C "$dir" fetch -q --depth 1 origin "$sha" || git -C "$dir" fetch -q origin
  fi
  git -C "$dir" checkout -q -f "$sha"
  git -C "$dir" clean -fdxq
  [ "$(git -C "$dir" rev-parse HEAD)" = "$sha" ] || die "$dir is not at $sha"
}

step_deps() {
  log "Installing build dependencies"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    build-essential bc bison flex libssl-dev libelf-dev dwarves cpio kmod rsync \
    python3 git curl ca-certificates zip unzip xz-utils lz4 zstd file openssl patch perl mkbootimg
}

step_clean() {
  log "Removing the object directory and old artifacts"
  rm -rf "$OUT" "$DIST"
}

step_fetch() {
  log "Fetching sources (pinned commits)"
  mkdir -p "$WORK"
  case "$(stat -f -c %T "$WORK")" in v9fs|9p|drvfs|fuseblk) die "$WORK is on a Windows mount; use the Linux filesystem";; esac
  fetch_commit "$COMMON" "$KERNEL_URL" "$KERNEL_REF"
  fetch_commit "$WORK/susfs4ksu" "$SUSFS_URL" "$SUSFS_REF"
  fetch_commit "$WORK/wild_builder" "$WILD_URL" "$WILD_REF"
  fetch_commit "$WORK/wild_patches" "$WILDP_URL" "$WILDP_REF"
  fetch_commit "$WORK/AnyKernel3" "$AK3_URL" "$AK3_REF"
  if [ ! -x "$CLANG/bin/clang" ]; then
    log "Downloading ZyCromerZ Clang 19 (~720 MB)"
    mkdir -p "$CLANG"
    curl -L --fail --retry 5 --retry-delay 5 -C - -o "$WORK/zyc-clang.tar.gz" "$CLANG_URL"
    tar -C "$CLANG" -xzf "$WORK/zyc-clang.tar.gz"
    rm -f "$WORK/zyc-clang.tar.gz"
  fi
  "$CLANG/bin/clang" --version | sed -n 1p
  sed -n '2,4p' "$COMMON/Makefile" | tr '\n' ' '; echo
}

step_ksu() {
  log "Adding SukiSU Ultra (builtin @ ${SUKISU_REF:0:8})"
  cd "$COMMON"
  fetch_commit "$COMMON/KernelSU" "$SUKISU_URL" "$SUKISU_REF"
  git -C KernelSU checkout -q -B builtin
  ln -sfn ../KernelSU/kernel drivers/kernelsu
  grep -q 'kernelsu' drivers/Makefile || printf '\nobj-$(CONFIG_KSU) += kernelsu/\n' >> drivers/Makefile
  grep -q 'drivers/kernelsu/Kconfig' drivers/Kconfig || sed -i '/endmenu/i\source "drivers/kernelsu/Kconfig"' drivers/Kconfig

  # Pin the version identity instead of querying api.github.com during the build.
  local mk=KernelSU/kernel/Makefile
  grep -q '^KSU_GITHUB_VER *:=' "$mk" && grep -q '^GITHUB_COMMITS *:=' "$mk" || die "unexpected SukiSU Makefile layout"
  sed -i -E "s|^KSU_GITHUB_VER *:=.*|KSU_GITHUB_VER   := ${SUKISU_TAG}|; s|^GITHUB_COMMITS *:=.*|GITHUB_COMMITS   := ${SUKISU_MAIN_COMMITS}|" "$mk"
  sed -i '/fetch --unshallow/d' "$mk"
  grep -n -E '^(KSU_GITHUB_VER|GITHUB_COMMITS) ' "$mk"

  # Fix a mismatch between the two source trees. The SUSFS kernel patch (fs/exec.c, fs/open.c,
  # fs/stat.c) declares `extern struct static_key_true ksu_su_compat_enabled` and tests it with
  # static_branch_likely(); susfs4ksu's own 10_enable_susfs_for_ksu.patch turns the driver variable
  # into a static key accordingly. The SukiSU builtin branch integrates SUSFS itself (so the 10_
  # patch is not applied) but still defines the variable as a 1-byte bool. Nothing notices at build
  # time (separate translation units, no LTO); at boot jump_label_init() then treats the bool and
  # its neighbours as a 16-byte static key and stores a pointer over them, and the manager's
  # "su compat" switch has no effect on the hooks. Define it the way the hooks expect.
  python3 - "$COMMON/KernelSU/kernel/feature" <<'PY'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
def sub(name, old, new):
    p = d / name
    s = p.read_text()
    if s.count(old) != 1:
        sys.exit(f"ERROR: {name}: expected exactly one {old.strip()!r} (found {s.count(old)}); "
                 "the SukiSU source changed - re-check the su_compat static-key fix")
    p.write_text(s.replace(old, new))
sub("sucompat.c", "bool ksu_su_compat_enabled __read_mostly = true;\n", """#ifdef CONFIG_KSU_SUSFS
/*
 * OP13T build fix: the SUSFS hooks in fs/exec.c, fs/open.c and fs/stat.c declare this symbol as
 * `struct static_key_true` and test it with static_branch_likely(), the way susfs4ksu's own
 * 10_enable_susfs_for_ksu.patch defines it. A plain bool here is a type mismatch across
 * translation units that the compiler cannot see.
 */
DEFINE_STATIC_KEY_TRUE(ksu_su_compat_enabled);
#else
bool ksu_su_compat_enabled __read_mostly = true;
#endif
""")
sub("sucompat.c", "    *value = ksu_su_compat_enabled ? 1 : 0;\n", """#ifdef CONFIG_KSU_SUSFS
    *value = static_key_enabled(&ksu_su_compat_enabled) ? 1 : 0;
#else
    *value = ksu_su_compat_enabled ? 1 : 0;
#endif
""")
sub("sucompat.c", "    ksu_su_compat_enabled = enable;\n", """#ifdef CONFIG_KSU_SUSFS
    if (enable)
        static_branch_enable(&ksu_su_compat_enabled);
    else
        static_branch_disable(&ksu_su_compat_enabled);
#else
    ksu_su_compat_enabled = enable;
#endif
""")
sub("sucompat.h", "extern bool ksu_su_compat_enabled;\n", """#ifdef CONFIG_KSU_SUSFS
#include <linux/jump_label.h>
extern struct static_key_true ksu_su_compat_enabled;
#else
extern bool ksu_su_compat_enabled;
#endif
""")
print("su_compat: ksu_su_compat_enabled is now a static key when CONFIG_KSU_SUSFS=y")
PY
  grep -c 'ksu_su_compat_enabled' KernelSU/kernel/feature/sucompat.c KernelSU/kernel/feature/sucompat.h
}

step_susfs() {
  log "Adding SUSFS ($(grep -o 'SUSFS_VERSION "[^"]*"' "$WORK/susfs4ksu/kernel_patches/include/linux/susfs.h"))"
  cd "$COMMON"
  local S="$WORK/susfs4ksu/kernel_patches"
  local compat="$WORK/wild_builder/scripts/kernel_fix_patches/apply_kernel_compat.py"
  local sub; sub=$(sed -n 's/^SUBLEVEL = //p' Makefile)
  [ ! -e fs/susfs.c ] || die "this tree already carries SUSFS; run the 'fetch' step first (it resets the tree), then ksu and susfs"
  cp "$S"/fs/* fs/
  cp "$S"/include/linux/* include/linux/
  rm -f "$WORK/susfs-compat.json"
  python3 "$compat" --stage 1 --root "$COMMON" --kmi android15-6.6 --sublevel "$sub" --soc sun --state "$WORK/susfs-compat.json"
  if ! grep -qxF 'extern void __fold_filemap_fixup_entry(struct vma_iterator *iter, unsigned long *end);' include/linux/page_size_compat.h; then
    patch -p1 --forward --batch --fuzz=0 < "$WORK/wild_patches/common/backports/fold_fixup_entries.patch"
  fi
  patch -p1 --forward --batch --no-backup-if-mismatch < "$S/50_add_susfs_in_gki-android15-6.6.patch"
  if [ -n "$(find . -name '*.rej' -print -quit)" ]; then find . -name '*.rej'; die "SUSFS patch left rejects"; fi
  python3 "$compat" --stage 2 --root "$COMMON" --kmi android15-6.6 --sublevel "$sub" --soc sun --state "$WORK/susfs-compat.json"
}

step_hmbird() {
  if [ "$HMBIRD" != 1 ]; then log "HMBIRD patch skipped (HMBIRD=0, raw OSS scheduler)"; return 0; fi
  log "Restoring the HMBIRD (Fengchi) scheduler core (CONFIG_HMBIRD_SCHED=y, as in the stock kernel)"
  cd "$COMMON"
  ! grep -qx 'CONFIG_HMBIRD_SCHED=y' arch/arm64/configs/gki_defconfig \
    || die "this tree already carries the HMBIRD patch; run the 'fetch' step first (it resets the tree) and redo the later steps"
  sed 's/\r$//' "$WORK/wild_patches/oneplus/hmbird/fengchi_OP13T_A15.patch" > "$WORK/fengchi_OP13T_A15.patch"
  patch -p1 --forward --batch --no-backup-if-mismatch -F 3 < "$WORK/fengchi_OP13T_A15.patch" > "$WORK/hmbird_patch.log" 2>&1 \
    || { grep -E -B2 'FAILED|rejects' "$WORK/hmbird_patch.log"; die "HMBIRD patch did not apply"; }
  grep -c '^patching file' "$WORK/hmbird_patch.log" | sed 's/^/files patched: /'
  grep -E 'fuzz|FAILED' "$WORK/hmbird_patch.log" || true
  if [ -n "$(find . -name '*.rej' -print -quit)" ]; then find . -name '*.rej'; die "HMBIRD patch left rejects"; fi
  grep -qx 'CONFIG_HMBIRD_SCHED=y' arch/arm64/configs/gki_defconfig || die "gki_defconfig lacks CONFIG_HMBIRD_SCHED=y"
  # Deliberately NOT applied: WildKernels' overwriter.patch + hmbird_config.patch, which rewrite the
  # device tree at boot to force /soc/oplus,hmbird/version_type/type = HMBIRD_OGKI. The phone's own
  # device tree is left untouched.
}

step_abi() {
  # The stock kernel was built with AOSP clang r510928, which does not know the counted_by
  # attribute, so __counted_by() expands to nothing there. Clang 19 does expand it, and genksyms
  # then computes different CRCs for every exported symbol whose signature reaches an annotated
  # struct (struct icc_path -> icc_put, icc_set_bw, icc_set_tag, of_icc_get, devm_of_icc_get).
  # With CONFIG_MODVERSIONS the phone's stock modules (arm_smmu, clk_qcom, gdsc_regulator, ...)
  # would then refuse to load. Keep the macro empty, exactly as the stock compiler saw it.
  # (Upstream later disabled __counted_by for clang < 19.1.3 anyway because of compiler bugs.)
  log "Keeping __counted_by a no-op, as with the stock compiler (module CRC compatibility)"
  cd "$COMMON"
  local f=include/linux/compiler_attributes.h
  sed -i 's|^#if __has_attribute(__counted_by__)$|#if 0 /* OP13T: keep __counted_by empty like the stock compiler (genksyms CRC compatibility) */|' "$f"
  grep -q '^#if 0 /\* OP13T: keep __counted_by empty' "$f" || die "could not neutralise __counted_by in $f"
  grep -n -A4 'OP13T: keep __counted_by empty' "$f"
}

step_lkmguard() {
  if [ "$LKM_GUARD" != 1 ]; then log "LKM guard skipped (LKM_GUARD=0)"; return 0; fi
  # The phone currently runs official KernelSU in LKM mode: its init_boot carries ksuinit + kernelsu.ko.
  # If this kernel is ever booted with that init_boot still in place (stock init_boot not flashed back,
  # or a `fastboot boot` trial), a second KernelSU must not be loaded next to the built-in one.
  # ksuinit normally notices a built-in KernelSU and skips the load; this makes it certain: WildKernels'
  # patch adds a build-time list of module names the loader refuses (their OP13T-6.6.56 config lists
  # "kernelsu" too). Only that one name is used here - no vendor module is blocked. When the load is
  # refused ksuinit logs the error and continues booting. The list is inactive outside a normal boot.
  log "Module loader: refuse a module named 'kernelsu' (leftover KernelSU LKM guard)"
  cd "$COMMON"
  local p="$WORK/wild_patches/common/vendor_modules/0001-Support-conditional-vendor-modules-blacklisting-6.1-and-above.patch"
  grep -q 'CONFIG_DEBLOAT_VENDOR_MODULES' kernel/module/main.c \
    || patch -p1 --forward --batch --no-backup-if-mismatch -F 3 < "$p"
  # the patch declares a local variable it never uses; drop it so the build stays free of warnings
  sed -i '/^static bool blacklisted(const char \*module_name)$/,/^}$/{/^\tint i;$/d}' kernel/module/main.c
  grep -q 'custom_module_blacklist = CONFIG_DEBLOAT_VENDOR_MODULES' kernel/module/main.c || die "LKM guard patch did not apply"
  grep -q '^config DEBLOAT_VENDOR_MODULES' arch/arm64/Kconfig || die "LKM guard Kconfig entry missing"
  if [ -n "$(find kernel arch/arm64 -name '*.rej' -print -quit)" ]; then die "LKM guard patch left rejects"; fi
}

step_config() {
  log "Configuring (gki_defconfig + KSU/SUSFS, 4K pages, LTO=$LTO)"
  cd "$COMMON"
  # A self-built kernel cannot verify the stock GKI module signatures, so the stock
  # system_dlkm modules load as unsigned; drop the export protection that would block them.
  rm -f android/abi_gki_protected_exports_*
  : > .scmversion
  local def=arch/arm64/configs/gki_defconfig guard_cfg=""
  if [ "$LKM_GUARD" = 1 ]; then guard_cfg='CONFIG_DEBLOAT_VENDOR_MODULES="kernelsu"'; fi
  sed -i '/^# >>> op13t additions$/,/^# <<< op13t additions$/d; /^CONFIG_LOCALVERSION=/d' "$def"
  cat >> "$def" <<EOF
# >>> op13t additions
${guard_cfg}
CONFIG_LOCALVERSION="${LOCALVER}"
# CONFIG_LOCALVERSION_AUTO is not set
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y
# <<< op13t additions
EOF
  kmake gki_defconfig
  local cfg=(scripts/config --file "$OUT/.config")
  "${cfg[@]}" -d WERROR -d LTO_CLANG_FULL -d LTO_CLANG_THIN -d LTO_NONE
  case "$LTO" in
    none) "${cfg[@]}" -e LTO_NONE ;;
    thin) "${cfg[@]}" -e LTO_CLANG -e LTO_CLANG_THIN ;;
    full) "${cfg[@]}" -e LTO_CLANG -e LTO_CLANG_FULL ;;
    *) die "LTO must be none|thin|full" ;;
  esac
  kmake olddefconfig
  for want in CONFIG_ARM64_4K_PAGES=y CONFIG_KSU=y CONFIG_KSU_SUSFS=y CONFIG_MODVERSIONS=y \
              CONFIG_TMPFS_XATTR=y "CONFIG_LOCALVERSION=\"${LOCALVER}\""; do
    grep -qxF "$want" "$OUT/.config" || die "config check failed: $want"
  done
  grep -q '^CONFIG_ARM64_16K_PAGES=y' "$OUT/.config" && die "16K pages enabled"
  if [ "$HMBIRD" = 1 ]; then grep -qx 'CONFIG_HMBIRD_SCHED=y' "$OUT/.config" || die "config check failed: CONFIG_HMBIRD_SCHED=y"; fi
  if [ "$LKM_GUARD" = 1 ]; then grep -qxF 'CONFIG_DEBLOAT_VENDOR_MODULES="kernelsu"' "$OUT/.config" || die "config check failed: CONFIG_DEBLOAT_VENDOR_MODULES"; fi
  echo "config OK: $(grep -c '^CONFIG_' "$OUT/.config") options set"
  grep -E '^CONFIG_(HMBIRD_SCHED|SLIM_SCHED|SCHED_CLASS_EXT|LTO_NONE|LTO_CLANG_THIN|CFI_CLANG|KSU|KSU_SUSFS|KPM|DEBLOAT_VENDOR_MODULES)=' "$OUT/.config" | tr '\n' ' '; echo
}

kmake() {
  # -D__ANDROID_COMMON_KERNEL__ is what the official GKI build passes (build.config.common); nothing in
  # this tree tests it, it is here only so the compiler flags match the reference builds.
  local kcflags="-Wno-error -pipe -D__ANDROID_COMMON_KERNEL__"
  [ "$CPU_OPT" = 1 ] && kcflags="$kcflags -mcpu=oryon-1"
  PATH="$CLANG/bin:$PATH" make -C "$COMMON" O="$OUT" -j"$JOBS" \
    ARCH=arm64 SUBARCH=arm64 LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu- \
    LOCALVERSION= KBUILD_BUILD_USER=build KBUILD_BUILD_HOST=op13t \
    KCFLAGS="$kcflags" "$@"
}

step_build() {
  log "Building Image with $JOBS jobs"
  cd "$COMMON"
  local t0 st; t0=$(date +%s)
  rm -f "$OUT/arch/arm64/boot/Image"      # a failed build must never leave an older Image to be picked up
  set +e
  kmake Image 2>&1 | tee "$WORK/build.log" | grep -E --line-buffered 'error|Error|warning: .*susfs|KernelSU|SukiSU|SUSFS|LD +vmlinux|OBJCOPY|Kernel:'
  st=${PIPESTATUS[0]}                     # the exit status of make itself, not of tee/grep
  set -e
  [ "$st" -eq 0 ] || { tail -n 60 "$WORK/build.log"; die "make exited with status $st"; }
  [ -f "$OUT/arch/arm64/boot/Image" ] || { tail -n 60 "$WORK/build.log"; die "Image was not produced"; }
  echo "built in $(( $(date +%s) - t0 ))s: $(stat -c %s "$OUT/arch/arm64/boot/Image") bytes; compiler warnings: $(grep -c 'warning:' "$WORK/build.log" || true)"
  # awk reads the whole stream, so no SIGPIPE under `set -o pipefail` (grep -m1 / head would cause one)
  strings "$OUT/arch/arm64/boot/Image" | awk '/^Linux version/ && !seen { print; seen = 1 }'
  # Guard for the su_compat fix in step_ksu and for the same class of mistake elsewhere: every
  # static-key use in the linked kernel must refer to a real 16-byte static key.
  python3 "$TOOLS_DIR/statickeys.py" "$OUT/vmlinux" ksu_su_compat_enabled ksu_is_init_rc_hook_enabled ksu_is_input_hook_enabled \
    || die "static-key consistency check failed (driver and kernel hooks disagree about a variable)"
}

step_collect() {
  log "Collecting artifacts into $DIST"
  rm -rf "$DIST"; mkdir -p "$DIST"
  cp "$OUT/arch/arm64/boot/Image" "$DIST/Image"
  cp "$OUT/.config" "$DIST/kernel.config"
  cp "$OUT/vmlinux.symvers" "$DIST/vmlinux.symvers" 2>/dev/null || cp "$OUT/Module.symvers" "$DIST/vmlinux.symvers"
  cp "$OUT/System.map" "$DIST/System.map"
  # the symbol lists the module loader of THIS kernel enforces (generated from android/abi_gki_aarch64*)
  cp "$OUT/include/generated/gki_module_unprotected.h" "$OUT/include/generated/gki_module_protected_exports.h" "$DIST/"
  grep -E 'SukiSU-Ultra version|SUSFS_VERSION' "$WORK/build.log" | sort -u > "$DIST/ksu_version.txt" || true
  local rel; rel=$(strings "$DIST/Image" | awk '/^Linux version/ && !seen { print $3; seen = 1 }')
  [ -n "$rel" ] || die "could not read the kernel release from the Image"
  rm -rf "$WORK/ak3"; cp -r "$WORK/AnyKernel3" "$WORK/ak3"; rm -rf "$WORK/ak3/.git"
  cp "$DIST/Image" "$WORK/ak3/Image"
  ( cd "$WORK/ak3" && zip -q -r9 "$DIST/AnyKernel3_SukiSU-Ultra_${rel}.zip" . )
  {
    echo "kernel_release=$rel"
    echo "kernel_source=$KERNEL_URL @ $KERNEL_REF"
    echo "sukisu=$SUKISU_URL @ $SUKISU_REF (branch builtin; version identity pinned to v$SUKISU_TAG / KSU_VERSION $((40000 + SUKISU_MAIN_COMMITS - 2815)) to match the manager of that release)"
    echo "susfs=$SUSFS_URL @ $SUSFS_REF"
    echo "local_changes=su_compat switch defined as a static key (matches the SUSFS hooks); __counted_by kept empty (module CRCs); lkm_guard=$LKM_GUARD"
    echo "compiler_warnings=$(grep -c 'warning:' "$WORK/build.log" || true)"
    echo "compat_helper=$WILD_URL @ $WILD_REF ; $WILDP_URL @ $WILDP_REF"
    echo "anykernel3=$AK3_URL @ $AK3_REF"
    echo "clang=$("$CLANG/bin/clang" --version | sed -n 1p)"
    echo "lto=$LTO cpu_opt=$CPU_OPT hmbird=$HMBIRD lkm_guard=$LKM_GUARD"
    ( cd "$DIST" && sha256sum Image ./*.zip )
  } > "$DIST/BUILD_INFO.txt"
  cat "$DIST/BUILD_INFO.txt"
}

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(deps fetch ksu susfs hmbird abi lkmguard config build collect)
for s in "${steps[@]}"; do "step_$s"; done
log "Done"
