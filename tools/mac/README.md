# macOS 传输层：`discprobe-spt`

`discprobe-spt` 是 discprobe 在 macOS 上的 SCSI 直通层。Python 侧的对应文件是 `discprobe/macscsi.py`，它和 Windows 的 `discprobe/spti.py` 承担同样的角色：发一条裸 CDB、拿回数据和 sense、带回时间戳。所有"测什么、怎么算"的逻辑仍然在 Python 里（`timing.py`、`geo.py`、`analysis.py`），这个 helper 只负责 IOKit 那一段管道。

平时**不需要手动编译或调用**它：`python3 -m discprobe ...` 发现 `tools/mac/build/discprobe-spt` 不存在时，会自动执行一次 `tools/mac/build.sh`。

## 为什么需要它

macOS 没有 `IOCTL_SCSI_PASS_THROUGH`。要发一条裸 CDB 只能走 IOKit 的 SCSITaskLib，而它需要一段 CoreFoundation/IOKit 的样板代码和一次独占握手。更麻烦的是，光驱的逻辑单元已经被内核的多媒体驱动占用：直接找 SCSITask factory 通常拿不到接口（`kIOReturnUnsupported`），得先取 MMC user client（`kIOMMCDeviceUserClientTypeID`），再从它拿 `SCSITaskDeviceInterface`。把这些封进一个小 C 程序、Python 只跟它对帧，比在 Python 里绑 IOKit 省事得多。

## 构建

需要 Xcode 命令行工具（`clang` 和 macOS SDK 头文件）。没装就先：

```bash
xcode-select --install
```

然后：

```bash
sh tools/mac/build.sh
# built: /…/disc-probe/tools/mac/build/discprobe-spt
```

脚本只有一条 clang：`-O2 -Wall -Wextra -std=c11`，链接 `IOKit`、`CoreFoundation`、`DiskArbitration` 三个框架。产物在 `tools/mac/build/`（已在 `.gitignore` 里忽略）。

不想放在默认位置时，用环境变量 `DISCPROBE_SPT` 指向任意一份编译好的 helper：

```bash
DISCPROBE_SPT=/tmp/discprobe-spt python3 -m discprobe list
```

设了 `DISCPROBE_SPT` 但路径不存在时**不会**自动编译，会直接报错。

## 两个子命令

```bash
tools/mac/build/discprobe-spt devices
tools/mac/build/discprobe-spt serve [--device diskN]
```

- `devices`：把 IORegistry 里找得到的光驱打成 JSON 数组，每项有 `name`（BSD 名，如 `disk10`）、`class`、`vendor`、`product`、`revision`。`python3 -m discprobe list` 用的就是它。
- `serve`：打开并独占指定驱动器（不给 `--device` 时取第一台），之后在 stdin/stdout 上说下面的帧协议，直到收到退出命令或管道断开。打开成功时在 stderr 打一行 `discprobe-spt: diskN ready`；失败时 stderr 给出原因，退出码 3（独占失败）或 4（找不到驱动器）。

## 协议

双向、小端、定长帧：stdin 只进请求，stdout 只出响应。没有分隔符，每一侧都必须一次读满。

**请求，28 字节**

| 偏移 | 类型 | 含义 |
|---|---|---|
| 0 | u8 | 命令：`0` = 执行一条 CDB，`1` = 退出 |
| 1 | u8 | CDB 长度（上限 16） |
| 2 | u8 | 方向：`0` = 无数据，`1` = 写向设备（一律拒绝），`2` = 从设备读 |
| 3 | u8 | 保留，填 0 |
| 4 | u32 | 数据长度（上限 128 KiB） |
| 8 | u32 | 超时，毫秒（填 0 按 30000 处理） |
| 12 | u8[16] | CDB，不足 16 字节补 0 |

**响应，32 字节**；方向为 `2` 时后面紧跟 `transferred` 个数据字节。

| 偏移 | 类型 | 含义 |
|---|---|---|
| 0 | i32 | `IOReturn`（0 = 内核这层成功） |
| 4 | u8 | SCSI task status（`0xFF` = helper 自己拒绝，没到设备） |
| 5 | u8 | sense key |
| 6 | u8 | ASC |
| 7 | u8 | ASCQ |
| 8 | u64 | 命令开始时间戳，ns |
| 16 | u64 | 命令结束时间戳，ns |
| 24 | u32 | 实际传输字节数 |
| 28 | u32 | 保留 |

几条约定：

- **时间戳只在这里打。** `t_start` / `t_end` 是 `mach_absolute_time()` 在 `ExecuteTaskSync` 调用前后各取一次，再用 `mach_timebase_info` 换成纳秒。它们跨过内核和 USB/SATA 往返，但不含 Python 侧的调度，所以回到 Python 后不再重打。
- **只读白名单查两遍。** C 侧的操作码表与 `discprobe/scsi.py` 的 `ALLOWED_OPCODES` 相同（14 个）。请求里的 opcode 不在表里、或方向是写向设备时，helper 不碰设备，直接回 `kIOReturnUnsupported` 和 `task_status = 0xFF`。
- 数据长度超过 128 KiB 会被截断（`MAX_TRANSFER`）；超出部分不会发给驱动器。
- 一台机器最多认 16 台驱动器（`MAX_DEVS`）。

## 签名、公证与 Gatekeeper

release 里的 `discprobe-spt` 用 Developer ID Application 证书签了名（hardened runtime + 时间戳），
并把整份二进制提交给 Apple 公证服务（`notarytool`，状态 Accepted）。

