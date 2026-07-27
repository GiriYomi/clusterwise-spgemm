# OpenEvolve 70 轮谱系与实验有效性分析

分析对象：

- `evolve_runs/vlength_20260726_061744`
- `evolve_runs/hierarchical_20260726_163816`

本文把日志中的 70 个真实迭代与各 checkpoint 的 program 数据合并，按 parent ID 重建谱系；同时对 EVOLVE block 做精确 hash、语义 diff，并读取每次评测 artifact 中的逐矩阵 timing、cluster 数和失败原因。

## 1. 最重要的结论

1. **两次运行的 CPU、线程、正确性和防 score-cheat 设置本身是合格的。** 两次 manifest 都记录了 `threads=10`、`cpu_list=5-9,15-19`、performance governor。这 10 个 CPU 都是最高频率 3.9 GHz 的 Cortex-X925 大核。候选必须通过源码边界、编译、输入不变性、cluster contract 和 SpGEMM 数值正确性检查。运行时 evaluator 与现在的 evaluator SHA-256 完全一致。
2. **fingerprint 完全没有进入 evaluator、配置、候选代码或 LLM prompt。** 两个 run 的全部存档中 `fingerprint` 命中数均为 0。因此不存在 fingerprint 恒为 0 后误导 LLM 的问题。
3. **Vlength 报告的最佳 `1.0253` 不是可信的 2.53% 算法提升。** 最终最佳算法实际上第 2 轮就出现；完全相同的 EVOLVE block 被重新评测 23 次，分数从 `0.9614` 到 `1.0253`，均值仅 `0.9928`。第 21 轮只是同一代码的最高噪声样本，属于典型 winner's curse。
4. **Hierarchical 的 70 轮只有 16 轮得到有效性能评测。** 另外 54 轮全部因“修改 EVOLVE block 外代码”被硬拒绝。51/54 只是把 marker 从 `// EVOLVE-BLOCK-START` 改成了前面多一个空格的 ` // EVOLVE-BLOCK-START`。更严重的是，OpenEvolve 仍把 0 分节点放入 population/archive，导致无效父节点继续产生 41 个注定失败的后代。
5. **Hierarchical 的最终最佳也不是第 39 轮的新算法。** 第 4、33、39、48、62 轮在数值逻辑上等价，并产生完全相同的五组 cluster 数。这个语义家族 5 次评测的平均分是 `1.00382`，标准差 `0.00432`，95% 均值区间约 `[0.9985, 1.0092]`，仍包含 1.0。它是一个值得复测的方向，但现在不能宣称确定获得 0.86% 提升。
6. **最有趣的算法发现来自 Hierarchical：最佳家族主动生成更多、更小的 clusters，而不是追求更多合并。** 相对 canonical baseline，cluster 数在 AS365/M6/kkt_power/patents/webbase 分别增加约 8.95%/8.51%/32.00%/11.51%/1.11%。这通常减少 CSR-cluster padding 和线程负载不均，代价是部分 cache reuse；五次平均 kernel 分数略有改善，预处理还快约 2%–14%。
7. **目前没有 holdout 证据。** evaluator 定义了 NLR、GAP-road、europe_osm、com-LiveJournal 四个 final-audit 数据集，但本机没有对应矩阵，也没有 hierarchical close-pairs。这次结果只能说明训练矩阵上的表现，不能说明跨数据集泛化。

## 2. 数据恢复边界

OpenEvolve 的 checkpoint 保存的是“当时 population”，不是永不删除的逐轮 append-only archive。

- Vlength：日志有 70 次迭代，但 checkpoint 并集只保留了 66 个真实候选源码。第 61、63、68、69 轮在下一个 checkpoint 前被 population cleanup 删除，因此只剩 ID、parent、耗时和 score；源码/LLM response 已不可恢复。
- Hierarchical：70 个真实候选都能从 checkpoint 并集中恢复。
- Vlength checkpoint 并集共有 76 个 program ID，Hierarchical 有 82 个；多出来的不是额外迭代，而是 island migration 复制出的新 UUID。迁移 copy 继承相同代码和 metrics，不能当作新算法或新测量。

因此本文严格区分：

- **真实迭代节点**：日志中 `Iteration N: Program ...` 的 70 个节点；
- **迁移节点**：新 UUID、相同代码和相同旧 metrics，不计入 70 次算法尝试；
- **语义家族**：忽略注释、排版和等价整型写法后，实际 clustering 决策相同的一组节点。

