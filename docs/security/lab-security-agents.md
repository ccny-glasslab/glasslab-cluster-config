# Lab Security Agents

`scripts/lab-security-agent` runs OpenCode against the Glasslab exo endpoint in
a disposable Git worktree. The worker is an untrusted investigator: its report,
diff, and claimed tests require independent review.

The dispatcher gives OpenCode an isolated HOME/XDG environment and does not
propagate SSH, GitHub, Kubernetes, or SOPS credentials. OpenCode sharing, web
tools, delegation, shell execution, and external-directory access are denied.
The worker may edit its disposable worktree, but it cannot commit, push, merge,
deploy, or alter the source checkout through the supported command.

Run the first bounded discovery:

```bash
curl -fsS http://192.168.1.17:52415/v1/models | jq '.data[].id'
scripts/lab-security-agent discover first-secret-audit \
  --assignment security/lab-agent/assignments/first-secret-audit.md
```

The command prints the worktree, validated JSON result, and summary paths.
Inspect `runtime/worktree.diff` as untrusted input. Reproduce every candidate's
evidence and classify it as rejected, validated, or escalated before starting
a repair. Never place plaintext secret values in assignments or reports.

Run deterministic tests without contacting exo:

```bash
python3 -m unittest tests.security.test_lab_security_agent -v
```

Worktrees are retained intentionally. Use Git's exact worktree path after
review; never recursively delete `.lab-agents` as a broad cleanup shortcut.
