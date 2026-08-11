import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LESSON = ROOT / "s07_skill_loading" / "code.py"


def load_lesson(workdir: Path):
    fake_anthropic = types.ModuleType("anthropic")
    fake_dotenv = types.ModuleType("dotenv")

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = types.SimpleNamespace(create=None)

    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv.load_dotenv = lambda override=True: None

    previous_modules = {
        "anthropic": sys.modules.get("anthropic"),
        "dotenv": sys.modules.get("dotenv"),
    }
    previous_cwd = Path.cwd()
    previous_model = os.environ.get("MODEL_ID")

    spec = importlib.util.spec_from_file_location("s07_skill_test", LESSON)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    sys.modules["anthropic"] = fake_anthropic
    sys.modules["dotenv"] = fake_dotenv
    try:
        os.chdir(workdir)
        os.environ["MODEL_ID"] = "test-model"
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)
        if previous_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = previous_model
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_catalog_stays_small_and_load_skill_returns_the_full_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = root / "skills" / "code-review"
        skill_dir.mkdir(parents=True)
        manifest = """---
name: code-review
description: |
  Review code for bugs,
  regressions, and missing tests.
---

# Code Review

UNIQUE_FULL_INSTRUCTION
"""
        (skill_dir / "SKILL.md").write_text(manifest)

        lesson = load_lesson(root)

        assert lesson.SKILL_LOADER.catalog() == (
            "- code-review: Review code for bugs, regressions, and missing tests."
        )
        assert "code-review" in lesson.SYSTEM
        assert "UNIQUE_FULL_INSTRUCTION" not in lesson.SYSTEM
        assert lesson.SKILL_LOADER.load("code-review") == manifest
        assert lesson.TOOL_HANDLERS["load_skill"]("code-review") == manifest


def test_s07_exposes_only_base_tools_and_load_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))

        assert [tool["name"] for tool in lesson.TOOLS] == [
            "bash",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "load_skill",
        ]
