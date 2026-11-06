# macOS Mode 1 CD-R 读取计时实测

2026-11-07，在 Apple SuperDrive（固件 2.00）上，用提交
`897436f5953731ad44832077ae30359d3094ce36` 的 `geo` 测量一张已封盘的单会话、单轨
Verbatim Digital Vinyl AZO Mode 1 CD-R。光驱将已封盘介质报告为 `0x0008`（CD-ROM）。
先卸载目标光盘卷，让 macOS helper 取得独占访问。将 `DRIVE` 换成 `discprobe list` 显示的设备名。

```sh
python3 -m discprobe geo --drive DRIVE --speed 1411 --ranges 1500-359440 --reps 32 --max-targets 36
```

| 项目 | 实测值 |
|---|---|
| 确认可读的 LBA 范围（结束不含） | `[0, 359694)` |
| 梯子测量范围（结束不含）及参考 LBA | `[1500, 359440)`，参考 180470 |
| `READ(12)` 缓存测试中位耗时 | 普通 1.4435 ms，FUA 57.2548 ms，选择 FUA |
| 末次参考周期 | 28.70227 ms |
| 测点与拟合残差 RMS | 36 / 36，0.18494° |
| 测量跨度角度估计 1σ | 0.12073° |
| `u = r0/b`，`v = p/b` | 89452.67393，5.40317287 |

日志中的 2,424 次 `READ(12)` 均成功，且都位于确认的可读范围内。108 帧是读取尾部的模型假设。
这些结果描述相对螺旋几何，绝对沿轨偏移与绝对通道位长仍未测定。

从仓库根目录运行下面的命令，可用 [targets.csv](targets.csv) 中的舍入后测点复算拟合与跨度不确定度。
CSV 按测量顺序保留 LBA、相位、解缠角度、拟合权重对应的 σ 和参考周期。

```sh
python3 - <<'PY'
import csv
from discprobe.geo import Ladder

ladder = Ladder(None)
ladder._set_model("cd")
ladder.ref = 180470
ladder.n_ref = ladder._n_end(ladder.ref)
with open("evidence/mac/cd-r/targets.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        ladder.targets.append({"n": ladder._n_end(int(row["lba"])),
                               "unwrapped_deg": float(row["unwrapped_deg"]),
                               "sigma_deg": float(row["sigma_deg"])})
        ladder.T = float(row["period_ms"]) / 1000
ladder.fit()
result = ladder.result()
for key in ("u", "v", "residual_rms_deg", "angle_sigma_measured_span_deg"):
    print(key, result[key])
PY
```
