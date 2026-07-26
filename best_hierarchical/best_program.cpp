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
#include <queue>

#include <sys/ioctl.h>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <filesystem>
namespace fs = std::filesystem;

#include "../utility.h"
#include "../cluster_utility.h"
#include "../CSR.h"
#include "../CSR_VlengthCluster.h"
#include "../multiply.h"

#include "../hash_mult.h"
#include "../hash_mult_vlengthcluster.h"
#include "sample_common.hpp"

using namespace std;

#define VALUETYPE double
#define INDEXTYPE int64_t

#define VALIDATE 0

using item_t = pair<VALUETYPE, pair<INDEXTYPE, INDEXTYPE>>;
auto cmp = [](const item_t &a, const item_t &b){ return a.first < b.first; };
priority_queue<item_t, vector<item_t>, decltype(cmp)> sims(cmp);

// EVOLVE-BLOCK-START
static map<INDEXTYPE, vector<INDEXTYPE>> hierachical_clustering_v0(CSR<INDEXTYPE, VALUETYPE> &sp,
                                                       map<pair<INDEXTYPE, INDEXTYPE>, VALUETYPE> &close_pairs,
                                                       int cluster_size) {

  const double UB_PRUNE = 0.005;

  // Reset heap to avoid stale entries and reduce duplicate work across runs
  while(!sims.empty()) sims.pop();
  // Precompute row degrees to bias merges toward degree-balanced pairs
  vector<INDEXTYPE> deg(sp.rows);
  for (INDEXTYPE r = 0; r < sp.rows; ++r) deg[r] = sp.rowptr[r+1] - sp.rowptr[r];

  // Seed heap with positive pairs, mildly preferring degree-balanced merges; prune hopeless by degree upper bound
  for (auto &p: close_pairs) {
    if (p.second <= (VALUETYPE)0) continue;
    INDEXTYPE u = p.first.first, v = p.first.second;
    if (u > v) std::swap(u, v);
    INDEXTYPE du = deg[u], dv = deg[v];
    INDEXTYPE mx = du > dv ? du : dv;
    INDEXTYPE mn = du < dv ? du : dv;
    if (mx > 0) {
      double ub = (double)mn / (double)mx;
      if (ub < UB_PRUNE) continue;
    }
    double bal = (du + dv > 0) ? 1.0 - fabs((double)du - (double)dv) / (double)(du + dv) : 1.0; // [0,1]
    double w = (double)p.second * (1.0 + 0.05 * bal);
    if (w > 0) sims.push(make_pair((VALUETYPE)w, make_pair(u, v)));
  }
  vector<INDEXTYPE> clusters(sp.rows);
  vector<INDEXTYPE> sz(sp.rows);
  vector<char> valid(sp.rows, 1);
  for (int i=0; i<sp.rows; i++) {
    clusters[i] = i;
    sz[i] = 1;
  }

  //	cout << "start clustering" << endl;

  while (!sims.empty()) {
    item_t s = sims.top();
    sims.pop();
    INDEXTYPE i = s.second.first;
    INDEXTYPE j = s.second.second;
    if (clusters[i] == i && clusters[j] == j) {
      if (!valid[i] || !valid[j]) continue;
      if (sz[i] < sz[j]) {
        clusters[i] = j;
        sz[j] += sz[i];
        if (sz[j] >= cluster_size) valid[j] = 0;
      } else {
        clusters[j] = i;
        sz[i] += sz[j];
        if (sz[i] >= cluster_size) valid[i] = 0;
      }
    } else {
      // path compress to current roots
      while (i != clusters[i]) {
        clusters[i] = clusters[clusters[i]];
        i = clusters[i];
      }
      while (j != clusters[j]) {
        clusters[j] = clusters[clusters[j]];
        j = clusters[j];
      }
      if (!valid[i] || !valid[j]) continue;
      if (i != j) {
        INDEXTYPE a = i, b = j;
        if (a > b) std::swap(a, b);
        auto p = make_pair(a, b);
        auto it = close_pairs.find(p);
        if (it == close_pairs.end()) {
          // Degree-based Jaccard upper bound to skip hopeless pairs
          INDEXTYPE di = deg[a], dj = deg[b];
          INDEXTYPE mx = di > dj ? di : dj;
          INDEXTYPE mn = di < dj ? di : dj;
          if (mx > 0) {
            double ub = (double)mn / (double)mx;
            if (ub < UB_PRUNE) { close_pairs[p] = (VALUETYPE)0; continue; }
          }
          // Compute Jaccard using common elements and cached degrees
          VALUETYPE inter = (VALUETYPE)sp.common_elements(a, b);
          VALUETYPE denom = (VALUETYPE)(di + dj) - inter;
          VALUETYPE s2 = (denom > (VALUETYPE)0) ? (inter / denom) : (VALUETYPE)0;
          // Cache even zero/near-zero similarities to avoid recomputation
          close_pairs[p] = s2;
          if (s2 > (VALUETYPE)0) {
            // Mild biases: degree-balance and approaching target cluster size
            double fullness = std::min(1.0, (double)(sz[i] + sz[j]) / std::max(1, cluster_size));
            double bal = (di + dj > 0) ? 1.0 - fabs((double)di - (double)dj) / (double)(di + dj) : 1.0; // [0,1]
            double w = (double)s2 * (1.0 + 0.05 * bal) * (0.9 + 0.1 * fullness);
            if (w > 0) sims.push(make_pair((VALUETYPE)w, p));
          }
        }
      }
    }
  }

  map<INDEXTYPE, vector<INDEXTYPE>> reordered_dict;

  for (int i=0; i<clusters.size(); i++) {
    int r = i;
    while (r != clusters[r]) {
      clusters[r] = clusters[clusters[r]];
      r = clusters[r];
    }
    clusters[i] = r;
    reordered_dict[r].push_back(i);
  }

  // Locality-aware intra-cluster ordering: sort rows by descending nnz (tie-break by row id)
  for (auto &kv : reordered_dict) {
    auto &vec = kv.second;
    std::sort(vec.begin(), vec.end(), [&](INDEXTYPE a, INDEXTYPE b){
      auto da = sp.rowptr[a+1] - sp.rowptr[a];
      auto db = sp.rowptr[b+1] - sp.rowptr[b];
      if (da != db) return da > db;
      return a < b;
    });
  }

  return reordered_dict;
}
// EVOLVE-BLOCK-END