## 3. Vlength 谱系

### 3.1 简化分支图

图中分数是该次观测，不代表重复测量均值。

```text
S0 canonical pivot-Jaccard greedy (seed, 0.9892)
├─ I1 adaptive pivot+last、空行合并 (0.9000)
│  └─ I3 回退到简单 pivot greedy (1.0150)
│     ├─ I6 多 anchor + degree UB (1.0143；同代码家族均值 1.0006)
│     │  ├─ I8 best-anchor/adaptive (0.9801)
│     │  └─ I10 two-anchor strict UB (1.0126)
│     ├─ I9 heavy-row chain (0.9874)
│     └─ I18 strict two-anchor (0.9888)
│        └─ I20 relaxed two-anchor (1.0206；同代码家族均值 0.9937)
│           ├─ I48 完全相同代码再次测得 1.0222
│           └─ 后续加入 empty-row/chain-bias 等均未稳定超过
├─ I2 adaptive chain family C (1.0118)
│  ├─ I11 anchor sketch (0.9932)
│  ├─ I17 majority vote (0.9074)
│  └─ C 在 I15/I19/I21/... 被反复重新生成，共 23 次
├─ I4 chain + degree prune + zero-row packing (0.9934)
└─ I5 chain-friendly adaptive (0.9770)
   ├─ I7 简单 pivot greedy (1.0106)
   └─ I21 回到与 I2 完全相同的 C (1.0253，最终被选为 best)
```

### 3.2 关键算法节点为何这样表现

#### S0 / I3 / I7：简单 pivot greedy

canonical 算法以 cluster 第一行为 pivot，只要后续连续行与 pivot 的 Jaccard 不低于 0.3 就继续扩展，最多 8 行。I3 与 S0 在 cluster 决策上等价，只是重写了代码；I7 也是同一决策顺序的简化版本。

S0 得分 `0.9892`，I3 得分 `1.0150`，I7 得分 `1.0106`。等价 clustering 之间已经出现约 2.6 个百分点跨度，这本身证明单次 score 的噪声足以制造“改进”。

#### I1：自适应阈值 + second chance

I1 对重行放宽阈值，对极短行收紧阈值；pivot 不满足时，再尝试与 cluster 最后一行匹配，并无条件吸收空行。它的意图是允许局部 pattern drift，但结果 `0.9000`。原因不是正确性失败，而是 chain rule 容易把“相邻两行相似、但与 cluster 早期行不相似”的行串起来，扩大 cluster 内 sparsity pattern 差异，增加 clustered-format padding 和负载不均。I3 把它完全回退到简单 pivot 后立刻观测到 `1.0150`。

#### C 家族（I2 / I21 等）：adaptive chain

最终 best 的真实逻辑是：

- 优先比较 candidate 与上一已接纳行；
- chain 阈值比 anchor 阈值低 0.05；
- 两行 degree 和大于 64 时再放宽 0.05；
- 任一 degree 不超过 2 时收紧 0.05；
- chain 失败后再与 cluster 第一行比较。

它确实改变了 clusters：相对 canonical，cluster 数在 AS365/M6/webbase 分别减少约 1.63%/1.60%/1.70%，说明它形成了略大的连续 clusters；kkt_power 和 patents 几乎不变。

但是 23 次完全相同 block 的统计是：

| 指标 | 值 |
|---|---:|
| n | 23 |
| mean combined | 0.99280 |
| median combined | 0.98876 |
| stddev | 0.01524 |
| min / max | 0.96138 / 1.02526 |
| 近似 95% mean CI | [0.9862, 0.9994] |

逐矩阵平均 speedup：patents `0.9852`、webbase `1.0113`、kkt `1.0011`、AS365 `1.0011`、M6 `0.9971`。它可能对 webbase 有约 1% 的真实好处，但在 patents/M6 上抵消了收益。最终 `1.0253` 的样本由 patents `1.1201` 和 kkt `1.0323` 两个异常高观测推动；同代码在第 33 轮的 patents 只有 `0.8879`。

这正是 max-selection bias：23 次重复中挑最大值，天然会把正噪声当作 fitness 改进。第 21 轮代码并不优于第 2 轮代码，甚至一个字符都没有变化。

#### I6 家族：多 anchor + degree upper bound

