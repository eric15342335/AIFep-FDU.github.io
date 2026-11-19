"""Values that several files must agree on. Each file keeps the value its own tool needs; these tests
make sure the copies never drift apart."""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import tableqa
from agent import GATEWAY
from tableqa.agent import NETWORK

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_host_reaches_the_gateway_on_the_published_port():
    litellm = load_yaml("agent/compose.yaml")["services"]["litellm"]
    command = litellm["command"]
    port = command[command.index("--port") + 1]
    assert f"127.0.0.1:{urlsplit(GATEWAY).port}:{port}" in litellm["ports"]
    probe = re.search(r"http://127\.0\.0\.1:(\d+)/", " ".join(litellm["healthcheck"]["test"]))
    assert probe and probe.group(1) == port


def test_sandbox_reaches_the_gateway_by_service_name_and_port():
    compose = load_yaml("agent/compose.yaml")
    command = compose["services"]["litellm"]["command"]
    models = json.loads((ROOT / "agent/pi/models.json").read_text(encoding="utf-8"))
    base = urlsplit(models["providers"]["local"]["baseUrl"])
    assert base.hostname == "litellm" and str(base.port) == command[command.index("--port") + 1]
    assert compose["networks"]["sandbox"]["name"] == NETWORK
    assert "sandbox" in compose["services"]["litellm"]["networks"]


def test_gateway_gets_the_model_name_from_the_env_file():
    environment = load_yaml("agent/compose.yaml")["services"]["litellm"]["environment"]
    assert environment["AGENT_MODEL"].startswith("${AGENT_MODEL:?")


def test_shared_dependency_pins_match_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = dict(re.fullmatch(r"([\w.-]+)==(\S+)", d).groups() for d in project["dependencies"])  # type: ignore[union-attr]
    pins = {name.lower(): version for name, version in pins.items()}
    shared = {"openpyxl", "pillow", "pymupdf"}  # the baseline's packages, also used inside the sandbox
    for name in ("requirements.txt", "agent/sandbox-requirements.txt"):
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        found = dict(m.groups() for m in (re.fullmatch(r"([\w.-]+)==(\S+)", x.strip()) for x in lines) if m)
        found = {package.lower(): version for package, version in found.items()}
        assert {package: found.get(package) for package in shared} == {p: pins[p] for p in shared}, name


def test_package_version_matches_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert tableqa.__version__ == project["version"]
