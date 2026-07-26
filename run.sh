#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$PROJECT_ROOT/bin"
TESTDATA="$PROJECT_ROOT/testdata"
DATA_DIR="$PROJECT_ROOT/data"
RESULTS_DIR="$PROJECT_ROOT/results"
NTHREADS="${OMP_NUM_THREADS:-$(nproc)}"

# ── 10 representative matrices from the paper ─────────────
# Paper: "Improving SpGEMM Performance Through Matrix Reordering
#         and Cluster-wise Computation" (arXiv:2507.21253)
# Operation: A^2 (matrix squaring), 64 threads on AMD EPYC 7763
REPRESENTATIVE_MATRICES=(
    "Williams/webbase-1M"
    "Pajek/patents_main"
    "DIMACS10/AS365"
    "SNAP/com-LiveJournal"
    "DIMACS10/europe_osm"
    "GAP/GAP-road"
    "Zaoui/kkt_power"
    "DIMACS10/M6"
    "DIMACS10/NLR"
)

# ── Build ──────────────────────────────────────────────────
build() {
    echo "=== Building dependencies ==="
    (cd "$PROJECT_ROOT" && CC=gcc make rmat 2>&1 | tail -3)

    mkdir -p "$BIN"
    CXXFLAGS="-O3 -fopenmp -std=c++17 -DTBB"
    INCLUDES="-I${PROJECT_ROOT}/GTgraph/sprng2.0-lite/include"
    OBJS="${PROJECT_ROOT}/GTgraph/R-MAT/graph.o ${PROJECT_ROOT}/GTgraph/R-MAT/utils.o ${PROJECT_ROOT}/GTgraph/R-MAT/init.o ${PROJECT_ROOT}/GTgraph/R-MAT/globals.o"
    LIBS="-L${PROJECT_ROOT}/GTgraph/sprng2.0-lite/lib -lsprng -ltbb -ltbbmalloc -lm"

    echo "=== Compiling VlengthClusterSpGEMM ==="
    g++ $CXXFLAGS $INCLUDES -o "$BIN/VlengthClusterSpGEMM" \
        "$PROJECT_ROOT/sample/VlengthClusterSpGEMM.cpp" $OBJS $LIBS

    echo "=== Compiling HierarchicalClusterSpGEMM ==="
    g++ $CXXFLAGS $INCLUDES -o "$BIN/HierarchicalClusterSpGEMM" \
        "$PROJECT_ROOT/sample/HierarchicalClusterSpGEMM.cpp" $OBJS $LIBS

    echo "=== Compiling GenerateCandidatePairs ==="
    g++ $CXXFLAGS $INCLUDES -o "$BIN/GenerateCandidatePairs" \
        "$PROJECT_ROOT/sample/GenerateCandidatePairs.cpp" $OBJS $LIBS

    echo "=== Build complete ==="
}

# ── Download ───────────────────────────────────────────────
download_matrix() {
    local group_name="$1"   # e.g. "DIMACS10/AS365"
    local name="${group_name##*/}"

    local mtx_file="$DATA_DIR/$name/$name.mtx"
    if [[ -f "$mtx_file" ]]; then
        echo "[skip] $name already exists"
        return
    fi

    mkdir -p "$DATA_DIR/$name"

    # Try primary URL first, fallback to secondary
    local url1="https://suitesparse-collection-website.herokuapp.com/MM/${group_name}.tar.gz"
    local url2="https://sparse.tamu.edu/MM/${group_name}.tar.gz"

    echo "[download] $name ..."
    if ! wget -q --show-progress -O "$DATA_DIR/$name.tar.gz" "$url1" 2>/dev/null; then
        if ! wget -q --show-progress -O "$DATA_DIR/$name.tar.gz" "$url2" 2>/dev/null; then
            echo "[ERROR] Failed to download $name"
            rm -f "$DATA_DIR/$name.tar.gz"
            return 1
        fi
    fi

    echo "[extract] $name ..."
    tar -xzf "$DATA_DIR/$name.tar.gz" -C "$DATA_DIR/"
    rm -f "$DATA_DIR/$name.tar.gz"

    if [[ -f "$mtx_file" ]]; then
        echo "[ok] $name -> $mtx_file"
    else
        echo "[ERROR] $name extracted but .mtx not found"
        return 1
    fi
}