它保留 pivot 和最多 3 个最近行作为 anchors，用 `min(deg_i,deg_j)/max(...)` 作为 Jaccard 上界，跳过不可能达到阈值的比较。3 次完全相同代码的 combined 为 `1.0143/0.9772/1.0103`，均值 `1.0006`。平均上 kkt 约 `+1.22%`，但 patents 方差极大。它是 Vlength 中比最终 best 更值得复测的方向：均值接近不回退，且 UB 有可能降低 preprocessing；但现有 n=3 不足以确认。

#### I20 家族：two-anchor + growth relaxation

它只保留 pivot 和 last 两个 anchors，先用 degree UB 选更可能匹配的 anchor，并随 cluster 增长最多放宽 0.05，再按 degree mismatch 额外放宽。5 次完全相同代码 combined 为 `1.0206/0.9437/1.0000/1.0222/0.9818`，均值 `0.9937`、标准差 `0.0325`。平均上 webbase、kkt、AS365 有正趋势，但 patents 极不稳定。I20 和 I48 的高分同样不能当作稳定成果。

#### 低分复杂分支的共同原因

- majority vote / 检查任意 in-cluster row：允许 transitive drift，cluster 内部不再内聚；
- 过强 high-degree 惩罚：可能错过最能复用 B rows 的重行组合；
- anchor-only：无法利用相邻行 pattern 缓慢漂移；
- 过度 permissive threshold：减少 cluster 数不等于更快，padding 和线程 imbalance 会超过 cache reuse 收益；
- 复杂 heuristics 增加 preprocessing，但 preprocessing 不进入正向 reward，只设 4x 上限，因此搜索没有动力主动降低这部分成本。

### 3.3 Vlength 70 轮节点摘要

`C` 是最终 adaptive-chain 家族；`M` 是多-anchor 家族；`T` 是 I20 two-anchor 家族。`same` 表示 EVOLVE block 字节完全相同，并非仅仅相似。

| 轮次 | parent | score | 改动/结果 |
|---:|---|---:|---|
| 1 | seed | 0.9000 | adaptive pivot+last、空行吸收；明显退化 |
| 2 | seed | 1.0118 | 产生 C |
| 3 | I1 | 1.0150 | 回退为简单 pivot，语义等同 seed |
| 4 | seed | 0.9934 | chain + degree UB + zero-row packing |
| 5 | seed | 0.9770 | adaptive chain 的另一写法 |
| 6 | I3 migration copy | 1.0143 | 产生 M：最多四 anchors + UB |
| 7 | I5 | 1.0106 | 回退简单 pivot |
| 8 | I6 | 0.9801 | best-anchor + 自适应阈值，退化 |
| 9 | I3 | 0.9874 | 只对 heavy rows 开 chain |
| 10 | I6 | 1.0126 | strict two-anchor + UB |
| 11 | C | 0.9932 | 4-entry anchor sketch |
| 12 | I10 | 0.9772 | same M |
| 13 | seed | 0.9764 | chain + adaptive degree threshold |
| 14 | I10 | 1.0103 | same M |
| 15 | I7 | 0.9878 | same C |
| 16 | M | 0.9981 | 简单 pivot 重写 |
| 17 | C | 0.9074 | majority-vote，严重 drift |
| 18 | I3 copy | 0.9888 | strict two-anchor |
| 19 | I7 | 1.0107 | same C |
| 20 | I18 | 1.0206 | 产生 T |
| 21 | I5 | 1.0253 | same C；被误选为最终 best |
| 22 | M | 0.9437 | same T，显示高方差 |
| 23 | C | 0.9800 | chain + UB pruning |
| 24 | simple pivot | 1.0138 | two-anchor + mild adaptive |
| 25 | I4 | 0.9961 | same C |
| 26 | I24 | 0.9380 | high-degree tightening/cache-thrash guard 过强 |
| 27 | I7 | 0.9737 | chain + UB |
| 28 | I18 | 1.0000 | same T |
| 29 | I3 | 0.9701 | same C |
| 30 | C migration copy | 1.0154 | two-anchor + UB + growth relaxation |
| 31–36 | 多父节点 | 0.9614–1.0055 | 六次全部回到 same C |
| 37 | C | 0.9721 | run-length/cohesion guard |
| 38 | I18 | 0.9954 | adaptive two-anchor |
| 39–41 | 多父节点 | 0.9840–1.0112 | 三次全部回到 same C |
| 42 | C | 1.0157 | two-anchor + UB + mild adaptive |
| 43 | C | 0.9766 | 检查任意 cluster member，drift/成本增加 |
| 44 | I8 | 0.9987 | same C |
| 45 | C | 1.0051 | degree-aware chain-or-anchor |
| 46 | M | 0.9829 | same C |
| 47 | C | 0.9986 | UB prefilter + zero-row handling |
| 48 | C | 1.0222 | same T；高噪声重复 |
| 49 | I4 | 1.0040 | same C |
| 50 | I38 | 0.9818 | same T |
| 51 | C | 0.9236 | anchor-only，明显退化 |
| 52 | I30 | 1.0125 | 更 permissive 的 two-anchor |
| 53 | C | 0.9797 | chain + degree gating |
| 54 | C copy | 0.9853 | pivot guard 限制 drift |
| 55 | I9 | 1.0063 | same C |
| 56 | C | 0.9957 | two-anchor + UB + adaptive |
| 57 | I43 | 0.9783 | same C |
| 58 | T | 1.0056 | zero-row fast path + chain bias |
| 59 | I11 | 0.9984 | same C |
| 60 | C | 1.0077 | two-anchor + UB + adaptive |
| 61 | I9 | 0.9743 | 源码被 cleanup 删除，无法恢复具体改动 |
| 62 | I52 | 0.9807 | same C |
| 63 | C | 0.9018 | 源码被 cleanup 删除 |
| 64 | C | 0.9988 | two-anchor + UB + mild adaptive |
| 65 | C | 0.8855 | cohesion-aware/degree-guided，严重退化 |
| 66 | I8 | 0.9888 | same C |
| 67 | seed | 0.9942 | adaptive chain 新写法 |
| 68 | T | 0.9602 | 源码被 cleanup 删除 |
| 69 | I67 | 0.0000 | 约 41 秒即失败，属于 hard/cascade gate；具体源码已删除 |
| 70 | C | 0.9986 | two-anchor + degree UB/adaptive |

