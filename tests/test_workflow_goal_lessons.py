from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_lesson(name: str, script: Path):
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_lesson(script: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=script.parent,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def test_workflow_runtime_resumes_from_journal(tmp_path: Path) -> None:
    script = tmp_path / "code.py"
    shutil.copy2(ROOT / "s18_workflow_runtime" / "code.py", script)

    first = run_lesson(script)
    resumed = run_lesson(script, "resume")

    assert "status=completed" in first
    assert "async_launched" in first
    assert "status=cached" in resumed
    assert "status=completed  agents=0  tokens=0" in resumed


def test_workflow_runtime_rejects_unsafe_artifact_names() -> None:
    workflow = load_lesson(
        "workflow_name_test", ROOT / "s18_workflow_runtime" / "code.py"
    )

    for name in ("../escape", "../../escape", "nested/name"):
        with pytest.raises(workflow.WorkflowInputError):
            workflow.validate_meta({"name": name, "description": "unsafe"})


def test_workflow_runtime_enforces_budget_and_shared_agent_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = load_lesson(
        "workflow_limit_test", ROOT / "s18_workflow_runtime" / "code.py"
    )
    budget = workflow.Budget(total=1)
    with pytest.raises(workflow.WorkflowInputError):
        budget.add(2)
    assert budget.spent() == 0

    journal = workflow.WorkflowJournal(
        "wf_limit-test_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_limit-test_0001", {})
    state = workflow.ExecutionState(
        task, journal, workflow.MockAgentRunner(), workflow.Budget(), {}
    )

    async def child(child_state, _args):
        return await child_state.agent("second call")

    monkeypatch.setattr(workflow, "AGENT_CAP", 1)
    monkeypatch.setitem(
        workflow.WORKFLOWS,
        "limit-child",
        ({"name": "limit-child", "description": "test"}, child),
    )

    async def run() -> None:
        await state.agent("first call")
        with pytest.raises(workflow.WorkflowInputError):
            await state.workflow("limit-child")

        async def fail_stage(_value, _item, _index):
            raise RuntimeError("stage failed")

        with pytest.raises(RuntimeError, match="stage failed"):
            await state.pipeline(["item"], fail_stage)

    try:
        asyncio.run(run())
    finally:
        journal.close()


def test_workflow_runtime_rejects_corrupt_resume_journal(tmp_path: Path) -> None:
    workflow = load_lesson(
        "workflow_journal_test", ROOT / "s18_workflow_runtime" / "code.py"
    )
    run_id = "wf_corrupt_0001"
    (tmp_path / f"{run_id}.journal.jsonl").write_text("{not-json}\n")

    with pytest.raises(workflow.WorkflowInputError, match="line 1"):
        workflow.WorkflowJournal(run_id, resume=True, store=tmp_path)
