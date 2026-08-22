Inspect `tests/security` and scripts involved in SOPS backup, restore, and
credential hygiene. Identify candidate cases where plaintext secrets can be
persisted, exposed through process arguments or environment, restored outside
the intended boundary, or executed through an attacker-controlled program.

Do not inspect ignored secret values. You may edit only this disposable
worktree for experiments; do not commit any change.
