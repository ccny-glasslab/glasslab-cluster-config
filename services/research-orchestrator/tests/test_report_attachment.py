"""Discord-native report artifact inspectability.

A report event's file(s) are posted alongside the announcement through the
bot REST multipart path (the webhook path is JSON-only), and any loading
problem degrades to the plain announcement instead of failing the workflow.
"""

from __future__ import annotations

from collections.abc import Callable
import email
from email import policy
from hashlib import sha256
import json
from pathlib import Path

import httpx

from app.discord_adapter import DiscordAttachment, DiscordHttpAdapter
from app.main import build_engine, build_report_attachment_loader
from app.schemas import ArtifactRecord, EventRecord


class _StubStore:
    def __init__(self, artifacts: list[ArtifactRecord]) -> None:
        self._artifacts = artifacts

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return [
            artifact
            for artifact in self._artifacts
            if artifact.run_id == run_id
        ]


def _artifact(
    root: Path,
    *,
    relative: str,
    artifact_type: str,
    content: bytes,
) -> ArtifactRecord:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ArtifactRecord(
        run_id='run-1',
        type=artifact_type,
        uri=f'artifact://run-1/{relative}',
        sha256=sha256(content).hexdigest(),
        metadata={'path': str(path)},
    )


def _report_created_event(root: Path, content: bytes) -> EventRecord:
    path = root / 'reports' / 'report.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return EventRecord(
        sequence_number=1,
        run_id='run-1',
        source='honeydew',
        event_type='report.created',
        payload={
            'uri': 'artifact://run-1/reports/report.md',
            'path': str(path),
            'sha256': sha256(content).hexdigest(),
        },
    )


def _adapter(
    transport: httpx.MockTransport,
    loader: (
        Callable[[EventRecord], tuple[DiscordAttachment, ...]] | None
    ) = None,
    *,
    webhook_url: str | None = None,
) -> DiscordHttpAdapter:
    return DiscordHttpAdapter(
        bot_token='bot-token',
        channel_id='channel-1',
        webhook_url=webhook_url,
        transport=transport,
        attachment_loader=loader,
    )


def _multipart_form(
    request: httpx.Request,
) -> dict[str, tuple[str | None, bytes]]:
    raw = (
        f'Content-Type: {request.headers["content-type"]}\r\n\r\n'
    ).encode() + request.content
    message = email.message_from_bytes(raw, policy=policy.default)
    form: dict[str, tuple[str | None, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        payload = part.get_payload(decode=True)
        if name is not None and payload is not None:
            form[name] = (part.get_filename(), payload)
    return form


def _recording_transport(
    requests: list[httpx.Request],
) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'id': 'message-1'})

    return httpx.MockTransport(respond)


def test_report_attachment_posts_multipart_instead_of_webhook(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    content = b'# Findings\n\nResult.\n'
    event = _report_created_event(tmp_path, content)
    loader = build_report_attachment_loader(
        store=_StubStore([]),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
    )
    adapter = _adapter(
        _recording_transport(requests),
        loader,
        webhook_url='https://discord.com/api/webhooks/webhook-id/token',
    )

    status_id = adapter.publish(
        thread_id='thread-1',
        status_message_id=None,
        event=event,
    )

    assert status_id is None
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == '/api/v10/channels/thread-1/messages'
    assert '/webhooks/' not in str(request.url)
    assert request.headers['content-type'].startswith('multipart/form-data')
    form = _multipart_form(request)
    assert form['files[0]'][0] == 'report.md'
    assert form['files[0]'][1] == content
    payload = json.loads(form['payload_json'][1])
    assert payload['content'].startswith('**Honeydew:** Report ready')
    assert payload['embeds'][0]['title'] == 'Report ready'
    assert any(
        field['name'] == 'SHA-256'
        for field in payload['embeds'][0]['fields']
    )


def test_loader_exception_still_posts_the_plain_message(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def broken_loader(event: EventRecord) -> tuple[DiscordAttachment, ...]:
        raise RuntimeError('artifact read failed')

    adapter = _adapter(_recording_transport(requests), broken_loader)

    status_id = adapter.publish(
        thread_id='thread-1',
        status_message_id=None,
        event=_report_created_event(tmp_path, b'# Findings\n'),
    )

    assert status_id is None
    assert len(requests) == 1
    assert requests[0].headers['content-type'].startswith('application/json')
    payload = json.loads(requests[0].content)
    assert 'Report ready' in payload['content']


def test_oversized_bundle_file_is_skipped_and_message_still_posts(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    small = b'%PDF-1.4 small\n'
    pdf = _artifact(
        tmp_path,
        relative='reports/report.pdf',
        artifact_type='report.pdf',
        content=small,
    )
    docx = _artifact(
        tmp_path,
        relative='reports/report.docx',
        artifact_type='report.docx',
        content=b'x' * 4096,
    )
    loader = build_report_attachment_loader(
        store=_StubStore([pdf, docx]),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024,
    )
    event = EventRecord(
        sequence_number=2,
        run_id='run-1',
        source='orchestrator',
        event_type='report.bundle_created',
        payload={'pdf': pdf.uri, 'docx': docx.uri},
    )
    adapter = _adapter(
        _recording_transport(requests),
        loader,
        webhook_url='https://discord.com/api/webhooks/webhook-id/token',
    )

    adapter.publish(
        thread_id='thread-1',
        status_message_id=None,
        event=event,
    )

    assert len(requests) == 1
    assert '/webhooks/' not in str(requests[0].url)
    form = _multipart_form(requests[0])
    assert form['files[0]'][0] == 'report.pdf'
    assert form['files[0]'][1] == small
    assert 'files[1]' not in form


def test_symlinked_report_file_is_not_attached(tmp_path: Path) -> None:
    real = tmp_path / 'reports' / 'real.md'
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b'# Findings\n')
    link = tmp_path / 'reports' / 'report.md'
    link.symlink_to(real)
    event = EventRecord(
        sequence_number=1,
        run_id='run-1',
        source='honeydew',
        event_type='report.created',
        payload={
            'uri': 'artifact://run-1/reports/report.md',
            'path': str(link),
            'sha256': sha256(real.read_bytes()).hexdigest(),
        },
    )
    loader = build_report_attachment_loader(
        store=_StubStore([]),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024,
    )

    assert loader(event) == ()


def test_build_engine_wires_the_report_attachment_loader(
    orchestrator_bundle,
) -> None:
    settings, _store, cluster, runtime, _engine = orchestrator_bundle
    settings = settings.model_copy(
        update={
            'discord_enabled': True,
            'discord_bot_token': 'test-token',
            'discord_channel_id': 'channel-1',
            'maximum_discord_artifact_bundle_bytes': 1024,
        }
    )
    engine = build_engine(settings, runtime=runtime, cluster=cluster)
    adapter = engine.discord
    assert isinstance(adapter, DiscordHttpAdapter)
    assert adapter.attachment_loader is not None

    content = b'# Findings\n'
    path = Path(settings.shared_mount_root) / 'reports' / 'report.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    event = EventRecord(
        sequence_number=1,
        run_id='run-1',
        source='honeydew',
        event_type='report.created',
        payload={
            'uri': 'artifact://run-1/reports/report.md',
            'path': str(path),
            'sha256': sha256(content).hexdigest(),
        },
    )

    attachments = adapter.attachment_loader(event)

    assert [(item.filename, item.content) for item in attachments] == [
        ('report.md', content)
    ]