download_representative() {
    echo "=== Downloading 10 representative matrices from SuiteSparse ==="
    echo "    (Total ~1.3 GB compressed, ~3 GB extracted)"
    echo ""
    mkdir -p "$DATA_DIR"
    for mat in "${REPRESENTATIVE_MATRICES[@]}"; do
        download_matrix "$mat"
    done
    echo ""
    echo "=== Download complete ==="
}

# ── Generate close-pairs for HierarchicalClusterSpGEMM ─────
generate_close_pairs() {
    local name="$1"
    local mtx_file="$DATA_DIR/$name/$name.mtx"
    local topk="${2:-7}"
    local pairs_dir="$DATA_DIR/close_pairs"
    local pairs_file="$pairs_dir/${name}_closepairs.txt"

    if [[ -f "$pairs_file" ]]; then
        echo "[skip] close-pairs for $name already exists"
        return
    fi

    if [[ ! -f "$mtx_file" ]]; then
        echo "[ERROR] Matrix file not found: $mtx_file"
        return 1
    fi

    mkdir -p "$pairs_dir"
    echo "[generate] close-pairs for $name (topk=$topk) ..."
    export CLOSE_PAIR_DATA_PATH="$pairs_dir"
    OMP_NUM_THREADS=$NTHREADS "$BIN/GenerateCandidatePairs" \
        text "$mtx_file" "$mtx_file" "$topk" -s
    echo "[ok] $pairs_file"
}

# ── Run ────────────────────────────────────────────────────
run_vlength_gen() {
    local scale="${1:-14}"
    local edgefactor="${2:-16}"
    local max_cluster="${3:-8}"
    local threads="${4:-$NTHREADS}"

    echo ""
    echo "=============================="
    echo " VlengthClusterSpGEMM (gen)"
    echo " scale=$scale edgefactor=$edgefactor max_cluster=$max_cluster threads=$threads"
    echo "=============================="
    OMP_NUM_THREADS=$threads "$BIN/VlengthClusterSpGEMM" gen rmat "$scale" "$edgefactor" "$max_cluster" "$threads"
}

run_vlength_text() {
    local matA="${1:?usage: run_vlength_text <matA> <matB> [max_cluster] [threads]}"
    local matB="${2:?usage: run_vlength_text <matA> <matB> [max_cluster] [threads]}"
    local max_cluster="${3:-8}"
    local threads="${4:-$NTHREADS}"

    echo ""
    echo "=============================="
    echo " VlengthClusterSpGEMM (text)"
    echo " A=$matA B=$matB max_cluster=$max_cluster threads=$threads"
    echo "=============================="
    OMP_NUM_THREADS=$threads "$BIN/VlengthClusterSpGEMM" text "$matA" "$matB" "$max_cluster" "$threads"
}

run_hierarchical_text() {
    local matA="${1:?usage: run_hierarchical_text <matA> <matB> <close_pairs> [cluster_size] [threads]}"
    local matB="${2:?usage: run_hierarchical_text <matA> <matB> <close_pairs> [cluster_size] [threads]}"
    local pairs="${3:?usage: run_hierarchical_text <matA> <matB> <close_pairs> [cluster_size] [threads]}"
    local cluster_size="${4:-8}"
    local threads="${5:-$NTHREADS}"

    echo ""
    echo "=============================="
    echo " HierarchicalClusterSpGEMM (text)"
    echo " A=$matA B=$matB pairs=$pairs cluster_size=$cluster_size threads=$threads"
    echo "=============================="
    OMP_NUM_THREADS=$threads "$BIN/HierarchicalClusterSpGEMM" text "$matA" "$matB" "$pairs" "$cluster_size" "$threads"
}

