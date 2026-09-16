"""Deterministic structural contract for ``problem.md`` (issue #496).

The task-bundle guide's mandatory sections are enforced at import: a missing
or empty required section rejects the archive with the offending section
named, instead of the compiler model silently reinterpreting the document.
Section matching is order-insensitive and tolerant of formatting - any ATX
heading level, case-insensitive, optional trailing colon, and trailing
parenthetical qualifiers such as " (exact)" - and a section is empty only when
it has no body content before the next heading of the same or higher level.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class RequiredProblemSection:
    name: str
    heading_prefixes: tuple[str, ...]


PROBLEM_REQUIRED_SECTIONS: tuple[RequiredProblemSection, ...] = (
    RequiredProblemSection('Objective', ('objective',)),
    RequiredProblemSection('Inputs', ('inputs', 'input')),
    RequiredProblemSection('Method and architecture', ('method',)),
    RequiredProblemSection('Hyperparameter search space', ('hyperparameter',)),
    RequiredProblemSection(
        'Evaluation rubric',
        ('evaluation rubric', 'rubric'),
    ),
    RequiredProblemSection(
        'Evidence artifacts',
        ('evidence artifact', 'artifacts'),
    ),
)

_HEADING_RE = re.compile(
    r'^\s{0,3}(?P<hashes>#{1,6})[ \t]+(?P<title>.+?)[ \t]*#*[ \t]*$'
)
_WHITESPACE_RE = re.compile(r'\s+')


def _normalized_title(title: str) -> str:
    return _WHITESPACE_RE.sub(' ', title.strip().lower()).rstrip(':')


def _matches_section(title: str, section: RequiredProblemSection) -> bool:
    normalized = _normalized_title(title)
    return any(
        normalized.startswith(prefix) for prefix in section.heading_prefixes
    )


def problem_section_errors(text: str) -> list[str]:
    """Return one actionable error per missing or empty required section."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append(
                (index, len(match.group('hashes')), match.group('title'))
            )
    errors: list[str] = []
    for section in PROBLEM_REQUIRED_SECTIONS:
        located = next(
            (
                (index, level)
                for index, level, title in headings
                if _matches_section(title, section)
            ),
            None,
        )
        if located is None:
            errors.append(
                f'problem.md is missing the required section '
                f'"{section.name}"; add a heading such as "## {section.name}"'
            )
            continue
        index, level = located
        end = next(
            (
                heading_index
                for heading_index, heading_level, _ in headings
                if heading_index > index and heading_level <= level
            ),
            len(lines),
        )
        if not '\n'.join(lines[index + 1 : end]).strip():
            errors.append(
                f'problem.md section "{section.name}" is empty; add the '
                'required content under its heading'
            )
    return errors
