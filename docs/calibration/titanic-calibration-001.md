# Calibration 001 — Kaggle Titanic (pre-registration)

**Status:** PRE-REGISTERED (recorded before the attempt)
**Date:** 2026-08-26
**Branch / run refs:** (filled in after the run)

## Purpose

First calibration run: prove the Honeydew–Beaker research pipeline can take a
crack at a well-defined, already-solved problem and reproduce a known-good
result. This is the ground-truth tier before any novel work.

## Problem

Kaggle Titanic — binary survival classification. Classic train set (~891 rows,
12 features). This is the canonical beginner ML problem with well-known results.

## Pre-registered prediction

- **Pipeline:** dataset registered in the registry → task bundle with exact
  rubric → Honeydew protocol draft → Beaker implementation (tabular pipeline,
  sklearn model — RandomForest / gradient boosting expected) → bounded CPU
  cluster job → evaluator → report.
- **Metric:** `accuracy` on a held-out validation split of the train set.
- **Predicted range: 0.78 – 0.84** (competent non-ensemble Titanic pipelines
  land here; public leaderboard top ≈ 0.83).
- **Acceptance ("aced"):** `accuracy >= 0.78` with the evidence bundle
  (`metrics.json`, `report.md`, `source.zip`) and integrity pass.
- **Failed calibration:** `accuracy < 0.78`, or the run fails to complete
  through the pipeline.

## Method (the run's own contract)

1. Register `train.csv` in the dataset registry (`glasslab-dataset://<sha256>`).
2. Task bundle: `problem.md` per the authoring guide (exact metric keys,
   thresholds, stopping conditions).
3. Compile → preflight (must be `ready=True`).
4. Run: Honeydew drafts protocol → Beaker implements + validates → approval →
   bounded cluster job → evaluator → report.
5. Report outcome against this pre-registration.

## Risk register

- Agent-runtime slowness / turn timeout — mitigated by the retry fix (#231,
  deployed).
- Rubric-contract completeness — mitigated by the authoring guide (#234).
- Dataset availability — Kaggle API with the cluster's `kaggle-credentials`
  secret, or the public mirror as fallback.
- Beaker pipeline quality on tabular data — expected fine (canonical problem).

## Deviation policy

Any deviation from this plan (different metric, different data source, or
relaxed acceptance) is recorded here with a reason before the run is judged.