Vlength 至少有 28/70 次是已知 exact-block 重复，其中 C 一项就浪费了 22 个额外评测预算。四个被删除节点是否还有重复无法判断，所以这是下界。

## 4. Hierarchical 谱系

### 4.1 有效主干图

```text
H0 canonical raw-Jaccard PQ + union-find (seed, 0.9932)
├─ I1 cosine-like reweight (0.9791)
│  └─ I3 marker 前多空格 → INVALID
│     └─ 17 个后代继续继承 INVALID 前缀
└─ I2 Jaccard / sqrt((deg+1)(deg+1)) (0.9932)
   ├─ I4 overlap+balance family F (0.9968)
   │  ├─ I6 小改/简化 (0.9694)
   │  ├─ I16 hub penalty + canonicalization (0.9108)
   │  │  └─ I48 删除 hub/canonicalization，回到 F (1.0049)
   │  ├─ I26 canonical pair 去重 (0.9058)
   │  ├─ I54 把 close_pairs 的 Jaccard 误当 common count (0.8756)
   │  │  ├─ I62 修正语义，回到 F (1.0047)
   │  │  └─ I68 只修一半 (0.9040)
   │  ├─ I58 absolute-overlap pruning (proxy 0.3214)
   │  └─ I69 加 16x degree-ratio guard (1.0030)
   └─ I5 normalized score + adaptive cutoff (0.9941)
      ├─ I33 去掉 cutoff，回到 F (1.0042)
      └─ I39 与 I33 完全相同 (1.0086，最终 best)
```

### 4.2 最佳语义家族 F 的真实变化

相对 canonical raw Jaccard，F 使用：

```text
I = J * (deg_i + deg_j) / (1 + J)       # 由 Jaccard 反推交集大小
overlap = I / min(deg_i, deg_j)          # overlap coefficient
balance = (min_degree / max_degree)^0.25 # 轻度 degree 不平衡惩罚
score = J * (0.5 + 0.5*overlap) * balance
```

随后按这个 score 重排 close-pairs 的 merge 顺序，仍用 capacity guard 和 union-find。它不改变 correctness，只改变“哪些 pair 先占满 cluster 容量”。

关键点不是它合并更多，而是**它让许多 cluster 更早以不同组合占满，最终保留更多 clusters**：

| 数据集 | baseline clusters | F clusters | 变化 |
|---|---:|---:|---:|
| AS365 | 606,897 | 661,193 | +8.95% |
| M6 | 560,155 | 607,839 | +8.51% |
| kkt_power | 407,591 | 538,009 | +32.00% |
| patents_main | 103,788 | 115,729 | +11.51% |
| webbase-1M | 603,437 | 610,142 | +1.11% |

