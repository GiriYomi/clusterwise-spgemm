"""
Evaluator for Cluster-wise SpGEMM evolution.

Evaluates evolved clustering strategies in VlengthClusterSpGEMM and
HierarchicalClusterSpGEMM by compiling the modified C++ source and
benchmarking A^2 (matrix squaring) on SuiteSparse matrices.

Key design:
- Subprocess isolation: compile + run in a child process so segfaults
  don't kill the evolution loop.
- Warmup run (1st run discarded) + 3 timed runs per matrix.
- If clustering metrics are identical to baseline, reuse baseline timing
  score (combined_score = 1.0) to avoid noise misleading the LLM.
- If clustering changed: use median of 3 timed runs for combined_score,
  report mean as a secondary metric.
- Timing is the primary signal (80%), clustering quality secondary (20%).
- Slowdown penalty: if median speedup < 0.8, score is penalized.
"""

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import shutil
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.join(_PROJECT_ROOT, "bin")
os.makedirs(_BIN_DIR, exist_ok=True)

# Dataset locations are overridable via env vars so the same evaluator runs
# unchanged on any host (e.g. DGX Spark keeps data in ~/datasets/spgemm):
#   DATA_PATH             root holding <name>/<name>.mtx  (default: <root>/data)
#   CLOSE_PAIR_DATA_PATH  close-pairs directory           (default: <DATA_PATH>/close_pairs)
_DATA_DIR = os.environ.get("DATA_PATH", os.path.join(_PROJECT_ROOT, "data"))
_CLOSE_PAIRS_DIR = os.environ.get(
    "CLOSE_PAIR_DATA_PATH", os.path.join(_DATA_DIR, "close_pairs"))

_GTGRAPH_OBJS = " ".join(
    os.path.join(_PROJECT_ROOT, "GTgraph", "R-MAT", f)
    for f in ["graph.o", "utils.o", "init.o", "globals.o"]
)

# TBB may come from a conda env (e.g. env `spgemm` provides tbb-devel).
# Set TBB_PREFIX explicitly (run_evolve.sh does this on DGX Spark);
# left unset, the system TBB is used — matches the original local setup.
_TBB_PREFIX = os.environ.get("TBB_PREFIX")

_INCLUDES = f"-I{os.path.join(_PROJECT_ROOT, 'GTgraph', 'sprng2.0-lite', 'include')}"
if _TBB_PREFIX:
    _INCLUDES += f" -I{os.path.join(_TBB_PREFIX, 'include')}"

_LIBS = f"-L{os.path.join(_PROJECT_ROOT, 'GTgraph', 'sprng2.0-lite', 'lib')}"
if _TBB_PREFIX:
    _LIBS += f" -L{os.path.join(_TBB_PREFIX, 'lib')}"
_LIBS += " -lsprng -ltbb -ltbbmalloc -lm"
_CXXFLAGS = "-O3 -fopenmp -std=c++17 -DTBB"

_NTHREADS = os.cpu_count() or 4

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------
# Timing (stabilized via warmup+median) is the primary signal (80%).
# Clustering quality is secondary (20%) — guards against "fewer clusters but slower".
_WEIGHT_TIMING = 0.80
_WEIGHT_CLUSTERING = 0.20

# Clustering score cap — prevents "infinite cluster reduction" from dominating
_CLUSTERING_SCORE_CAP = 2.0

# Slowdown penalty threshold — if median speedup < this, penalize
_SLOWDOWN_THRESHOLD = 0.8

# Number of timed runs (after 1 warmup)
_TIMING_RUNS = 3

# ---------------------------------------------------------------------------
# Dataset definitions (9 representative matrices from the paper)
# ---------------------------------------------------------------------------
DATASETS: List[Dict[str, Any]] = [
    {"name": "patents_main", "group": "Pajek/patents_main", "stage1": True},
    {"name": "webbase-1M", "group": "Williams/webbase-1M", "stage1": True},
    {"name": "kkt_power", "group": "Zaoui/kkt_power", "stage1": True},
    {"name": "AS365", "group": "DIMACS10/AS365", "stage1": True},
    {"name": "M6", "group": "DIMACS10/M6", "stage1": True},
    {"name": "NLR", "group": "DIMACS10/NLR", "stage1": False},
    {"name": "GAP-road", "group": "GAP/GAP-road", "stage1": False},
    {"name": "europe_osm", "group": "DIMACS10/europe_osm", "stage1": False},
    {"name": "com-LiveJournal", "group": "SNAP/com-LiveJournal", "stage1": False},
]

