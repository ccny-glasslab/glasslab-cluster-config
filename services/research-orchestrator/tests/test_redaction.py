"""Redaction of credential-shaped content before it leaves the process.

Covers app.redaction.redact_payload directly (the primitive used by
turn_inspection.py to sanitize TurnRecord.input_event/structured_output for
GET /runs/{run_id}/turns and the /research-turns Discord command).
"""

from __future__ import annotations

from app.redaction import REDACTED, redact_free_text, redact_payload


def test_redacts_known_credential_field_names() -> None:
    payload = {
        'discord_bot_token': 'not-a-real-token-but-should-be-hidden',
        'x_glasslab_operator_token': 'operator-secret-value',
        'model_api_key': 'sk-live-not-real',
        'kubeconfig': {'clusters': [{'name': 'glasslab'}]},
        'client_secret': 'abc',
        'authorization': 'irrelevant-value',
        'objective': 'Compare two bounded metric-learning methods.',
    }

    redacted = redact_payload(payload)

    assert redacted['discord_bot_token'] == REDACTED
    assert redacted['x_glasslab_operator_token'] == REDACTED
    assert redacted['model_api_key'] == REDACTED
    # A sensitive key redacts its whole value, container or not.
    assert redacted['kubeconfig'] == REDACTED
    assert redacted['client_secret'] == REDACTED
    assert redacted['authorization'] == REDACTED
    assert redacted['objective'] == payload['objective']


def test_benign_token_shaped_keys_survive() -> None:
    payload = {'token_count': 42, 'token_budget': 4000}

    redacted = redact_payload(payload)

    assert redacted == payload


def test_redacts_discord_bot_token_shaped_string_under_innocuous_key() -> None:
    # A real Discord bot token embedded in ordinary text, e.g. pasted by
    # accident into a rejection reason or produced-file summary. The sentence
    # deliberately avoids other trigger keywords so this exercises the
    # token-shape detector specifically, not the keyword-context patterns.
    # Built from separate segments (rather than one dotted literal) so this
    # synthetic fixture isn't shaped like a credential in the source text
    # itself, only once assembled at runtime.
    token_segments = (
        'NzI1ODk4NzY1NDMyMTA5ODc2',
        'GxKq0v',
        'abcdefghijklmnopqrstuvwxyz012345',
    )
    token = '.'.join(token_segments)
    payload = {'summary': f'Deploy update note: {token}'}

    redacted = redact_payload(payload)

    assert token not in redacted['summary']
    assert redacted['summary'] == REDACTED


def test_redacts_kubeconfig_content_by_shape() -> None:
    kubeconfig_text = (
        'apiVersion: v1\nkind: Config\nusers:\n- user:\n'
        '    client-certificate-data: LS0tLS1CRUdJTi==\n'
        '    client-key-data: LS0tLS1CRUdJTi==\n'
    )
    payload = {'produced_text': kubeconfig_text}

    redacted = redact_payload(payload)

    assert redacted['produced_text'] == REDACTED


def test_redacts_pem_private_key_block() -> None:
    pem = '-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----'
    payload = {'notes': pem}

    redacted = redact_payload(payload)

    assert redacted['notes'] == REDACTED


def test_redact_free_text_preserves_credential_prose() -> None:
    # Keyword heuristics would nuke these sentences; free-text redaction must
    # not, because reports and answers legitimately discuss credentials.
    prose = (
        'Password managers and api_key= placeholders are ordinary '
        'documentation; rotate the token when a secret expires.'
    )

    assert redact_free_text(prose) == prose


def test_redact_free_text_redacts_concrete_credential_formats() -> None:
    # Built from segments so the synthetic key never appears as a literal
    # credential in the source text itself.
    github_token = 'gh' + 'p_' + 'a' * 36
    text = f'Leaked key {github_token} must never leave the process.'

    redacted = redact_free_text(text)

    assert github_token not in redacted
    assert 'Leaked key' in redacted
    assert REDACTED in redacted


def test_redact_free_text_redacts_whole_pem_block() -> None:
    body = 'MIIBfakefakefake'
    pem = (
        '-----BEGIN RSA PRIVATE KEY-----\n'
        f'{body}\n'
        '-----END RSA PRIVATE KEY-----'
    )

    redacted = redact_free_text(f'Attached material:\n{pem}\ndone')

    assert body not in redacted
    assert 'PRIVATE KEY' not in redacted
    assert 'Attached material:' in redacted
    assert redacted.count(REDACTED) == 1


def test_redact_free_text_redacts_truncated_pem_block() -> None:
    # A BEGIN header with no END marker must still fail closed through
    # end-of-string rather than leaving the key body readable.
    body = 'MIIBtruncatedfakebody'
    truncated = f'-----BEGIN RSA PRIVATE KEY-----\n{body}\n'

    redacted = redact_free_text(f'prefix {truncated}')

    assert body not in redacted
    assert '-----BEGIN' not in redacted
    assert 'PRIVATE KEY' not in redacted


def test_leaves_provenance_hashes_and_uris_untouched() -> None:
    # sha256 digests and evidence URIs are exactly the structured input/output
    # content the turn-inspection endpoint exists to expose; they must never
    # be treated as credentials.
    payload = {
        'sha256': 'a' * 64,
        'evaluation_contract_digest': 'b' * 64,
        'evidence': ['artifact://run-1/protocol/program.md'],
        'kind': 'protocol_draft',
    }

    redacted = redact_payload(payload)

    assert redacted == payload


def test_redacts_nested_containers_recursively() -> None:
    payload = {
        'requested_actions': [
            {
                'type': 'submit_matrix',
                'arguments': {'api_key': 'super-secret'},
                'reason': 'Run the corrected matrix.',
            }
        ]
    }

    redacted = redact_payload(payload)

    assert redacted['requested_actions'][0]['arguments']['api_key'] == REDACTED
    assert redacted['requested_actions'][0]['reason'] == (
        'Run the corrected matrix.'
    )


def test_redacts_url_with_embedded_token_query_parameter() -> None:
    payload = {
        'source_url': 'https://example.com/data.csv?token=abcdef0123456789',
    }

    redacted = redact_payload(payload)

    assert redacted['source_url'] == REDACTED


def test_non_string_leaves_are_untouched() -> None:
    payload = {'done': True, 'turn_number': 3, 'score': 0.87, 'note': None}

    redacted = redact_payload(payload)

    assert redacted == payload
