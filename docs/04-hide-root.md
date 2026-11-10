# 04 · 隐藏 root 与 Xposed

分工(见 [`01-overview-and-risks.md`](01-overview-and-risks.md)):

- **SUSFS(内核)** 藏 `su` 文件/路径;
- **HideMyApplist(HMA,Xposed 模块)** 藏包名可见性 + Xposed 模块列表。

整套栈:**SukiSU Ultra → Zygisk Next(Zygisk 提供者)→ LSPosed → HideMyApplist**。

---

## 一、SUSFS:藏掉 `su`

### 最关键的坑:已授 root 的 app 被 SUSFS 豁免

**SUSFS 故意不对「你已经授予 root 的 app」隐藏任何东西。** 所以:

> 你想骗过的每一个 app(检测器 Momo/Ruru、银行 app 等),在 SukiSU 管理器里都必须设为**拒绝 root(默认)**,**不要给它们授 root**。

作者一开始就栽在这:给 Ruru 授了 root → 加了 `sus_path` 它照样看得见 `su`;把 Ruru 改成非 root 后立刻变绿。**如果你加了路径还是被检出,先查是不是给检测器授了 root。**

### 加 sus_path

```bash
adb shell su -c "/data/adb/ksud susfs add-sus-path /system/bin/su"
```

这让非特权进程在 `stat`/`openat` 等调用里**查不到** `/system/bin/su`(Su File 检测的根因)。

### 持久化(重要)

**SukiSU 自带的 `susfs_manager` 模块只持久化 uname/日志开关,不持久化 `sus_path`**——重启后你加的路径就没了。要靠开机脚本补回。本仓库提供 [`scripts/zzz_susfs_hide.sh`](../scripts/zzz_susfs_hide.sh):

```bash
adb push scripts/zzz_susfs_hide.sh /data/local/tmp/
adb shell su -c 'cp /data/local/tmp/zzz_susfs_hide.sh /data/adb/post-fs-data.d/ && \
  chown root:root /data/adb/post-fs-data.d/zzz_susfs_hide.sh && \
  chmod 700 /data/adb/post-fs-data.d/zzz_susfs_hide.sh'
```

`post-fs-data.d` 里的脚本每次开机早期由 `ksud` 执行,把 `sus_path` 重新加上。脚本里可按需追加更多路径。

---

## 二、HideMyApplist:藏包名和 Xposed 模块列表

HMA 是 Xposed 模块,需要 **LSPosed 已启用**(LSPosed 跑在 Zygisk Next 上)。它在 `PackageManager` 层对指定 app 隐藏「哪些包已安装」,顺带也能挡掉对 Xposed 模块列表的扫描。

### 设置步骤

1. 在 LSPosed 里**启用 HideMyApplist**,重启使其生效。
2. 打开 HMA,**建一个模板**(Template),在模板的黑名单里加入你想隐藏的包,典型几类:
   - root 管理器:`com.sukisu.ultra`(以及历史上的 `me.weishu.kernelsu`、`io.github.a13e300.ksuwebui` 等)
   - Xposed:`org.lsposed.manager`
   - 隐藏工具本身:`com.tsng.hidemyapplist`
   - 其它你不想被看到的管理/调试类:如 `moe.shizuku.privileged.api`(Shizuku)等
3. **把模板套到每一个「作用域 app」**(scope,即你想骗过的 app:检测器、银行等)。
   > **最常见的错误**(作者踩过):模板建好了,但每个作用域的「应用模板」是空的,等于**没挂到任何 app**、对谁都不生效。务必确认每个作用域 app 下都勾选了你的模板。
4. HMA 的服务在**开机时读取配置**,改完**重启手机**才稳。

### 验证

- **Ruru**:包名/Xposed 模块应全部通过。
- **Momo**:包名相关项通过;若把 Momo 自己也加进 HMA 作用域导致它报「包管理服务异常」,把 Momo 移出作用域即可。

---

## 三、整体验收

重启后用 Ruru 跑一遍,目标是**所有类别通过**(含「环境检测 / Su File」),且**重启后依旧**。普通 app / 银行 app 的常规 root 检测、包名扫描、Xposed 扫描应查不出。

如果某项仍红:

1. Su File 还在 → 查检测器是不是被授了 root(见上「最关键的坑」);或 `sus_path` 没持久化(查开机脚本)。
2. 包名/模块被看到 → 查 HMA 模板是否真的套到了该作用域 app;改完有没有重启。
