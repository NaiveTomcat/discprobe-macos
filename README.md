# discprobe-macos

在 macOS 上，用光驱**自己**测出一张已刻录光盘的螺旋几何：每个扇区在盘面上的转角 θ(N)，
进而得到内半径 r0 与轨距 p，直接导出 DiscImageStudio 可用的预设。

不用扫描仪，不用拍照描点，不用标定板。做法是让光驱反复读同一张盘，记录每条命令**读完的时刻**：
在恒定转速（CAV）下，读完时刻的主轴相位就是读块末端在盘面上的角度，与寻道时间无关。
再像爬梯子一样逐级外推，把这些对 360° 取模的角度解缠成整盘的转角曲线，最后拟合成 u = r0/b、
v = p/b 两个比值（b 是通道位长）。

- **零第三方依赖。** 测量、拟合、导出全部是 Python 3.10+ 标准库；唯一需要编译的是 IOKit 直通用的
  小 C helper（`tools/mac/`），第一次运行时自动构建，只需要 Xcode 命令行工具。
- **只读。** 只有 14 个读类 CDB 在允许列表里；写盘、封盘、格式化命令在到达设备前就被拒掉。
- **实测过的。** MATSHITA BD-MLT UJ260AF（USB）+ RITEKF1 DVD-R：22 级全部收敛，
  24–58 mm 内角度不确定度 **0.174°（1σ）**，见下面的数字。

## 为什么需要这个

DVD 的标称几何是"内半径 24.0 mm、轨距 0.74 µm、位长 133.3 nm"。但刻录机的写速控制和空白盘的
ATIP 决定了**实际**的螺旋，每批盘、每台机器都不一样。偏差 1% 听起来很小，累计到整张盘就是上千度：

这台机器上实测的这张盘，u、v 与标称值相比，在 24–58 mm 范围内累计转角差了 **52218°（约 145 圈）**。
按标称几何去刻图案，图案会糊成一片；按测出来的几何去刻，24–58 mm 内的角度误差是 0.17°。

DiscImageStudio 用"内半径 + 外半径 + 扇区数 + 三次轨距形状"描述一张盘，和本工具的常轨距
（u, v）模型不是同一族，所以 `disexport` 会把结果重新拟合成 DIS 的预设。

## 快速开始

两条路都行，**都不需要 root，也不要加 `sudo`**：

- **用 release 包**（推荐，不用任何编译器）：下载 `discprobe-macos-<版本>.tar.gz` 解开即可。包里的
  `tools/mac/build/discprobe-spt` 是编译好的 arm64 + x86_64 通用二进制，已用 Developer ID 签名并公证。
- **从源码跑**：`git clone` 之后第一次运行会自动用 clang 编译这个 helper，这时需要 Xcode 命令行工具
  （`xcode-select --install`）。

两种都只要 Python 3.10+（macOS 12.3 起系统不再预装 Python 3；装了 Xcode 工具链或自己装一个即可）。

```bash
git clone https://github.com/NaiveTomcat/discprobe-macos
cd discprobe-macos

python3 -m discprobe list              # 看驱动器：disk10  MATSHITA BD-MLT UJ260AF 1.01
python3 -m discprobe info --drive disk10
```

从源码跑时 `list` 第一次会自动编译 helper（`tools/mac/build.sh`）；用 release 包时那一步已经有现成的二进制。

**如果 macOS 拦住了 helper**（下载来的文件带 quarantine 标记，Gatekeeper 会拒跑命令行二进制，即使已签名公证）：
程序会自己清掉那个下载标记并在 stderr 打一行提示，不需要 `sudo`，也不会改动文件内容。手动等价命令是
`xattr -d com.apple.quarantine tools/mac/build/discprobe-spt`。

## 一张盘的标准流程

需要一张**已经刻录并封盘**的盘，最好写满。盘上写什么数据不重要（随机数据最稳），空白区读不出东西。
没有现成的盘可以用 `python3 -m discprobe random --output iso/random.iso` 生成一个写满随机数据的镜像，
再用 `hdiutil burn iso/random.iso` 刻到 DVD±R 上。

