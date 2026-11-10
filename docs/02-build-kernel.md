# 02 · 构建内核(SukiSU Ultra + 内置 SUSFS)

> 脚本见 [`build/build_13t.sh`](../build/build_13t.sh)(构建)与 [`build/pack_verify.sh`](../build/pack_verify.sh)(离线校验 + 打包)。本页解释**配方和为什么**;确切实现以脚本为准。

## 需要什么

- **Linux**(实测 WSL2 Ubuntu 24.04)。源码**必须放在 Linux 文件系统里**(不能在 `/mnt/c` 下,需要大小写敏感的文件系统)。
- 约 1–2 GB 下载(内核源、susfs、补丁、AnyKernel3、ZyC Clang ≈720 MB)、几 GB 编译空间、几分钟编译时间(多核约 5 分钟)。
- **你自己手机的原厂 `boot.img`**(校验/打包用),见 [`05-recovery.md`](05-recovery.md) 的导出方法。

## 钉死的输入(本次使用的提交)

| 组件 | 仓库 / 分支 | 提交 | 说明 |
|---|---|---|---|
| 内核源 | `OnePlusOSS/android_kernel_common_oneplus_sm8750`,分支 `oneplus/sm8750_v_15.0.2_oneplus_13t` | `b1fee232c2b5` | = `PKX110_15.0.2.103(CN01)`,Linux 6.6.56。这是 `OnePlusOSS/kernel_manifest` 的 `oneplus_13t.xml` 指定分支里**离固件 15.0.2.108 最近**的提交 |
| SukiSU Ultra | `SukiSU-Ultra/SukiSU-Ultra`,分支 `builtin` | `1cfa57340590` | 内置 SUSFS 的内核驱动,**UAPI 6**;该提交本身是上游对 "mount_hide 导致桌面崩溃"(issue #1035)的修复 |
| SUSFS | `gitlab.com/simonpunk/susfs4ksu`,分支 `gki-android15-6.6` | `eb162d1a32d1` | **SUSFS v2.3.0**,与上面 `builtin` 提交接口吻合的最新内核侧 |
| 兼容补丁 | `WildKernels/OnePlus_KernelSU_SUSFS` | `10f9741e` | 让 SUSFS 补丁能套到 OnePlus 的 android15-6.6 树上 |
| 补丁集 | `WildKernels/kernel_patches` | `b6690c6c` | 提供风驰(HMBIRD)补丁、模块黑名单补丁、backport |
| AnyKernel3 | `palazik/AnyKernel3` | `fcb882be` | 可选,打 AnyKernel3 包用 |
| 工具链 | ZyCromerZ Clang | `19.0.0git-20240723` | ColorOS/OxygenOS 流程用的 Clang 19 |

> **给其他固件版本的人:** 把 `KERNEL_REF` 改成 `OnePlusOSS` 仓库里**与你固件小版本最接近的提交**(`build_13t.sh` 里 `KERNEL_REF` 是环境变量,可覆盖)。其余组件对 13T / android15-6.6 / 4K 通用。固件差太多时,厂商模块的符号集合会变,务必跑 `pack_verify.sh` 的离线校验再刷。

## 配套管理器(确切版本,很重要)

- **必须用 `SukiSU_v4.3.0-r2`**(2026-11-05 的官方 release):
  `https://github.com/SukiSU-Ultra/SukiSU-Ultra/releases/download/v4.3.0-r2/SukiSU_v4.3.0-r2_41150-release.apk`
- **为什么不能随便钉版本:** 管理器的 `isFullFeatured()` 和 `ksud` 都要求 **内核 UAPI 版本 == 管理器 UAPI 版本**。`builtin` 这个提交是 **6**;管理器 v4.3.0 / r1 / r2 也是 6。而 **v4.2.0 是 2、v4.2.1 是 5**,会被判不匹配。本构建把内核上报的版本号钉成 `41150`(与 r2 一致),管理器就不会提示版本不符。
- 内核靠 **APK 的签名证书**认管理器(内核里内置的是 SukiSU 官方发布签名),所以**用官方 release 包**,不要用 PR 构建或改过包名的版本。

管理器里两处在此内核上不可用,属正常:**「AVC 日志伪装」**(`builtin` 没这功能,开了会失败,保持关闭)和 **KPM**(本内核未启用 `CONFIG_KPM`,**不要**用管理器对这个 boot 做 KPM 修补)。

## 两个关键修正(配方的精华)

### ① `__counted_by` 必须保持空宏 —— 否则不开机

原厂内核用 AOSP Clang r510928 编译,它**不认识** `__counted_by` 属性,宏展开为空。而 Clang 19 **会**展开它,导致 `genksyms` 给凡是签名里碰到被标注结构体(如 `struct icc_path`)的导出符号算出**不同的 CRC**——`icc_put`、`icc_set_bw` 等 5 个符号 CRC 变了,`arm_smmu`、`clk_qcom`、`gdsc_regulator` 等 **23 个厂商模块会拒绝加载 → 不开机**。

修法:把 `include/linux/compiler_attributes.h` 里的 `#if __has_attribute(__counted_by__)` 关掉,让宏保持为空,**和原厂编译器看到的一样**。(上游后来也因编译器 bug 对 Clang < 19.1.3 禁用了它。)

### ② `ksu_su_compat_enabled` 要改成静态键 —— 真实缺陷

SUSFS 的内核补丁(`fs/exec.c`、`fs/open.c`、`fs/stat.c`)把 `ksu_su_compat_enabled` 声明为 `struct static_key_true` 并用 `static_branch_likely()` 测试;而 SukiSU `builtin` 分支把它定义成 **1 字节的 bool**。编译期发现不了(不同编译单元 + 无 LTO),但**开机时 `jump_label_init()` 会把这个 bool 和相邻变量当成 16 字节静态键,往上面写一个指针**,并且管理器里「关闭 su 兼容」开关失效。

修法:当 `CONFIG_KSU_SUSFS=y` 时,把它定义成 `DEFINE_STATIC_KEY_TRUE(ksu_su_compat_enabled)`,读写改用 `static_key_enabled()` / `static_branch_enable/disable()`,和 hooks 期望的一致。确切改动见 [`build/build_13t.sh`](../build/build_13t.sh) 的 `step_ksu`。

> 构建脚本的 `build` 步骤会用 [`tools/statickeys.py`](../tools/statickeys.py) 做兜底检查:链接后的内核里**每一处静态键引用**都必须指向一个真正的 16 字节静态键对象。修正前它会报出恰好这 3 处,修正后为 0。

## 关键配置项

`step_config` 在 `gki_defconfig` 之上追加(节选):

```
CONFIG_LOCALVERSION="-android15-8-g<commit>-sukisu-4k"
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y          # 隐藏路径(藏 su 靠它)
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
CONFIG_TMPFS_XATTR=y
```

构建脚本会**强制校验**:`CONFIG_ARM64_4K_PAGES=y`、`CONFIG_KSU_SUSFS=y`、`CONFIG_MODVERSIONS=y`、`CONFIG_LOCALVERSION` 正确,且**没有** `CONFIG_ARM64_16K_PAGES=y`(选错页大小是最常见的 bootloop 原因之一)。LTO 默认 `none`(原厂就是 `CONFIG_LTO_NONE=y`)。

风驰(HMBIRD / Fengchi)调度核心:原厂是 `CONFIG_HMBIRD_SCHED=y` 的 OGKI 构建,开源树只有旧的 SLIM_SCHED,所以套 WildKernels 的 `fengchi_OP13T_A15.patch` 把它补回来;**不**套它改写设备树的补丁(`HMBIRD=1` 默认开启)。

## 怎么跑

```bash
# 一次性装 WSL(管理员终端):
wsl --install -d Ubuntu-24.04

# 然后(把 <REPO> 换成本仓库在 WSL 里的路径,如 /mnt/c/.../oneplus-13t-pkx110-sukisu-susfs):
wsl -d Ubuntu-24.04 -u root -- bash <REPO>/build/build_13t.sh
# 把你自己手机导出的原厂镜像放到 <REPO>/stock/ 下(见下),再:
wsl -d Ubuntu-24.04 -u root -- bash <REPO>/build/pack_verify.sh
```

`build_13t.sh` 的步骤:`deps`(apt 依赖)→ `fetch`(按提交拉源码)→ `ksu`(SukiSU + 修正 su 兼容类型)→ `susfs` → `hmbird` → `abi`(`__counted_by` 置空)→ `lkmguard`(内核拒绝加载名为 `kernelsu` 的模块,防两套 KSU 并存)→ `config` → `build` → `collect`。可覆盖的环境变量:`WORK`(默认 `~/op13t`)、`KERNEL_REF`、`JOBS`、`LTO`(none/thin/full)、`HMBIRD`(1/0)、`LKM_GUARD`(1/0)、`CPU_OPT`(1=`-mcpu=oryon-1`)。

## 离线校验(`pack_verify.sh` 做了什么)

它**不连手机**,用你放进 `stock/`(原厂镜像)和 `device/`(从手机只读导出的事实)的数据核对新内核,任一项失败就非零退出、**不生成 `release/`**。核对包括:

- Image 头声明 4K 页,`CONFIG_ARM64_4K_PAGES=y`,版本串带 `-4k`;
- 手机上**全部厂商模块需要的内核符号:全部导出、CRC 全部一致**;`module_layout` CRC、`sizeof(struct module)`、vermagic 逐模块一致;
- 模块加载器的其它拒绝规则(GPL、符号命名空间、重复导出、未签名模块只能用白名单符号);
- 用 **BTF 比对真实结构体布局**(`task_struct`、`rq`、`mm_struct`、`inode`、`module` 等逐成员);
- boot.img **仅替换内核**,头部其余字段、AVB 属性原样保留;用 AOSP `unpack_bootimg` 独立解析,取出的内核与编译产物逐字节相同。

> `stock/` 和 `device/` 的内容需要你**从自己的手机导出**(root + adb 只读)。[`tools/modcheck.py`](../tools/modcheck.py) 能提取模块事实。**这些文件是设备私有的,不要提交到 git**(`.gitignore` 已排除)。只做构建、不做完整校验的话,`build_13t.sh` 本身就能产出 Image;但**强烈建议**至少用自己的 `stock/boot_b.img` 走一遍 `pack_verify.sh` 的符号校验再刷。

## 已知且不影响开机的差异

- 7 个 system_dlkm 模块(6 个 kunit 测试 + `9pnet_fd`)用到白名单外符号,自编内核无法验证原厂签名会拒绝它们并打 `Protected symbol` 日志——但**没有任何模块依赖它们**,且它们各自独立加载,互不连累。所有自编 GKI 内核都有此现象。
- 风驰补丁是社区对原厂实现的还原:共用数据位置全部吻合,但原厂每个调度窗口的一个回调、`sched_yield` 限流未接上(厂商模块对空回调有判断,不崩溃)。**游戏模式(风驰开启)下调度表现可能与原厂不同**;平时 `scx_enable=0` 不受影响。

## 重新构建的可复现性

所有依赖都钉到了具体提交,SukiSU 版本号钉死(不再在编译时请求 GitHub API),`fetch` 会把源码树还原到干净状态。同样的输入应得到同样的内核。
