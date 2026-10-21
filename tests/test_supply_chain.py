"""Keep deploy/CI inputs immutable and require verified dependency artifacts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The only permitted action outside actions/: the SonarCloud scanner, pinned to a commit SHA, and the shared gate guard,
# a reusable workflow pinned to the same full SHA as its guard-ref (asserted below).
ALLOWED_THIRD_PARTY_ACTIONS = {
    "SonarSource/sonarqube-scan-action@d209202bc7d53ff1cc128f7f907dac145c9d6ae9",
    "dflippojr/.github/.github/workflows/gate-guard.yml@882623c67a5c3bab64da2136e6070a5325440b47",
}


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
    assert actions and all(
        re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action) or action in ALLOWED_THIRD_PARTY_ACTIONS
        for action in actions
    )
    assert ci.count("persist-credentials: false") == sum(action.startswith("actions/checkout@") for action in actions)
    assert "ref: 83c6aa8b7a88a973911015a6423975745b43664a" in ci
    assert "guard-ref: 882623c67a5c3bab64da2136e6070a5325440b47" in ci
    assert "pip install -r daemon/requirements.txt" not in ci
    assert "--require-hashes -r requirements-e2e.lock" in ci
    assert "python -m build --no-isolation" in ci


def _jobs(workflow: str) -> dict[str, str]:
    """Split a workflow's `jobs:` section into {job id: job text} (two-space job ids, as this repo writes them)."""
    section = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", section, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_drift_runs_nightly_on_hosted_runners_with_locked_inputs_and_only_the_default_token():
    drift = (ROOT / ".github/workflows/drift.yml").read_text(encoding="utf-8")
    assert re.search(r'^  schedule:\n    - cron: "[^"]+"$', drift, re.M) and "workflow_dispatch:" in drift
    actions = re.findall(r"uses: (\S+)", drift)
    assert actions and all(re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action) for action in actions)
    assert drift.count("persist-credentials: false") == sum(a.startswith("actions/checkout@") for a in actions)
    assert set(re.findall(r"runs-on: (.+)", drift)) <= {"ubuntu-latest", "${{ matrix.os }}"}
    assert "self-hosted" not in drift and "secrets." not in drift
    assert "pip install -r daemon/requirements.txt" not in drift
    assert drift.count("--require-hashes -r requirements-e2e.lock") == 2
    header = drift.split("\njobs:\n", 1)[0]
    assert re.search(r"^permissions:\n  contents: read\n\n", header, re.M)
    jobs = _jobs(drift)
    assert set(jobs) == {"resolve", "daemon", "ui", "report"}
    for job_id, job in jobs.items():
        assert ("issues: write" in job) == (job_id == "report")
        assert "github.token" not in job or job_id == "report"
    # A pull request that changes the workflow runs it, but its report writes nothing.
    assert "github.event_name == 'pull_request' && '--dry-run'" in jobs["report"]


def test_release_publishes_on_version_tags_with_pinned_actions():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'tags: ["v[0-9]+.[0-9]+.[0-9]+"]' in release
    assert "self-hosted" not in release
    actions = re.findall(r"uses: (\S+)", release)
    assert actions and all(
        re.fullmatch(r"(actions/[a-z-]+|pypa/gh-action-pypi-publish)@[a-f0-9]{40}", action) for action in actions)
    assert "actions/attest-build-provenance@" in release
    assert release.count("persist-credentials: false") == sum(a.startswith("actions/checkout@") for a in actions)
    assert '"$TAG" != "v$version"' in release and "harness_hub.__version__" in release
    assert "--require-hashes -r requirements-build.lock" in release
    assert "python -m build --no-isolation" in release
    assert 'scripts/smoke-image.sh "$IMAGE:$VERSION" "$VERSION"' in release
    assert "IMAGE: ghcr.io/dflippojr/agent-harness-hub" in release
    assert 'docker push "$IMAGE:$VERSION"' in release and 'docker push "$IMAGE:latest"' in release
    assert "password" not in release.lower().replace("--password-stdin", "")
    assert "PYPI_API_TOKEN" not in release and "secrets." not in release.replace("secrets.GITHUB_TOKEN", "")


