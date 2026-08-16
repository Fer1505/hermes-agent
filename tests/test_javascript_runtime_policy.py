from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_NODE_RANGE = ">=22.22.0"


def _load_json(path: str) -> dict:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def test_first_party_node_engines_exclude_node_20() -> None:
    root_package = _load_json("package.json")
    desktop_package = _load_json("apps/desktop/package.json")
    package_lock = _load_json("package-lock.json")

    assert root_package["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert desktop_package["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert package_lock["packages"][""]["engines"]["node"] == SUPPORTED_NODE_RANGE
    assert package_lock["packages"]["apps/desktop"]["engines"]["node"] == SUPPORTED_NODE_RANGE


def test_javascript_ci_workflow_uses_supported_node_and_root_gate() -> None:
    workflow = (REPO_ROOT / ".github/workflows/javascript-ci.yml").read_text(encoding="utf-8")

    assert "node-version: 22" in workflow
    assert "node-version: 20" not in workflow
    assert "npm ci" in workflow
    assert "npm run ci:js" in workflow


def test_docs_workflows_no_longer_use_node_20() -> None:
    for path in (
        ".github/workflows/deploy-site.yml",
        ".github/workflows/docs-site-checks.yml",
    ):
        workflow = (REPO_ROOT / path).read_text(encoding="utf-8")

        assert "node-version: 26" in workflow
        assert "node-version: 20" not in workflow
