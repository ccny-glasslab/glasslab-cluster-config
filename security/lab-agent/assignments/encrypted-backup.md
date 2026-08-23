Inspect only `scripts/backup-glasslab-secrets.sh`,
`scripts/pull-glasslab-secrets-backup.sh`, and backup-focused cases in
`tests/security/test_secret_backup_restore.py`.

Look for candidate paths that persist plaintext, publish incomplete or
unverified backup pairs, omit encrypted inventory records, leak values through
argv/environment/logs, or accept unsafe remote/filename input. Do not inspect
ignored secret values. Use the disposable worktree only.
