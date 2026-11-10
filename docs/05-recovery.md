# 05 · 备份与回滚(救砖)

> **这一页是整件事的安全垫,先做再刷。** 13T 的 EDL(9008)需要厂商鉴权的 Firehose,民间基本救不回,所以**唯一可靠的后路 = 解锁的 Bootloader + fastboot + 你自己备份的原厂 `boot` / `init_boot`**。

## 一、先备份你自己的分区(刷之前做)

需要 root + adb。把分区按名字 dump 出来(**只读,不改手机**):

```bash
# 确认当前槽位(a 或 b)
adb shell getprop ro.boot.slot_suffix        # 形如 _b

# 以当前槽 b 为例,导出关键分区(换成你的槽):
adb exec-out su -c "dd if=/dev/block/by-name/boot_b"        > boot_b.img
adb exec-out su -c "dd if=/dev/block/by-name/init_boot_b"   > init_boot_b.img
# 另一个槽的原厂 init_boot(通常是纯净无 root 的,留着回「完全原厂」用):
adb exec-out su -c "dd if=/dev/block/by-name/init_boot_a"   > init_boot_a.img
```

> Windows + Git Bash 下给 adb 传 `/dev/...` 路径前先 `export MSYS_NO_PATHCONV=1`,并用 `adb exec-out`(二进制安全)而不是 `adb pull`。

记下每个文件的 `sha256sum`,和**刷机前再 dump 一次**的结果对比,确保备份完整。把这些 `.img` 存到**手机以外**的安全位置。**不要把它们提交到 git / 传到公开仓库**——它们是你设备私有的。

你至少需要:

| 文件 | 用途 |
|---|---|
| `boot_<slot>.img` | 原厂内核,回滚用 |
| `init_boot_<slot>.img` | 你当前的 init_boot(若已 root,含 KSU LKM),回到「当前状态」用 |
| 原厂纯净 `init_boot`(无 root) | 回到「完全原厂」或装 OTA 前用 |

## 二、怎么进 Bootloader(fastboot)

- 系统能进 + adb 可用:`adb reboot bootloader`
- 系统进不去(bootloop):**关机**,然后试
  - **音量上 + 音量下 + 电源**,或
  - **电源 + 音量下**
  见到 fastboot / Bootloader 界面**松手**。注意:有的界面**几秒无操作会自动重启**,命令要提前备好。
- 确认连上:`fastboot devices`;`fastboot getvar current-slot`。

## 三、回滚命令

> 先 `fastboot getvar current-slot` 确认槽位,把 `_b` 换成你的槽。

### 目标 A:回到刷机前的状态(原厂内核 + 你原来的 root)

```bash
fastboot flash boot_b      boot_b.img                  # 你备份的原厂 boot
fastboot flash init_boot_b init_boot_b.img             # 你备份的当前 init_boot
fastboot reboot
```

### 目标 B:回到完全原厂、无 root(想彻底干净,或装 OTA 前)

```bash
fastboot flash boot_b      boot_b.img
fastboot flash init_boot_b <原厂纯净 init_boot>.img
fastboot reboot
```

## 四、注意

- **刷后不开机 / 卡 logo / 反复重启** → 进 fastboot → 目标 A。
- **开机但功能异常**(相机/网络/指纹等)→ 进 fastboot → 目标 A。
- **两条 flash 中间断了** → 重进 fastboot 把两条都刷一遍;中间态也能开机。
- **绝不** `fastboot flashing lock`(在改过的状态下上锁会直接变砖)。
- **不要指望自动切槽救场**:系统更新合并后,另一个槽通常已无法独立启动。
- **连 fastboot 都进不去(极少)**:13T 的 EDL 需厂商鉴权,基本只能找付费线刷服务。这也是为什么强烈建议先做 [`03-flash-and-boot.md`](03-flash-and-boot.md) 的 `fastboot boot` 临时启动。
- **授权丢了**:回到官方 KernelSU 后在管理器里重新授权;想恢复整份列表,先给 Shell 授权再
  `adb shell su -c "cp /data/adb/ksu.bak-official/.allowlist /data/adb/ksu/.allowlist"`,然后立刻重启。

## 五、个人数据

以上所有操作只动 `boot` / `init_boot` 分区,**不碰 `userdata`**;app、照片、设置始终原样。(唯一的例外是 `/data/adb/ksu` 里的授权列表格式会被新内核转换,见上。)
