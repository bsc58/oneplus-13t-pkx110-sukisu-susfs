# 01 · 原理、现状与风险

## 设备

- OnePlus 13T,型号 **PKX110**,代号 **OP60F5L1**
- SoC Qualcomm **SM8750**(Snapdragon 8 Elite),平台 `sun`,ARM64
- 国行 **ColorOS 15 / Android 15 / API 35**,A/B 分区机型
- 内核 GKI **`android15-6.6`**,**4K 页**(原厂内核标识形如 `6.6.30-android15-8-g...-abogki...-4k`)
- Bootloader 需**已解锁**(`fastboot getvar unlocked` = yes;`verifiedbootstate` = orange)

## 要解决的检测

以 **Momo**(`io.github.vvb2060.mahoshojo`)和 **Ruru**(`com.byxiaorun.detector`)为代表,root/Xposed 检测大致分三类:

1. **`su` 文件 / 路径(Su File)**:普通 app 直接 `stat("/system/bin/su")`。只要这个文件在磁盘上可见就会被查出。
2. **包名可见性**:扫描 `org.lsposed.manager`、`com.tsng.hidemyapplist`、root 管理器包名等是否安装。
3. **Xposed 模块列表 / 框架特征**:读取已激活的 Xposed 模块、`me.weishu.*` 之类特征。

## 为什么普通 KernelSU + umount 不够

- **Su File 不是挂载点**。在本机上 `/system/bin/su`(`root:shell`,约 314 KB)的 `stat %m` 落在 `/`(`dm-22`),`/proc/mounts` 里也没有 `/system/bin` 的 overlay。它是**真实存在于只读系统分区里的文件**,不是模块叠上去的挂载,所以 **KernelSU 的「卸载模块挂载」(umount)清不掉它**。
- 要在不改系统分区的前提下让它「消失」,只能在**内核 VFS 层**做手脚——这正是 **SUSFS 的 `sus_path`** 做的事:对非特权进程,让指定路径在 `stat`/`openat` 等系统调用里查无此物。
- 包名可见性和 Xposed 模块列表,SUSFS **不管**;那是 **HideMyApplist(HMA)** 在 `PackageManager` 层做的事。两者分工不同,都要。

结论:**SukiSU Ultra(内置 SUSFS)负责藏 `su`,HideMyApplist 负责藏包名/模块列表。** 缺一不可。

## 为什么要自己编内核

SUSFS 是**内核级**功能,必须编进内核。而:

- **没有现成的预构建**精确匹配「13T + 国行 ColorOS 15 + 6.6.56 + 4K」。能搜到的 SukiSU+SUSFS 预构建基本是 OnePlus 13(PJZ110)/ Ace 系列、OxygenOS 导向,或默认 16K 页。
- 页大小选错(16K)在 4K 固件上**直接 bootloop**。
- 内核源码版本若与固件不匹配,厂商模块会因**符号 CRC 不符**拒绝加载,**不开机**——这是社区里绝大多数不开机报告的根因。

所以本仓库的核心是一条**把所有依赖钉到具体提交、并做了离线符号校验**的构建配方,让你能为**自己的固件版本**编出一个大概率能开机的内核。详见 [`02-build-kernel.md`](02-build-kernel.md)。

## 能达到什么

- Ruru 全部类别通过(含 Su File / 环境检测),且重启后持久;
- 普通 app、银行类 app 的常规 root 检测、包名扫描、Xposed 模块扫描查不出。

## 风险清单(动手前逐条确认)

| 风险 | 说明 | 对策 |
|---|---|---|
| 刷错内核不开机 | 页大小/源码版本不匹配、模块 CRC 不符 | 用本仓库配方 + 先 `fastboot boot` 临时启动试(不写分区) |
| 变砖且进不去 fastboot | 极端情况;13T 的 EDL 需鉴权 Firehose | **提前确认按键能进 Bootloader**;备份好原厂镜像 |
| 丢 root 授权 | 新内核会把 `.allowlist` 转成 SukiSU 格式 | 先 `cp -a /data/adb/ksu /data/adb/ksu.bak-official` 留底 |
| 模块被全部停用 | 救砖模块计数 / 音量键安全模式误触发 | 见 [`03-flash-and-boot.md`](03-flash-and-boot.md) 的「模块被停用」小节 |
| OTA 覆盖内核 | 系统自动升级会覆盖 boot、掉 root | 关闭自动更新;升级前先刷回原厂,见 [`06-cautions.md`](06-cautions.md) |
| 硬件防回滚 | ColorOS 16.0.3.501+ 之后降级变砖 | **不要升到该版本及以上** |

**在继续之前,请先完成 [`05-recovery.md`](05-recovery.md) 里的备份步骤。** 没有备份,不要刷。