为何更多 clusters 反而可能更快：CSR_VlengthCluster 把同一 cluster 的多行统一组织。cluster 内 sparsity pattern 差异越大，padding、无效工作和线程间负载差异越大。raw Jaccard 优先最大相对重叠，但不显式控制 degree imbalance；F 的 overlap/balance 会改变容量竞争，使部分低质量合并不再发生。kkt_power cluster 数增加最多，同时五次平均 kernel speedup 约 `1.0145`，说明该矩阵更可能受 padding/imbalance 而非 cache reuse 主导。

F 的五次等价测量统计：

| 指标 | mean | median | stddev | min–max |
|---|---:|---:|---:|---:|
| combined | 1.00382 | 1.00467 | 0.00432 | 0.99676–1.00862 |
| patents | 1.00026 | 1.00157 | 0.01193 | 0.98286–1.01622 |
| webbase | 1.00785 | 1.00291 | 0.01137 | 0.99835–1.02485 |
| kkt_power | 1.01445 | 1.02379 | 0.01760 | 0.98736–1.02834 |
| AS365 | 1.00396 | 1.00308 | 0.00335 | 1.00070–1.00930 |
| M6 | 1.00370 | 1.00357 | 0.00656 | 0.99368–1.01109 |

F 的 preprocessing median 平均比 canonical 低：AS365 约 10.6%、M6 12.5%、kkt 7.6%、patents 14.4%、webbase 2.4%。preprocessing 没进入 reward，所以这不是 score-cheat；它是额外的工程收益。但必须用独立重复和 holdout 确认。

### 4.3 其他有效节点的因果解释

| 轮次 | score | 关键变化 | 为什么这样 |
|---:|---:|---|---|
| 1 | 0.9791 | 把 Jaccard 转为 cosine-like | kkt +6.9%，但 AS365/M6 cluster 数增加约 53%/52%，kernel -7.3%/-5.3%；不同矩阵最优 tradeoff 不同 |
| 2 | 0.9932 | `J/sqrt((deg+1)(deg+1))` | 强烈按 degree 缩放，但总体接近 baseline；patents -3.8% 成为 worst |
| 4 | 0.9968 | 首次产生 F | 后续所谓 best 的实际起点；单次略低于 1 属于测量波动 |
| 5 | 0.9941 | F 前身加全局 cutoff 和 16x degree-ratio guard | kkt +6.4%，但 patents -5.2%；cutoff 对小图/degree 分布不稳 |
| 6 | 0.9694 | F 的 merge/root 逻辑简化 | AS/M6 cluster 数暴增约 63%/62%，cache/padding tradeoff失衡 |
| 16 | 0.9108 | hub penalty + canonical pair 去重 | 过度惩罚重行并丢弃部分方向，AS/M6 约 -20% |
| 26 | 0.9058 | 只保留 `(min,max)` 方向 | close-pairs 输入不保证双向齐全；`if (i != min) continue` 会直接丢 pair，产生大量碎片 |
| 33 | 1.0042 | 从 I5 去掉 cutoff，回到 F | 避免过强过滤；与 I4 数值逻辑相同 |
| 39 | 1.0086 | 与 I33 exact same | 没有新算法变化，纯重复测量较高 |
| 44 | 0.9003 | cluster degree/hub 动态权重 | 再次过度避免重行组合，AS/M6 约 -20% |
| 48 | 1.0049 | 从 I16 删除 hub/canonicalization | 恢复 F，性能立即恢复 |
| 54 | 0.8756 | 把 `p.second` 当 intersection count 再转 Jaccard | 实际文件中 `p.second` 已是 Jaccard；二次转换把 score 压得极小并改变排序，AS/M6 约 -24%/-25% |
| 58 | 0.3214 | `I<1.5` prune + absolute-overlap/log 权重 | patents cluster 数从 103,788 翻到 205,670，proxy 仅 0.321，未进入五矩阵 full stage |
| 62 | 1.0047 | 明确恢复“provided Jaccard” | 从 I54 的 0.8756 恢复到 F，构成强因果证据 |
| 68 | 0.9040 | 修正 seed score，但仍 canonicalize/drop orientations | AS/M6 仍约 -19.6%，只修复了 I54 的一部分 |
| 69 | 1.0030 | F + 16x degree-ratio guard | kkt +3.3%，webbase -1.5%；约束可能对部分图有价值，但不如无 guard 稳定 |

