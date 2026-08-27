Inspect only `scripts/restore-glasslab-secrets.sh`,
`scripts/secret_backup_restore.py`, and restore-focused cases in
`tests/security/test_secret_backup_restore.py`.

Look for candidate archive traversal, symlink, ownership, recipient-policy,
PATH/environment command execution, incomplete validation, rollback loss, or
plaintext exposure. Do not inspect ignored secret values. Use the disposable
worktree only.
