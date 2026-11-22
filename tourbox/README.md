# tourbox：巡展跳听盒差分母带成品仓

把混音母带做成巡展“跳听盒”的**流式离线交付**。纯 Python 3.11 标准库实现
（唯一可选的 numpy 依赖只在旧仓适配器 `adapters.py` 里；封版、解码、核对、
计划与设备复播全部在 numpy 不可用时照常工作，见 `tests/test_bundle.py`）。

与旧 `audiomix` 仓的关系：只消费其**不可变 render**（已渲染 PCM + 发布产品 +
块追溯），不读取编辑器私有操作日志。一条母带可以对应单条旧 render，也可以把
两段（或多段）旧 render 按明确顺序拼成一条新带。

## 目录

```
tourbox/
  varint.py    LEB128/zigzag 变长整数（任意精度，差值 ±65535 不回绕）
  tbm.py       差分母带记录流（TBM1）：两类记录的流式编码/解码/设备起播
  pcmio.py     裸 int16 PCM/WAV 读写、规范 JSON、SHA-256 摘要
  master.py    封版：已渲染 PCM + 原 render + 采样范围 + 来源身份 + 接缝
  verify.py    离线核对：流还原、候选片头复播、跨原块接缝、来源对应、防篡改
  planner.py   跳听卡/复位点布局：价值最大 → 加权预读最小（精确分支限界）
  plan.py      销售计划封版、设备侧重解复核、逐卡复播、具名 clip 锚点
  adapters.py  旧 audiomix ArchiveStore -> 来源段（本仓唯一可选 numpy 边界）
  cli.py       命令行入口
tests/         宽整数/封版核对/计划锚点测试（断言式，无第三方测试框架）
demo/          题面例子与双来源版本的端到端 CLI 走查
run_all_checks.sh
```

## 设备合同：两类记录

记录流 `master.tbm` 以 4 字节魔数 `TBM1` 开头，其后每条记录为
`kind(varint) + value(zigzag 有符号 varint)`：

| kind | 含义 | value 合法域 | 还原方式 |
|----|----|----|----|
| 0 差分 | 相对**前一个已解码样本**的差值 | `[-65535, 65535]` | `s[i] = s[i-1] + delta` |
| 1 复位 | 本处绝对样本值，脱离上文可起播 | `[-32768, 32767]` | `s[i] = value` |

- 所有加减在 Python 任意精度整数上完成，**禁止先压进 16 位**：
  `32767 → -32768` 的差是 `-65535`，窄类型回绕会错误地得到 `1`
  （`tests/test_wide_int.py::test_wide_delta_no_int16_wrap`）。
- 无上文遇到差分记录、越界差值/复位值、还原后超出 int16，一律拒绝。
- 只有编带师**批准的片头**（清单 `approved_reset_positions`，必含起点 0）
  允许写复位；实际刻录点 `burned_reset_positions` 必须是其**子集**。
  编码器不自行决定任何复位点，封版后不可改写——临时请求只能申请母带新版本。

## 来源身份：同 PCM 不同授权绝不并源

每个来源 run 的身份输入是

```json
{"render_id", "sample_range":[lo,hi], "clip_id", "occurrence", "license_id"}
```

的规范 SHA-256（`provenance_id`），**刻意不含任何 PCM/波形摘要**。因此两段
授权不同的讲解即使逐样本相同，身份也必然不同；设备核对的是“输出样本 ↔ 这条
具名来源”的对应，而不是按内容反查来源。`occurrence` 消歧同一音效的反复使用：
沿具名 clip 找片头时只跟随该卡签下的那一次。

## 交付目录与离线核对

```
manifest.json      自摘要清单（刻录/批准复位点、provenance runs、接缝、render 索引）
master.tbm         差分母带记录流
master.pcm         已渲染 PCM（封版时逐样本等于 tbm 还原值，双向自证）
renders/<id>.json  原 render 元数据与全文摘要
renders/<id>.wav   随包原 render（可选；存在即逐字节核对）
```

`verify` 在一台没有编辑器、没有 numpy 的设备上完成：

