#!/usr/bin/env python3
"""Resolve and validate the registry-pinned research workspace runner image.

The authoritative workflow-registry contract
(``services/common/schemas/workflow_registry.py``) requires active
``runner_image`` refs to be digest-pinned:

    ghcr.io/<owner>/glasslab-research-workspace-runner@sha256:<64 lowercase hex>

This helper parses such a ref for the release workflow
(``.github/workflows/research-workspace-runner-image.yml``), validates the
repository portion and the digest independently, and emits the
``GITHUB_OUTPUT`` keys consumed by the workflow. It deliberately rejects
mutable tag refs so the registry requirement cannot weaken back to tags.

Authority contract:

- The digest embedded in the workflow definitions remains authoritative. This
  script never rewrites definitions.
- The only tag this release path publishes is the immutable, commit-derived
  publication alias ``sha-<full-sha>-<matrix-name>``. After a release build,
  the newly built digest is surfaced in the workflow step summary so registry
  definitions can adopt it deliberately.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_runner_image(image_ref: str) -> tuple[str, str]:
    """Return ``(repository, digest)`` for a digest-pinned ref.

    Raises ``ValueError`` when the ref is not pinned to a
    ``sha256:<64 lowercase hex>`` digest.
    """
    repository, separator, digest = image_ref.partition("@")
    if not separator:
        raise ValueError(
            "runner_image must be digest-pinned with "
            f"@sha256:<64 lowercase hex>: {image_ref!r}"
        )
    if not repository:
        raise ValueError(f"runner_image repository is empty: {image_ref!r}")
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError(
            "runner_image digest must match sha256:<64 lowercase hex>: "
            f"{digest!r}"
        )
    return repository, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve and validate the digest-pinned runner image from a "
            "workflow-registry definition, emitting GITHUB_OUTPUT keys."
        )
    )
    parser.add_argument(
        "--definition",
        required=True,
        help="Path to the workflow-registry definition JSON.",
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub repository owner (case-insensitive).",
    )
    parser.add_argument(
        "--image",
        default="glasslab-research-workspace-runner",
        help="Expected image name within the owner's GHCR namespace.",
    )
    parser.add_argument(
        "--sha",
        required=True,
        help="Full commit SHA used to derive the immutable publication alias.",
    )
    parser.add_argument(
        "--matrix-name",
        required=True,
        help="Release matrix entry name, disambiguating the publication alias.",
    )
    args = parser.parse_args(argv)

    try:
        definition = json.loads(Path(args.definition).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"failed to read definition {args.definition!r}: {exc}", file=sys.stderr)
        return 1

    image_ref = definition.get("runner_image")
    if not image_ref:
        print("runner_image is missing from the definition", file=sys.stderr)
        return 1

    try:
        repository, digest = parse_runner_image(image_ref)
    except ValueError as exc:
        print(f"invalid runner_image: {exc}", file=sys.stderr)
        return 1

    expected = f"ghcr.io/{args.owner.lower()}/{args.image}"
    if repository != expected:
        print(
            f"runner_image repository mismatch: {repository!r} != {expected!r}",
            file=sys.stderr,
        )
        return 1

    publication_tag = f"sha-{args.sha}-{args.matrix_name}"
    print(f"image={repository}")
    print(f"digest={digest}")
    print(f"image_ref={image_ref}")
    print(f"tag={publication_tag}")
    print(f"sha_ref={repository}:{publication_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())