# Baseline cache stores both timings and clustering metrics.
_BASELINE_CACHE_FILE = os.path.join(_PROJECT_ROOT, "baseline_timings.json")

# Fingerprint cache: maps clustering fingerprint → cached combined_score + metrics.
# If a candidate produces the same clustering as any previous candidate (or parent),
# we reuse the cached score to eliminate timing noise entirely.
_FINGERPRINT_CACHE_FILE = os.path.join(_PROJECT_ROOT, "fingerprint_cache.json")

# Timeouts
_COMPILE_TIMEOUT = 120  # seconds
_RUN_TIMEOUT_STAGE1 = 120  # seconds per matrix
_RUN_TIMEOUT_STAGE2 = 600  # seconds per matrix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_mtx_path(name: str) -> str:
    return os.path.join(_DATA_DIR, name, f"{name}.mtx")


def _get_pairs_path(name: str) -> str:
    # Two naming conventions exist:
    #   local:     <name>_closepairs.txt
    #   DGX Spark: <name>.mtx  (same content, different suffix)
    for fname in (f"{name}_closepairs.txt", f"{name}.mtx"):
        path = os.path.join(_CLOSE_PAIRS_DIR, fname)
        if os.path.exists(path):
            return path
    return os.path.join(_CLOSE_PAIRS_DIR, f"{name}_closepairs.txt")


def _detect_program_type(program_path: str) -> str:
    """Detect whether the evolved file is VlengthCluster or Hierarchical."""
    with open(program_path, "r") as f:
        content = f.read()
    if "hierachical_clustering" in content or "HierarchicalCluster" in content:
        return "hierarchical"
    return "vlength"


def _compile(source_path: str, binary_path: str) -> Tuple[bool, str]:
    """Compile a C++ source file. Returns (success, error_message).

    Because the source uses relative #include paths like "../utility.h",
    the file must be compiled from the sample/ directory. If the source is
    not already inside sample/, we copy it there first.
    """
    sample_dir = os.path.join(_PROJECT_ROOT, "sample")
    src_abs = os.path.abspath(source_path)

    # If source is outside sample/, copy it in so relative includes work
    if not src_abs.startswith(os.path.abspath(sample_dir)):
        tmp_src = os.path.join(sample_dir, f"_candidate_{os.path.basename(source_path)}")
        shutil.copy2(src_abs, tmp_src)
        compile_src = tmp_src
    else:
        compile_src = src_abs
        tmp_src = None

    cmd = f"g++ {_CXXFLAGS} {_INCLUDES} -o {binary_path} {compile_src} {_GTGRAPH_OBJS} {_LIBS}"
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=_COMPILE_TIMEOUT, cwd=_PROJECT_ROOT,
        )
        if result.returncode != 0:
            errors = [l for l in result.stderr.split("\n") if "error" in l.lower()]
            err_msg = "\n".join(errors[:10]) if errors else result.stderr[-500:]
            return False, f"Compilation failed:\n{err_msg}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out"
    finally:
        if tmp_src and os.path.exists(tmp_src):
            os.unlink(tmp_src)