int main(int argc, char *argv[]) {
  INDEXTYPE cluster_size = 8;
  const bool sortOutput = false;
  vector<int> tnums = {64};
  CSR<INDEXTYPE, VALUETYPE> A_csr, B_csr;

  if (argc < 5) {
    cout
        << "Normal usage: ./spgemm {gen|binary|text} {rmat|er|matrix1.txt} {scale|matrix2.txt} {edgefactor|candidate_pairs.txt} <cluster_size> <numthreads>"
        << endl;
    return -1;
  }
  if (argc >= 6) {
    cout
        << "Normal usage: ./spgemm {gen|binary|text} {rmat|er|matrix1.txt} {scale|matrix2.txt} {edgefactor|candidate_pairs.txt} <cluster_size> <numthreads>"
        << endl;
    cluster_size = atoi(argv[5]);
  }
  if (argc >= 7) {
    cout
        << "Normal usage: ./spgemm {gen|binary|text} {rmat|er|matrix1.txt} {scale|matrix2.txt} <cluster_size> <signature_length> <band_size> <numthreads>"
        << endl;
    tnums = {atoi(argv[6])};
  }

  /* Generating input matrices based on argument */
  SetInputMatricesAsCSR(A_csr, B_csr, argv, cluster_size);
  A_csr.sortIds();
  B_csr.sortIds();

  /* Count total number of floating-point operations */
  long long int nfop = get_flop(A_csr, B_csr);
  cout << "Total number of floating-point operations including addition and multiplication in SpGEMM (A * B): " << nfop
       << endl << endl;

  double start, end, msec, ave_msec, mflops;

  map<pair<INDEXTYPE, INDEXTYPE>, VALUETYPE> close_pairs;
  std::ifstream closepair_file(argv[4]);
  if (!closepair_file.is_open()) {
    std::cout << "Couldn't open file " << argv[4] << std::endl;
    std::exit(-2);
  }

  INDEXTYPE u, v;
  VALUETYPE common;

  while(closepair_file >> u >> v >> common) {
    if (u > v) std::swap(u, v);
    close_pairs.insert(make_pair(make_pair(u, v), common));
  }
  closepair_file.close();

  // set the highest number of allowed concurrent threads to run clustering/reordering algorithm
  omp_set_num_threads(tnums[tnums.size() - 1]);
  map<INDEXTYPE, vector<INDEXTYPE>> clusters = hierachical_clustering_v0(A_csr, close_pairs, cluster_size);

  // Print clustering stats (deterministic metrics for evaluation)
  cout << "# of clusters: " << clusters.size() << endl;
  INDEXTYPE hier_max_cs = 0;
  for (auto& kv : clusters) {
    hier_max_cs = max(hier_max_cs, (INDEXTYPE)kv.second.size());
  }
  cout << "max_cluster_size for SpGEMM: " << hier_max_cs << endl;

  // create A_csr_vlength_cluster from A_csr and reconstructed_clusters
  CSR_VlengthCluster<INDEXTYPE, VALUETYPE> A_csr_vlength_cluster(A_csr, clusters);

  /* Execute HashSpGEMMCluster */
  cout << "Evaluation of HashSpGEMMCluster" << endl;
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
    start = omp_get_wtime();
    CSR<INDEXTYPE,VALUETYPE> C_csr;
    csr_vlength_cluster2csr(C_csr, C_csr_vlength_cluster);
    end = omp_get_wtime();
    cout << "Reconstruct output CSR time: " << (end - start) * 1000 << " [milli seconds]" << endl;

    C_csr.sortIds();
    CSR<INDEXTYPE,VALUETYPE> C_csr_hash;
    RowSpGEMM<false, sortOutput>(A_csr, B_csr, C_csr_hash, multiplies<VALUETYPE>(), plus<VALUETYPE>(), "");
    if(!sortOutput) C_csr_hash.sortIds();
    cout << "RowSpGEMM == HashSpGEMMVLCluster ? " << (C_csr_hash==C_csr) << endl << endl;
    C_csr_hash.make_empty();
    C_csr.make_empty();
#endif
    C_csr_vlength_cluster.make_empty();
  }

  A_csr.make_empty();
  A_csr_vlength_cluster.make_empty();
  B_csr.make_empty();

  return 0;
}
