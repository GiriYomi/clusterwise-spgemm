# OpenEvolve × Cluster-wise SpGEMM

Evolving the **clustering strategy** of Cluster-wise SpGEMM with
[OpenEvolve](https://github.com/codelion/openevolve) (open-source AlphaEvolve).
The LLM repeatedly rewrites the code between `EVOLVE-BLOCK-START` /
`EVOLVE-BLOCK-END` markers in the C++ sources; the evaluator compiles each
candidate, benchmarks A² on SuiteSparse matrices, and scores it against the
baseline. This document covers the evolve setup only — see `README.md` for the
underlying SpGEMM library.

---

## 1. File structure

### Evolve framework (this bundle)

| File | Role |
|---|---|
| `evaluator.py` | OpenEvolve evaluator: compile → run → score. Entry points `evaluate()` / `evaluate_stage1()` / `evaluate_stage2()` |
| `config_vlength.yaml` | Evolution config for VlengthCluster (LLM, prompt, islands, 70 iterations) |
| `config_hierarchical.yaml` | Same for HierarchicalCluster |
| `sample/VlengthClusterSpGEMM.cpp` | Initial program #1 — greedy consecutive-row clustering (evolve block inside `main`) |
| `sample/HierarchicalClusterSpGEMM.cpp` | Initial program #2 — priority-queue + union-find clustering (evolve block = whole `hierachical_clustering_v0` function) |
| `HANDOFF.md` | Session handoff: past results, observations, next steps |

Both `.cpp` files **must keep** their `// EVOLVE-BLOCK-START` / `// EVOLVE-BLOCK-END`
markers — OpenEvolve only mutates code between them.

### The two experiments

| | vlength | hierarchical |
|---|---|---|
| Constraint | rows in a cluster must be **consecutive** | rows grouped **freely** (union-find) |
| Output contract | `vector<INDEXTYPE> offset` + `real_max_cluster_size` | `map<INDEXTYPE, vector<INDEXTYPE>>` |
| Extra input | — | close-pairs file per matrix |

### Generated at runtime (git-ignored, machine-specific)

| Path | What |
|---|---|
| `baseline_timings.json` | Baseline timing + clustering metrics cache. **Auto-created on first run — never copy between machines** |
| `fingerprint_cache.json` | Clustering-fingerprint → score cache (noise elimination). Machine-specific too |
| `bin/spgemm_candidate_*` | Transient candidate binaries (auto-deleted; leftovers only after a killed run) |
| `bin/baseline_vlength`, `bin/baseline_hierarchical` | Compiled baseline binaries |

---

## 2. Prerequisites

1. **Data** — 5 stage-1 matrices, each as `<DATA_PATH>/<name>/<name>.mtx`:
   `patents_main`, `webbase-1M`, `kkt_power`, `AS365`, `M6`
2. **Close pairs** (hierarchical only) — `<CLOSE_PAIR_DATA_PATH>/<name>_closepairs.txt`
   or `<name>.mtx` (both naming conventions are accepted)
3. **GTgraph objects** — `make rmat` once (produces `GTgraph/R-MAT/*.o` + `libsprng.a`)
4. **Python** — `pip install openevolve` (developed against 0.2.16)
5. **API key** — `export OPENAI_API_KEY=...` (config uses `gpt-5`)

### Path configuration (env vars, all optional)

`evaluator.py` resolves paths in this order:

| Env var | Default | DGX Spark value |
|---|---|---|
| `DATA_PATH` | `<repo>/data` | `~/datasets/spgemm` |
| `CLOSE_PAIR_DATA_PATH` | `<DATA_PATH>/close_pairs` | `~/datasets/spgemm/reordering/close_pairs` |
| `TBB_PREFIX` | unset → system TBB | `$CONDA_PREFIX` of the `spgemm` conda env |

`TBB_PREFIX` adds `-I$TBB_PREFIX/include -L$TBB_PREFIX/lib` at compile time and
prepends `$TBB_PREFIX/lib` to `LD_LIBRARY_PATH` when running candidates.

---

## 3. Running an experiment

```bash
cd <repo-root>
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# --- vlength ---
openevolve-run \
  sample/VlengthClusterSpGEMM.cpp evaluator.py \
  --config config_vlength.yaml \
  --output evolve_runs/vlength_${TIMESTAMP}

# --- hierarchical ---
openevolve-run \
  sample/HierarchicalClusterSpGEMM.cpp evaluator.py \
  --config config_hierarchical.yaml \
  --output evolve_runs/hierarchical_${TIMESTAMP}
```

On DGX Spark, export the env vars first:

```bash
conda activate spgemm
export DATA_PATH=~/datasets/spgemm
export CLOSE_PAIR_DATA_PATH=~/datasets/spgemm/reordering/close_pairs
export TBB_PREFIX=$CONDA_PREFIX
export OPENAI_API_KEY=...   # added manually, never committed
```

Notes:
- A 70-iteration run takes **~3.5 h** (each iteration ≈ LLM call + compile + 5 matrices × 4 runs). Run it inside `tmux`/`nohup`.
- First run also measures the baseline (~2 min extra, cached afterwards).
- Resume from a checkpoint:
  `openevolve-run ... --checkpoint evolve_runs/<run>/checkpoints/checkpoint_<N>`

---

## 4. Where results land

Every run writes to **`evolve_runs/<experiment>_<timestamp>/`** — this is the
agreed location on both machines (local and Spark):

```
evolve_runs/
└── vlength_20260420_210803/          # <experiment>_<YYYYmmdd_HHMMSS>
    ├── best/
    │   ├── best_program.cpp          # winning program (markers preserved)
    │   └── best_program_info.json    # score, generation, iteration, metrics
    ├── checkpoints/checkpoint_<N>/   # every 5 iterations, resumable
    └── logs/openevolve_*.log         # full evolution log
```

Promotion convention (matches the Spark repo layout): copy a validated
`best_program.cpp` into **`best_vlength/`** or **`best_hierarchical/`** at the
repo root, then `make best_hw` builds `bin/Best{Vlength,Hierarchical}SpGEMM_hw`
and `scripts/evolve/run_best.sh` benchmarks best-vs-baseline on all matrices,
writing to **`results/evolve-<timestamp>/`**.

---

## 5. Scoring (how to read `combined_score`)

```
combined_score = 0.80 × avg_timing_speedup_median + 0.20 × avg_clustering_score
```

- **Timing (80 %)** — `baseline_median / candidate_median`, 1 warm-up + 3 timed
  runs per matrix. Values > 1.0 are faster than baseline.
- **Clustering (20 %)** — `0.8 × min(baseline_clusters/cand_clusters, 2.0) +
  0.2 × min(cand_max_cs/baseline_max_cs, 2.0)`, capped at 2.0.
- Slowdown penalty: if avg speedup < 0.8, score is multiplied by it.
- Baseline scores exactly **1.0**; anything above is a genuine improvement.
- Identical clustering fingerprints reuse cached scores (timing-noise immunity).

Reference results so far (70 iterations each, local machine):
**vlength 1.1346** (webbase-1M 1.41×) · **hierarchical 1.0281** — details and
evolved-strategy analysis in `HANDOFF.md`.