def _run_spgemm_once(binary_path: str, matrix_name: str, program_type: str,
                     timeout: int) -> Dict[str, Any]:
    """
    Run the SpGEMM binary on a matrix (A^2) once.
    Returns dict with keys:
      - "ok": bool
      - "error": str (if not ok)
      - "time_ms": float (timing, noisy)
      - "num_clusters": int (deterministic)
      - "max_cluster_size": int (deterministic)
    """
    mtx = _get_mtx_path(matrix_name)
    if not os.path.exists(mtx):
        return {"ok": False, "error": f"Matrix file not found: {mtx}"}

    if program_type == "hierarchical":
        pairs = _get_pairs_path(matrix_name)
        if not os.path.exists(pairs):
            return {"ok": False, "error": f"Close-pairs file not found: {pairs}"}
        cmd = [binary_path, "text", mtx, mtx, pairs, "8", str(_NTHREADS)]
    else:
        cmd = [binary_path, "text", mtx, mtx, "8", str(_NTHREADS)]

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(_NTHREADS)
    if _TBB_PREFIX:  # candidate binaries link against conda-provided TBB
        env["LD_LIBRARY_PATH"] = os.path.join(_TBB_PREFIX, "lib") + ":" + env.get("LD_LIBRARY_PATH", "")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=_PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Runtime timed out after {timeout}s"}

    if result.returncode != 0:
        if result.returncode < 0:
            return {"ok": False, "error": f"Killed by signal {-result.returncode} (segfault/OOM)"}
        return {"ok": False, "error": f"Exit code {result.returncode}: {result.stderr[-300:]}"}

    output = result.stdout

    time_match = re.search(
        r"computes C = A \* B in ([\d.]+) \[milli seconds\].*?([\d.]+) \[MFLOPS\]",
        output,
    )
    if not time_match:
        return {"ok": False, "error": f"Could not parse timing from output:\n{output[-500:]}"}

    time_ms = float(time_match.group(1))

    num_clusters = None
    max_cs = None

    nc_match = re.search(r"# of clusters:\s*(\d+)", output)
    if nc_match:
        num_clusters = int(nc_match.group(1))

    mcs_match = re.search(r"max_cluster_size for SpGEMM:\s*(\d+)", output)
    if mcs_match:
        max_cs = int(mcs_match.group(1))

    return {
        "ok": True,
        "time_ms": time_ms,
        "num_clusters": num_clusters,
        "max_cluster_size": max_cs,
    }


def _run_spgemm_stabilized(binary_path: str, matrix_name: str, program_type: str,
                           timeout: int) -> Dict[str, Any]:
    """
    Run SpGEMM with 1 warmup + N timed runs. Returns clustering metrics
    from run 1, and median/mean timing from the timed runs.

    Returns dict with keys:
      - "ok": bool
      - "error": str (if not ok)
      - "time_ms_median": float
      - "time_ms_mean": float
      - "time_ms_all": list[float]  (all timed runs, for diagnostics)
      - "num_clusters": int (deterministic, from first successful run)
      - "max_cluster_size": int (deterministic)
    """
    # --- Warmup run (discard timing, keep clustering metrics) ---
    warmup = _run_spgemm_once(binary_path, matrix_name, program_type, timeout)
    if not warmup["ok"]:
        return warmup

    num_clusters = warmup["num_clusters"]
    max_cs = warmup["max_cluster_size"]

    # --- Timed runs ---
    timings = []
    for _ in range(_TIMING_RUNS):
        r = _run_spgemm_once(binary_path, matrix_name, program_type, timeout)
        if not r["ok"]:
            return r
        timings.append(r["time_ms"])

    return {
        "ok": True,
        "time_ms_median": statistics.median(timings),
        "time_ms_mean": statistics.mean(timings),
        "time_ms_all": timings,
        "num_clusters": num_clusters,
        "max_cluster_size": max_cs,
    }


# ---------------------------------------------------------------------------
# Baseline management
# ---------------------------------------------------------------------------
def _load_baseline_cache() -> Dict[str, Any]:
    """Load cached baseline data (timings + clustering metrics)."""
    if os.path.exists(_BASELINE_CACHE_FILE):
        with open(_BASELINE_CACHE_FILE) as f:
            return json.load(f)
    return {}


