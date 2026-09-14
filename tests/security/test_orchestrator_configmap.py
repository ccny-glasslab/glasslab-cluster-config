"""Drift guard: tracked orchestrator configmap must match live split-model serving.

The live cluster serves the orchestrator's split models on the exo instances at
``.17`` and ``.18`` port ``52417``: Honeydew's reasoning model is served from
``.18`` while Beaker, the structured turns, and the task compiler run on ``.17``.
This test fails when the tracked manifest drifts back to the retired single
endpoint (``:52415``) or loses the per-agent routing keys.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
CONFIGMAP_PATH = KUBE_ROOT / "research-orchestrator" / "10-configmap.yaml"
ENV_EXAMPLE_PATH = (
    REPOSITORY_ROOT / "services" / "research-orchestrator" / ".env.example"
)

PREFIX = "GLASSLAB_ORCHESTRATOR_"
HONEYDEW_REASONING_BASE_URL = "http://192.168.1.18:52417/v1"
BEAKER_BASE_URL = "http://192.168.1.17:52417/v1"
FALLBACK_ENDPOINT_SUFFIX = ":52417/v1"
STALE_PORT = "52415"

PER_AGENT_BASE_URL_KEYS = {
    f"{PREFIX}AGENT_BASE_URL_HONEYDEW": HONEYDEW_REASONING_BASE_URL,
    f"{PREFIX}AGENT_BASE_URL_BEAKER": BEAKER_BASE_URL,
    f"{PREFIX}HONEYDEW_REASONING_AGENT_BASE_URL": HONEYDEW_REASONING_BASE_URL,
    f"{PREFIX}HONEYDEW_STRUCTURED_AGENT_BASE_URL": BEAKER_BASE_URL,
    f"{PREFIX}TASK_COMPILER_AGENT_BASE_URL": BEAKER_BASE_URL,
}

PER_AGENT_MODEL_KEYS = {
    f"{PREFIX}AGENT_MODEL_HONEYDEW": "mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit",
    f"{PREFIX}AGENT_MODEL_BEAKER": "mlx-community/Qwen3-Coder-Next-4bit",
    f"{PREFIX}HONEYDEW_REASONING_AGENT_MODEL": (
        "mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit"
    ),
    f"{PREFIX}HONEYDEW_STRUCTURED_AGENT_MODEL": "mlx-community/Qwen3-Coder-Next-4bit",
    f"{PREFIX}TASK_COMPILER_AGENT_MODEL": "mlx-community/Qwen3-Coder-Next-4bit",
}


def configmap_data() -> dict[str, str]:
    document = next(
        item
        for item in yaml.safe_load_all(CONFIGMAP_PATH.read_text(encoding="utf-8"))
        if item
    )
    return document["data"]


def env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


class OrchestratorConfigmapSplitServingTests(unittest.TestCase):
    def test_configmap_declares_per_agent_base_urls(self):
        data = configmap_data()
        for name, expected in PER_AGENT_BASE_URL_KEYS.items():
            with self.subTest(key=name):
                self.assertIn(name, data)
                self.assertEqual(data[name], expected)

    def test_configmap_declares_per_agent_models(self):
        data = configmap_data()
        for name, expected in PER_AGENT_MODEL_KEYS.items():
            with self.subTest(key=name):
                self.assertIn(name, data)
                self.assertEqual(data[name], expected)

    def test_honeydew_reasoning_points_at_split_serving_host(self):
        self.assertEqual(
            configmap_data()[f"{PREFIX}HONEYDEW_REASONING_AGENT_BASE_URL"],
            HONEYDEW_REASONING_BASE_URL,
        )

    def test_fallback_qwen_base_url_targets_split_serving_port(self):
        value = configmap_data()[f"{PREFIX}QWEN_BASE_URL"]
        self.assertTrue(value.endswith(FALLBACK_ENDPOINT_SUFFIX), value)
        self.assertNotIn(STALE_PORT, value)

    def test_opencode_turn_timeout_stays_1800(self):
        self.assertEqual(
            configmap_data()[f"{PREFIX}OPENCODE_TURN_TIMEOUT_SECONDS"], "1800"
        )

    def test_env_example_mirrors_per_agent_routing(self):
        values = env_example_values()
        for name, expected in {**PER_AGENT_BASE_URL_KEYS, **PER_AGENT_MODEL_KEYS}.items():
            with self.subTest(key=name):
                self.assertIn(name, values)
                self.assertEqual(values[name], expected)
        fallback = values[f"{PREFIX}QWEN_BASE_URL"]
        self.assertTrue(fallback.endswith(FALLBACK_ENDPOINT_SUFFIX), fallback)
        self.assertNotIn(STALE_PORT, fallback)


if __name__ == "__main__":
    unittest.main()
