# Handoff — OpenEvolve SpGEMM Clustering Evolution

**Last session:** 2026-07-26（上一轮实验 2026-04-20 → 04-21）
**Branch:** `evolve`
**Status:** 本地两个实验各跑完 70 轮，均超过 baseline。evolve 框架已同步到 DGX Spark，待远端手动装 openevolve + API key 后即可跑。
**文档分工:** 结构 / 运行方式 / 结果位置 → [README_EVOLVE.md](README_EVOLVE.md)；本文件只记实验状态、结论和下一步。

---

## 1. 项目在做什么

用 [OpenEvolve](https://github.com/codelion/openevolve)（AlphaEvolve 开源实现）演化 Cluster-wise SpGEMM 的**聚类策略**。
LLM 反复改写 C++ 源码里 `EVOLVE-BLOCK-START/END` 之间的代码块，评估器编译 + 在 SuiteSparse 矩阵上跑 A² 打分，择优迭代。

两条独立演化线：

| | VlengthCluster | Hierarchical |
|---|---|---|
| 源文件 | [sample/VlengthClusterSpGEMM.cpp](sample/VlengthClusterSpGEMM.cpp) | [sample/HierarchicalClusterSpGEMM.cpp](sample/HierarchicalClusterSpGEMM.cpp) |
| Config | [config_vlength.yaml](config_vlength.yaml) | [config_hierarchical.yaml](config_hierarchical.yaml) |
| 演化目标 | `main()` 里的行分组循环 | `hierachical_clustering_v0()` 整个函数 |
| 约束 | 行必须**连续**（offset 数组划边界） | 行**可任意**分组（union-find 合并） |
| 输出契约 | `vector<INDEXTYPE> offset` + `INDEXTYPE real_max_cluster_size` | `map<INDEXTYPE, vector<INDEXTYPE>>` |
| 额外输入 | 无 | `data/close_pairs/<name>_closepairs.txt` |

两者最终都喂给同一个 `CSR_VlengthCluster` 构造函数和 `HashSpGEMMVLCluster` kernel。

---

## 2. 怎么跑

完整命令、路径环境变量（`DATA_PATH` / `CLOSE_PAIR_DATA_PATH` / `TBB_PREFIX`）见 [README_EVOLVE.md](README_EVOLVE.md) §3。

### 环境坑（重要）

- **本地必须用 `/home/yomi/anaconda3/bin/` 下的 openevolve-run**。系统 `/usr/bin/python3` 没装 `openevolve`。
- `OPENAI_API_KEY` 在本地 `~/.bashrc` 第 145 行（2026-07 已换新 key）。若遇 429 `insufficient_quota`，是账号没余额，不是限流——换 key 或改用 Anthropic 模型。
- Config 里 `primary_model: "gpt-5"` 实测可用。
- 单次 70 轮耗时约 **3.5 小时**（每轮 ~3 分钟：LLM 生成 + 编译 + 5 矩阵 × 4 次运行）。建议后台/tmux 跑。
- `evaluator.py` 的数据路径可用环境变量覆盖（不设则用本地默认 `data/`，行为与旧版完全一致）。

### 本地先决条件（已满足，无需重做）

- 9 个 SuiteSparse 矩阵已下载在 `data/`（`./run.sh download`）
- 9 个 close-pairs 文件已生成在 `data/close_pairs/`（`./run.sh gen-pairs-all`）
- 二进制已编译在 `bin/`（`./run.sh build`）
- `baseline_timings.json` 已有两种 program type 的 baseline

## 2b. DGX Spark 部署状态（2026-07-26 同步）

远程 `dgx-spark:~/projects/clusterwise-spgemm`（ssh config 里主机名是 `dgx-spark`）。
该 repo 是更新的分支：多了 `scripts/`、`tests/`、`best_*/`、新 makefile（`sample_hw` / `best_hw` target），源码用 `omp_get_max_threads()` 替代硬编码 64 线程。

**已同步过去的文件**（本地 → 远程，远程被覆盖的旧版备份在 `~/projects/clusterwise-spgemm/.pre_evolve_backup/`）：

- `evaluator.py`、`config_vlength.yaml`、`config_hierarchical.yaml`
- `sample/VlengthClusterSpGEMM.cpp`、`sample/HierarchicalClusterSpGEMM.cpp`
  ——远程原版**没有 EVOLVE-BLOCK 标记**，且 hierarchical 缺 evaluator 必需的
  `# of clusters` / `max_cluster_size for SpGEMM` 统计输出，必须用本地版
- `HANDOFF.md`、`README_EVOLVE.md`

**远程数据位置**（已就绪，勿动）：

- 矩阵：`~/datasets/spgemm/<name>/<name>.mtx`（5 个 stage1 全有）
- close-pairs：`~/datasets/spgemm/reordering/close_pairs/<name>.mtx`（注意后缀是 .mtx，evaluator 两种命名都认）
- GTgraph `.o` + `libsprng.a` 已编译好

**远端跑之前还差两步（手动）：**

1. `pip install openevolve`（conda `spgemm` 环境或 base，推荐 0.2.16 与本地一致）
2. `export OPENAI_API_KEY=...`（用户手动添加，不进 git）

跑法见 README_EVOLVE.md §3 的 DGX Spark 小节（需 export 三个路径变量）。
**注意**：`baseline_timings.json` / `fingerprint_cache.json` 是机器相关的，**没有**同步过去——spark 首跑会自动重新测 baseline，这是正确行为，别从本地拷。

---

## 3. 评分机制（[evaluator.py](evaluator.py)）

```
combined_score = 0.80 × avg_timing_speedup_median + 0.20 × avg_clustering_score
```

- **timing (80%)**：`baseline_median_ms / candidate_median_ms`，1 次 warmup + 3 次计时取中位数
- **clustering (20%)**：`0.8 × min(baseline_clusters/cand_clusters, 2.0) + 0.2 × min(cand_max_cs/baseline_max_cs, 2.0)`，封顶 2.0 防刷分
- **slowdown penalty**：若 `avg_timing_speedup_median < 0.8`，`combined *= avg_speedup`
- baseline 得分 = 1.0，**> 1.0 即改进**

两个 config 的 prompt 里都写了 80/20，与代码常量 `_WEIGHT_TIMING=0.80` / `_WEIGHT_CLUSTERING=0.20` 一致。

**Stage-1 数据集**（实际只用这 5 个，`evaluate()` 硬编码只跑 stage1）：
`patents_main`, `webbase-1M`, `kkt_power`, `AS365`, `M6`

`NLR`, `GAP-road`, `europe_osm`, `com-LiveJournal` 标了 `stage1: False`，当前从未被跑到——`evaluate_stage2()` 直接调用 `evaluate()`，也只跑 stage1。想上全量得改 [evaluator.py:662](evaluator.py#L662)。

### Fingerprint 缓存

`fingerprint_cache.json` 按 (num_clusters, max_cs) across all matrices 做指纹。指纹命中则直接复用历史分数，跳过计时——用来消除计时噪声。**注意**：如果改了评分权重或数据集，这个缓存会返回旧权重下的分数，需要删掉重跑 baseline。

---

## 4. 上次实验结果

### VlengthCluster — `evolve_runs/vlength_20260420_210803/`

**combined_score = 1.1346**（+13.5%），best 出现在 iteration 28 / generation 3

| 矩阵 | baseline ms | best ms | speedup | clusters |
|---|---|---|---|---|
| patents_main | 16.23 | 15.57 | 1.042 | 239k → 207k |
| webbase-1M | 50.25 | 35.67 | **1.409** | 248k → 164k |
| kkt_power | 206.16 | 199.76 | 1.032 | 2055k → 2052k |
| AS365 | 471.65 | 528.02 | 0.893 | 3799k → 1988k |
| M6 | 427.64 | 505.13 | 0.847 | 3502k → 1816k |

`avg_timing_speedup_median = 1.0445`, `avg_clustering_score = 1.4946`

**演化出的策略**（[best_program.cpp](evolve_runs/vlength_20260420_210803/best/best_program.cpp)）：
把 Jaccard 换成 **overlap coefficient**（从 Jaccard 反推交集大小再除以 min degree），再叠四层启发式：
1. **degree-aware 阈值放松**：min-degree 越大放松越少（≥1024 放松 0.02，≥256 放松 0.06，≥64 放松 0.03）——高度数行谨慎合并防 cache thrash
2. **adjacency stitching**：与簇头不够像时，退而比较**前一行**（阈值再降 0.05，且要求 degree 比 ≤ 2.5）
3. **degree budget**：`max_cluster_size × 512`，限制簇内触及的 B-row 总量
4. **绝对交集 fallback**：即使比例不够，共享列数 ≥ max(2, 0.06×mindeg) 也合并

### Hierarchical — `evolve_runs/hierarchical_20260421_031627/`

**combined_score = 1.0281**（+2.8%），best 出现在 iteration 27 / generation 2

| 矩阵 | baseline ms | best ms | speedup | clusters |
|---|---|---|---|---|
| patents_main | 4.24 | 4.06 | 1.046 | — |
| webbase-1M | 63.69 | 59.07 | 1.078 | — |
| kkt_power | 180.73 | 172.06 | 1.050 | — |
| AS365 | 354.33 | 352.43 | 1.005 | 584k → 554k |
| M6 | 322.14 | 325.04 | 0.991 | 539k → 508k |

`avg_timing_speedup_median = 1.0342`, `avg_clustering_score = 1.0037`

**演化出的策略**（[best_program.cpp](evolve_runs/hierarchical_20260421_031627/best/best_program.cpp)）：
骨架（priority queue + union-find）保持不变，唯一改动是把 `jaccard_similarity` 换成**列频率加权 Jaccard**：
- 预计算 `col_w[c]` = 引用列 c 的行数
- 相似度 = `Σ col_w[共享列] / (row_w[a] + row_w[b] - Σ col_w[共享列])`
- 直觉：共享**高频列**（对应被反复访问的 B-row）比共享冷门列更有 cache 价值

顺带修了个 bug：函数开头 `while(!sims.empty()) sims.pop()` 清空全局队列，避免跨次调用的陈旧状态。

---

## 5. 观察 & 下一步方向

**Vlength vs Hierarchical 的性质差异很明显：**

- Vlength 的 clustering score 冲到 1.49（AS365/M6 逼近 2.0 上限），但这两个矩阵反而**变慢了**（0.89x / 0.85x）。原因：连续性约束下，为了减少簇数把不太相似的行硬凑进一个簇，B-row 工作集反而变大。**80/20 权重下这仍然得了高分——评分函数可能被 clustering 项部分套利了。**
- Hierarchical 的 timing 各矩阵均衡（全部 ≈1.0），但 clustering 几乎没提升（1.0037）。baseline 的 union-find 已经做得不错，LLM 只能在相似度函数上做文章。

**可以试的方向：**

1. **Vlength 的 AS365/M6 回归**是最明显的短板。可以在 prompt 里加一句"不要为了减少簇数牺牲单矩阵性能"，或者把 clustering 权重降到 0.1。
2. **Hierarchical 空间没打开**：prompt 的 STRATEGIES 只写了"Use different similarity function"，LLM 果然只改了相似度。可以加入合并顺序、簇大小自适应、centroid-based merge 等方向。
3. **上全量 9 个矩阵**：现在只跑 5 个 stage1，`NLR`/`GAP-road`/`europe_osm`/`com-LiveJournal` 从没验证过。best program 在大矩阵上的表现是未知数。
4. **交叉验证**：把 vlength 的 overlap-coefficient 思路移植到 hierarchical，或反之（加权 Jaccard 用到 vlength）。

---

## 6. 文件位置速查

| 内容 | 路径 |
|---|---|
| 演化输出（本次） | `evolve_runs/vlength_20260420_210803/`, `evolve_runs/hierarchical_20260421_031627/` |
| best program | `<run_dir>/best/best_program.cpp` + `best_program_info.json` |
| checkpoint（可 resume） | `<run_dir>/checkpoints/checkpoint_70` |
| 完整日志 | `<run_dir>/logs/openevolve_*.log` |
| baseline 计时缓存 | `baseline_timings.json`（机器相关，勿跨机拷贝） |
| 指纹缓存 | `fingerprint_cache.json`（机器相关，勿跨机拷贝） |
| 更早的历史 run | `evolve_runs/20260316_*`, `evolve_runs/20260317_*` |
| evolve 使用文档 | `README_EVOLVE.md`（结构 / 运行 / 结果位置） |
| spark 端备份 | `dgx-spark:~/projects/clusterwise-spgemm/.pre_evolve_backup/` |

Resume 命令：`--checkpoint evolve_runs/<run>/checkpoints/checkpoint_70`

**清理提醒**：`bin/spgemm_candidate_*` 是被中断的 run 留下的临时二进制，可以删。