def _save_baseline_cache(cache: Dict[str, Any]):
    with open(_BASELINE_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _ensure_baseline(program_type: str, datasets: List[Dict]) -> Dict[str, Any]:
    """
    Ensure baseline data exists for the given datasets and program type.
    Compiles and runs the original (unmodified) source if needed.
    Uses stabilized timing (warmup + median of 3 runs).
    """
    cache = _load_baseline_cache()
    missing = []
    for ds in datasets:
        key = f"{program_type}_{ds['name']}"
        if f"{key}_median" not in cache or f"{key}_num_clusters" not in cache:
            missing.append(ds)

    if not missing:
        return cache

    # Compile baseline
    if program_type == "vlength":
        src = os.path.join(_PROJECT_ROOT, "sample", "VlengthClusterSpGEMM.cpp")
    else:
        src = os.path.join(_PROJECT_ROOT, "sample", "HierarchicalClusterSpGEMM.cpp")

    baseline_bin = os.path.join(_BIN_DIR, f"baseline_{program_type}")
    ok, err = _compile(src, baseline_bin)
    if not ok:
        print(f"[baseline] Failed to compile: {err}", file=sys.stderr)
        return cache

    for ds in missing:
        name = ds["name"]
        key = f"{program_type}_{name}"
        run_result = _run_spgemm_stabilized(
            baseline_bin, name, program_type, _RUN_TIMEOUT_STAGE2)

        if run_result["ok"]:
            cache[f"{key}_median"] = run_result["time_ms_median"]
            cache[f"{key}_mean"] = run_result["time_ms_mean"]
            # Keep legacy key for backwards compat
            cache[key] = run_result["time_ms_median"]
            if run_result["num_clusters"] is not None:
                cache[f"{key}_num_clusters"] = run_result["num_clusters"]
            if run_result["max_cluster_size"] is not None:
                cache[f"{key}_max_cs"] = run_result["max_cluster_size"]
            print(f"[baseline] {name}: median={run_result['time_ms_median']:.2f}ms "
                  f"mean={run_result['time_ms_mean']:.2f}ms "
                  f"runs={run_result['time_ms_all']} "
                  f"clusters={run_result['num_clusters']} "
                  f"max_cs={run_result['max_cluster_size']}", file=sys.stderr)
        else:
            print(f"[baseline] {name}: FAILED - {run_result['error']}", file=sys.stderr)

    _save_baseline_cache(cache)
    return cache


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _compute_clustering_score(
    baseline_num_clusters: Optional[int],
    baseline_max_cs: Optional[int],
    cand_num_clusters: Optional[int],
    cand_max_cs: Optional[int],
) -> Optional[float]:
    """
    Compute a deterministic clustering quality score (capped).

    Returns a ratio where >1.0 means candidate has better clustering.
    Capped at _CLUSTERING_SCORE_CAP to prevent "over-clustering" gaming.
    """
    if any(v is None for v in [baseline_num_clusters, baseline_max_cs,
                                cand_num_clusters, cand_max_cs]):
        return None

    if cand_num_clusters == 0 or baseline_num_clusters == 0:
        return None

    cluster_ratio = min(
        baseline_num_clusters / max(cand_num_clusters, 1),
        _CLUSTERING_SCORE_CAP,
    )
    max_cs_ratio = min(
        cand_max_cs / max(baseline_max_cs, 1),
        _CLUSTERING_SCORE_CAP,
    )

    score = 0.8 * cluster_ratio + 0.2 * max_cs_ratio
    return score


def _load_fingerprint_cache() -> Dict[str, Any]:
    if os.path.exists(_FINGERPRINT_CACHE_FILE):
        with open(_FINGERPRINT_CACHE_FILE) as f:
            return json.load(f)
    return {}


def _save_fingerprint_cache(cache: Dict[str, Any]):
    with open(_FINGERPRINT_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _clustering_fingerprint(
    program_type: str,
    cluster_data: Dict[str, Tuple[Optional[int], Optional[int]]],
) -> str:
    """
    Build a deterministic fingerprint from clustering metrics across all datasets.
    cluster_data: {matrix_name: (num_clusters, max_cluster_size)}
    If two candidates produce the same fingerprint, their clustering is identical.
    """
    parts = [program_type]
    for name in sorted(cluster_data.keys()):
        nc, mcs = cluster_data[name]
        parts.append(f"{name}:{nc}:{mcs}")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------
def _evaluate_candidate(
    program_path: str,
    datasets: List[Dict],
    timeout_per_matrix: int,
) -> Dict[str, Any]:
    """
    Compile the evolved program and benchmark it against baseline.

    Scoring strategy:
    1. Run once per matrix (probe) to get deterministic clustering metrics.
    2. Build clustering fingerprint from (num_clusters, max_cs) across all matrices.
    3. If fingerprint matches ANY previously seen candidate (parent, sibling, etc.):
       → Reuse cached combined_score. Zero extra timing overhead.
       → This eliminates noise: same clustering = same score, always.
    4. If fingerprint is NEW:
       → Probe served as warmup. Do 3 more timed runs per matrix.
       → combined_score = 0.60 * median_speedup + 0.40 * clustering_score
       → Mean speedup recorded as informational metric.
       → If median speedup < 0.8: apply penalty multiplier.
       → Cache the result for future fingerprint matches.
    """
    metrics: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}

    program_type = _detect_program_type(program_path)
    artifacts["program_type"] = program_type

    # --- Compile ---
    with tempfile.NamedTemporaryFile(suffix="", prefix="spgemm_candidate_", delete=False,
                                     dir=_BIN_DIR) as tmp:
        candidate_bin = tmp.name

    try:
        ok, err = _compile(program_path, candidate_bin)
        if not ok:
            metrics["compiles"] = 0.0
            metrics["combined_score"] = 0.0
            artifacts["compile_error"] = err
            return {"metrics": metrics, "artifacts": artifacts}

        metrics["compiles"] = 1.0
        os.chmod(candidate_bin, 0o755)

        # --- Get baseline data ---
        baseline = _ensure_baseline(program_type, datasets)

        # --- Phase 1: single probe run per dataset (get clustering metrics) ---
        probe_results = {}
        cluster_data = {}  # {name: (num_clusters, max_cs)}
        datasets_passed = 0

        for ds in datasets:
            name = ds["name"]
            baseline_key = f"{program_type}_{name}"
            baseline_nc = baseline.get(f"{baseline_key}_num_clusters")
            baseline_mcs = baseline.get(f"{baseline_key}_max_cs")

            probe = _run_spgemm_once(candidate_bin, name, program_type, timeout_per_matrix)
            probe_results[name] = probe

            if probe["ok"]:
                datasets_passed += 1
                cand_nc = probe["num_clusters"]
                cand_mcs = probe["max_cluster_size"]
                cluster_data[name] = (cand_nc, cand_mcs)

                if cand_nc is not None:
                    metrics[f"{name}_num_clusters"] = cand_nc
                    metrics[f"{name}_baseline_num_clusters"] = baseline_nc
                if cand_mcs is not None:
                    metrics[f"{name}_max_cluster_size"] = cand_mcs
                    metrics[f"{name}_baseline_max_cs"] = baseline_mcs
            else:
                artifacts[f"{name}_error"] = probe["error"]

        metrics["datasets_passed"] = float(datasets_passed)
        metrics["runs_successfully"] = float(datasets_passed) / max(len(datasets), 1)

        if datasets_passed == 0:
            metrics["combined_score"] = 0.0
            return {"metrics": metrics, "artifacts": artifacts}

        # --- Phase 2: check fingerprint cache ---
        fingerprint = _clustering_fingerprint(program_type, cluster_data)
        fp_cache = _load_fingerprint_cache()

        if fingerprint in fp_cache:
            # Clustering identical to a previous candidate → reuse score
            cached = fp_cache[fingerprint]
            metrics["combined_score"] = cached["combined_score"]
            metrics["avg_timing_speedup_median"] = cached.get("avg_timing_speedup_median", 1.0)
            metrics["avg_timing_speedup_mean"] = cached.get("avg_timing_speedup_mean", 1.0)
            metrics["avg_clustering_score"] = cached.get("avg_clustering_score", 1.0)
            metrics["fingerprint_cache_hit"] = 1.0
            artifacts["scoring_path"] = "fingerprint_cache_hit"

            # Record probe timings as informational
            for ds in datasets:
                name = ds["name"]
                probe = probe_results.get(name)
                if probe and probe["ok"]:
                    metrics[f"{name}_probe_ms"] = probe["time_ms"]

            return {"metrics": metrics, "artifacts": artifacts}

        # --- Phase 3: new fingerprint → full stabilized timing ---
        metrics["fingerprint_cache_hit"] = 0.0
        artifacts["scoring_path"] = "full_timing"

        timing_speedups_median = []
        timing_speedups_mean = []
        clustering_scores = []

        for ds in datasets:
            name = ds["name"]
            baseline_key = f"{program_type}_{name}"
            baseline_ms = baseline.get(f"{baseline_key}_median",
                                       baseline.get(baseline_key))
            baseline_nc = baseline.get(f"{baseline_key}_num_clusters")
            baseline_mcs = baseline.get(f"{baseline_key}_max_cs")

            if baseline_ms is None:
                metrics[f"{name}_score"] = 0.0
                artifacts[f"{name}_error"] = "no baseline data"
                timing_speedups_median.append(0.0)
                timing_speedups_mean.append(0.0)
                clustering_scores.append(0.0)
                continue

            probe = probe_results.get(name)
            if not probe or not probe["ok"]:
                timing_speedups_median.append(0.0)
                timing_speedups_mean.append(0.0)
                clustering_scores.append(0.0)
                continue

            # Probe was the warmup. Now do _TIMING_RUNS timed runs.
            run_timings = []
            run_ok = True
            for _ in range(_TIMING_RUNS):
                r = _run_spgemm_once(candidate_bin, name, program_type, timeout_per_matrix)
                if not r["ok"]:
                    run_ok = False
                    artifacts[f"{name}_error"] = r["error"]
                    break
                run_timings.append(r["time_ms"])

            if not run_ok or len(run_timings) == 0:
                timing_speedups_median.append(0.0)
                timing_speedups_mean.append(0.0)
                clustering_scores.append(0.0)
                continue

            median_ms = statistics.median(run_timings)
            mean_ms = statistics.mean(run_timings)

            metrics[f"{name}_candidate_ms_median"] = median_ms
            metrics[f"{name}_candidate_ms_mean"] = mean_ms
            metrics[f"{name}_candidate_ms_all"] = run_timings
            metrics[f"{name}_baseline_ms"] = baseline_ms

            speedup_median = baseline_ms / max(median_ms, 0.001)
            speedup_mean = baseline_ms / max(mean_ms, 0.001)
            metrics[f"{name}_speedup_median"] = speedup_median
            metrics[f"{name}_speedup_mean"] = speedup_mean
            timing_speedups_median.append(max(speedup_median, 0.0))
            timing_speedups_mean.append(max(speedup_mean, 0.0))

            # Clustering score (capped)
            cand_nc = probe["num_clusters"]
            cand_mcs = probe["max_cluster_size"]
            cs = _compute_clustering_score(baseline_nc, baseline_mcs, cand_nc, cand_mcs)
            if cs is not None:
                metrics[f"{name}_clustering_score"] = cs
                clustering_scores.append(cs)
            else:
                clustering_scores.append(1.0)

            # Per-dataset combined
            if cs is not None:
                metrics[f"{name}_score"] = (
                    _WEIGHT_TIMING * max(speedup_median, 0.0)
                    + _WEIGHT_CLUSTERING * cs
                )
            else:
                metrics[f"{name}_score"] = max(speedup_median, 0.0)

        # --- Aggregate ---
        n = len(timing_speedups_median)
        if n > 0:
            avg_speedup_median = sum(timing_speedups_median) / n
            avg_speedup_mean = sum(timing_speedups_mean) / n
            avg_clustering = sum(clustering_scores) / n

            metrics["avg_timing_speedup_median"] = avg_speedup_median
            metrics["avg_timing_speedup_mean"] = avg_speedup_mean
            metrics["avg_clustering_score"] = avg_clustering

            combined = (
                _WEIGHT_TIMING * avg_speedup_median
                + _WEIGHT_CLUSTERING * avg_clustering
            )

            # Slowdown penalty: if candidate is significantly slower,
            # penalize to prevent "good clustering but terrible timing"
            if avg_speedup_median < _SLOWDOWN_THRESHOLD:
                combined *= avg_speedup_median
                artifacts["slowdown_penalty_applied"] = True

            metrics["combined_score"] = combined
        else:
            metrics["combined_score"] = 0.0

        # --- Cache this fingerprint's results ---
        fp_cache[fingerprint] = {
            "combined_score": metrics["combined_score"],
            "avg_timing_speedup_median": metrics.get("avg_timing_speedup_median", 0.0),
            "avg_timing_speedup_mean": metrics.get("avg_timing_speedup_mean", 0.0),
            "avg_clustering_score": metrics.get("avg_clustering_score", 0.0),
        }
        _save_fingerprint_cache(fp_cache)

    finally:
        if os.path.exists(candidate_bin):
            os.unlink(candidate_bin)

    return {"metrics": metrics, "artifacts": artifacts}


# ---------------------------------------------------------------------------
# OpenEvolve entry-points
# ---------------------------------------------------------------------------
def evaluate_stage1(program_path: str):
    """
    Stage-1: quick validation on small matrices.
    Fast rejection of candidates that don't compile or crash.
    """
    stage1_datasets = [ds for ds in DATASETS if ds.get("stage1", True)]
    result = _evaluate_candidate(program_path, stage1_datasets, _RUN_TIMEOUT_STAGE1)

    try:
        from openevolve.evaluation_result import EvaluationResult
        return EvaluationResult(metrics=result["metrics"], artifacts=result.get("artifacts", {}))
    except ImportError:
        return result["metrics"]


def evaluate(program_path: str):
    """
    Evaluation on stage-1 (small) matrices only for fast iteration.
    """
    stage1_datasets = [ds for ds in DATASETS if ds.get("stage1", True)]
    result = _evaluate_candidate(program_path, stage1_datasets, _RUN_TIMEOUT_STAGE1)

    try:
        from openevolve.evaluation_result import EvaluationResult
        return EvaluationResult(metrics=result["metrics"], artifacts=result.get("artifacts", {}))
    except ImportError:
        return result["metrics"]


def evaluate_stage2(program_path: str):
    """Stage-2 runs the full evaluation."""
    return evaluate(program_path)


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="SpGEMM clustering evaluator for OpenEvolve"
    )
    parser.add_argument(
        "program_path",
        nargs="?",
        default=None,
        help="Path to evolved .cpp file. If omitted, evaluates the original VlengthClusterSpGEMM.",
    )
    parser.add_argument("--stage1", action="store_true", help="Run stage-1 only")
    parser.add_argument(
        "--type", choices=["vlength", "hierarchical"], default=None,
        help="Force program type (auto-detected if omitted)",
    )
    args = parser.parse_args()

    if args.program_path is None:
        args.program_path = os.path.join(
            _PROJECT_ROOT, "sample", "VlengthClusterSpGEMM.cpp"
        )

    print(f"Program: {args.program_path}")
    print(f"Threads: {_NTHREADS}")
    print(f"Scoring: timing={_WEIGHT_TIMING}, clustering={_WEIGHT_CLUSTERING}")
    print(f"Timing: 1 warmup + {_TIMING_RUNS} runs, median for score")
    print(f"Clustering score cap: {_CLUSTERING_SCORE_CAP}")
    print(f"Slowdown penalty threshold: {_SLOWDOWN_THRESHOLD}")
    print()

    if args.stage1:
        print("=== Stage-1 (quick validation) ===")
        result = evaluate_stage1(args.program_path)
    else:
        print("=== Full evaluation ===")
        result = evaluate(args.program_path)

    if hasattr(result, "metrics"):
        metrics = result.metrics
        artifacts = getattr(result, "artifacts", {})
    else:
        metrics = result
        artifacts = {}

    print("\n--- Metrics ---")
    for k, v in sorted(metrics.items()):
        print(f"  {k}: {v}")

    if artifacts:
        print("\n--- Artifacts ---")
        for k, v in sorted(artifacts.items()):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
