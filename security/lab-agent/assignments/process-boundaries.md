Inspect only `tests/security/test_secret_process_boundaries.py` and the scripts
explicitly exercised by those tests.

Look for untested or bypassable paths that expose secrets through process
arguments, inherited environment variables, shell tracing, temporary files,
exception text, or child processes. Also check whether the routine local and
CI gates actually execute the relevant boundary tests. Do not inspect ignored
secret values. Use the disposable worktree only.
