# S6 QA Transcript — Honeydew Method Advisory (real corpus)

Store: 15-source curated corpus (`statistical-learning-methods`), arctic-m-v1.5 index (3152 vectors), hybrid+rerank mode.

## QUESTION

> How should we assess whether clusters are stable rather than artifacts of initialization?

## QUERY PLAN

- assess whether clusters stable artifacts initialization
- methods for assess whether clusters stable
- stability assessment for assess whether clusters stable
- validation diagnostics for assess whether clusters stable

## RETRIEVED LITERATURE

- **Clustering Stability: An Overview** — section `6`, pp. 2–4 · score 0.016
  > Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against…
- **Clustering Stability: An Overview** — section `6`, pp. 2–3 · score 0.015
  > Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against…
- **Clustering Stability: An Overview** — section `11`, pp. 25–27 · score 0.014
  > In particular, if we
call the number of initial centers per cluster the initial conﬁguration,
one can say that each initial conﬁguration leads to a unique clustering,
and diﬀerent …
- **Critical limitations of consensus clustering in class discovery** — section `4`, pp. 3–4 · score 0.015
  > bution is to populate an ensemble of n-p matrices—for n samples
and p genes—using random values from a univariate uniform or
Although the pcNormal datasets have a known lack of sub…
- **Critical limitations of consensus clustering in class discovery** — section `4`, pp. 1–2 · score 0.015
  > is grouped together in multiple clustering runs, each with a certain
degree of permutation either by random initialization or by random
An early example that motivated this reasses…
- **An Introduction to Statistical Learning, seventh printing** — section `427`, pp. 413–414 · score 0.016
  > Practical Issues in Clustering
Clustering can be a very useful tool for data analysis in the unsupervised
setting. However, there are a number of issues that arise in performing
cl…

## HONEYDEW METHOD ADVISORY

# Methodology Advisory

Objective: How should we assess whether clusters are stable rather than artifacts of initialization?
Corpus: statistical-learning-methods (generated_by=extractive-fallback, index_version=rag-v1)

Uncertainty: The supplied corpus (statistical-learning-methods) supports these candidates only partially; 6 evidence spans were retrieved. Statements beyond cited spans are not supported.

## 1. Resampling-based clustering validation

Why: Evidence [03edd07d8b474c8c98c8caed47274cb0] states: "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “tes" A corroborating span [03edd07d8b474c8c98c8caed47274cb0] adds: "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “tes"

Assumptions:
- Clusters correspond to a stable latent structure rather than sampling noise.
- The chosen distance/linkage reflects domain similarity.
Preprocessing:
- Scale features before any distance-based step.
Diagnostics:
- Inspect consensus matrices across resamples.
- Track cluster-count selection stability.
Metrics:
- Adjusted Rand index across resamples.
- Silhouette on consensus labels.
Failure modes:
- Resampling can mask genuine small-cluster instability.
Baselines:
- Fixed-k k-means without a stability check.
Comparisons:
- Compare against gap-statistic cluster-count selection.

Citations: [1] [2] [3]

## 2. False-discovery-rate control

Why: Evidence [03edd07d8b474c8c98c8caed47274cb0] states: "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “tes" A corroborating span [c84ff93e738f4020836320b964514c3d] adds: "bution is to populate an ensemble of n-p matrices—for n samples
and p genes—using random values from a univariate uniform or
Although the pcNormal datasets have a known lack of substructure,
unimodal"

Assumptions:
- Tests are exchangedable enough for adaptive procedures.
Preprocessing:
- Fix the hypothesis family before looking at p-values.
Diagnostics:
- Plot sorted p-values against the Benjamini-Hochberg line.
Metrics:
- Number of discoveries at target FDR q.
Failure modes:
- Dependent tests can break FDR control guarantees.
Baselines:
- Uncorrected per-test alpha as the naive baseline.
Comparisons:
- Compare BH against Bonferroni power at matched error.

Citations: [4] [5] [6]

## CITATIONS

[1] knowledge://03edd07d8b474c8c98c8caed47274cb0 — section_path=6, pages 2-4, chars 1131-5310
    "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “test” our clustering results.
One of the mo"
[2] knowledge://03edd07d8b474c8c98c8caed47274cb0 — section_path=6, pages 2-3, chars 1131-3141
    "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “test” our clustering results.
One of the mo"
[3] knowledge://03edd07d8b474c8c98c8caed47274cb0 — section_path=11, pages 25-27, chars 41894-44528
    "In particular, if we
call the number of initial centers per cluster the initial conﬁguration,
one can say that each initial conﬁguration leads to a unique clustering,
and diﬀerent conﬁgurations lead to diﬀerent clusterings; see Figure 3.3

"
[4] knowledge://03edd07d8b474c8c98c8caed47274cb0 — section_path=6, pages 2-4, chars 1131-5310
    "Introduction
Model selection is a diﬃcult problem in non-parametric clustering. The
obvious reason is that, as opposed to supervised classiﬁcation, there is
no ground truth against which we could “test” our clustering results.
One of the mo"
[5] knowledge://c84ff93e738f4020836320b964514c3d — section_path=4, pages 3-4, chars 13075-15793
    "bution is to populate an ensemble of n-p matrices—for n samples
and p genes—using random values from a univariate uniform or
Although the pcNormal datasets have a known lack of substructure,
unimodal distribution15. However, the gene-gene c"
[6] knowledge://c84ff93e738f4020836320b964514c3d — section_path=4, pages 1-2, chars 6192-8964
    "is grouped together in multiple clustering runs, each with a certain
degree of permutation either by random initialization or by random
An early example that motivated this reassessment is the analysis
sample- or gene-subsampling. The resul"


## RECOMMENDED EXPERIMENT MATRIX

- Baseline (Resampling-based clustering validation): Fixed-k k-means without a stability check.
- Comparison (Resampling-based clustering validation): Compare against gap-statistic cluster-count selection.
- Baseline (False-discovery-rate control): Uncorrected per-test alpha as the naive baseline.
- Comparison (False-discovery-rate control): Compare BH against Bonferroni power at matched error.

## BENCHMARK CONTEXT

| mode | recall@10 | mrr@10 | ndcg@10 |
|---|---|---|---|
| lexical | 0.781 | 0.812 | 0.739 |
| dense | 0.906 | 0.875 | 0.840 |
| hybrid | 0.865 | 0.875 | 0.795 |
| hybrid+rerank | 0.865 | 0.875 | 0.795 |