但**签名公证并不能免除 Gatekeeper 对命令行二进制的拦截**：公证票据无法 staple 到一个裸可执行文件里，
所以从浏览器下载的副本仍带 `com.apple.quarantine`，第一次执行会被直接 SIGKILL。`macscsi._runnable()`
的处理是：发现这个标记就先 `xattr -d` 清掉（不需要任何权限），清不掉（例如目录只读）就把字节复制一份到
`~/Library/Caches/discprobe/` 再执行。两种情况都会在 stderr 说明，且不改动下载文件的内容。

从 `git clone` 或 `curl` 拿到的文件没有这个标记，不会触发。

## 独占与权限

- 打开时调用 `ObtainExclusiveAccess`。失败就先通过 DiskArbitration 把这块盘卸掉（最多等 6 秒），然后再试一次。
- **不需要 root。** 实测 MATSHITA BD-MLT UJ260AF（USB）在普通用户下即可独占，不要加 `sudo`。
- 盘在打开的那一刻必须能卸载：有别的程序正读这张盘（访达里打开着、有程序在扫描）时会失败。
- 同一个驱动器同一时间只给一个 `serve` 进程。已被别的 discprobe 占着时，新进程多半在 `devices` 那一步就看不到它（该驱动器的 IOMedia 一起消失，见下面故障排查第一行），而不是卡在独占上。

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `python3 -m discprobe list` 打印 `no optical drives found`，但 `drutil status` 仍看得到驱动器 | helper 靠 IOMedia 的 `BSD Name` 认盘，没有 IOMedia 的 service 会被跳过。盘没放进托盘、介质还没就绪，或另一个 discprobe 进程正独占这台驱动器时，都会这样（实测：`geo` 跑到一半时 `list` 是空列表）。先确认盘放好了、没有别的 discprobe 在跑。 |
| 独占失败，stderr 里带 `still mounted`（形如 `exclusive access to diskN failed: 0x…`） | 盘还挂着，或有别的进程在用。`diskutil unmountDisk /dev/diskN` 之后再试（`0x…` 是 `kIOReturnBusy` 之类的内核返回码）。 |
| `diskN did not hand out a SCSITaskDeviceInterface` | IORegistry 里这台设备的形态和预期不符（常见于非光驱或异常的 USB 桥接）。看 `ioreg -r -c IOBDServices` 里有没有 MMC user client。 |
| `no optical drive found` / `requested drive not found`（退出码 4） | 找不到驱动器，或 `--device` 的名字和 `devices` 输出里的 `name` 对不上。`--device` 只认 BSD 名，不认 `10`、`/dev/rdisk10` 这类写法（那几种是 Python 侧 `--drive` 的便利写法，由 `macscsi.normalise_spec` 转成 `disk10`）。 |
| `clang: error: …` 或头文件找不到 | 没装 Xcode 命令行工具：`xcode-select --install`，装完重跑 `sh tools/mac/build.sh`。 |
| `DISCPROBE_SPT points at …, which does not exist` | 环境变量指错了。改对，或者清掉它，让 Python 用默认路径并自动编译。 |

## 实测记录（2026-11-04，MATSHITA BD-MLT UJ260AF，USB）

DVD-R RITEKF1，2297888 扇区，普通用户，盘未挂载。原始数据已收进 `evidence/mac/`（完整运行目录在 `results/`）：

- `python3 -m discprobe list` → `disk10     MATSHITA BD-MLT UJ260AF 1.01`。
- `python3 -m discprobe info --drive disk10`（`evidence/mac/disc_info.json`）跑完 17 条命令：profile DVD-ROM、MID `RITEKF1`、last LBA 2297887；只有 ATIP、DVD+R ADIP 和 BD disc information 三条报不支持（介质本身没有，sense 5/24/00）。
- `python3 -m discprobe timing --drive disk10 --sectors 16 --reps 24`（`evidence/mac/timing_summary.json`）：
  - 缓存：普通重读 P50 **1016.2 µs**（P95 1058.9 µs，缓存命中），带 FUA P50 **50665.5 µs**（P95 50806.2 µs，读盘），策略 `fua`。Windows 那边 3 ms 的缓存判断门槛在这里同样适用。
  - 转速：内、中、外三个区的周期 25.38263 / 25.38274 / 25.38196 ms（**2363.8 rpm**），coherence 0.9999 / 0.9999 / 0.9922，抖动 0.872 / 0.649 / 0.710°，判定 **CAV**。
  - 相位：5 个目标单次标准差 0.72–1.10°，均值标准误 0.15–0.23°（n = 23–24）；参考扇区抖动 0.60–0.83°。
  - 50.6655 ms ÷ 25.3826 ms = 1.996：FUA 重读固定落在整圈上，这里是**两圈**，不能当"一圈耗时"用。
- `SET CD SPEED` 4x（5540 kB/s）生效（`evidence/mac/geo_run1/summary.json` 的 `set_cd_speed`，status 0）。
- 整条 `geo` 跑通（同一份 geo summary，22 级见同目录 `targets.csv`）：u = 180518.475、v = 5.5912078（r0 24.063 mm、轨距 0.7453 µm @ 133.3 nm），残差 rms 0.206°，测量范围内角度不确定度 0.146°（1σ），chi2/dof 0.89。

`devices` 的厂商、型号要试两组键名：块存储设备节点上是 `Vendor Name` / `Product Name`，它下面的 `IOSCSILogicalUnitNub` 上是 `Vendor Identification` / `Product Identification`。只查前一组时这台 USB 驱动器只剩 `Product Revision Level`，`list` 会打印成 `disk10     1.01`；现在两组都试（沿 IORegistry 往上取第一个非空值）。
