# OnePlus 13T (PKX110) · SukiSU Ultra + SUSFS + 隐藏 root

一份**已在真机验证可行**的完整记录:为 **OnePlus 13T(PKX110 / 代号 OP60F5L1,Snapdragon 8 Elite / SM8750 "sun",国行 ColorOS 15 / Android 15,GKI `android15-6.6`,4K 页)** 构建带**内置 SUSFS** 的 **SukiSU Ultra** 内核、刷入、并对检测工具(Momo / Ruru)和普通 app 隐藏 root 与 Xposed。

> **English summary (for searchers):** There is no ready-made SukiSU-Ultra + SUSFS prebuilt that matches the OnePlus 13T (PKX110) on China ColorOS 15. This repo documents how one was built from pinned sources, the two build fixes that actually matter (`__counted_by` module-CRC compatibility, and the `ksu_su_compat_enabled` static-key type mismatch between the SukiSU `builtin` branch and the SUSFS hooks), how to flash it with a full fastboot rollback path, and the operational tricks to actually hide `su` — SUSFS `sus_path` for `/system/bin/su`, plus the crucial gotcha that **SUSFS deliberately does not hide from root-granted apps**.
>
> **Keywords:** OnePlus 13T, PKX110, OP60F5L1, SM8750, Snapdragon 8 Elite, ColorOS 15, Android 15, GKI android15-6.6, 4K pages, SukiSU Ultra, SUSFS, KernelSU, Zygisk Next, LSPosed, HideMyApplist, hide root, Momo, Ruru, Su File, bootloop, fastboot, root detection bypass, 隐藏 root, 刷机.

---

## ⚠️ 风险与免责(先读这段)