```bash
# 1. 确认前提：缓存绕过 + 转速恒定（约 2 分钟）
python3 -m discprobe timing --drive disk10 --sectors 16 --reps 24

# 2. 测几何（约 4 分钟，22 级）
python3 -m discprobe geo --drive disk10 --max-targets 22 --reps 24

# 3. 导出 DiscImageStudio 预设
python3 -m discprobe disexport \
    --geo results/geo-disk10-<时间戳>/summary.json \
    --sectors 2297888 --r-min 25.8 --r-max 54.5 \
    --output results/geo-disk10-<时间戳>/dis_preset.json
```

每一步的结果都写在 `results/` 下按时间命名的目录里。

**测量期间这张盘不能挂载**，也不能有别的程序在读它：helper 要拿到独占访问，会先用 DiskArbitration
卸一次盘；还失败就手动 `diskutil unmountDisk /dev/disk10`。同一台驱动器同时只能有一个 discprobe 进程。

### Mode 1 数据 CD

对已经封盘的单会话、单数据轨 Mode 1 CD，可直接运行 `python3 -m discprobe geo --drive disk10`。
`geo` 自动识别介质；CD 默认请求 1411 kB/s，仍需要光驱实际保持恒定转速（CAV）。
光驱必须报告可确认的轨道类型和已记录范围。探测、预热、缓存测试和驱逐都限制在该范围内；
`--ranges` 可进一步限制梯子的测量点（结束 LBA 不包含在内）。音频、混合模式、Mode 2 和多会话 CD 暂不支持。

