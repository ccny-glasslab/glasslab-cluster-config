Inspect only `scripts/check-credential-hygiene.py` and
`tests/security/test_credential_hygiene.py`.

Look for candidate bypasses that allow plaintext passwords, tokens, private
keys, password hashes, secret-bearing diffs, or generated review artifacts to
escape detection. Trace exact parser and filesystem inputs to each decision.
Do not inspect ignored secret values. Use the disposable worktree only.