- 刷内核有**变砖风险**。本记录的作者在一台真机上成功刷入并长期使用,但**你的固件小版本、模块组合都可能不同**,成功案例不是保证。
- **13T 的 EDL(9008)需要厂商鉴权的 Firehose**,民间几乎救不回(基本只能找付费线刷服务)。所以**唯一可靠的安全垫 = 已解锁的 Bootloader + fastboot + 你自己备份的原厂 `boot` / `init_boot`**。动手前务必自己备份,见 [`docs/05-recovery.md`](docs/05-recovery.md)。
- **不要升级到 ColorOS 16.0.3.501 及以上**。社区反馈称从该版本起启用了硬件防回滚,之后再刷旧固件会**直接变砖**。
- 本仓库通过 [Releases](https://github.com/bsc58/oneplus-13t-pkx110-sukisu-susfs/releases/latest) 提供**预编译的 AnyKernel3 内核刷机包**(仅含内核、刷入时打到你**自己当前的 `boot`** 上,不含任何原厂整机镜像)。它与固件小版本、GKI 版本强绑定:**只适用于 PKX110 对应固件,刷错机型/固件可能不开机甚至变砖**。拿不准就按 [`docs/02-build-kernel.md`](docs/02-build-kernel.md) **以你自己手机的原厂 `boot.img` 为基底**重新构建(boot 头、AVB 属性、厂商模块符号都和你的固件版本绑定)。
- 一切后果自负。本仓库仅供学习与互操作性研究使用,不含任何担保(provided as-is, no warranty)。

## 下载 / 预编译刷机包

不想自己编译,预编译内核放在 [**Releases**](https://github.com/bsc58/oneplus-13t-pkx110-sukisu-susfs/releases/latest):

- 文件:`AnyKernel3_SukiSU-Ultra_6.6.56-android15-8-gb1fee232c2b5-sukisu-4k.zip`
- SHA256:`F8F0A72509CF6B996CE11BEEBC4B586DEBE5851F1AEC1A3CDE1B772D0E0D45B3`
- 这是 **AnyKernel3** 包(仅含内核 Image + 刷入脚本),刷入时把内核打到你**自己当前的 `boot`** 上,不含任何原厂镜像。
- **仅适用于 OnePlus 13T(PKX110)对应的 GKI 固件**(作者机为 15.0.2.108 CN01)。此包关闭了设备校验,**刷错机型/固件会变砖**——动手前务必先备份 `boot`(见 [`docs/05-recovery.md`](docs/05-recovery.md))。
- 刷入与回滚步骤见 [`docs/03-flash-and-boot.md`](docs/03-flash-and-boot.md);已 root 的可直接用 Kernel Flasher 刷这个 zip。

## 这是给谁的

如果你有一台 **OnePlus 13T(PKX110,国行 ColorOS 15)**、已解锁 Bootloader,想要:

1. 用 **SukiSU Ultra**(内核级 root,内置 SUSFS)替代普通 KernelSU;
2. 让 **Su File / `su` 路径**这类检测失效(SUSFS 在 VFS 层隐藏 `/system/bin/su`);
3. 让 **Momo / Ruru / 普通 app 查不出 root 和 Xposed**;

那么这里记录了一条已验证可行的完整路线,以及一路上所有的坑。

## 能达到什么效果

作者机器上最终的实测结果:

- **Ruru 12 项全绿**(含「环境检测 / Su File」),重启后依旧;
- 普通 app、包名可见性检测、Xposed 模块列表检测**查不出**(靠 HideMyApplist)。

## 快速开始

1. 读 [`docs/01-overview-and-risks.md`](docs/01-overview-and-risks.md) —— 原理、你会遇到什么、能到什么程度。
2. **先备份!** 按 [`docs/05-recovery.md`](docs/05-recovery.md) 把你自己的 `boot` / `init_boot` 导出留底。
3. 构建内核:[`docs/02-build-kernel.md`](docs/02-build-kernel.md)(用 [`build/build_13t.sh`](build/build_13t.sh),WSL2 / Linux)。
4. 刷入与首启:[`docs/03-flash-and-boot.md`](docs/03-flash-and-boot.md)。
5. 隐藏 root:[`docs/04-hide-root.md`](docs/04-hide-root.md)(SukiSU per-app + SUSFS `sus_path` + HideMyApplist)。
6. 长期注意事项:[`docs/06-cautions.md`](docs/06-cautions.md)(防回滚、OTA、模块更新)。

## 仓库结构

```
docs/      分步指南(中文)
build/     build_13t.sh      构建内核(WSL2 / Linux)
           pack_verify.sh    离线校验 + 打包 boot.img + 生成 release/
           wsl_run.sh        从 Windows 调用的小包装
tools/     Python 校验工具:boot 镜像解析/重打包、模块符号 CRC 比对、
           BTF 结构体布局比对、静态键一致性检查、config diff
scripts/   zzz_susfs_hide.sh 开机持久化 SUSFS sus_path 的脚本
```

**本仓库不含**任何分区镜像(`*.img`)、设备取证数据(`device/`、`stock/`)、root 授权名单或 app 列表——这些要么属于设备私有,要么**必须由你自己的手机生成**。`.gitignore` 已经把它们挡在外面,避免你不小心把隐私数据提交上去。

## 构建依据(一句话版)

| 组件 | 取值 |
|---|---|
| 内核源 | `OnePlusOSS/android_kernel_common_oneplus_sm8750`,分支 `oneplus/sm8750_v_15.0.2_oneplus_13t`,**钉到与你固件最接近的提交**(本例 `b1fee232` = `PKX110_15.0.2.103(CN01)`,用于固件 15.0.2.108) |
| Root | SukiSU-Ultra `builtin` 分支(UAPI **6**)@ `1cfa5734` |
| 管理器 | **必须 v4.3.0-r2**(UAPI 版本要与内核一致:v4.2.0=2、v4.2.1=5 会被判不匹配) |
| 隐藏 | susfs4ksu `gki-android15-6.6` @ `eb162d1a`(**v2.3.0**) |
| 两个关键修正 | ① `__counted_by` 保持空宏(否则 23 个厂商模块 CRC 不符拒绝加载 → 不开机)② `ksu_su_compat_enabled` 改静态键(SukiSU `builtin` 与 SUSFS 补丁的类型不一致) |
| 页大小 | **4K**(`CONFIG_ARM64_4K_PAGES=y`;多数 SM8750 构建默认 16K,选错直接 bootloop) |

详见 [`docs/02-build-kernel.md`](docs/02-build-kernel.md)。

## 致谢 / 上游项目

- [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra)
- [susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu)(SUSFS)
- [KernelSU](https://github.com/tiann/KernelSU)
- [WildKernels](https://github.com/WildKernels)(SUSFS 兼容补丁、风驰/HMBIRD 补丁)
- [palazik / actions_oplus_sm8750](https://github.com/palazik)(SM8750 构建工作流参考)
- [HideMyApplist](https://github.com/Dr-TSNG/HideMyApplist)
- 检测工具:Momo(`io.github.vvb2060.mahoshojo`)、Ruru(`com.byxiaorun.detector`)

本仓库**自身的脚本与文档**以 MIT 许可发布(见 [`LICENSE`](LICENSE));它所**构建的内核**及上述上游项目各自保留其原许可(内核为 GPLv2 等)。

---

> 如果这份记录帮到了你:它来自一次把一台 13T 从「能被检测到 root」做到「Ruru 全绿且持久」的真实折腾过程。路是通的,但每一步都请先备份、先看懂、再动手。