1. 清单/tbm/pcm 三个 SHA-256 自摘要（任何篡改离线可发现）；
2. 逐记录流式还原 == 已渲染 PCM，实际复位位置 == 刻录点 ⊆ 批准片头；
3. **每个批准候选片头**按真实跳听语义复播：取最近的不晚于目标的已刻录复位点
   `r`，解码后丢弃 `t-r` 个已还原样本，核对发出的实际 WAV 段；
4. 跨原块接缝：不复位处的差分在宽整数域核对（含 ±65535）；
5. 逐 run 把母带样本区间与随包原 render 的 `[lo,hi)` 对应；
6. 同 PCM 的 run 身份可区分性检查。

## 跳听卡布局优化

- 起点 0 已含复位（不占位置），另有 K 个候选，预算 B（题面例 B=1）。
- 卡内每个跳点 `t` 带点播次数 `w` 与预读上限 `L`；服务复位点
  `r = max{复位点 ≤ t}`，可兑现 iff `t-r ≤ L`。**全部跳点可兑现才能售整张卡**；
  连听走现有状态，不占新位置。
- 精确分支限界枚举候选子集：先最大化可售卡总价值；等值布局再最小化
  加权预读 `Σ w·(t-r)`；再持平取字典序最小布局，保证可复跑。
- 计划同样封版（自摘要），设备侧 `verify-plan` 会**重解同一个优化问题**
  复核最优性，并对每张已售卡逐跳只凭交付文件复播；未售卡给出具体不能承诺的
  跳点（目标、上限、最近可用复位点、实际预读）。

### 题面例子的精确复算（`demo/cli_walkthrough.py`）

起点 0 已复位，候选 {4,8} 选一个：

| 卡 | 跳点(上限) | 值 | 刻 4 | 刻 8 |
|----|----|----|----|----|
| 甲 | 5 (L=1) | 4 | r=4，预读1 ✓ | r=0，预读5 ✗ |
| 乙 | 9 (L=1) | 6 | r=4，预读5 ✗ | r=8，预读1 ✓ |
| 丙 | 5(L=1), 9(L=5) | 5 | 预读1、5 ✓ | 跳点5 预读5 ✗ |

- 刻 4：售甲+丙 = **9**；刻 8：只售乙 = 6 → 选刻 4，乙的跳点 9 留痕不可承诺。
- 乙价值提到 10：10 > 9 → **改刻 8**（需要编带师出母带新版本；旧计划与旧售出
  包仍可原样复核）。

## 节目调整与双来源版本

`tests/test_planner.py::test_two_source_versions_same_samples_moved_named_reference`
专门演示：两版母带 PCM 逐字节相同，仅把其中一个具名引用改挂到另一授权来源。

- **旧售出包**按母带样本位置订立：两版位置合同一致，设备逐跳复播结论不变；
- **按源位置生成的新卡**经 `anchor` 解析：跟随 `(clip_id, occurrence,
  source_sample)` 落到新版本的正确 provenance（授权随引用更新），同音效的另一
  次引用不会串台；引用在母带中移动时新片头位置随之改变，找不到则明确
  “不能承诺”，而不是静默换一条同字节内容。

## 命令速览

```bash
# 封版（规格 JSON：segments 给 render_id/范围/clip/occurrence/授权 + 样本正文）
python3 -m tourbox.cli build --spec spec/build.json --out out/bundle
# 离线全量核对
python3 -m tourbox.cli verify --bundle out/bundle
# 布局计划（卡目录 JSON：候选位置、预算、每张卡的跳点/次数/上限/价值）
python3 -m tourbox.cli plan --bundle out/bundle --cards cards.json --out plan.json
# 设备侧重解计划 + 逐跳复播
python3 -m tourbox.cli verify-plan --bundle out/bundle --plan plan.json
# 单卡复播（可导出实际 WAV）/ 具名 clip 锚点解析
python3 -m tourbox.cli replay-card --bundle out/bundle --card card.json --wav seg.wav
python3 -m tourbox.cli anchor --bundle out/bundle --card card.json

bash run_all_checks.sh     # 全量验证（含 numpy 禁用环境下的离线核对）
```
