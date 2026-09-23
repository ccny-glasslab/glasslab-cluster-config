# Real metrics-write excerpt, extracted from the frozen evidence corpus:
#   artifacts/e230e08ac0f3/beaker-worktree/research-workspace/
#   task-d777d26b32557f22/run.py
# The excerpt isolates the ``metrics_output`` dict literal and the bare
# ``with open('metrics.json', 'w')`` write; the model loop that fills
# ``metrics_output['models']`` afterwards is omitted.
import json


def write_metrics(results, cv_folds, seed):
    metrics_output = {
        'cv_folds': cv_folds,
        'seed': seed,
        'models': {},
    }

    with open('metrics.json', 'w') as f:
        json.dump(metrics_output, f, indent=2)
    print("Generated: metrics.json")
