"""Per-agent model routing characterization tests (issue #319).

The per-agent model override feature (#319) was merged behavior-neutral with
no test coverage. These tests lock the routing contract: each agent's turns
run against its own model on the shared endpoint when the per-agent override
is set, otherwise the shared effective model applies to both agents.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import httpx
import yaml

from app.config import (
    CONTROL_PLANE_SECRET_FIELDS,
    Settings,
    read_secret_file,
    secret_env_name,
)
from app.hermes_runtime import HermesProcessRuntime, _HermesHandle
from app.opencode_runtime import OpenCodeProcessRuntime
from app.schemas import AgentName, TurnKind


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def _handle(tmp_path: Path) -> _HermesHandle:
    workspace = tmp_path / 'honeydew-worktree'
    workspace.mkdir()
    return _HermesHandle(
        runtime_id='hermes-honeydew-test',
        run_id='run-test',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        base_url='http://hermes.test',
        api_key='test-api-key',
        process=FakeProcess(),  # type: ignore[arg-type]
        log_handle=(tmp_path / 'hermes.log').open('a'),
    )


def _read_opencode_config(workspace: Path, agent: AgentName) -> dict:
    return json.loads(
        (
            workspace.parent
            / 'runtime'
            / agent.value
            / 'config'
            / 'opencode'
            / 'opencode.json'
        ).read_text()
    )


def _write_hermes_config(
    settings: Settings, workspace: Path, agent: AgentName
) -> dict:
    runtime = HermesProcessRuntime(settings)
    hermes_home = runtime._write_runtime_config(
        agent=agent,
        workspace=workspace,
        port=4310,
    )
    return yaml.safe_load((hermes_home / 'config.yaml').read_text())


def test_honeydew_uses_agent_model_honeydew_override() -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3.6-27B-4bit'
    )


def test_beaker_uses_agent_model_beaker_override() -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_agent_model_falls_back_to_agent_model_name() -> None:
    settings = Settings(agent_model_name='mlx-community/Shared-Model-4bit')
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Shared-Model-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Shared-Model-4bit'
    )


def test_agent_model_falls_back_to_qwen_model_name_when_unset() -> None:
    settings = Settings(
        agent_model_name=None,
        qwen_model_name='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_per_agent_override_wins_over_shared_agent_model_name() -> None:
    settings = Settings(
        agent_model_name='mlx-community/Shared-Model-4bit',
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3.6-27B-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_opencode_runtime_config_passes_per_agent_model(tmp_path: Path) -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    runtime = OpenCodeProcessRuntime(settings)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.HONEYDEW)
    assert config['model'] == 'exo/mlx-community/Qwen3.6-27B-4bit'
    runtime._write_runtime_config(
        run_id='run-2',
        agent=AgentName.BEAKER,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.BEAKER)
    assert config['model'] == 'exo/mlx-community/Qwen3-Coder-Next-4bit'


def test_hermes_runtime_config_passes_per_agent_model(tmp_path: Path) -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    honeydew_config = _write_hermes_config(
        settings, workspace, AgentName.HONEYDEW
    )
    assert honeydew_config['model']['default'] == (
        'mlx-community/Qwen3.6-27B-4bit'
    )
    beaker_config = _write_hermes_config(settings, workspace, AgentName.BEAKER)
    assert beaker_config['model']['default'] == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_hermes_turn_payload_passes_per_agent_model(tmp_path: Path) -> None:
    submitted_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path == '/v1/runs':
            submitted_models.append(json.loads(request.content)['model'])
            return httpx.Response(200, json={'run_id': 'hermes-run-1'})
        if request.method == 'GET' and request.url.path == (
            '/v1/runs/hermes-run-1'
        ):
            return httpx.Response(
                200,
                json={
                    'status': 'completed',
                    'output': json.dumps(
                        {
                            'kind': 'protocol_draft',
                            'summary': 'Drafted the protocol.',
                            'produced_files': [],
                        }
                    ),
                },
            )
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
        hermes_poll_interval_seconds=0,
        hermes_structured_repair_attempts=0,
    )
    runtime = HermesProcessRuntime(
        settings,
        transport=httpx.MockTransport(handler),
    )
    handle = _handle(tmp_path)
    runtime._start_process = lambda **_kwargs: handle  # type: ignore[method-assign]

    runtime.run_turn(
        run_id='run-test',
        agent=AgentName.HONEYDEW,
        workspace=handle.workspace,
        session_id='glasslab-honeydew-run-test',
        prompt='Draft program.md.',
    )

    assert submitted_models == ['mlx-community/Qwen3.6-27B-4bit']

def test_base_url_for_honeydew_uses_override() -> None:
    settings = Settings(
        agent_base_url_honeydew='http://192.168.1.18:52416/v1',
        agent_base_url_beaker='http://192.168.1.17:52416/v1',
    )
    assert settings.base_url_for(AgentName.HONEYDEW) == (
        'http://192.168.1.18:52416/v1'
    )


def test_base_url_for_beaker_uses_override() -> None:
    settings = Settings(
        agent_base_url_honeydew='http://192.168.1.18:52416/v1',
        agent_base_url_beaker='http://192.168.1.17:52416/v1',
    )
    assert settings.base_url_for(AgentName.BEAKER) == (
        'http://192.168.1.17:52416/v1'
    )


def test_base_url_falls_back_to_shared_qwen_base_url() -> None:
    settings = Settings(qwen_base_url='http://192.168.1.17:52415/v1')
    assert settings.base_url_for(AgentName.HONEYDEW) == (
        'http://192.168.1.17:52415/v1'
    )
    assert settings.base_url_for(AgentName.BEAKER) == (
        'http://192.168.1.17:52415/v1'
    )


def test_per_agent_base_url_override_wins_over_shared() -> None:
    settings = Settings(
        qwen_base_url='http://192.168.1.17:52415/v1',
        agent_base_url_honeydew='http://192.168.1.18:52416/v1',
        agent_base_url_beaker='http://192.168.1.17:52416/v1',
    )
    assert settings.base_url_for(AgentName.HONEYDEW) == (
        'http://192.168.1.18:52416/v1'
    )
    assert settings.base_url_for(AgentName.BEAKER) == (
        'http://192.168.1.17:52416/v1'
    )


def test_agent_model_max_output_tokens_default_is_high_for_thinking() -> None:
    settings = Settings()
    assert settings.agent_model_max_output_tokens == 16384


def test_turn_timeouts_default_to_3600_for_thinking_overhead() -> None:
    settings = Settings()
    assert settings.opencode_turn_timeout_seconds == 3600.0
    assert settings.hermes_turn_timeout_seconds == 2400.0


def test_hermes_max_iterations_default_to_80_for_thinking() -> None:
    settings = Settings()
    assert settings.hermes_max_iterations == 80


def test_hermes_turn_payload_uses_model_override(tmp_path: Path) -> None:
    submitted_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path == '/v1/runs':
            submitted_models.append(json.loads(request.content)['model'])
            return httpx.Response(200, json={'run_id': 'hermes-run-1'})
        if request.method == 'GET' and request.url.path == (
            '/v1/runs/hermes-run-1'
        ):
            return httpx.Response(
                200,
                json={
                    'status': 'completed',
                    'output': json.dumps(
                        {
                            'kind': 'protocol_draft',
                            'summary': 'Drafted.',
                            'produced_files': [],
                        }
                    ),
                },
            )
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    settings = Settings(
        agent_model_honeydew='mlx-community/Thinking-4bit',
        task_compiler_agent_model='mlx-community/Coder-Next-4bit',
        hermes_poll_interval_seconds=0,
        hermes_structured_repair_attempts=0,
    )
    runtime = HermesProcessRuntime(
        settings,
        transport=httpx.MockTransport(handler),
    )
    handle = _handle(tmp_path)
    runtime._start_process = lambda **_kwargs: handle  # type: ignore[method-assign]

    runtime.run_turn(
        run_id='run-test',
        agent=AgentName.HONEYDEW,
        workspace=handle.workspace,
        session_id='glasslab-honeydew-run-test',
        prompt='Compile the task spec.',
        model_override='mlx-community/Coder-Next-4bit',
    )

    assert submitted_models == ['mlx-community/Coder-Next-4bit']


def test_task_compiler_model_defaults_to_effective_agent_model() -> None:
    settings = Settings(qwen_model_name='mlx-community/Shared-4bit')
    assert settings.task_compiler_model() == 'mlx-community/Shared-4bit'


def test_task_compiler_model_uses_override_when_set() -> None:
    settings = Settings(
        task_compiler_agent_model='mlx-community/Coder-Next-4bit',
    )
    assert settings.task_compiler_model() == 'mlx-community/Coder-Next-4bit'


def test_honeydew_model_for_verification_uses_reasoning_model() -> None:
    settings = Settings(
        honeydew_structured_agent_model='mlx-community/Coder-Next-4bit',
        honeydew_reasoning_agent_model='mlx-community/Thinking-4bit',
    )
    model, _ = settings.honeydew_model_for(TurnKind.VERIFICATION)
    assert model == 'mlx-community/Thinking-4bit'


def test_honeydew_model_for_protocol_draft_uses_structured_model() -> None:
    settings = Settings(
        honeydew_structured_agent_model='mlx-community/Coder-Next-4bit',
        honeydew_reasoning_agent_model='mlx-community/Thinking-4bit',
    )
    model, _ = settings.honeydew_model_for(TurnKind.PROTOCOL_DRAFT)
    assert model == 'mlx-community/Coder-Next-4bit'


def test_honeydew_structured_base_url_override() -> None:
    settings = Settings(
        honeydew_structured_agent_base_url='http://192.168.1.17:52416/v1',
    )
    assert settings.honeydew_structured_base_url() == (
        'http://192.168.1.17:52416/v1'
    )


def test_operator_auth_fails_closed_by_default() -> None:
    # Security C3: any deployment that omits the env var must reject
    # unauthenticated state-changing requests, not allow them through.
    assert Settings().require_operator_auth is True


# --- Control-plane secrets from read-only files (issue #597) ---------------
#
# The same-UID OpenCode child shares the orchestrator's PID namespace and can
# read /proc/1/environ. Control-plane secrets therefore come from files under
# GLASSLAB_ORCHESTRATOR_SECRETS_DIR mounted read-only, with the environment
# kept only as the local/test fallback.


def _write_secret(secrets_dir: Path, field_name: str, value: str) -> None:
    (secrets_dir / secret_env_name(field_name)).write_text(
        value + '\n', encoding='utf-8'
    )


def test_control_plane_secret_fields_are_exactly_the_documented_set() -> None:
    assert CONTROL_PLANE_SECRET_FIELDS == (
        'operator_api_token',
        'link_signing_secret',
        'discord_bot_token',
        'discord_webhook_url',
        'store_postgres_dsn',
    )


def test_secrets_dir_defaults_to_none() -> None:
    assert Settings().secrets_dir is None


def test_secret_files_take_precedence_over_environment(
    tmp_path: Path, monkeypatch
) -> None:
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    _write_secret(secrets_dir, 'operator_api_token', 'file-operator')
    _write_secret(secrets_dir, 'link_signing_secret', 'file-link')
    _write_secret(secrets_dir, 'discord_bot_token', 'file-discord')
    _write_secret(secrets_dir, 'discord_webhook_url', 'file-webhook')
    _write_secret(secrets_dir, 'store_postgres_dsn', 'postgresql://file/db')
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN', 'env-operator'
    )
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_LINK_SIGNING_SECRET', 'env-link'
    )
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN', 'env-discord'
    )
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL', 'env-webhook'
    )
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN', 'postgresql://env/db'
    )

    settings = Settings(
        secrets_dir=str(secrets_dir),
        store_backend='postgres',
    )

    assert settings.operator_api_token == 'file-operator'
    assert settings.link_signing_secret is not None
    assert settings.link_signing_secret.get_secret_value() == 'file-link'
    assert settings.discord_bot_token == 'file-discord'
    assert settings.discord_webhook_url == 'file-webhook'
    assert settings.store_postgres_dsn == 'postgresql://file/db'


def test_missing_secret_files_fall_back_to_environment(
    tmp_path: Path, monkeypatch
) -> None:
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN', 'env-operator'
    )

    settings = Settings(secrets_dir=str(secrets_dir))

    assert settings.operator_api_token == 'env-operator'


def test_empty_secret_file_falls_back_to_environment(
    tmp_path: Path, monkeypatch
) -> None:
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    # kubectl --from-file of an empty value writes a single newline.
    (secrets_dir / secret_env_name('discord_bot_token')).write_text(
        '\n', encoding='utf-8'
    )
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN', 'env-discord'
    )

    settings = Settings(secrets_dir=str(secrets_dir))

    assert settings.discord_bot_token == 'env-discord'


def test_postgres_dsn_file_satisfies_postgres_backend(tmp_path: Path) -> None:
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    _write_secret(secrets_dir, 'store_postgres_dsn', 'postgresql://file/db')

    settings = Settings(
        secrets_dir=str(secrets_dir),
        store_backend='postgres',
    )

    assert settings.store_postgres_dsn == 'postgresql://file/db'


def test_read_secret_file_returns_none_without_directory(tmp_path: Path) -> None:
    assert read_secret_file(None, 'operator_api_token') is None
    assert read_secret_file(str(tmp_path), 'operator_api_token') is None


def test_secret_env_name_is_prefixed_field_name() -> None:
    assert (
        secret_env_name('store_postgres_dsn')
        == 'GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN'
    )


# --- Default-deny bash allowlist (issue #597) ------------------------------
#
# OpenCode evaluates bash permission rules last-match-wins over the
# sort_keys=True config order, so the test replica sorts the rule keys and
# takes the last glob match (the same semantics the runtime documents).

SAFE_COMMANDS = (
    'pwd',
    'ls -la',
    'cat report.md',
    'head -n 5 report.md',
    'tail -f run.log',
    'wc -l report.md',
    'grep -n result report.md',
    'rg result .',
    'sort report.txt',
    'uniq report.txt',
    'diff a.txt b.txt',
    'tree src',
    'git status',
    'git diff HEAD~1',
    'git log --oneline',
    'git add -A',
    'git commit -m "implement candidate"',
    'git show HEAD',
    'git rev-parse HEAD',
    'pytest -q tests',
    'python3 -m pytest -q tests',
    'jq . data.json',
    'mkdir -p out',
    'cp a.txt b.txt',
    'mv a.txt b.txt',
    'rm -f scratch.txt',
    'touch marker',
    'echo hello',
)

# Legitimate absolute-path reads inside the run directory must stay allowed:
# the deny is scoped to /proc and the control-plane secret mount, not to every
# absolute path.
SAFE_ABSOLUTE_PATH_COMMANDS = (
    'cat /mnt/artifacts/research-orchestrator/runs/run-1/reports/report.md',
    'grep -n result /mnt/artifacts/research-orchestrator/runs/run-1/evidence.json',
)

DENIED_COMMANDS = (
    'python3 -c "import os"',
    "node -e 'process.exit()'",
    "sh -c 'id'",
    "bash -c 'id'",
    "perl -e 'print 1'",
    "ruby -e 'puts 1'",
    'env',
    'env | grep TOKEN',
    'printenv',
    'curl http://example.invalid',
    'wget http://example.invalid',
    'nc 10.0.0.1 4444',
    'ncat 10.0.0.1 4444',
    'socat - TCP:10.0.0.1:4444',
    'ssh user@host',
    'scp file user@host:/tmp',
    'kubectl get pods',
    'docker ps',
    'podman ps',
    'cat < /dev/tcp/10.0.0.1/443',
    'cat /dev/tcp/10.0.0.1/443',
    "sh -c 'cat < /dev/tcp/10.0.0.1/443'",
)

# Issue #597 F2: each entry is a real bypass of the pre-fix allowlist. Every
# one must classify deny.
BYPASS_COMMANDS = (
    'awk \'BEGIN{system("id")}\'',
    'find . -exec sh -c id ;',
    "sed -e '1e id'",
    'xargs sh -c id',
    'make -f evil.mk',
    "git -c core.pager='sh -c id' log",
    "git config alias.x '!sh -c id'",
    'tar --checkpoint-action=exec=sh bundle.tgz',
    'unzip -o bundle.zip',
    'tee /tmp/x',
    'cat /etc/glasslab-*/GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN',
    'cat /etc/glasslab-secrets/GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN',
    'cat "/etc/glasslab-secrets/GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN"',
    'cat /proc/1/environ',
    'cat /proc/self/environ',
    'grep TOKEN /proc/1/environ',
    'head -n 1 /proc/1/environ',
    'tail /proc/self/environ',
    'jq . /etc/glasslab-secrets/x',
    'rg x /etc/glasslab-secrets',
    'wc -c /etc/glasslab-secrets/x',
    'head /etc/glasslab-*/GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN',
)


def _bash_rules(agent: AgentName, enabled: bool) -> dict[str, str]:
    runtime = OpenCodeProcessRuntime(
        Settings(agent_bash_allowlist_enabled=enabled)
    )
    return runtime._permissions(agent)['bash']


def _classify(rules: dict[str, str], command: str) -> str:
    verdict = 'deny'
    for pattern in sorted(rules):
        if fnmatch.fnmatchcase(command, pattern):
            verdict = rules[pattern]
    return verdict


def test_bash_allowlist_disabled_keeps_legacy_denylist() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=False)
    assert rules['*'] == 'allow'
    assert rules['kubectl *'] == 'deny'
    assert rules['curl *'] == 'deny'
    assert 'rm *' not in rules


def test_bash_allowlist_enabled_is_default_deny() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    assert rules['*'] == 'deny'


def test_bash_allowlist_allows_every_documented_safe_command() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    for command in SAFE_COMMANDS:
        assert _classify(rules, command) == 'allow', command


def test_bash_allowlist_allows_run_directory_absolute_paths() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    for command in SAFE_ABSOLUTE_PATH_COMMANDS:
        assert _classify(rules, command) == 'allow', command


def test_bash_allowlist_denies_interpreters_shells_and_egress() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    for command in DENIED_COMMANDS:
        assert _classify(rules, command) == 'deny', command


def test_bash_allowlist_denies_every_reported_bypass() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    for command in BYPASS_COMMANDS:
        assert _classify(rules, command) == 'deny', command


def test_bash_allowlist_denies_secret_reads_for_every_reader_command() -> None:
    rules = _bash_rules(AgentName.BEAKER, enabled=True)
    for reader in (
        'cat',
        'head',
        'tail',
        'wc',
        'grep',
        'rg',
        'sort',
        'uniq',
        'diff',
        'jq',
        'tree',
    ):
        secret_read = (
            f'{reader} /etc/glasslab-secrets/'
            'GLASSLAB_ORCHESTRATOR_LINK_SIGNING_SECRET'
        )
        assert _classify(rules, secret_read) == 'deny', secret_read
        proc_read = f'{reader} /proc/1/environ'
        assert _classify(rules, proc_read) == 'deny', proc_read


def test_bash_allowlist_denies_git_push_for_both_agents() -> None:
    for agent in (AgentName.HONEYDEW, AgentName.BEAKER):
        rules = _bash_rules(agent, enabled=True)
        assert _classify(rules, 'git push origin main') == 'deny'


def test_bash_allowlist_honeydew_cannot_mutate_repository() -> None:
    honeydew = _bash_rules(AgentName.HONEYDEW, enabled=True)
    assert _classify(honeydew, 'git commit -m x') == 'deny'
    assert _classify(honeydew, 'git checkout main') == 'deny'
    assert _classify(honeydew, 'git switch main') == 'deny'

    beaker = _bash_rules(AgentName.BEAKER, enabled=True)
    assert _classify(beaker, 'git commit -m x') == 'allow'
    assert _classify(beaker, 'git status') == 'allow'


def test_bash_allowlist_is_on_by_default() -> None:
    # Fail closed: an unconfigured deployment gets the default-deny allowlist,
    # not the legacy denylist that permits interpreters.
    assert Settings().agent_bash_allowlist_enabled is True


def test_secrets_dir_from_environment_enables_file_loading(
    tmp_path: Path, monkeypatch
) -> None:
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    _write_secret(secrets_dir, 'operator_api_token', 'env-dir-operator')
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_SECRETS_DIR', str(secrets_dir)
    )
    monkeypatch.delenv(
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN', raising=False
    )

    settings = Settings()

    assert settings.secrets_dir == str(secrets_dir)
    assert settings.operator_api_token == 'env-dir-operator'
