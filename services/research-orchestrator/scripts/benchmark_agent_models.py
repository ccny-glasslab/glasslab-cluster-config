#!/usr/bin/env python3
"""Standalone model benchmark for the exo pair (#321).

Scores candidate models served by the exo OpenAI-compatible endpoint on the
envelope contract the orchestrator engine enforces, so a model can be judged
before any config change:

- envelope validity (AgentTurnResult schema compliance per turn kind)
- grounding (unanswerable honesty + citation format/resolution)
- latency (time-to-first-token and tokens/sec, streaming)

Prompts mirror the templates in app/engine.py (research_answer,
protocol_draft, final_report). Keep them in sync when engine prompts change.

Run from any host with network to the exo endpoint; stdlib only:

    python3 scripts/benchmark_agent_models.py \
      --models mlx-community/Qwen3-Coder-Next-4bit \
      --turns-per-test 3 --report-out /tmp/model-bench.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = 'http://192.168.1.17:52415/v1'
DEFAULT_MODELS = [
    'mlx-community/Qwen3-Coder-Next-4bit',
    'mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit',
    'mlx-community/Qwen3.5-122B-A10B-4bit',
    'mlx-community/Llama-3.3-70B-Instruct-4bit',
]
KNOWN_KINDS = {
    'protocol_draft',
    'research_answer',
    'methodology_review',
    'implementation_plan',
    'implementation_proposal',
    'experiment_analysis',
    'final_report',
    'verification',
    'revision',
    'contract_candidate',
    'task_spec_proposal',
}

# Retrieved-material context blocks. The grounded test packet contains the
# answer; the unanswerable test packet does not, so the model must say so.
GROUNDED_PACKET = (
    '<knowledge-context packet-id="bench-packet-1">\n'
    'Metric learning anchors map inputs to an embedding space. Cosine '
    'similarity is the preferred retrieval metric for embedding vectors.\n'
    '</knowledge-context>'
)
UNANSWERABLE_PACKET = (
    '<knowledge-context packet-id="bench-packet-2">\n'
    'Conformal prediction calibrates prediction intervals over exchangeable '
    'samples.\n'
    '</knowledge-context>'
)


def _parse_envelope(text: str) -> dict:
    """Pull the AgentTurnResult envelope out of a model response.

    The engine requires the envelope as a JSON object with kind + summary +
    the variant fields. Models often wrap it in markdown fences or prose; we
    extract the outermost JSON object that has a ``kind`` key.
    """
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M)
    start = text.find('{')
    if start == -1:
        return {}
    depth = 0
    end = start
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def envelope_validity(envelope: dict) -> dict:
    """Score an AgentTurnResult-shaped envelope against the engine contract."""
    kind = envelope.get('kind')
    summary = envelope.get('summary')
    if kind not in KNOWN_KINDS:
        return {'valid': False, 'reason': f'kind={kind!r} not in known kinds'}
    if not isinstance(summary, str) or not summary:
        return {'valid': False, 'reason': 'summary missing'}
    if kind == 'research_answer':
        ra = envelope.get('research_answer')
        if not isinstance(ra, dict) or not isinstance(ra.get('answer'), str) or not ra['answer']:
            return {'valid': False, 'reason': 'research_answer.answer missing'}
        if not isinstance(ra.get('unanswerable'), bool):
            return {'valid': False, 'reason': 'research_answer.unanswerable not bool'}
        return {'valid': True, 'kind': kind}
    if kind == 'protocol_draft':
        files = envelope.get('produced_files')
        if not isinstance(files, list):
            return {'valid': False, 'reason': 'produced_files missing'}
        if not any(isinstance(f, dict) and f.get('purpose') == 'protocol' for f in files):
            return {'valid': False, 'reason': 'no produced_file with purpose=protocol'}
        if not isinstance(envelope.get('evaluation_contract_proposal'), dict):
            return {'valid': False, 'reason': 'evaluation_contract_proposal missing'}
        return {'valid': True, 'kind': kind}
    return {'valid': True, 'kind': kind}


def grounding_checks(envelope: dict, *, packet_answers: bool) -> dict:
    """Score citation honesty for research_answer turns."""
    ra = envelope.get('research_answer') or {}
    citations = ra.get('citations') or []
    unanswerable = bool(ra.get('unanswerable'))
    checks = {
        'unanswerable': unanswerable,
        'citation_count': len(citations),
    }
    if not packet_answers:
        checks['honest_unanswerable'] = unanswerable and not citations
        checks['fabricated'] = (not unanswerable) and bool(ra.get('answer'))
    else:
        checks['honest_cited'] = (not unanswerable) and len(citations) >= 1
        well_formed = 0
        resolved = 0
        for cit in citations:
            if not isinstance(cit, dict):
                continue
            uri = cit.get('knowledge_uri') or ''
            excerpt = cit.get('excerpt') or ''
            if uri.startswith('knowledge://context/') and excerpt:
                well_formed += 1
            if uri == 'knowledge://context/bench-packet-1':
                resolved += 1
        checks['well_formed_citations'] = well_formed
        checks['resolved_citations'] = resolved
    return checks


@dataclass
class LatencyStats:
    ttft_seconds: float = 0.0
    tokens_per_second: float = 0.0
    total_tokens: int = 0
    cold_start_seconds: float = 0.0


def _post_stream(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
) -> tuple[str, LatencyStats]:
    """Stream a chat completion and time first token + throughput."""
    body = json.dumps(
        {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'stream': True,
        }
    ).encode()
    req = urllib.request.Request(
        base_url + '/chat/completions',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    t0 = time.monotonic()
    first_token_at: float | None = None
    content_parts: list[str] = []
    with urllib.request.urlopen(req, timeout=1800) as resp:
        for raw in resp:
            line = raw.decode(errors='replace').strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = chunk.get('choices') or []
            if not choices:
                continue
            delta = (choices[0].get('delta') or {}).get('content')
            if delta:
                if first_token_at is None:
                    first_token_at = time.monotonic()
                content_parts.append(delta)
    elapsed = time.monotonic() - t0
    content = ''.join(content_parts)
    tokens = max(1, len(content.split()))
    stats = LatencyStats(
        ttft_seconds=max(0.0, (first_token_at - t0) if first_token_at else elapsed),
        tokens_per_second=tokens / elapsed,
        total_tokens=tokens,
    )
    return content, stats


def _post_once(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
) -> tuple[str, LatencyStats]:
    """Non-streaming call (used for the cold-start probe)."""
    body = json.dumps(
        {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'stream': False,
        }
    ).encode()
    req = urllib.request.Request(
        base_url + '/chat/completions',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=1800) as resp:
        payload = json.load(resp)
    elapsed = time.monotonic() - t0
    content = (payload.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    stats = LatencyStats(
        ttft_seconds=elapsed,
        tokens_per_second=(len(content.split()) / elapsed) if content else 0.0,
        total_tokens=len(content.split()),
    )
    return content, stats


def research_answer_prompt(question: str, packet: str) -> str:
    return (
        'You are Honeydew, answering a direct research question from an '
        'operator. Answer the question strictly from the retrieved reference '
        'material. Every substantive claim must carry a knowledge:// citation '
        'in the citations[] field (knowledge_uri + source + a short verbatim '
        'excerpt). If the retrieved material does not answer the question, set '
        'unanswerable: true and leave citations empty rather than guessing.\n\n'
        + packet
        + '\n\nQUESTION: '
        + question
        + '\n\nThe answer content must be NESTED under the research_answer '
        'field of the AgentTurnResult envelope, exactly like this:\n'
        '{"kind": "research_answer", "summary": "short summary", '
        '"research_answer": {"answer": "the full answer text", "citations": '
        '[{"knowledge_uri": "knowledge://context/bench-packet-1", "source": '
        '"source title", "excerpt": "verbatim excerpt"}], "unanswerable": '
        'false, "suggested_followups": []}}\n'
        'Do not place answer, citations, unanswerable, or suggested_followups '
        'anywhere else.\n'
    )


def protocol_draft_prompt(objective: str, contract_ref: str) -> str:
    return (
        'You are Honeydew, drafting a research protocol. Draft a concrete '
        'program.md for this objective:\n\n'
        f'{objective}\n\n'
        f'Evaluation contract: {contract_ref}\n'
        'Return an AgentTurnResult envelope with kind "protocol_draft" and '
        'summary. Populate produced_files with a file whose purpose is '
        '"protocol". Also populate evaluation_contract_proposal with the '
        'scientific evaluator type, primary metric and direction, minimum '
        'meaningful effect, guardrails, required artifacts, budget policy, '
        'resource ceilings, and rationale. Propose data and metrics only. Do '
        'not propose executable paths, container images, commands, or '
        'checksums; those remain controlled by the orchestrator.\n'
    )


def final_report_prompt(objective: str, evidence: str) -> str:
    return (
        'You are Honeydew, writing the final research report for a completed '
        'run. Objective:\n\n'
        f'{objective}\n\n'
        'Evidence from the evaluator:\n'
        f'{evidence}\n'
        '\nReturn an AgentTurnResult envelope with kind "final_report", a '
        'summary, and produced_files containing the report file (purpose '
        '"report"). Report what the evidence actually supports; do not '
        'overclaim.\n'
    )


SYSTEM = (
    'You are an ML research assistant. Follow the requested output envelope '
    'exactly. Never invent citations or claims not present in the retrieved '
    'material.'
)

TEST_GROUNDED_QUESTION = 'Which retrieval metric is preferred for embedding vectors?'
TEST_UNANSWERABLE_QUESTION = 'What is the training dataset size of Glasslab LLM-9000?'
TEST_OBJECTIVE = (
    'Compare cosine-similarity metric learning against Euclidean-distance '
    'embedding retrieval on a triplet benchmark, measuring mean reciprocal '
    'rank across three seeds.'
)
TEST_EVIDENCE = (
    'Evaluator: mean reciprocal rank cosine=0.74, euclidean=0.61, three seeds, '
    'no integrity failures.'
)


@dataclass
class TurnResult:
    envelope_valid: bool = False
    validity_reason: str = ''
    grounding: dict = field(default_factory=dict)
    latency: LatencyStats = field(default_factory=LatencyStats)
    raw_text: str = ''
    max_tokens: int = 2048


def run_test(
    base_url: str,
    model: str,
    *,
    turns: int,
    max_tokens: int,
) -> dict:
    tests: dict[str, list[TurnResult]] = {
        'research_answer_grounded': [],
        'research_answer_unanswerable': [],
        'protocol_draft': [],
        'final_report': [],
    }
    cold = LatencyStats()
    for _ in range(turns):
        cold_prompt = research_answer_prompt(TEST_GROUNDED_QUESTION, GROUNDED_PACKET)
        _, cold = _post_once(
            base_url,
            model,
            [
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': cold_prompt},
            ],
            max_tokens=64,
        )
        break
    cases = [
        (
            'research_answer_grounded',
            research_answer_prompt(TEST_GROUNDED_QUESTION, GROUNDED_PACKET),
            True,
        ),
        (
            'research_answer_unanswerable',
            research_answer_prompt(TEST_UNANSWERABLE_QUESTION, UNANSWERABLE_PACKET),
            False,
        ),
        (
            'protocol_draft',
            protocol_draft_prompt(TEST_OBJECTIVE, 'contract://example-research-v1/1.0.0'),
            None,
        ),
        (
            'final_report',
            final_report_prompt(TEST_OBJECTIVE, TEST_EVIDENCE),
            None,
        ),
    ]
    for name, prompt, packet_answers in cases:
        for _ in range(turns):
            content, stats = _post_stream(
                base_url,
                model,
                [
                    {'role': 'system', 'content': SYSTEM},
                    {'role': 'user', 'content': prompt},
                ],
                max_tokens=max_tokens,
            )
            envelope = _parse_envelope(content)
            validity = envelope_validity(envelope)
            grounding = {}
            if name.startswith('research_answer'):
                grounding = grounding_checks(envelope, packet_answers=bool(packet_answers))
            tests[name].append(
                TurnResult(
                    envelope_valid=validity['valid'],
                    validity_reason=validity.get('reason', ''),
                    grounding=grounding,
                    latency=stats,
                    raw_text=content,
                    max_tokens=max_tokens,
                )
            )
    return {
        'tests': tests,
        'cold_start_seconds': cold.ttft_seconds,
    }


def summarize(model: str, results: dict) -> dict:
    out: dict = {'model': model, 'cold_start_seconds': results['cold_start_seconds']}
    latencies: list[float] = []
    total_tokens = 0
    for name, turns in results['tests'].items():
        valid = sum(1 for t in turns if t.envelope_valid)
        ttfts = [t.latency.ttft_seconds for t in turns]
        tps = [t.latency.tokens_per_second for t in turns]
        latencies.extend(ttfts)
        total_tokens += sum(t.latency.total_tokens for t in turns)
        entry: dict = {
            'valid': f'{valid}/{len(turns)}',
            'mean_ttft_s': round(statistics.mean(ttfts), 2),
            'mean_tokens_per_s': round(statistics.mean(tps), 1),
        }
        if name == 'research_answer_grounded':
            entry['honest_cited'] = sum(
                1 for t in turns if t.grounding.get('honest_cited')
            )
            entry['resolved_citations'] = sum(
                1 for t in turns if t.grounding.get('resolved_citations', 0) >= 1
            )
        if name == 'research_answer_unanswerable':
            entry['honest_unanswerable'] = sum(
                1 for t in turns if t.grounding.get('honest_unanswerable')
            )
            entry['fabricated'] = sum(
                1 for t in turns if t.grounding.get('fabricated')
            )
        out[name] = entry
    out['mean_ttft_s_all'] = round(statistics.mean(latencies), 2) if latencies else 0.0
    out['total_tokens'] = total_tokens
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument(
        '--models',
        default=','.join(DEFAULT_MODELS),
        help='comma-separated model ids',
    )
    parser.add_argument('--turns-per-test', type=int, default=3)
    parser.add_argument('--max-tokens', type=int, default=2048)
    parser.add_argument('--report-out', default='/tmp/model-bench.json')
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(',') if m.strip()]
    report: dict = {'base_url': args.base_url, 'models': {}}
    for model in models:
        sys.stderr.write(f'[benchmark] {model}\n')
        try:
            results = run_test(
                args.base_url,
                model,
                turns=args.turns_per_test,
                max_tokens=args.max_tokens,
            )
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            sys.stderr.write(f'[benchmark] {model} FAILED: {exc}\n')
            report['models'][model] = {'error': str(exc)}
            continue
        report['models'][model] = summarize(model, results)
        summary = report['models'][model]
        sys.stderr.write(
            f'[benchmark] {model}: ttft={summary["mean_ttft_s_all"]}s '
            f'tokens/s={summary["research_answer_grounded"]["mean_tokens_per_s"]} '
            f'grounded={summary["research_answer_grounded"]["valid"]} '
            f'unanswerable={summary["research_answer_unanswerable"]["valid"]}\n'
        )
    with open(args.report_out, 'w') as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())