# ── Benchmark: run both algorithms on all downloaded matrices ──
bench() {
    local threads="${1:-$NTHREADS}"
    local max_cluster="${2:-8}"
    mkdir -p "$RESULTS_DIR"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local logfile="$RESULTS_DIR/bench_${timestamp}.log"

    echo "=== Benchmark: threads=$threads max_cluster=$max_cluster ==="
    echo "=== Logging to $logfile ==="
    echo ""

    {
        echo "# Benchmark run: $(date)"
        echo "# Threads: $threads, Max cluster size: $max_cluster"
        echo "# Host: $(hostname), CPU: $(lscpu | grep 'Model name' | sed 's/.*: *//')"
        echo ""

        for mat in "${REPRESENTATIVE_MATRICES[@]}"; do
            local name="${mat##*/}"
            local mtx_file="$DATA_DIR/$name/$name.mtx"

            if [[ ! -f "$mtx_file" ]]; then
                echo "[SKIP] $name: matrix file not found, run './run.sh download' first"
                continue
            fi

            echo "================================================================"
            echo " Matrix: $name"
            echo " Operation: A^2 (matrix squaring, following the paper)"
            echo "================================================================"

            # VlengthClusterSpGEMM: A * A
            echo "--- VlengthClusterSpGEMM ---"
            OMP_NUM_THREADS=$threads "$BIN/VlengthClusterSpGEMM" \
                text "$mtx_file" "$mtx_file" "$max_cluster" "$threads" 2>&1 || echo "[FAILED]"
            echo ""

            # HierarchicalClusterSpGEMM: A * A with close-pairs
            local pairs_file="$DATA_DIR/close_pairs/${name}_closepairs.txt"
            if [[ -f "$pairs_file" ]]; then
                echo "--- HierarchicalClusterSpGEMM ---"
                OMP_NUM_THREADS=$threads "$BIN/HierarchicalClusterSpGEMM" \
                    text "$mtx_file" "$mtx_file" "$pairs_file" "$max_cluster" "$threads" 2>&1 || echo "[FAILED]"
            else
                echo "[SKIP] HierarchicalClusterSpGEMM: no close-pairs file for $name"
                echo "       Run './run.sh gen-pairs $name' first"
            fi
            echo ""
        done
    } 2>&1 | tee "$logfile"

    echo ""
    echo "=== Results saved to $logfile ==="
}

# ── Main ───────────────────────────────────────────────────
usage() {
    cat <<'EOF'
Usage: ./run.sh <command> [args...]

Commands:
  build                                        Build all binaries
  download                                     Download 10 representative matrices (~1.3 GB)
  gen-pairs [name] [topk]                      Generate close-pairs for hierarchical clustering
  gen-pairs-all [topk]                         Generate close-pairs for all downloaded matrices

  vlength-gen [scale] [ef] [max_cluster] [t]   VlengthCluster with RMAT generator
  vlength-text <A> <B> [max_cluster] [t]       VlengthCluster with .mtx files
  hierarchical <A> <B> <pairs> [cs] [t]        HierarchicalCluster with .mtx files

  bench [threads] [max_cluster]                Run both algorithms on all downloaded matrices (A^2)
  test                                         Quick smoke test on tiny data

Workflow (following the paper):
  1. ./run.sh build              # compile everything
  2. ./run.sh download           # get SuiteSparse matrices
  3. ./run.sh gen-pairs-all      # generate candidate close-pairs
  4. ./run.sh bench              # benchmark A^2 on all matrices
EOF
}

case "${1:-}" in
    build)
        build
        ;;
    download)
        download_representative
        ;;
    gen-pairs)
        shift
        name="${1:?usage: ./run.sh gen-pairs <matrix_name> [topk]}"
        generate_close_pairs "$name" "${2:-7}"
        ;;
    gen-pairs-all)
        topk="${2:-7}"
        for mat in "${REPRESENTATIVE_MATRICES[@]}"; do
            name="${mat##*/}"
            generate_close_pairs "$name" "$topk"
        done
        ;;
    vlength-gen)
        shift; run_vlength_gen "$@"
        ;;
    vlength-text)
        shift; run_vlength_text "$@"
        ;;
    hierarchical)
        shift; run_hierarchical_text "$@"
        ;;
    bench)
        shift; bench "$@"
        ;;
    test)
        build
        echo ""
        echo "############################################"
        echo "# Quick test: VlengthClusterSpGEMM (gen)  #"
        echo "############################################"
        run_vlength_gen 10 4 8 4

        echo ""
        echo "############################################"
        echo "# Quick test: VlengthClusterSpGEMM (text) #"
        echo "############################################"
        run_vlength_text "$TESTDATA/test_matrix.mtx" "$TESTDATA/test_matrix.mtx" 8 4

        echo ""
        echo "################################################"
        echo "# Quick test: HierarchicalClusterSpGEMM (text) #"
        echo "################################################"
        run_hierarchical_text "$TESTDATA/test_matrix.mtx" "$TESTDATA/test_matrix.mtx" "$TESTDATA/close_pairs.txt" 8 4

        echo ""
        echo "=== All tests passed ==="
        ;;
    *)
        usage
        ;;
esac