def test_release_refuses_commits_off_main_and_moves_latest_only_forward():
    jobs = _jobs((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    build, ghcr = jobs["build"], jobs["ghcr"]
    assert "fetch-depth: 0" in build
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in build
    assert build.index("--is-ancestor") < build.index("python -m build")
    assert "git tag -l 'v*' --merged origin/main --sort=-v:refname" in build
    assert 'if [[ "$TAG" == "$highest" ]]' in build
    assert "::warning::$TAG is not the highest release tag on main" in build
    assert "LATEST: ${{ needs.build.outputs.latest }}" in ghcr
    latest_push = ghcr.index('docker push "$IMAGE:latest"')
    assert ghcr.rindex('if [[ "$LATEST" == "true" ]]', 0, latest_push) > ghcr.index('docker push "$IMAGE:$VERSION"')


def test_release_docs_pin_the_attestation_signer_and_the_owner_setup():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n### Release\n", 1)[1].split("\n### ", 1)[0]
    verify = re.search(r"gh attestation verify oci://\S+ \\\n(.*\\\n)*.*--deny-self-hosted-runners", section)
    assert verify, "README must show the attestation verify command"
    assert ('--cert-identity "https://github.com/dflippojr/agent-harness-hub/.github/workflows/release.yml'
            '@refs/tags/vX.Y.Z"') in verify.group(0)
    assert "reviewer prompt for this workflow only" in section
    assert "accident guard" in section
    assert "branch ruleset for `main`" in section
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "reviewer prompt for this workflow only" in release and "accident guard" in release


def test_release_attests_the_pushed_image_digest():
    ghcr = _jobs((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["ghcr"]
    assert "subject-name: ${{ env.IMAGE }}" in ghcr
    assert "subject-digest: ${{ steps.push.outputs.digest }}" in ghcr
    assert "push-to-registry: true" in ghcr
    assert ghcr.index("actions/attest-build-provenance@") > ghcr.index('docker push "$IMAGE:$VERSION"')


def test_release_grants_publish_permissions_only_to_the_publishing_jobs():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    header = release.split("\njobs:\n", 1)[0]
    assert re.search(r"^permissions:\n  contents: read\n\n", header, re.M)
    jobs = _jobs(release)
    assert set(jobs) == {"build", "pypi", "ghcr"}
    assert "permissions:" not in jobs["build"] and "docker push" not in jobs["build"]
    assert re.search(r"permissions:\n      id-token: write\n    steps:", jobs["pypi"])
    assert "name: pypi" in jobs["pypi"] and "pypa/gh-action-pypi-publish@" in jobs["pypi"]
    ghcr_permissions = r"permissions:\n      attestations: write\n      id-token: write\n      packages: write\n"
    assert re.search(ghcr_permissions + r"    steps:", jobs["ghcr"])
    assert "name: ghcr" in jobs["ghcr"] and "needs: [build, pypi]" in jobs["ghcr"]
    for job_id, job in jobs.items():
        assert ("id-token" in job) == (job_id in {"pypi", "ghcr"})
        assert ("packages:" in job) == (job_id == "ghcr")
        assert ("attestations:" in job) == (job_id == "ghcr")
        assert ("environment:" in job) == (job_id in {"pypi", "ghcr"})
        assert "python -m build" not in job or job_id == "build"
        assert "docker build" not in job or job_id == "build"


def test_package_is_mit_licensed_and_the_image_carries_the_source():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "agent-harness-hub"
    assert project["license"] == "MIT" and project["license-files"] == ["LICENSE"]
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'org.opencontainers.image.source="https://github.com/dflippojr/agent-harness-hub"' in docker
    assert "COPY pyproject.toml README.md LICENSE " in docker
    assert "!LICENSE" in (ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_ci_and_release_smoke_test_the_image_as_a_non_root_user():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/smoke-image.sh agent-harness-hub:ci" in ci
    smoke = (ROOT / "scripts/smoke-image.sh").read_text(encoding="utf-8")
    assert '--version' in smoke and '--user "$user"' in smoke and "type=bind,src=$state,dst=/hub-state" in smoke
    assert "--network host" in smoke and "/healthz" in smoke


def _code(workflow: str) -> str:
    """The workflow without its comment lines, so a trigger or key named in a comment doesn't count."""
    return "\n".join(line for line in workflow.splitlines() if not line.lstrip().startswith("#")) + "\n"


def test_trusted_gate_guard_runs_from_the_default_branch_and_runs_no_pull_request_code():
    # ci.yml's gate-guard job runs on pull_request, so a pull request supplies its own judge: it can replace that job
    # with a green no-op. Under pull_request_target this caller is read from main, and the called workflow only
    # fetches the pull request's commits as git objects; its verdict is the gate-guard check the ruleset requires.
    target = _code((ROOT / ".github/workflows/gate-guard-target.yml").read_text(encoding="utf-8"))
    header = target.split("\njobs:\n", 1)[0]
    assert re.search(r"^on:\n  pull_request_target:\n    types: \[opened, synchronize, reopened, edited, labeled, "
                     r"unlabeled, closed\]\n\n", header, re.M)
    assert "pull_request:" not in target and "workflow_dispatch" not in target
    assert re.search(r"^permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n  checks: write\n\n",
                     header, re.M)
    assert "group: gate-guard-target-${{ github.event.pull_request.head.sha }}" in header
    assert "cancel-in-progress: true" in header
    jobs = _jobs(target)
    assert set(jobs) == {"judge"}
    sha = "882623c67a5c3bab64da2136e6070a5325440b47"
    assert f"    uses: dflippojr/.github/.github/workflows/gate-guard-target.yml@{sha}\n" in jobs["judge"]
    assert f"      guard-ref: {sha}\n" in jobs["judge"]
    for unsafe in ("steps:", "run:", "actions/checkout", "secrets", "self-hosted", "github.event.pull_request.title",
                   "github.head_ref"):
        assert unsafe not in jobs["judge"], unsafe
    # One reviewed script judges both checks.
    assert f"guard-ref: {sha}" in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_review_caller_grants_every_permission_review_v3_requests():
    # A called workflow can't hold more than its caller grants. review-v3 reads the issues a pull request closes, the
    # spec it reviews against, so it needs issues: read on top of what review-v2 needed.
    review = _code((ROOT / ".github/workflows/review.yml").read_text(encoding="utf-8"))
    assert "uses: dflippojr/agent-harness/.github/workflows/review.yml@review-v3\n" in review
    assert "tooling_ref: review-v3\n" in review
    header = review.split("\njobs:\n", 1)[0]
    permissions = re.search(r"^permissions:\n((?:  .+\n)+)", header, re.M)
    assert permissions, header
    assert set(permissions.group(1).split("\n")) - {""} == {
        "  contents: read", "  pull-requests: write", "  checks: write", "  issues: read"}


def test_ci_gate_needs_every_job_but_sonar_and_main_runs_are_never_cancelled():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = _jobs(ci)
    gate = jobs["gate"]
    needs = re.search(r"^    needs: \[([^\]]*)\]$", gate, re.M)
    assert needs, gate
    # A new job joins the gate, or the ruleset that requires only `gate` would not cover it.
    assert {job.strip() for job in needs.group(1).split(",")} == set(jobs) - {"gate", "sonar"}
    assert "    if: always()\n" in gate
    assert 'select(.value.result != "success")' in gate and "exit 1" in gate
    assert "group: ci-${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in ci
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in ci
    assert re.search(r"python -m pytest .*--cov-fail-under=92\b", jobs["test"])
