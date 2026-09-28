"""Manifest wiring guard for the corpus ingestion Jobs.

The corpus Jobs used to run against a SQLite file on the shared PVC and to
mount script ConfigMaps that were not tracked in the repo (so ``kubectl
apply`` alone was insufficient). These tests parse the tracked manifests and
pin the production contract: the configured Postgres store, staged raw PDFs
under the configured raw root, the shared-artifacts PVC, the DSN secret, and
orchestrator-matching ownership (uid/gid 10001) so the orchestrator can read
what the Jobs stage. Every ConfigMap a Job references must exist as a tracked
manifest under kubeadm/.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = REPO_ROOT / 'kubeadm' / 'glasslab-v2' / 'jobs'
KUBEADM_DIR = REPO_ROOT / 'kubeadm'

RAW_ROOT = '/mnt/artifacts/research-orchestrator/rag/raw'
PVC_NAME = 'glasslab-shared-artifacts'
DSN_SECRET = 'glasslab-research-orchestrator'
DSN_KEY = 'GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN'

CORPUS_INGEST_PATH = JOBS_DIR / 'corpus-ingest.yaml'
ARXIV_SYNC_PATH = JOBS_DIR / 'corpus-arxiv-sync.yaml'
POSTGRES_POLICY_PATH = (
    REPO_ROOT / 'kubeadm' / 'glasslab-v2' / 'postgres' / '50-network-policy.yaml'
)
EMBED_CONFIGMAP_PATH = JOBS_DIR / 'corpus-embed-script-configmap.yaml'
EMBED_SCRIPT_PATH = (
    REPO_ROOT
    / 'services'
    / 'research-orchestrator'
    / 'scripts'
    / 'corpus_gpu_embed.py'
)

_PINNED_IMAGE_RE = re.compile(r'(@sha256:[0-9a-f]{64}|:[0-9a-f]{40})$')


def _load(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding='utf-8') as handle:
        return [doc for doc in yaml.safe_load_all(handle) if doc]


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    if doc['kind'] == 'CronJob':
        return doc['spec']['jobTemplate']['spec']['template']['spec']
    return doc['spec']['template']['spec']


def _pod_template_labels(doc: dict[str, Any]) -> dict[str, Any]:
    if doc['kind'] == 'CronJob':
        template = doc['spec']['jobTemplate']['spec']['template']
    else:
        template = doc['spec']['template']
    return template.get('metadata', {}).get('labels', {})


def _postgres_ingress_allowed_names() -> set[str]:
    policy = _load(POSTGRES_POLICY_PATH)[0]
    names: set[str] = set()
    for rule in policy['spec']['ingress']:
        for source in rule.get('from', []):
            name = (
                source.get('podSelector', {})
                .get('matchLabels', {})
                .get('app.kubernetes.io/name')
            )
            if name:
                names.add(name)
    return names


def _container(doc: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    containers = _pod_spec(doc)['containers']
    for container in containers:
        if name is None or container['name'] == name:
            return container
    raise AssertionError(f'no container {name!r} in {containers!r}')


def _command_text(container: dict[str, Any]) -> str:
    return ' '.join(str(part) for part in container.get('command', []))


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {entry['name']: entry for entry in container.get('env', [])}


def _env_value(env: dict[str, Any], name: str) -> Any:
    entry = env[name]
    if 'value' in entry:
        return entry['value']
    return entry['valueFrom']


def _iter_configmap_refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        config_map = node.get('configMap')
        if isinstance(config_map, dict) and 'name' in config_map:
            yield config_map['name']
        for value in node.values():
            yield from _iter_configmap_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_configmap_refs(value)


def _tracked_configmap_names() -> set[str]:
    names: set[str] = set()
    for path in KUBEADM_DIR.rglob('*.yaml'):
        for doc in _load(path):
            if isinstance(doc, dict) and doc.get('kind') == 'ConfigMap':
                names.add(doc['metadata']['name'])
    return names


def test_corpus_ingest_job_uses_postgres_and_stages_raw() -> None:
    docs = _load(CORPUS_INGEST_PATH)
    job = next(doc for doc in docs if doc.get('kind') == 'Job')
    container = _container(job)
    command = _command_text(container)
    env = _env(container)

    assert 'ingest_corpus.py' in command
    assert '--store' not in command
    assert RAW_ROOT in command

    assert _env_value(env, 'GLASSLAB_ORCHESTRATOR_STORE_BACKEND') == 'postgres'
    assert _env_value(env, 'GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT') == RAW_ROOT
    assert _env_value(
        env, 'GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN'
    ) == {'secretKeyRef': {'name': DSN_SECRET, 'key': DSN_KEY}}

    pod_spec = _pod_spec(job)
    assert pod_spec.get('automountServiceAccountToken') is False
    assert pod_spec.get('serviceAccountName')
    assert pod_spec['securityContext']['fsGroup'] == 10001
    assert container['securityContext']['runAsUser'] == 10001
    assert container['securityContext']['runAsGroup'] == 10001

    mount_paths = {mount['mountPath'] for mount in container['volumeMounts']}
    assert any(RAW_ROOT.startswith(path) for path in mount_paths)
    claims = {
        volume.get('persistentVolumeClaim', {}).get('claimName')
        for volume in pod_spec['volumes']
    }
    assert PVC_NAME in claims


def test_corpus_arxiv_sync_uses_configured_store_and_raw_root() -> None:
    docs = _load(ARXIV_SYNC_PATH)
    cron = next(doc for doc in docs if doc.get('kind') == 'CronJob')
    container = _container(cron)
    command = _command_text(container)
    env = _env(container)

    assert 'ingest_arxiv.py' in command
    assert '--store' not in command
    assert '.db' not in command
    assert '--with-index' not in command
    assert _env_value(env, 'GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT') == RAW_ROOT
    assert _env_value(env, 'GLASSLAB_ORCHESTRATOR_STORE_BACKEND') == 'postgres'
    assert _env_value(
        env, 'GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN'
    ) == {'secretKeyRef': {'name': DSN_SECRET, 'key': DSN_KEY}}

    pod_spec = _pod_spec(cron)
    assert pod_spec.get('automountServiceAccountToken') is False
    assert pod_spec.get('serviceAccountName')
    assert pod_spec['securityContext']['fsGroup'] == 10001
    assert container['securityContext']['runAsUser'] == 10001
    assert container['securityContext']['runAsGroup'] == 10001
    assert _PINNED_IMAGE_RE.search(container['image']), container['image']

    claims = {
        volume.get('persistentVolumeClaim', {}).get('claimName')
        for volume in pod_spec['volumes']
    }
    assert PVC_NAME in claims


def test_corpus_job_pods_are_admitted_by_the_postgres_ingress_policy() -> None:
    allowed = _postgres_ingress_allowed_names()
    for path in (CORPUS_INGEST_PATH, ARXIV_SYNC_PATH):
        doc = _load(path)[0]
        name = _pod_template_labels(doc).get('app.kubernetes.io/name')
        assert name, f'{path.name} pod template has no app.kubernetes.io/name label'
        assert name in allowed, (
            f'{path.name} pod label {name!r} is not admitted by '
            'glasslab-postgres-ingress, so the Job cannot reach Postgres'
        )


def test_every_job_configmap_reference_is_tracked() -> None:
    referenced: set[str] = set()
    for path in JOBS_DIR.glob('*.yaml'):
        for doc in _load(path):
            referenced.update(_iter_configmap_refs(doc))

    untracked = referenced - _tracked_configmap_names()
    assert untracked == set(), (
        'Jobs reference ConfigMaps that no tracked manifest defines: '
        f'{sorted(untracked)}'
    )


def test_corpus_embed_configmap_matches_the_tracked_script() -> None:
    config_map = next(
        doc
        for doc in _load(EMBED_CONFIGMAP_PATH)
        if doc.get('kind') == 'ConfigMap'
    )
    embedded = config_map['data']['corpus_gpu_embed.py']
    assert embedded == EMBED_SCRIPT_PATH.read_text(encoding='utf-8')
