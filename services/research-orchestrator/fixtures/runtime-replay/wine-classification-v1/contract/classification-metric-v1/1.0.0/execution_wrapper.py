#!/usr/bin/env python3
"""
Evaluation contract execution wrapper for wine quality classification benchmark.
This script orchestrates training, evaluation, and reporting per the approved contract.
"""

import json
import os
import sys
import time
import traceback
import subprocess
from pathlib import Path


def load_config():
    """Load configuration from contract descriptor."""
    config_path = Path(__file__).parent / "contract.json"
    with open(config_path, "r") as f:
        return json.load(f)


def run_stage(stage_name, script_path, **kwargs):
    """Execute a stage script and capture output."""
    cmd = [sys.executable, str(script_path)]
    for key, value in kwargs.items():
        cmd.extend([f"--{key.replace('_', '-')}", str(value)])
    
    print(f"[STAGE {stage_name}] Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] Stage {stage_name} failed:")
        print(result.stdout)
        print(result.stderr)
        return False
    print(f"[STAGE {stage_name}] Completed successfully")
    return True


def main():
    """Main execution wrapper."""
    start_time = time.time()
    contract_dir = Path(__file__).parent
    src_dir = contract_dir / "src"
    
    if not src_dir.exists():
        print(f"[ERROR] Source directory not found: {src_dir}")
        sys.exit(1)
    
    sys.path.insert(0, str(src_dir))
    
    try:
        from train import main as train_main
        from evaluate import main as evaluate_main
        from plot import main as plot_main
        
        print("=" * 60)
        print("Wine Quality Classification Benchmark")
        print("Evaluation Contract Execution Wrapper")
        print("=" * 60)
        
        result = train_main()
        if not result:
            print("[ERROR] Training stage failed")
            sys.exit(1)
        
        result = evaluate_main()
        if not result:
            print("[ERROR] Evaluation stage failed")
            sys.exit(1)
        
        result = plot_main()
        if not result:
            print("[ERROR] Plotting stage failed")
            sys.exit(1)
        
        elapsed = (time.time() - start_time) / 60.0
        print(f"[SUMMARY] Total execution time: {elapsed:.2f} minutes")
        
        metrics = {
            "training_wallclock_minutes": elapsed,
            "timestamp": time.time(),
            "contract_id": "classification-metric-v1",
            "version": "1.0.0"
        }
        
        metrics_path = contract_dir / "reports" / "tables" / "execution_summary.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        
        print("[SUCCESS] All stages completed successfully")
        return 0
        
    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
