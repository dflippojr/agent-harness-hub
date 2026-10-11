"""Keep deploy/CI inputs immutable and require verified dependency artifacts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_locked_dependencies_have_versions_and_hashes():
    for path in ROOT.glob("requirements*.lock"):
        entries = re.split(r"\n(?=[a-zA-Z])", path.read_text(encoding="utf-8"))
        packages = [entry for entry in entries if entry and entry[0].isalpha()]
        assert packages, path
        for entry in packages:
            assert re.match(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", entry), (path, entry)
            assert re.search(r"--hash=sha256:[a-f0-9]{64}", entry), (path, entry)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = (project["project"]["dependencies"] + project["project"]["optional-dependencies"]["dev"]
                    + project["build-system"]["requires"])
    assert all("==" in requirement for requirement in requirements)


def test_docker_and_ci_install_only_locked_external_inputs():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3.12-slim@sha256:[a-f0-9]{64}$", docker, re.M)
    assert "pip install --require-hashes -r requirements.lock -r requirements-build.lock" in docker
    assert "pip install --no-deps --no-build-isolation ." in docker
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!requirements.lock" in ignore and "!requirements-build.lock" in ignore
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    actions = re.findall(r"uses: (\S+)", ci)
    assert actions and all(re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action) for action in actions)
    assert ci.count("persist-credentials: false") == sum(action.startswith("actions/checkout@") for action in actions)
    assert "ref: aa5a9047dbe4bcba6b7ec3348d0cd529c00ebe2b" in ci
    assert "pip install -r daemon/requirements.txt" not in ci
    assert "--require-hashes -r requirements-e2e.lock" in ci
    assert "python -m build --no-isolation" in ci