### 4.4 54 个无效节点怎样形成

13 个独立 invalid root 及其真实迭代后代如下；同一组内的后代都继承 root 的 block 外改动：

| invalid root | parent 状态 | 覆盖轮次 | 数量 |
|---:|---|---|---:|
| 3 | valid I1 | 3,7,9,11,23,37,41,43,45,47,49,51,53,55,59,61,63,65 | 18 |
| 8 | valid I6 | 8,18,30,32,36,52,60,64 | 8 |
| 10 | valid I6 | 10,12,14,20,22,24,28,34,38,40,42,46,50,56 | 14 |
| 13 | valid I2 | 13 | 1 |
| 15 | valid I5 | 15 | 1 |
| 17 | valid I4 | 17,21,31 | 3 |
| 19 | valid seed | 19,29 | 2 |
| 25 | valid seed | 25,35 | 2 |
| 27 | valid I2 | 27 | 1 |
| 57 | valid I5 | 57 | 1 |
| 66 | valid I16 | 66 | 1 |
| 67 | valid I39 | 67 | 1 |
| 70 | valid I4 copy | 70 | 1 |

其中 51 个候选的 prefix 差异只有：

```diff
-// EVOLVE-BLOCK-START
+ // EVOLVE-BLOCK-START
```

第 3 轮的 LLM response 明确把 marker 放进 SEARCH/REPLACE，并在 replacement marker 前输出一个空格。evaluator 的 `_split_evolve_block` 把 marker 本身计入 prefix，所以这个空格属于 block 外修改，硬拒绝是按规则正确执行的。

真正的系统问题是 OpenEvolve 0.2.16 的 database：

- `add()` 无条件把 score=0 program 加入 programs/island/archive；
- exploration 从 island 中 uniform random，不过滤 score=0；
- random parent 从全部 programs 选择；
- population 未超过 60 前不会清理；
- 新候选在当轮 cleanup 中还被保护，invalid lineage 很容易存活和繁殖。

因此 13 次原始格式错误进一步浪费了 41 次后代预算。Hierarchical 的有效搜索预算不是 70，而是 16。

## 5. evaluator 与实验设计审计

### 5.1 做对的部分

- CPU affinity 正确：10 个大核，线程数与 mask 数量严格相等；不是大小核混跑。
- governor 为 performance，vLLM 在实验前停止，两组实验串行运行。
- 每个矩阵做 3 组 baseline/candidate 交错进程；每进程 warmup 后内部 10 次 kernel，取 paired ratio 中位数。
- score 只包含 kernel speedup；cluster 数和 preprocessing 只作为 artifact，不能直接骗分。
- correctness gate 检查源码边界、禁止 I/O/计时/进程控制、编译、自测、输入不变性、cluster contract 和数值结果。
- best program、checkpoint_70、完整 evaluator/config/run manifest 均落盘；manifest hash 表明实验后 evaluator/config 未被偷偷改变。
- fingerprint 不存在。

### 5.2 影响结论的问题

#### A. 没有 duplicate suppression，也没有对同代码聚合 fitness

Vlength 至少 28 次 exact block 重复。OpenEvolve 把每次 noisy observation 当作独立 algorithm，并用最大单次 score 更新 best。正确做法应该是：发现相同 normalized block 时不重新占用进化轮次；若主动复测，则把多次结果聚合成 median/trimmed mean，并让 best 使用聚合值。

#### B. winner's curse 大于所宣称的收益

Vlength C 家族标准差 1.52%，最大值比均值高 3.25 个百分点；最终报告提升 2.53% 完全落在这个选择偏差内。Hierarchical F 家族标准差 0.43%，最终最大 0.86% 也只比家族均值高约 0.48 个百分点。

#### C. patents_main 太短，主导噪声和 worst term

Vlength patents kernel 约 70–85 ms，H 约 26 ms，远短于大矩阵。即使内部跑 10 次，它对 OS 抖动、cache 状态和频率瞬态仍更敏感。Vlength C 家族的 patents speedup 范围 `0.8879–1.1201`，远大于其他矩阵。

