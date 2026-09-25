"""T15: CI/locks как проверяемый контракт (CI01-CI06 механика на файлах).

Эти тесты держат инварианты, которые иначе тихо деградируют:
декоративный lock рядом с ranges-установкой, publish в обход gate,
required-check с вымышленным именем job, .env в build-контексте.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
LOCK = (ROOT / "uv.lock").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")


def triggers(workflow: dict) -> dict:
    """YAML 1.1 превращает ключ `on:` в True — читаем оба варианта."""
    return workflow.get("on") or workflow.get(True) or {}


def _normalize(name: str) -> str:
    # PEP 503: дефисы/подчёркивания/точки взаимозаменяемы в именах дистрибуций
    return re.sub(r"[-_.]+", "-", name).lower()


def test_uv_lock_covers_every_pyproject_dependency() -> None:
    """CI01: lock реальный — в нём есть каждый runtime- и test-пакет."""
    # только блоки dependencies = [...] (project + optional test), без markers
    blocks = re.findall(r"dependencies = \[(.*?)\]", PYPROJECT, re.DOTALL)
    specs = re.findall(r'"([A-Za-z0-9_.\-]+)\s*(?:[<>=!~\[;\s]|")', "\n".join(blocks))
    locked = {_normalize(m) for m in re.findall(r'^name = "([^"]+)"$', LOCK, re.MULTILINE)}
    missing = {s for s in specs if _normalize(s) not in locked}
    assert not missing, f"uv.lock не покрывает: {missing}"


def test_dockerfile_installs_frozen_lock_not_ranges() -> None:
    """п.1: никакого pip install из диапазонов; только uv sync --frozen."""
    assert "uv sync --frozen" in DOCKERFILE
    assert not re.search(r"\bpip install\b(?!.*uv)", DOCKERFILE), "Dockerfile снова ставит ranges"
    assert "COPY pyproject.toml uv.lock ./" in DOCKERFILE
    assert "USER 10001:10001" in DOCKERFILE  # п.9 rootless-дефолт


def test_dockerignore_excludes_secrets_and_local_env() -> None:
    """CI06/p.7: .env не попадает в build-контекст."""
    lines = {l.strip() for l in DOCKERIGNORE.splitlines()}
    assert ".env" in lines and ".env.*" in lines
    assert ".git" in lines


def test_required_ci_lanes_use_frozen_sync_and_no_continue_on_error() -> None:
    """п.4/CI02: обязательные lanes — frozen-установка и не continue-on-error."""
    test_job = CI["jobs"]["test"]
    assert test_job["strategy"]["fail-fast"] is False
    for m in test_job["strategy"]["matrix"]["include"]:
        if m["lane"] in ("fast",):
            assert not m.get("allow_failure"), f"fast-lane {m['python-version']} стал необязательным"
    installs = " ".join(s.get("run", "") for s in test_job["steps"])
    assert "uv sync --frozen" in installs
    integration = CI["jobs"]["integration"]
    assert "continue-on-error" not in integration
    assert "-m integration" in yaml.dump(integration)
    assert "docker-compose.test.yml" in yaml.dump(integration)
    # фиксированное имя job для branch protection (п.6: реальные названия)
    assert CI["jobs"]["required-test"]["name"] == "test"


def test_release_publish_is_ci_gated_and_manifest_after_full_matrix() -> None:
    """п.5/CI03-CI04: publish только после зелёного CI; vX и git-тег — после всей матрицы."""
    assert "workflow_dispatch" not in triggers(RELEASE), "release нельзя запускать вручную в обход CI"
    assert triggers(RELEASE)["workflow_run"]["workflows"] == ["CI"]
    publish_steps = yaml.dump(RELEASE["jobs"]["publish"])
    assert ":v${{" not in publish_steps, "matrix не должен пушить vX до полной матрицы"
    assert "${{ github.event.workflow_run.head_sha }}" in publish_steps
    assert "promote" in RELEASE["jobs"]
    assert RELEASE["jobs"]["promote"]["needs"] == ["version", "publish"]
    assert RELEASE["jobs"]["tag"]["needs"] == ["version", "publish", "promote", "smoke"]
    assert RELEASE["jobs"]["smoke"]["needs"] == ["publish", "promote"]
    smoke_script = yaml.dump(RELEASE["jobs"]["smoke"])
    assert "imagetools inspect" in smoke_script and "@" in smoke_script  # CI07: по digest


def test_release_cleanup_removes_partial_candidates() -> None:
    cleanup = RELEASE["jobs"]["cleanup-publish"]
    assert cleanup["needs"] == ["version", "publish", "promote", "smoke"]
    cond = cleanup["if"]
    assert "publish.result == 'failure'" in cond and "smoke.result == 'failure'" in cond
