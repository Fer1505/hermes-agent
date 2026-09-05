from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_NODE_RANGE = "^22.22.0 || ^24.11.0 || >=26.0.0"


def _load_json(path: str) -> dict:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def _load_workflow(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"workflow must be a mapping: {path}"
    return loaded


def _workflow_steps(workflow: dict):
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_defaults = job.get("defaults") or {}
        job_workdir = (job_defaults.get("run") or {}).get("working-directory", ".")
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            yield (
                str(job_name),
                step,
                str(step.get("working-directory") or job_workdir),
            )


_NPM_RUN_RE = re.compile(
    r"(?:^|(?:&&|\|\||;|\n)\s*)npm\s+run\s+"
    r"(?:(?:--prefix|-C)\s+(?P<prefix>\S+)\s+)?"
    r"(?P<script>[^\s\\]+)"
)


def _literal_npm_run_contracts(path: Path):
    workflow = _load_workflow(path)
    for job_name, step, workdir in _workflow_steps(workflow):
        if "run" not in step:
            continue
        command = str(step["run"])
        for match in _NPM_RUN_RE.finditer(command):
            prefix = match.group("prefix")
            script = match.group("script")
            if "${{" in script or (prefix and "${{" in prefix):
                # js-tests discovers the package and script from each package's
                # own scripts map at runtime. It is not a static relationship.
                assert path.name == "js-tests.yml"
                continue
            package_dir = Path(prefix or workdir)
            manifest_path = REPO_ROOT / package_dir / "package.json"
            yield path, job_name, command, manifest_path, script


def test_first_party_node_engines_exclude_node_20() -> None:
    root_package = _load_json("package.json")
    desktop_package = _load_json("apps/desktop/package.json")
    package_lock = _load_json("package-lock.json")

    assert root_package["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert desktop_package["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert package_lock["packages"][""]["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert package_lock["packages"]["apps/desktop"]["engines"]["node"] == SUPPORTED_NODE_RANGE


def test_javascript_ci_workflow_uses_supported_node_and_root_gate() -> None:
    workflow = _load_workflow(REPO_ROOT / ".github/workflows/javascript-ci.yml")
    steps = list(_workflow_steps(workflow))
    node_steps = [
        step for _job, step, _workdir in steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert len(node_steps) == 1
    assert int(node_steps[0]["with"]["node-version"]) >= 22

    commands = [
        str(step["run"]).strip()
        for _job, step, _workdir in steps
        if "run" in step
    ]
    assert "npm ci" in commands
    gate = next(command for command in commands if command.startswith("npm run "))
    argv = shlex.split(gate)
    assert argv[:2] == ["npm", "run"]

    root_scripts = _load_json("package.json")["scripts"]
    assert argv[2] in root_scripts
    assert root_scripts[argv[2]] == "npm run --ws check"


def test_docs_workflows_no_longer_use_node_20() -> None:
    for path in (
        ".github/workflows/deploy-site.yml",
        ".github/workflows/docs-site-checks.yml",
    ):
        workflow = _load_workflow(REPO_ROOT / path)
        versions = [
            int(step["with"]["node-version"])
            for _job, step, _workdir in _workflow_steps(workflow)
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        ]
        assert versions
        assert min(versions) >= 22


def test_static_workflow_npm_run_commands_resolve_to_package_scripts() -> None:
    """Every literal workflow npm script must exist in its owning manifest."""
    contracts = []
    for path in sorted((REPO_ROOT / ".github/workflows").glob("*.y*ml")):
        contracts.extend(_literal_npm_run_contracts(path))

    assert contracts
    errors = []
    for path, job_name, command, manifest_path, script in contracts:
        if not manifest_path.is_file():
            errors.append(
                f"{path.name}:{job_name}: missing {manifest_path.relative_to(REPO_ROOT)} "
                f"for {command!r}"
            )
            continue
        scripts = json.loads(manifest_path.read_text(encoding="utf-8")).get(
            "scripts", {}
        )
        if script not in scripts:
            errors.append(
                f"{path.name}:{job_name}: {script!r} absent from "
                f"{manifest_path.relative_to(REPO_ROOT)}"
            )
    assert not errors, "\n".join(errors)
