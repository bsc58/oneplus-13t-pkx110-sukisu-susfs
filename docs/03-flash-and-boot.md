# 03 · 刷入与首次启动

> 前提:Bootloader 已解锁;系统固件版本与你**构建所用的原厂镜像一致**(中途装过 OTA 就不匹配,别刷);手里有本仓库产出的 `boot_sukisu_13t.img` 和一份**原厂 `init_boot`**。

本机是 A/B 分区。下面以活动槽 `b` 为例;**先用 `fastboot getvar current-slot` 确认你的槽位**,把命令里的 `_b` 换成你的槽。

## 关于 LKM → 内置 的切换(重要)

如果你现在是**官方 KernelSU 的 LKM 模式**,你的 `init_boot` 里带着 `ksuinit` + `kernelsu.ko`。换成**内置 SukiSU 的内核**时,要**同时把 `init_boot` 刷回原厂**——否则等于两套 KernelSU 并存。本仓库的内核加了一道保险(拒绝加载名为 `kernelsu` 的模块),但正确做法仍是刷回原厂 `init_boot`。

## 第 0 步:手机还正常时,先做三件事

1. **确认按键能进 Bootloader。** 进不了系统时 adb 不可用,只能靠按键。13T 资料互相矛盾,**两种都试、记住哪种有效**:
   - 关机后同时按住 **音量上 + 音量下 + 电源**;
   - 或 **电源 + 音量下**。
   确认进入的是 **fastboot / Bootloader** 界面(不是 recovery)。

2. **备份 KernelSU 数据目录。** 新内核一启动(包括下面的临时启动)就会把授权列表 `/data/adb/ksu/.allowlist` 改写成 SukiSU 格式,之后一旦回到官方 KernelSU 可能读不回。先留一份:

   ```bash
   adb shell su -c "cp -a /data/adb/ksu /data/adb/ksu.bak-official"
   ```

3. **记住两种会让模块「全部被停用」的情况**(只是停用,不是删除),下面要连续重启好几次,都容易误触发——见本页末尾「模块被停用怎么办」。

## 第 1 步(可选,强烈建议):不写分区,临时启动试一次

```bash
adb reboot bootloader
fastboot getvar current-slot          # 应显示 b;不是就停下,后面命令按你的槽改
fastboot boot boot_sukisu_13t.img
```

- `fastboot boot` 只把内核**载入内存启动,不写任何分区**;重启后内核就回到原厂。但 `/data` 里的授权列表会像第 0 步说的那样被转换。
- 它在 13T 上**不保证可用**(有机型会报错或直接启动原系统——那样也没损失)。
- 若成功进系统:`adb shell uname -r` 应显示 `6.6.56-android15-8-g...-sukisu-4k`。**趁机检查 Wi-Fi、蓝牙、移动网络、相机、指纹、充电、NFC 是否正常。**
- 注意这一次启动是**过渡状态**(旧 `ksud` + 新内核),带 SELinux 规则的模块(如 Zygisk Next、LSPosed)多半**暂时不工作**,属正常;重启回原内核即可。
- 卡住不动:长按电源(不行再加音量上)强制重启,回到原状态。

## 第 2 步:正式刷入

```bash
adb reboot bootloader
fastboot getvar current-slot                              # 必须是 b,否则停下
fastboot flash init_boot_b init_boot_stock.img            # 原厂 init_boot(去掉 KSU LKM)
fastboot flash boot_b   boot_sukisu_13t.img               # 新内核
fastboot reboot
```

- 两条 `flash` 都显示 **OKAY** 之后再 `reboot`。任何一条报错:**不要重启**,先重试同一条;仍不行就留在 fastboot 里按 [`05-recovery.md`](05-recovery.md) 刷回。
- 顺序是**先 init_boot 后 boot**;停在中间任何一步,手机都是一套能启动的组合。
- 想先核对文件:`Get-FileHash boot_sukisu_13t.img`(PowerShell)或 `sha256sum`,与你构建时记录的哈希对比。

## 第 3 步:开机后

1. 安装 **SukiSU 管理器 v4.3.0-r2**(版本原因见 [`02-build-kernel.md`](02-build-kernel.md)),打开一次(它会装入自己的 `ksud`),**等约 4 分钟**再**重启一次**。这次重启前仍是过渡状态(旧 `ksud` + 新内核),重启后才由新 `ksud` 接管,进入正常状态。
2. 在管理器里核对 root 授权(新内核会自动迁移授权列表);卸载官方 KernelSU 管理器(`me.weishu.kernelsu`)。
3. **你原有的 KernelSU 模块不会丢、也不用重装。** 它们在 `/data/adb/modules`,刷 boot / init_boot 不碰 `/data`;SukiSU 的 `ksud` 用同一个目录、同一套启用/停用约定,状态原样保留。
4. 管理器里的「su 兼容」保持**开启**(修正后这个开关才真正生效)。
5. 接着做 [`04-hide-root.md`](04-hide-root.md):加 SUSFS `sus_path` 藏 `su`,并配 HideMyApplist。

## 模块被停用怎么办

不是内核把模块删了,多半是这两种之一触发:

- **救砖类模块的开机计数**:有的「自动救砖」模块会数开机次数——连续几次开机都没撑够一段时间(比如没到 3–4 分钟、或卡开机动画),它就把其余模块**全部停用**并自动重启(这是它在帮你排除问题模块)。所以**每次进系统后等满约 4 分钟再重启或进 Bootloader**。
- **音量键安全模式**:开机过程中音量键被按满 3 次会进入 KernelSU 安全模式,这次开机停用所有模块。不想触发就别在开机时连按音量键。

触发后模块都还在,**在管理器里把原先启用的重新打开**即可。如果是某个救砖脚本把自己权限改成了 000,按那个模块的说明把权限改回去(通常 `chmod 700`)。
