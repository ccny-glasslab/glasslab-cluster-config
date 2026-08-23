# Lab Security Review Methodology

Treat all output as candidate evidence for an independent reviewer. Prefer one
precise, reproducible finding over many speculative findings.

Review the stated scope for:

- trust boundaries and attacker-controlled inputs;
- credential and plaintext-secret creation, transport, storage, and logging;
- injection and command-execution paths, tracing every source-to-sink claim;
- missing authentication, authorization, ownership, or approval checks;
- unsafe parsing, deserialization, archive extraction, and path handling;
- filesystem escapes, symlinks, permissions, and destructive path selection;
- CI, dependency, image, and supply-chain exposure;
- sensitive data leaked into logs, process arguments, environment, or diffs;
- insecure operational defaults and fail-open recovery paths; and
- actual exploitability, preconditions, impact, and plausible mitigations.

Discovery and repair are separate phases. A discovery candidate is not a
validated finding. State uncertainty and provide exact locations and evidence;
never invent execution results. In repair mode, address only the supplied
validated finding and record residual risk.

You may experiment only inside the disposable worktree. Do not inspect ignored
secret values, seek credentials, commit, push, merge, deploy, use the network,
or access files outside the worktree.