评分又加入 `0.20 * worst_speedup`。短矩阵一旦偶然成为 worst，会被几何平均计一次、worst 再计一次；反之偶然很高也会抬升 geometric mean。这使小矩阵噪声被放大。建议为每个矩阵使用最小累计 kernel 时间而非固定 10 次，并对最终候选提高 paired repeats。

#### D. invalid program 可以繁殖

Hierarchical 54/70 被同一格式问题吞掉。硬门本身没有错，database selection 才是核心：hard-gate score=0 的 program 不应进入 parent pool、archive 或 MAP-Elites cell；至少应标为 non-reproductive。

#### E. diff prompt 暴露 marker，marker 又被当作不可变 prefix

模型被要求“只改 EVOLVE block”，但 diff prompt 给它整份源文件，SEARCH/REPLACE 可以包含 marker。一个前导空格就永久污染 lineage。应让模型只看到 block body，或 apply patch 后 canonicalize marker 行，再做 prefix/suffix 比较。

#### F. 缺少真正 holdout

四个 holdout 名称只是 evaluator 常量，本机数据不存在，所以没有 final audit。五个训练矩阵的 metrics 又完整返回给 LLM，算法当然会逐步适应这些矩阵。没有 holdout 就不能判断 F 的 degree/overlap 权重是否泛化。

#### G. 运行记录不是全量 append-only

Vlength 有 4 个真实候选源码在 checkpoint 间被删除，导致无法完整解释全部 70 次。研究型实验应为每轮单独保存 candidate source、parent、LLM response、gate result 和 artifact，不应只依赖 population checkpoint。

## 6. 成果应当怎样表述

### Vlength

不应表述为“进化得到 2.53% 加速”。更准确的结论是：

> 搜索反复提出 adaptive chain 和 two-/multi-anchor contiguous clustering。adaptive-chain 能把部分图的 cluster 数减少约 1.6%，对 webbase 可能有约 1% 收益，但 23 次相同代码的总体均值没有超过 baseline；当前 best 是重复测量最大值，尚无稳定加速证据。

可以保留的候选研究方向：

1. 多-anchor + degree UB（I6 family）：均值最接近 1，kkt 有正趋势；
2. two-anchor + UB（I20 family）：在 webbase/kkt/AS 有正趋势，但需要解决 patents 噪声；
3. 不要把“cluster 更少”当 proxy，必须同时记录 padding、每 cluster nnz 分布和线程 load balance。

### Hierarchical

不应表述为“70 轮稳定找到 0.86% 加速”。更准确的结论是：

> 70 轮中仅 16 轮有效。一个 overlap/balance 重排家族被独立生成/恢复 5 次，五次平均 combined 约 +0.38%，并显著降低 preprocessing；它生成更多、更小 clusters，暗示减少 padding/负载不均可能比最大化 cluster 合并更重要。但 95% 区间仍覆盖 baseline，且没有 holdout，因此只能称为 promising candidate。

Hierarchical 的科学价值高于 Vlength：I54→I62 的修复、I16→I48 的回退都提供了清楚的因果对照，证明：

- `close_pairs` 的值必须按 Jaccard 使用，不能当 intersection count；
- hub penalty/canonical orientation filtering 很容易过度碎片化；
- 更少 merges 可能改善 kernel，但过度碎片化会让 AS/M6 崩溃；
- 最优点位于 cache reuse 与 padding/load-balance 的中间，而不是单调追求 cluster 数最少。

## 7. 建议的下一步验证顺序

1. 修复 marker/body 边界和 invalid-parent reproduction，再重跑 Hierarchical；否则 70 轮预算不可比。
2. 加 normalized-code duplicate detection；重复候选若要测量，放入独立 resampling 队列并聚合结果。
3. 对 Vlength 的 C/M/T 三个语义家族和 canonical 做随机交错的独立复测，至少 7–10 个外层 repeats；报告 median 和 bootstrap CI，不报告单次 max。
4. 对 Hierarchical F、I69 degree-guard 和 canonical 做同样复测；同时记录每矩阵 cluster-size histogram、padding ratio、每 cluster nnz 和每线程工作量。
5. 补齐至少 3–4 个 holdout 矩阵及 close-pairs，在选定 best 之后只运行一次 final audit；holdout 结果不再反馈给进化。
6. 让 patents_main 达到最小累计计时（例如每个外层进程至少数秒），避免它以 26–80 ms 的短 kernel 主导 worst-score 噪声。
7. 每轮 append-only 保存 source/response/artifact，确保未来能真正逐节点复盘完整 70 轮。
