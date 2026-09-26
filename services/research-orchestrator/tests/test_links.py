"""Signed, self-contained links to reports, artifacts, and context packets.

Covers the token format and tamper resistance, the redemption route's
allowlist/IDOR/traversal defenses, escape-first HTML rendering, the fail-closed
configuration behavior, and the Discord artifact:// fallback.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest

from app.config import Settings
from app.discord_adapter import DiscordHttpAdapter, DiscordRenderer
from app.links import (
    LinkError,
    LinkKind,
    build_link_url,
    sign_link,
    verify_link,
)
from app.main import build_engine, build_link_builder, create_app
from app.schemas import ArtifactRecord, EventRecord, RunCreateRequest, utc_now

BASE_URL = 'https://glasslab.example'
SECRET = 'link-signing-secret-for-tests'
OPERATOR_TOKEN = 'test-operator-token'
RUN_ID = 'run-abcdef12'
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _forge_token(
    *,
    kind: str,
    run_id: str,
    ref: str,
    exp: int,
    kid: str = 'v1',
    secret: str = SECRET,
) -> str:
    # Locks the documented canonical payload (glink + v1 + kid prefix, fields
    # newline-joined). Only used to prove that a correctly signed but
    # out-of-policy payload still fails closed at redemption.
    payload = '\n'.join(
        ('glink', 'v1', kid, kind, run_id, ref, str(exp))
    ).encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), payload, sha256).digest()
    return f'{_b64url(payload)}.{_b64url(signature)}'


def _report_token(
    *,
    run_id: str = RUN_ID,
    ref: str = 'reports/report.md',
    ttl_seconds: int = 3600,
    now: datetime | None = None,
) -> str:
    return sign_link(
        SECRET,
        kind=LinkKind.REPORT,
        run_id=run_id,
        ref=ref,
        ttl_seconds=ttl_seconds,
        now=now or utc_now(),
    )


def _link_settings(settings: Settings, **overrides: object) -> Settings:
    update: dict[str, object] = {
        'public_base_url': BASE_URL,
        'link_signing_secret': SecretStr(SECRET),
        'require_operator_auth': False,
    }
    update.update(overrides)
    return settings.model_copy(update=update)


def _write_artifact(
    root: Path,
    *,
    run_id: str,
    relative: str,
    content: bytes,
    artifact_type: str = 'artifact',
    sha256_override: str | None = None,
    metadata_path: Path | None = None,
    file_root: Path | None = None,
) -> ArtifactRecord:
    path = (file_root or root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ArtifactRecord(
        run_id=run_id,
        type=artifact_type,
        uri=f'artifact://{run_id}/{relative}',
        sha256=sha256_override or sha256(content).hexdigest(),
        metadata={'path': str(metadata_path or path)},
    )


def _link_client(settings: Settings, engine) -> TestClient:
    return TestClient(create_app(settings, engine=engine, start_watcher=False))


# --- token format, signing, and verification -------------------------------


def test_sign_verify_round_trip() -> None:
    token = _report_token(now=NOW)

    payload = verify_link(SECRET, token, now=NOW)

    assert payload.kind is LinkKind.REPORT
    assert payload.run_id == RUN_ID
    assert payload.ref == 'reports/report.md'
    assert payload.kid == 'v1'
    assert payload.exp == int(NOW.timestamp()) + 3600


def test_token_is_one_opaque_path_segment_with_embedded_payload() -> None:
    token = _report_token(now=NOW)

    assert token.count('.') == 1
    assert '?' not in token and '&' not in token and '/' not in token
    assert '=' not in token
    encoded_payload, encoded_signature = token.split('.')
    decoded = base64.urlsafe_b64decode(
        encoded_payload + '=' * (-len(encoded_payload) % 4)
    ).decode('utf-8')
    fields = decoded.split('\n')
    assert fields[0] == 'glink'
    assert fields[1] == 'v1'
    assert fields[2] == 'v1'
    assert fields[3] == 'report'
    assert fields[4] == RUN_ID
    assert fields[5] == 'reports/report.md'
    assert fields[6] == str(int(NOW.timestamp()) + 3600)
    assert base64.urlsafe_b64decode(
        encoded_signature + '=' * (-len(encoded_signature) % 4)
    ) == hmac.new(
        SECRET.encode('utf-8'),
        decoded.encode('utf-8'),
        sha256,
    ).digest()


def _tamper_payload(token: str, mutate) -> str:
    encoded_payload, encoded_signature = token.split('.')
    decoded = base64.urlsafe_b64decode(
        encoded_payload + '=' * (-len(encoded_payload) % 4)
    ).decode('utf-8')
    return f'{_b64url(mutate(decoded).encode("utf-8"))}.{encoded_signature}'


@pytest.mark.parametrize(
    'mutate',
    [
        pytest.param(
            lambda text: text.replace(RUN_ID, 'run-99999999'),
            id='run_id',
        ),
        pytest.param(
            lambda text: text.replace('reports/report.md', 'reports/other.md'),
            id='ref',
        ),
        pytest.param(
            lambda text: text.replace('\nreport\n', '\nartifact\n'),
            id='kind',
        ),
        pytest.param(
            lambda text: text.rsplit('\n', 1)[0] + '\n9999999999',
            id='exp',
        ),
        pytest.param(
            lambda text: text.replace('\nv1\n', '\nv2\n', 1),
            id='kid',
        ),
    ],
)
def test_tampered_fields_are_rejected(mutate) -> None:
    token = _report_token(now=NOW)

    with pytest.raises(LinkError):
        verify_link(SECRET, _tamper_payload(token, mutate), now=NOW)


def test_tampered_signature_is_rejected() -> None:
    token = _report_token(now=NOW)
    encoded_payload, encoded_signature = token.split('.')
    flipped = encoded_signature[:-1] + (
        'A' if encoded_signature[-1] != 'A' else 'B'
    )

    with pytest.raises(LinkError):
        verify_link(SECRET, f'{encoded_payload}.{flipped}', now=NOW)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(LinkError):
        verify_link('a-different-secret', _report_token(now=NOW), now=NOW)


def test_unknown_kid_is_rejected() -> None:
    token = sign_link(
        SECRET,
        kind=LinkKind.REPORT,
        run_id=RUN_ID,
        ref='reports/report.md',
        kid='v2',
        ttl_seconds=3600,
        now=NOW,
    )

    with pytest.raises(LinkError):
        verify_link(SECRET, token, now=NOW)


def test_expired_and_zero_ttl_tokens_are_rejected() -> None:
    aged = _report_token(ttl_seconds=60, now=NOW - timedelta(hours=2))
    immediate = _report_token(ttl_seconds=0, now=NOW)

    with pytest.raises(LinkError):
        verify_link(SECRET, aged, now=NOW)
    with pytest.raises(LinkError):
        verify_link(SECRET, immediate, now=NOW)


@pytest.mark.parametrize(
    'token',
    [
        '',
        'garbage',
        'a.b',
        'not-base64!.not-base64!',
        '....',
        'AAAA',
        'AAAA.',
        '.AAAA',
        'AAAA.BBBB.CCCC',
        'A' * 5000,
    ],
)
def test_garbage_and_truncated_tokens_are_rejected(token: str) -> None:
    with pytest.raises(LinkError):
        verify_link(SECRET, token, now=NOW)


@pytest.mark.parametrize(
    'ref',
    [
        'reports/../../etc/passwd',
        '../reports/report.md',
        '/etc/passwd',
        'reports\\..\\secret.md',
        'reports/\x00report.md',
        'reports/%2e%2e/secret.md',
        'reports/%252e%252e/secret.md',
        'reports/./report.md',
        'reports//report.md',
        'reports/',
        '',
        'reports/report.md\n',
    ],
)
def test_traversal_and_non_canonical_refs_are_rejected_at_signing(
    ref: str,
) -> None:
    with pytest.raises(LinkError):
        sign_link(
            SECRET,
            kind=LinkKind.ARTIFACT,
            run_id=RUN_ID,
            ref=ref,
            ttl_seconds=3600,
            now=NOW,
        )


def test_verify_revalidates_a_correctly_signed_out_of_policy_ref() -> None:
    forged = _forge_token(
        kind='artifact',
        run_id=RUN_ID,
        ref='reports/../secret.md',
        exp=int(NOW.timestamp()) + 3600,
    )

    with pytest.raises(LinkError):
        verify_link(SECRET, forged, now=NOW)


def test_run_relative_ref_derives_from_artifact_uri() -> None:
    from app.links import run_relative_ref

    assert (
        run_relative_ref('artifact://run-abcdef12/reports/report.md', RUN_ID)
        == 'reports/report.md'
    )
    assert run_relative_ref('artifact://run-abcdef12/', RUN_ID) is None
    assert run_relative_ref('artifact://other-run/reports/report.md', RUN_ID) is None
    assert run_relative_ref('https://example.test/report.md', RUN_ID) is None


# --- URL builder and settings ----------------------------------------------


def test_build_link_url_returns_none_when_not_configured() -> None:
    common = {
        'kind': LinkKind.REPORT,
        'run_id': RUN_ID,
        'ref': 'reports/report.md',
        'ttl_seconds': 3600,
        'now': NOW,
    }

    assert build_link_url(public_base_url=None, secret=SECRET, **common) is None
    assert build_link_url(public_base_url=BASE_URL, secret=None, **common) is None
    assert build_link_url(public_base_url='', secret=SECRET, **common) is None


def test_build_link_url_returns_redeemable_url() -> None:
    url = build_link_url(
        public_base_url=BASE_URL,
        secret=SECRET,
        kind=LinkKind.REPORT,
        run_id=RUN_ID,
        ref='reports/report.md',
        ttl_seconds=3600,
        now=NOW,
    )

    assert url is not None
    assert url.startswith(f'{BASE_URL}/links/')
    payload = verify_link(SECRET, url.removeprefix(f'{BASE_URL}/links/'), now=NOW)
    assert payload.ref == 'reports/report.md'


def test_public_base_url_requires_https_unless_localhost() -> None:
    assert Settings().public_base_url is None
    assert (
        Settings(public_base_url='https://glasslab.example/').public_base_url
        == 'https://glasslab.example'
    )
    assert (
        Settings(public_base_url='http://localhost:8000').public_base_url
        == 'http://localhost:8000'
    )
    assert (
        Settings(public_base_url='http://127.0.0.1:8000').public_base_url
        == 'http://127.0.0.1:8000'
    )
    with pytest.raises(ValidationError):
        Settings(public_base_url='http://glasslab.example')
    with pytest.raises(ValidationError):
        Settings(public_base_url='glasslab.example')


def test_link_signing_secret_must_not_reuse_operator_token() -> None:
    with pytest.raises(ValidationError):
        Settings(
            public_base_url=BASE_URL,
            operator_api_token=OPERATOR_TOKEN,
            link_signing_secret=OPERATOR_TOKEN,
        )
    settings = Settings(
        public_base_url=BASE_URL,
        operator_api_token=OPERATOR_TOKEN,
        link_signing_secret='a-dedicated-signing-secret',
    )
    assert settings.link_signing_secret is not None
    assert (
        settings.link_signing_secret.get_secret_value()
        != settings.operator_api_token
    )


def test_build_link_builder_is_none_without_public_base_url(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, _ = orchestrator_bundle

    assert build_link_builder(settings) is None


def test_build_link_builder_fails_closed_without_secret(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, _ = orchestrator_bundle
    configured = settings.model_copy(
        update={'public_base_url': BASE_URL, 'link_signing_secret': None}
    )

    builder = build_link_builder(configured)

    assert builder is not None
    assert builder(LinkKind.REPORT, RUN_ID, 'reports/report.md') is None


def test_build_link_builder_emits_verifiable_token(orchestrator_bundle) -> None:
    settings, _, _, _, _ = orchestrator_bundle
    builder = build_link_builder(_link_settings(settings))
    assert builder is not None

    url = builder(LinkKind.REPORT, RUN_ID, 'reports/report.md')

    assert url is not None
    payload = verify_link(
        SECRET, url.removeprefix(f'{BASE_URL}/links/'), now=utc_now()
    )
    assert payload.run_id == RUN_ID
    assert payload.ref == 'reports/report.md'


# --- Discord projection: links when configured, artifact:// otherwise -----


def _report_created_event(run_id: str = RUN_ID) -> EventRecord:
    return EventRecord(
        sequence_number=1,
        run_id=run_id,
        source='honeydew',
        event_type='report.created',
        payload={
            'uri': f'artifact://{run_id}/reports/report.md',
            'path': '/mnt/artifacts/reports/report.md',
            'sha256': 'a' * 64,
        },
    )


def _bundle_created_event(run_id: str = RUN_ID) -> EventRecord:
    return EventRecord(
        sequence_number=2,
        run_id=run_id,
        source='orchestrator',
        event_type='report.bundle_created',
        payload={
            'pdf': f'artifact://{run_id}/reports/report-20260925.pdf',
            'docx': f'artifact://{run_id}/reports/report-20260925.docx',
        },
    )


def test_renderer_keeps_artifact_uri_when_links_are_unset() -> None:
    renderer = DiscordRenderer()

    report = renderer.render(_report_created_event())
    bundle = renderer.render(_bundle_created_event())

    assert report is not None
    assert f'artifact://{RUN_ID}/reports/report.md' in report.content
    assert '/links/' not in report.content
    assert bundle is not None
    assert f'`artifact://{RUN_ID}/reports/report-20260925.pdf`' in bundle.content
    assert f'`artifact://{RUN_ID}/reports/report-20260925.docx`' in bundle.content
    assert '/links/' not in bundle.content


def test_renderer_emits_signed_urls_for_report_and_bundle() -> None:
    calls: list[tuple[LinkKind, str, str]] = []

    def builder(kind: LinkKind, run_id: str, ref: str) -> str:
        calls.append((kind, run_id, ref))
        return f'{BASE_URL}/links/{kind.value}-{Path(ref).name}'

    renderer = DiscordRenderer(link_builder=builder)

    report = renderer.render(_report_created_event())
    bundle = renderer.render(_bundle_created_event())

    assert report is not None
    assert f'{BASE_URL}/links/report-report.md' in report.content
    assert 'artifact://' not in report.content
    assert bundle is not None
    assert f'{BASE_URL}/links/artifact-report-20260925.pdf' in bundle.content
    assert f'{BASE_URL}/links/artifact-report-20260925.docx' in bundle.content
    assert 'artifact://' not in bundle.content
    assert calls == [
        (LinkKind.REPORT, RUN_ID, 'reports/report.md'),
        (LinkKind.ARTIFACT, RUN_ID, 'reports/report-20260925.pdf'),
        (LinkKind.ARTIFACT, RUN_ID, 'reports/report-20260925.docx'),
    ]


def test_renderer_falls_back_when_link_building_fails() -> None:
    def builder(kind: LinkKind, run_id: str, ref: str) -> str:
        raise LinkError('out of policy')

    renderer = DiscordRenderer(link_builder=builder)

    report = renderer.render(_report_created_event())

    assert report is not None
    assert f'Report ready: artifact://{RUN_ID}/reports/report.md' == report.content


def test_build_engine_wires_the_link_builder(orchestrator_bundle) -> None:
    settings, _, cluster, runtime, _ = orchestrator_bundle
    configured = settings.model_copy(
        update={
            'discord_enabled': True,
            'discord_bot_token': 'test-token',
            'discord_channel_id': 'channel-1',
            'public_base_url': BASE_URL,
            'link_signing_secret': SecretStr(SECRET),
        }
    )

    engine = build_engine(configured, runtime=runtime, cluster=cluster)
    adapter = engine.discord

    assert isinstance(adapter, DiscordHttpAdapter)
    assert adapter.link_builder is not None
    assert adapter.renderer.link_builder is not None


def test_build_engine_leaves_link_builder_unset_without_base_url(
    orchestrator_bundle,
) -> None:
    settings, _, cluster, runtime, _ = orchestrator_bundle
    configured = settings.model_copy(
        update={
            'discord_enabled': True,
            'discord_bot_token': 'test-token',
            'discord_channel_id': 'channel-1',
        }
    )

    engine = build_engine(configured, runtime=runtime, cluster=cluster)
    adapter = engine.discord

    assert isinstance(adapter, DiscordHttpAdapter)
    assert adapter.link_builder is None


# --- redemption route -------------------------------------------------------


def test_report_link_renders_escaped_markdown_with_headers(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='inspect the escaped signed report')
    )
    content = (
        b'# Findings\n\n'
        b'<script>alert(1)</script>\n'
        b'<img src=x onerror=alert(1)>\n'
        b'Result: 0.91\n'
    )
    artifact = _write_artifact(
        tmp_path,
        run_id=run.run_id,
        relative='reports/report.md',
        content=content,
        artifact_type='report',
    )
    engine.store.save_artifact(artifact)
    token = _report_token(run_id=run.run_id)

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert (
        response.headers['content-security-policy']
        == "default-src 'none'; style-src 'unsafe-inline'"
    )
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['content-disposition'] == 'inline'
    assert '<script>' not in response.text
    assert '<img' not in response.text
    assert '&lt;script&gt;' in response.text
    assert '&lt;img' in response.text
    assert '# Findings' in response.text


@pytest.mark.parametrize(
    'relative',
    ['plots/loss.png', 'tables/scores.csv', 'shared-artifacts/notes.txt'],
)
def test_artifact_link_serves_verified_bytes_as_attachment(
    tmp_path: Path,
    orchestrator_bundle,
    relative: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='download a signed artifact')
    )
    content = f'artifact-bytes-for-{relative}'.encode()
    artifact = _write_artifact(
        tmp_path, run_id=run.run_id, relative=relative, content=content
    )
    engine.store.save_artifact(artifact)
    token = sign_link(
        SECRET,
        kind=LinkKind.ARTIFACT,
        run_id=run.run_id,
        ref=relative,
        ttl_seconds=3600,
        now=utc_now(),
    )

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 200
    assert response.content == content
    assert (
        response.headers['content-disposition']
        == f'attachment; filename="{Path(relative).name}"'
    )
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'


def test_token_for_run_a_cannot_read_run_b_artifact(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run_a = engine.create_run(RunCreateRequest(objective='run A report scope'))
    run_b = engine.create_run(RunCreateRequest(objective='run B report scope'))
    artifact_a = _write_artifact(
        tmp_path,
        file_root=tmp_path / run_a.run_id,
        run_id=run_a.run_id,
        relative='reports/report.md',
        content=b'# Run A report\n',
        artifact_type='report',
    )
    artifact_b = _write_artifact(
        tmp_path,
        file_root=tmp_path / run_b.run_id,
        run_id=run_b.run_id,
        relative='reports/report.md',
        content=b'# Run B private report\n',
        artifact_type='report',
    )
    engine.store.save_artifact(artifact_a)
    engine.store.save_artifact(artifact_b)
    token_a = _report_token(run_id=run_a.run_id)

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token_a}')

    assert response.status_code == 200
    assert 'Run A report' in response.text
    assert 'Run B private report' not in response.text


def test_token_never_falls_back_to_another_run_artifact(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run_a = engine.create_run(RunCreateRequest(objective='run A without report'))
    run_b = engine.create_run(RunCreateRequest(objective='run B with report'))
    artifact_b = _write_artifact(
        tmp_path,
        run_id=run_b.run_id,
        relative='reports/report.md',
        content=b'# Run B report\n',
        artifact_type='report',
    )
    engine.store.save_artifact(artifact_b)
    token_a = _report_token(run_id=run_a.run_id)

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token_a}')

    assert response.status_code == 404
    assert 'Run B report' not in response.text


@pytest.mark.parametrize(
    'ref',
    ['reports/../secret.md', 'plots/%2e%2e/secret.png'],
)
def test_route_rejects_signed_but_out_of_policy_ref(
    tmp_path: Path,
    orchestrator_bundle,
    ref: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='forge a traversal ref'))
    forged = _forge_token(
        kind='artifact',
        run_id=run.run_id,
        ref=ref,
        exp=int(utc_now().timestamp()) + 3600,
    )

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{forged}')

    assert response.status_code == 404


def test_symlink_escape_is_unreachable(
    tmp_path: Path,
    tmp_path_factory,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='follow a symlink out'))
    outside = tmp_path_factory.mktemp('outside') / 'secret.md'
    outside.write_bytes(b'outside-the-run-root')
    link = tmp_path / 'reports' / 'leak.md'
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    artifact = ArtifactRecord(
        run_id=run.run_id,
        type='report',
        uri=f'artifact://{run.run_id}/reports/leak.md',
        sha256=sha256(outside.read_bytes()).hexdigest(),
        metadata={'path': str(link)},
    )
    engine.store.save_artifact(artifact)
    token = _report_token(run_id=run.run_id, ref='reports/leak.md')

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404
    assert b'outside-the-run-root' not in response.content


def test_digest_mismatch_is_unreachable(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='tamper a linked file'))
    artifact = _write_artifact(
        tmp_path,
        run_id=run.run_id,
        relative='reports/report.md',
        content=b'# Tampered report\n',
        artifact_type='report',
        sha256_override='0' * 64,
    )
    engine.store.save_artifact(artifact)
    token = _report_token(run_id=run.run_id)

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404
    assert b'Tampered report' not in response.content


@pytest.mark.parametrize(
    'relative',
    [
        'source.zip',
        'task.zip',
        'metrics.json',
        'evaluation.json',
        'runtime/beaker/session.log',
        'beaker-worktree/src/main.py',
    ],
)
def test_non_linkable_refs_are_unreachable(
    tmp_path: Path,
    orchestrator_bundle,
    relative: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='reach eval internals'))
    artifact = _write_artifact(
        tmp_path,
        run_id=run.run_id,
        relative=relative,
        content=b'private-eval-bytes',
    )
    engine.store.save_artifact(artifact)
    token = sign_link(
        SECRET,
        kind=LinkKind.ARTIFACT,
        run_id=run.run_id,
        ref=relative,
        ttl_seconds=3600,
        now=utc_now(),
    )

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404
    assert b'private-eval-bytes' not in response.content


def test_report_kind_outside_reports_prefix_is_denied(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='mislabel a plot ref'))
    artifact = _write_artifact(
        tmp_path,
        run_id=run.run_id,
        relative='plots/loss.png',
        content=b'plot-bytes',
    )
    engine.store.save_artifact(artifact)
    token = _report_token(run_id=run.run_id, ref='plots/loss.png')

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404


def test_packet_link_renders_the_shared_packet_page(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='inspect a signed packet citation')
    )
    packet = engine.knowledge.retrieve(
        run_id=run.run_id,
        agent='honeydew',
        turn_number=1,
        turn_kind='research_answer',
        query='what is a metric space',
        run_scope=run.run_id,
        allowed_source_types=None,
    )
    linked = _link_settings(settings, knowledge_root=str(tmp_path / 'knowledge'))
    token = sign_link(
        SECRET,
        kind=LinkKind.PACKET,
        run_id=run.run_id,
        ref=f'knowledge://context:{packet.packet_id}',
        ttl_seconds=3600,
        now=utc_now(),
    )

    with _link_client(linked, engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 200
    assert 'Knowledge packet' in response.text
    assert packet.packet_id in response.text
    assert (
        response.headers['content-security-policy']
        == "default-src 'none'; style-src 'unsafe-inline'"
    )


def test_packet_link_for_another_run_is_denied(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run_a = engine.create_run(RunCreateRequest(objective='packet run A scope'))
    run_b = engine.create_run(RunCreateRequest(objective='packet run B scope'))
    packet = engine.knowledge.retrieve(
        run_id=run_b.run_id,
        agent='honeydew',
        turn_number=1,
        turn_kind='research_answer',
        query='what is a metric space',
        run_scope=run_b.run_id,
        allowed_source_types=None,
    )
    linked = _link_settings(settings, knowledge_root=str(tmp_path / 'knowledge'))
    token = sign_link(
        SECRET,
        kind=LinkKind.PACKET,
        run_id=run_a.run_id,
        ref=packet.packet_id,
        ttl_seconds=3600,
        now=utc_now(),
    )

    with _link_client(linked, engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404


# --- fail-closed configuration and bad tokens ------------------------------


def test_links_route_returns_503_when_signing_secret_is_missing(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    misconfigured = settings.model_copy(
        update={'public_base_url': BASE_URL, 'link_signing_secret': None}
    )

    with _link_client(misconfigured, engine) as client:
        response = client.get('/links/whatever')

    assert response.status_code == 503


def test_links_route_returns_404_when_not_configured(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _link_client(settings, engine) as client:
        response = client.get('/links/whatever')

    assert response.status_code == 404


@pytest.mark.parametrize(
    'token',
    ['garbage', 'a.b', 'AAAA.BBBB.CCCC'],
)
def test_bad_token_returns_404(
    orchestrator_bundle,
    token: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404


def test_expired_token_returns_404(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    token = _report_token(
        ttl_seconds=60,
        now=utc_now() - timedelta(hours=2),
    )

    with _link_client(_link_settings(settings), engine) as client:
        response = client.get(f'/links/{token}')

    assert response.status_code == 404


# --- operator-token separation ---------------------------------------------


def test_link_route_is_not_operator_auth_and_operator_routes_stay_gated(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='auth separation probe'))
    artifact = _write_artifact(
        tmp_path,
        run_id=run.run_id,
        relative='reports/report.md',
        content=b'# Auth separation\n',
        artifact_type='report',
    )
    engine.store.save_artifact(artifact)
    secured = settings.model_copy(
        update={
            'public_base_url': BASE_URL,
            'link_signing_secret': SecretStr(SECRET),
            'require_operator_auth': True,
            'operator_api_token': OPERATOR_TOKEN,
        }
    )
    token = _report_token(run_id=run.run_id)

    with _link_client(secured, engine) as client:
        assert client.get('/runs').status_code == 401
        assert (
            client.get(
                '/runs',
                headers={'X-Glasslab-Operator-Token': OPERATOR_TOKEN},
            ).status_code
            == 200
        )
        # A link token must never satisfy require_operator.
        assert (
            client.get(
                '/runs',
                headers={'X-Glasslab-Operator-Token': token},
            ).status_code
            == 401
        )
        # The operator token is not a link token, and the link route itself
        # needs no operator header.
        assert client.get(f'/links/{OPERATOR_TOKEN}').status_code == 404
        assert client.get(f'/links/{token}').status_code == 200
