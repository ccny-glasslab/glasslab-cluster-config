"""Fixed method-family table for extractive advisory generation.

Pure data: each family is a match rule (``any_of`` plus optional ``required``
keyword groups, matched against lowercased chunk text) and the default
assumption/preprocessing/diagnostic/metric lists rendered into candidates.
No logic lives here; ``app.corpus_rag.advisory`` consumes the table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Family:
    """One method family: match rule plus its fixed default templates."""

    label: str
    any_of: tuple[str, ...]
    required: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    preprocessing: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()
    baselines: tuple[str, ...] = ()
    comparisons: tuple[str, ...] = ()


_FAMILIES: tuple[_Family, ...] = (
    _Family(
        label='Resampling-based clustering validation',
        any_of=('cluster', 'stability'),
        assumptions=(
            'Clusters correspond to a stable latent structure rather than sampling noise.',
            'The chosen distance/linkage reflects domain similarity.',
        ),
        preprocessing=('Scale features before any distance-based step.',),
        diagnostics=(
            'Inspect consensus matrices across resamples.',
            'Track cluster-count selection stability.',
        ),
        metrics=('Adjusted Rand index across resamples.', 'Silhouette on consensus labels.'),
        failure_modes=('Resampling can mask genuine small-cluster instability.',),
        baselines=('Fixed-k k-means without a stability check.',),
        comparisons=('Compare against gap-statistic cluster-count selection.',),
    ),
    _Family(
        label='Penalized regression (lasso/elastic-net)',
        any_of=('regulariz', 'lasso', 'penal'),
        assumptions=('The true signal is sparse or weakly dense.',),
        preprocessing=('Standardize predictors so penalties act uniformly.',),
        diagnostics=('Trace coefficient paths across penalty values.',),
        metrics=('Held-out MSE at the selected penalty.',),
        failure_modes=('Correlated groups can make feature selection unstable.',),
        baselines=('Unpenalized OLS on the same design matrix.',),
        comparisons=('Compare lasso against elastic-net under correlation.',),
    ),
    _Family(
        label='Imbalanced-learning evaluation protocol',
        any_of=('imbalanc', 'smote', 'resampl'),
        required=('metric',),
        assumptions=('Class imbalance reflects real prevalence, not collection artifact.',),
        preprocessing=('Resample training folds only; never the validation folds.',),
        diagnostics=('Check per-class recall, not pooled accuracy.',),
        metrics=('PR-AUC and balanced accuracy instead of raw accuracy.',),
        failure_modes=('Oversampling before splitting leaks duplicates into validation.',),
        baselines=('Majority-class predictor as the floor.',),
        comparisons=('Compare class weighting against SMOTE-style oversampling.',),
    ),
    _Family(
        label='Probability calibration workflow',
        any_of=('calibrat',),
        assumptions=('Ranking quality exists before calibration is attempted.',),
        preprocessing=('Fit calibration on a held-out split, not the training split.',),
        diagnostics=('Plot reliability diagrams per class.',),
        metrics=('Expected calibration error and Brier score.',),
        failure_modes=('Calibration on small holdouts overfits the mapping.',),
        baselines=('Uncalibrated model scores as the reference.',),
        comparisons=('Compare Platt scaling against isotonic regression.',),
    ),
    _Family(
        label='Nested cross-validation protocol',
        any_of=('cross-valid', 'nest'),
        assumptions=('Model selection and performance estimation must not share splits.',),
        preprocessing=('Keep the outer folds untouched by any tuning step.',),
        diagnostics=('Report inner-vs-outer score gaps as selection bias.',),
        metrics=('Outer-fold performance aggregated over all outer folds.',),
        failure_modes=('Tuning inside the outer folds inflates the estimate.',),
        baselines=('Single holdout split with one tuning pass.',),
        comparisons=('Compare nested CV variance against repeated single splits.',),
    ),
    _Family(
        label='Assumption-light regression alternatives',
        any_of=('quantile', 'robust', 'bootstrap'),
        assumptions=('Residual normality or homoscedasticity cannot be assumed.',),
        preprocessing=('Winsorize or transform only with a stated rule.',),
        diagnostics=('Inspect residual scale across fitted-value ranges.',),
        metrics=('Median absolute error alongside mean error.',),
        failure_modes=('Robust losses can hide genuine influential outliers.',),
        baselines=('Ordinary least squares as the parametric reference.',),
        comparisons=('Compare quantile regression against Huber-type losses.',),
    ),
    _Family(
        label='False-discovery-rate control',
        any_of=('fdr', 'multiple', 'discover'),
        assumptions=('Tests are exchangedable enough for adaptive procedures.',),
        preprocessing=('Fix the hypothesis family before looking at p-values.',),
        diagnostics=('Plot sorted p-values against the Benjamini-Hochberg line.',),
        metrics=('Number of discoveries at target FDR q.',),
        failure_modes=('Dependent tests can break FDR control guarantees.',),
        baselines=('Uncorrected per-test alpha as the naive baseline.',),
        comparisons=('Compare BH against Bonferroni power at matched error.',),
    ),
)