CD 每扇区有 98 个 588 位通道帧（57,624 位）。`geo` 以扇区末尾再加 108 个 CIRC 帧作为读取完成位置的模型，
参见 [Ashley-QwQ/disc-probe 的 CD 物理模型](https://github.com/Ashley-QwQ/disc-probe/blob/934bb784202dfadc420aa39355a0f31ac08e7614/docs/CD_PHYSICAL_MODEL.md)。
结果是测量跨度内的相对螺旋几何；读取计时不能单独确定绝对沿轨偏移。CD 结果不支持 `disexport`。

## 三个实验在做什么

### `timing`

1. **cache**：对同一个 ECC 块连读 40 次，比较普通读与带 FUA 读。中位耗时低于 3 ms 说明数据来自
   驱动器缓存——那么相位就不可信。本机实测：普通读 1016 µs，FUA 50665 µs，所以用 FUA。
2. **period**：在内、中、外三个区重复读同一扇区，用周期图从完成时刻估出一圈的周期 T。
   三个区一致就是 CAV，相位法才成立；随半径变大就是 CLV，要换一个读速档再测。
3. **phase**：交替读参考扇区 R 和目标 X，测 X 的完成时刻相对 R 的主轴相位。随机寻道时间不影响它。

**注意 FUA 重读会固定花整数圈。** 本机 50.6655 ms ÷ 25.3826 ms = 1.996，即每次重读固定停在**两圈**上，
所以那个 50 ms 不是"读一圈的时间"，也不能当读速用。

### `geo`（梯子）

1. 先稳住转速（反复读参考扇区，直到周期不再变化）。
2. 第一级测离参考 1.5 圈和 4 圈的扇区：标称模型在这么近的距离只差几度，由此定出解码延迟常数 c。
3. 每测一级就拟合一次 u、v、c（对 u、v 加宽松先验），然后挑"预测误差小于 25°"的候选里离参考最远的
   作为下一级；整圈数由预测决定。每一级都让转速更准，可达距离按指数增长，约 20 级覆盖整个可读半径。
4. 若某一级的残差或 χ²/dof 爆掉，说明整圈数错了，程序停下而不是硬拟合。

### `disexport`

把测出的角度映射重新拟合成 DiscImageStudio 的"内半径 + 外半径 + 三次轨距"族，并报告两者之间的
最大角度差，用来判断这份预设能不能直接用。

## 本机实测（2026-11-04，MATSHITA BD-MLT UJ260AF / USB）

盘：DVD-R，MID `RITEKF1`，2,297,888 扇区（4.38 GiB），写满随机数据并已封盘。
完整的数据、命令和结论见 [`evidence/mac/README.md`](evidence/mac/README.md)。

| 量 | 值 |
|---|---|
| 缓存 / 读取策略 | plain 1016 µs、FUA 50665 µs → `fua` |
| 主轴 | 25.3826 ms 一圈（2363.8 rpm），三区一致 → **CAV** |
| 相位噪声 | 单次 0.72–1.10°，均值标准误 0.15–0.23°（24 次平均） |
| 梯子 | 22 / 22 级，残差 rms 0.206°，χ²/dof 0.89，c = 0.011° |
| u = r0/b | 180518.475 ± 0.015 |
| v = p/b | 5.5912078 ± 0.0000006 |
| 内半径 r0（位长 133.3 nm） | **24.06311 mm**（标称 24.0） |
| 轨距 p（同位长） | **0.745308 µm**（标称 0.74） |
| 角度不确定度 | 实测跨度 25.756–54.486 mm：0.146°；外推 24–58 mm：**0.174°（1σ）** |
| 换参考扇区重测 | 与第一次相差 **0.279°**（两次之差应约 √2σ = 0.246°） |
| DIS 预设 | 内 24.0631 mm、外 58.2273 mm（对应 2,297,888 扇区），与实测角度图最大差 **0.0010°** |

判断精度时按 **0.2° 量级**用：报告的 1σ 偏乐观约 15%（模拟里也是这个量级）。

## 适用范围

校准是**逐盘**的，因为螺旋由刻录机的写速控制与空白盘共同决定：

```
(空白盘批次) × (这台刻录机) × (这个写速档) × (这次封盘方式)
```

- 同一批空白盘、同一台刻录机、同一个写速 → 可以直接复用；逐盘复测只是确认一致性（结果相差应在 0.3° 以内）；
- 换空白批次、换刻录机、换写速 → 必须重测，差 1% 就是几千度；
- 盘上写什么数据不影响几何，只影响能不能稳定读出。

## 边界

- **只测已刻录区。** 空白盘没有可读数据，`timing` 会直接报错退出。
- **必须是 CAV。** 低速档在有些驱动器上是 CLV，这时相位不是盘面角度，程序会打印警告；
  换一个读速档（`--speed`）再测。
- **USB 光驱的转速档可能被钳住。** 本机请求 4x 也还是 2363.8 rpm，不影响方法，但别指望它按你要的速度转。
- **DVD 为主。** CD 的块长和轨道间距不同，`--r0-mm/--pitch-um`、`--sectors` 都要相应改；蓝光未验证。
- **macOS 后端只在这个型号的 USB 驱动器上验证过。** 见 [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md) §12。

## 文件结构

```
discprobe/__main__.py   命令行：list / info / timing / geo / disexport / random / selftest
discprobe/macscsi.py    传输层：驱动 tools/mac 的 helper，拿回数据和纳秒时间戳
discprobe/scsi.py       结果类型、sense 解析、只读操作码白名单
discprobe/mmc.py        MMC 命令构造与解析（CDB、profile、TOC、容量…）
discprobe/timing.py     cache / period / phase 三个实验
discprobe/analysis.py   周期图、圆统计、相位提取
discprobe/geo.py        梯子测量与 u/v/c 拟合
discprobe/calib.py      螺旋几何模型 θ(n)、协方差报告
discprobe/linalg.py     3×3 以内的纯 Python 线性代数
discprobe/disexport.py  重拟合为 DiscImageStudio 预设
discprobe/sim.py        模拟驱动器（CAV/CLV、缓存、FUA、抖动、离群值）
discprobe/selftest.py   不接硬件的全套自检
tools/mac/              IOKit 传输层 helper（C，600 行）与构建脚本
evidence/mac/           本机实测的原始数据与报告
docs/KNOWLEDGE.md       方法原理、硬件实测、踩过的坑（含更广的研究笔记）
```

## 自检

不接任何硬件：

```bash
python3 -m discprobe selftest        # 模拟驱动器上跑完整流程，并检查解析器和预设导出
python3 -m discprobe geo --sim cav   # 只跑梯子，用模拟驱动器
```

模拟器会给出真实几何，自检直接与之对比：CAV 下相位误差 0.8–2.2°、梯子在全盘 85% 范围内约 1°，
与报告的不确定度一致。

## 许可与来源

GPL-3.0-or-later，见 [LICENSE](LICENSE)。

本项目是 [Ashley-QwQ/disc-probe](https://github.com/Ashley-QwQ/disc-probe) 的 macOS 分支：
测量方法与分析代码来自上游；这个仓库只保留在 macOS 上跑校准所需的部分，并新增
macOS 后端（[`discprobe/macscsi.py`](discprobe/macscsi.py) + [`tools/mac/`](tools/mac/)）。
上游仓库里还有配合平板扫描仪的真值工具（辐条盘、纹理环盘、多假设标定盘）、
刻图与颜色工具，以及 Windows 的 SPTI 后端。
