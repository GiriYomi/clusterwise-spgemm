#include <omp.h>
#include <stdio.h>
#include <iostream>
#include <algorithm>
#include <numeric>
#include <functional>
#include <fstream>
#include <iterator>
#include <ctime>
#include <cmath>
#include <string>
#include <sstream>
#include <random>

#include <filesystem>
namespace fs = std::filesystem;

#include "../utility.h"
#include "../CSR.h"
#include "../CSR_VlengthCluster.h"
//#include "../Reorder.h"
#include "../multiply.h"

#include "../hash_mult.h"
#include "../hash_mult_vlengthcluster.h"
#include "sample_common.hpp"

using namespace std;

#define VALUETYPE double
#define INDEXTYPE int64_t

#define VALIDATE 0
#define MAX_CLUSTER_SIZE 8

int main(int argc, char *argv[]) {
  VALUETYPE similarity_th = 0.3;
  VALUETYPE eps = 0.000001f;
  INDEXTYPE max_cluster_size = 8;
  const bool sortOutput = false;
  vector<int> tnums;
  CSR<INDEXTYPE, VALUETYPE> A_csr, B_csr;

  if (argc < 4) {
    cout
        << "Normal usage: ./spgemm {gen|binary|text} {rmat|er|matrix1.txt} {scale|matrix2.txt} <max_cluster_size> <numthreads>"
        << endl;
    return -1;
  } else if (argc < 6) {
    cout
        << "Normal usage: ./spgemm {gen|binary|text} {rmat|er|matrix1.txt} {scale|matrix2.txt} <max_cluster_size> <numthreads>"
        << endl;

    cout << "Running on 1, 16, 32, 64 threads" << endl;
    tnums = {64};
    if (argc == 5) max_cluster_size = atoi(argv[4]);
  } else {
    cout << "Running on " << argv[5] << " processors" << endl << endl;
    max_cluster_size = atoi(argv[4]);
    tnums = {atoi(argv[5])};
  }

  cout << "max_cluster_size: " << max_cluster_size << endl;

  /* Generating input matrices based on argument */
  SetInputMatricesAsCSR(A_csr, B_csr, argv);

  A_csr.sortIds();
  B_csr.sortIds();

  /* Count total number of floating-point operations */
  long long int nfop = get_flop(A_csr, B_csr);
  cout << "Total number of floating-point operations including addition and multiplication in SpGEMM (A * B): " << nfop
       << endl << endl;

  double start, end, msec, ave_msec, mflops;

  // EVOLVE-BLOCK-START
  // Gap-tolerant clustering with fast early-accept using last-row similarity,
  // plus adaptive threshold and bounded multi-seed fallback.
  vector<INDEXTYPE> offset;
  INDEXTYPE curr_off = 0;
  offset.push_back(curr_off);

  // precompute row degrees (cheap similarity gate)
  vector<int> deg;
  deg.reserve((size_t)A_csr.rows);
  for (INDEXTYPE r = 0; r < A_csr.rows; ++r)
    deg.push_back((int)(A_csr.rowptr[r + 1] - A_csr.rowptr[r]));

  const int K = 3;                   // fall back to up to K seeds if needed
  const int GAP_LIMIT = 1;           // allow at most one weak row to bridge gaps
  const VALUETYPE TH_FLOOR = 0.18;   // minimum effective threshold
  const VALUETYPE TH_DECAY = 0.06;   // threshold decay per accepted row
  const VALUETYPE DEG_RATIO = 4.0;   // degree ratio gate vs seed

  INDEXTYPE real_max_cluster_size = 0;
  while (curr_off < A_csr.rows) {
    INDEXTYPE s = curr_off;      // cluster start (seed)
    INDEXTYPE t = s;             // inclusive end
    int gaps = 0;

    while (t + 1 < A_csr.rows) {
      if (max_cluster_size != -1 && (t + 1 - s) >= max_cluster_size) break;

      INDEXTYPE cand = t + 1;

      // adaptive threshold (looser as cluster grows)
      INDEXTYPE clen = t - s + 1;
      VALUETYPE eff_th = similarity_th - TH_DECAY * (VALUETYPE)(clen - 1);
      if (eff_th < TH_FLOOR) eff_th = TH_FLOOR;

      // quick degree gate vs seed row only (cheap and effective)
      int bd = deg[(size_t)s], cd = deg[(size_t)cand];
      int hi = bd > cd ? bd : cd;
      int lo = bd < cd ? bd : cd;
      if (lo == 0) lo = 1;
      if (((VALUETYPE)hi / (VALUETYPE)lo) > DEG_RATIO) {
        if (gaps < GAP_LIMIT) { t = cand; ++gaps; continue; }
        else break;
      }

      // 1) Fast path: check similarity with the last row in cluster.
      VALUETYPE sim_last = A_csr.jaccard_similarity(t, cand);
      if (sim_last + eps >= eff_th) {
        t = cand; gaps = 0; continue;
      }

      // 2) Fallback: bounded multi-seed average over last up to K rows.
      VALUETYPE sum_sim = sim_last;
      int cnt = 1;
      INDEXTYPE ss = (t + 1 > s + K) ? (t - (K - 1)) : s;
      for (INDEXTYPE rr = t - 1; rr >= ss; --rr) {
        sum_sim += A_csr.jaccard_similarity(rr, cand);
        ++cnt;
      }
      VALUETYPE avg_sim = sum_sim / (VALUETYPE)cnt;

      if (avg_sim + eps >= eff_th) {
        t = cand; gaps = 0;
      } else {
        if (gaps < GAP_LIMIT) { t = cand; ++gaps; }
        else break;
      }
    }

    INDEXTYPE next_off = t + 1;
    INDEXTYPE csize = next_off - s;
    if (csize > real_max_cluster_size) real_max_cluster_size = csize;
    offset.push_back(next_off);
    curr_off = next_off;
  }
  // EVOLVE-BLOCK-END

  cout << "# of clusters: " << offset.size() << endl;
  cout << "max_cluster_size for SpGEMM: " << real_max_cluster_size << endl;

  // create A_csr_vlength_cluster from A_csr and reconstructed_clusters
  CSR_VlengthCluster<INDEXTYPE, VALUETYPE> A_csr_vlength_cluster(A_csr, offset);

  /* Execute HashSpGEMMVLCluster */
  cout << "Evaluation of HashSpGEMMVLCluster" << endl;
  for (int tnum: tnums) {
    omp_set_num_threads(tnum);

    CSR_VlengthCluster<INDEXTYPE, VALUETYPE> C_csr_vlength_cluster;

    /* First execution is excluded from evaluation */
    HashSpGEMMVLCluster<sortOutput>(A_csr_vlength_cluster, B_csr, C_csr_vlength_cluster, multiplies<VALUETYPE>(), plus<VALUETYPE>());
    C_csr_vlength_cluster.make_empty();

    ave_msec = 0;
    for (int i = 0; i < ITERS; ++i) {
      start = omp_get_wtime();
      HashSpGEMMVLCluster<sortOutput>(A_csr_vlength_cluster, B_csr, C_csr_vlength_cluster, multiplies<VALUETYPE>(), plus<VALUETYPE>());
      end = omp_get_wtime();
      msec = (end - start) * 1000;
      ave_msec += msec;
      if (i < ITERS - 1) {
        C_csr_vlength_cluster.make_empty();
      }
    }
    ave_msec /= ITERS;
    mflops = (double) nfop / ave_msec / 1000;

    printf("HashSpGEMMVLCluster with %3d threads computes C = A * B in %f [milli seconds] (%f [MFLOPS])\n",
           tnum, ave_msec, mflops);

#if VALIDATE
    // convert C_csr_vlength_cluster to CSR format and save in C_csr
    start = omp_get_wtime();
    CSR<INDEXTYPE,VALUETYPE> C_csr;
    csr_vlength_cluster2csr(C_csr, C_csr_vlength_cluster);
    end = omp_get_wtime();
    cout << "Reconstruct output CSR time: " << (end - start) * 1000 << " [milli seconds]" << endl;
    C_csr.sortIds();

    // reconstruct A_csr by reordering the rows and save in A_csr_new
    // we have to do this because we did HashSpGEMMVLCluster on the modified A_csr
    //    - by creating CSR_VlengthCluster by accessing rows of A_csr in pp.final_clusters order
    // so to compare HashSpGEMMVLCluster result with RowSpGEMM, we need to perform (A_csr_new * B_csr)
    Triple<INDEXTYPE, VALUETYPE> *triples = new Triple<INDEXTYPE, VALUETYPE>[A_csr.nnz];
    INDEXTYPE triplet_id = 0;
    INDEXTYPE row_id = 0;
    for (auto t: pp.final_reordered_rows) {
      for (INDEXTYPE idx = A_csr.rowptr[t]; idx < A_csr.rowptr[t + 1]; idx += 1) {
        triples[triplet_id] = Triple<INDEXTYPE, VALUETYPE>(row_id, A_csr.colids[idx], A_csr.values[idx]);
        triplet_id += 1;
      }
      row_id += 1;
    }

    CSR<INDEXTYPE, VALUETYPE> A_csr_new(triples, A_csr.nnz, A_csr.rows, A_csr.cols);
    A_csr_new.sortIds();

    CSR<INDEXTYPE,VALUETYPE> C_csr_hash;
    RowSpGEMM<false, sortOutput>(A_csr_new, B_csr, C_csr_hash, multiplies<VALUETYPE>(), plus<VALUETYPE>(), "");
    if(!sortOutput) C_csr_hash.sortIds();

    cout << "RowSpGEMM == HashSpGEMMVLCluster ? " << (C_csr_hash==C_csr) << endl << endl;

    C_csr.make_empty();
    A_csr_new.make_empty();
    C_csr_hash.make_empty();
#endif
    C_csr_vlength_cluster.make_empty();
  }

  A_csr.make_empty();
  B_csr.make_empty();

  return 0;
}
