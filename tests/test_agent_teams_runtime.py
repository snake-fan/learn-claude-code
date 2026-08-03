import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LESSON = ROOT / "s15_agent_teams" / "code.py"
DOWNSTREAM_LESSONS = (
    ROOT / "s16_mcp_plugin" / "code.py",
    ROOT / "s17_integrated_harness" / "code.py",
)
RUNTIME_LESSONS = (LESSON, *DOWNSTREAM_LESSONS)


def load_lesson(temp_cwd: Path, lesson_path: Path = LESSON):
    fake_anthropic = types.ModuleType("anthropic")
    fake_yaml = types.ModuleType("yaml")

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = types.SimpleNamespace(create=None)

    fake_dotenv = types.ModuleType("dotenv")
    setattr(fake_anthropic, "Anthropic", FakeAnthropic)
    setattr(fake_dotenv, "load_dotenv", lambda override=True: None)
    setattr(fake_yaml, "safe_load", lambda value: {})
    setattr(fake_yaml, "YAMLError", ValueError)

    previous_modules = {
        "anthropic": sys.modules.get("anthropic"),
        "dotenv": sys.modules.get("dotenv"),
        "yaml": sys.modules.get("yaml"),
    }
    previous_cwd = Path.cwd()
    previous_model = os.environ.get("MODEL_ID")

    name = f"agent_teams_test_{lesson_path.parent.name}_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, lesson_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {lesson_path}")
    module = importlib.util.module_from_spec(spec)

    sys.modules["anthropic"] = fake_anthropic
    sys.modules["dotenv"] = fake_dotenv
    sys.modules["yaml"] = fake_yaml
    sys.modules[name] = module
    try:
        os.chdir(temp_cwd)
        os.environ["MODEL_ID"] = "test-model"
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)
        if previous_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = previous_model
        for module_name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def init_git_repo(root: Path):
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Tests"],
        cwd=root, check=True,
    )
    (root / "tracked.txt").write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=root, check=True
    )


class AgentTeamsRuntimeTests(unittest.TestCase):
    def test_downstream_lessons_keep_the_merged_runtime_contract(self):
        for lesson_path in DOWNSTREAM_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                source = lesson_path.read_text()
                self.assertIn("worktree: str | None = None", source)
                self.assertIn("teammate_assignments", source)
                self.assertIn(
                    "def complete_task(task_id: str, owner: str = \"agent\")",
                    source,
                )
                self.assertIn(
                    "def create_worktree(name: str, task_id: str)", source
                )
                self.assertIn(
                    "def remove_worktree(name: str, "
                    "discard_changes: bool = False)",
                    source,
                )
                self.assertIn("def run_remove_worktree(name: str)", source)
                self.assertNotIn("keep_worktree", source)
                self.assertNotIn("@{push}", source)
                self.assertNotRegex(
                    source, r'''branch["']\s*,\s*["']-[dD]'''
                )
                self.assertNotRegex(source, r"git\s+branch\s+-[dD]")

    def test_inbox_delivery_is_runtime_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))

            tool_names = {tool["name"] for tool in lesson.TOOLS}
            self.assertNotIn("check_inbox", tool_names)
            self.assertIn("create_worktree", tool_names)
            self.assertIn("remove_worktree", tool_names)
            self.assertNotIn("keep_worktree", tool_names)
            worktree_tools = {
                tool["name"]: tool["input_schema"] for tool in lesson.TOOLS
                if tool["name"] in {"create_worktree", "remove_worktree"}
            }
            for schema in worktree_tools.values():
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(schema["properties"]["name"]["maxLength"], 64)
            self.assertIn("wait for the user's confirmation",
                          lesson.PROMPT_SECTIONS["teams"])
            self.assertIn("creating a Task", lesson.PROMPT_SECTIONS["teams"])
            self.assertIn("not a sandbox", lesson.PROMPT_SECTIONS["teams"])
            self.assertNotIn(
                "discard_changes",
                worktree_tools["remove_worktree"]["properties"],
            )

            lesson.BUS.send("alice", "lead", "done", "result")
            events = lesson.consume_lead_inbox()

            self.assertEqual([event["type"] for event in events], ["result"])
            self.assertIn("[result] alice: done",
                          lesson.format_team_events(events))

    def test_model_worktree_tool_never_exposes_destructive_discard(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    tool_defs = getattr(lesson, "TOOLS", None)
                    if tool_defs is None:
                        tool_defs = lesson.BUILTIN_TOOLS
                    schema = next(
                        tool["input_schema"] for tool in tool_defs
                        if tool["name"] == "remove_worktree"
                    )

                    self.assertNotIn("discard_changes", schema["properties"])
                    self.assertEqual(list(schema["properties"]), ["name"])
                    with self.assertRaises(TypeError):
                        lesson.run_remove_worktree(
                            "example", discard_changes=True
                        )

    def test_mcp_lesson_retains_s15_cron_and_background_tools(self):
        required = {
            "bash", "schedule_cron", "list_crons", "cancel_cron",
            "spawn_teammate", "create_worktree", "remove_worktree",
        }
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    tool_defs = getattr(lesson, "TOOLS", None)
                    if tool_defs is None:
                        tool_defs = lesson.BUILTIN_TOOLS
                    tool_names = {tool["name"] for tool in tool_defs}
                    bash_schema = next(
                        tool["input_schema"] for tool in tool_defs
                        if tool["name"] == "bash"
                    )

                    self.assertTrue(required.issubset(tool_names))
                    self.assertIn(
                        "run_in_background", bash_schema["properties"]
                    )
                    self.assertTrue(
                        lesson.should_run_background(
                            "bash", {"run_in_background": True}
                        )
                    )
                    self.assertTrue(callable(lesson.consume_cron_queue))
                    self.assertTrue(callable(lesson.collect_background_results))

    def test_integrated_permission_uses_mcp_tool_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(
                Path(tmp), ROOT / "s17_integrated_harness" / "code.py"
            )
            lesson.connect_mcp("deploy")
            status = types.SimpleNamespace(
                name="mcp__deploy__status", input={"service": "web"}
            )
            trigger = types.SimpleNamespace(
                name="mcp__deploy__trigger", input={"service": "web"}
            )

            self.assertIsNone(lesson.permission_hook(status))
            with patch("builtins.input", return_value="no"):
                self.assertEqual(
                    lesson.permission_hook(trigger),
                    "Permission denied by user",
                )

    def test_message_bus_rejects_unregistered_or_unsafe_recipients(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lesson = load_lesson(root)
            lesson.active_teammates["alice"] = "idle"

            with self.assertRaises(ValueError):
                lesson.BUS.send("alice", "../escape", "bad")
            self.assertFalse((root / "escape.jsonl").exists())

            result = lesson._teammate_send_message(
                "alice", "ghost", "Are you there?"
            )
            self.assertIn("not active", result)
            self.assertFalse((lesson.MAILBOX_DIR / "ghost.jsonl").exists())

    def test_reserved_teammate_names_do_not_shadow_runtime_identities(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)

                    for name in ("lead", "agent", "Lead", "Agent"):
                        rejected = lesson.spawn_teammate_thread(
                            name, "backend", "Inspect auth."
                        )
                        self.assertIn("reserved", rejected.lower())
                        self.assertNotIn(name, lesson.active_teammates)

                    lesson.BUS.send("alice", "lead", "still routable")
                    self.assertEqual(
                        lesson.BUS.read_inbox("lead")[0]["content"],
                        "still routable",
                    )

                    lesson.active_teammates["Alice"] = "idle"
                    duplicate = lesson.spawn_teammate_thread(
                        "alice", "backend", "Inspect auth."
                    )
                    self.assertIn("already exists", duplicate)

    def test_public_task_tools_return_errors_for_bad_ids(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    for task_id in ("../escape", "task_missing"):
                        for tool_name in (
                            "run_get_task",
                            "run_claim_task",
                            "run_complete_task",
                        ):
                            with self.subTest(tool=tool_name, task_id=task_id):
                                result = getattr(lesson, tool_name)(task_id)
                                self.assertIn("Error:", result)

    def test_plan_gate_blocks_mutating_tools_until_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            calls = []
            block = types.SimpleNamespace(
                name="write_file",
                input={"path": "config.py", "content": "VALUE = 1"},
            )
            handlers = {
                "write_file": lambda **kwargs: calls.append(kwargs) or "wrote"
            }

            lesson.plan_gates["alice"] = "pending"
            blocked = lesson._run_teammate_tool("alice", block, handlers)
            self.assertIn("Blocked", blocked)
            self.assertEqual(calls, [])

            lesson.plan_gates["alice"] = "approved"
            allowed = lesson._run_teammate_tool("alice", block, handlers)
            self.assertEqual(allowed, "wrote")
            self.assertEqual(len(calls), 1)

    def test_s17_teammate_dispatch_runs_permission_and_post_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(
                Path(tmp), ROOT / "s17_integrated_harness" / "code.py"
            )
            block = types.SimpleNamespace(
                name="write_file",
                input={"path": "config.py", "content": "VALUE = 1"},
            )
            calls = []
            handlers = {
                "write_file": lambda **kwargs: calls.append(
                    ("handler", kwargs)
                ) or "wrote"
            }
            lesson.plan_gates["alice"] = "approved"
            lesson.HOOKS["PreToolUse"] = [
                lambda seen: calls.append(("pre", seen.name)) or "denied"
            ]
            lesson.HOOKS["PostToolUse"] = [
                lambda seen, output: calls.append(
                    ("post", seen.name, output)
                )
            ]

            denied = lesson._run_teammate_tool("alice", block, handlers)
            self.assertEqual(denied, "denied")
            self.assertEqual(calls, [("pre", "write_file")])

            calls.clear()
            lesson.HOOKS["PreToolUse"] = [
                lambda seen: calls.append(("pre", seen.name))
            ]
            allowed = lesson._run_teammate_tool("alice", block, handlers)

            self.assertEqual(allowed, "wrote")
            self.assertEqual(
                calls,
                [
                    ("pre", "write_file"),
                    ("handler", block.input),
                    ("post", "write_file", "wrote"),
                ],
            )

    def test_normalized_mcp_tool_name_collisions_are_rejected(self):
        for lesson_path in DOWNSTREAM_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    first = lesson.MCPClient("docs.one")
                    first.register(
                        [{"name": "get.version", "inputSchema": {}}],
                        {"get.version": lambda: "one"},
                    )
                    second = lesson.MCPClient("docs_one")
                    second.register(
                        [{"name": "get_version", "inputSchema": {}}],
                        {"get_version": lambda: "two"},
                    )
                    lesson.mcp_clients.clear()
                    lesson.mcp_clients.update({
                        "docs.one": first,
                        "docs_one": second,
                    })

                    with self.assertRaisesRegex(
                        ValueError, "collision.*mcp__docs_one__get_version"
                    ):
                        lesson.assemble_tool_pool()

    def test_plan_rejection_requires_a_new_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            lesson.active_teammates["alice"] = "idle"

            self.assertIn("Plan requested",
                          lesson.run_request_plan("alice", "Refactor auth"))
            request = lesson.BUS.read_inbox("alice")
            self.assertEqual(request[0]["type"], "plan_request")
            self.assertEqual(lesson.plan_gates["alice"], "required")

            submission = lesson._teammate_submit_plan("alice", "1. Read\n2. Test")
            request_id = submission.split("(")[1].split(")")[0]
            self.assertEqual(lesson.pending_requests[request_id].status, "pending")

            result = lesson.run_review_plan(
                request_id, False, "Add a rollback step."
            )
            self.assertIn("rejected", result)
            self.assertEqual(lesson.plan_gates["alice"], "pending")

            responses = lesson.BUS.read_inbox("alice")
            accepted, _ = lesson.apply_plan_response("alice", responses[-1])
            self.assertTrue(accepted)
            self.assertEqual(lesson.plan_gates["alice"], "rejected")

            second = lesson._teammate_submit_plan(
                "alice", "1. Read\n2. Change\n3. Test\n4. Roll back on failure"
            )
            second_id = second.split("(")[1].split(")")[0]
            self.assertNotEqual(second_id, request_id)
            self.assertEqual(lesson.pending_requests[second_id].status, "pending")

    def test_mismatched_plan_response_cannot_release_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            lesson.active_teammates["alice"] = "waiting_approval"
            submission = lesson._teammate_submit_plan("alice", "1. Read\n2. Test")
            request_id = submission.split("(")[1].split(")")[0]

            forged = {
                "from": "lead",
                "to": "alice",
                "type": "plan_approval_response",
                "content": "Approved",
                "metadata": {"request_id": "req_stale", "approve": True},
            }
            accepted, notice = lesson.apply_plan_response("alice", forged)

            self.assertFalse(accepted)
            self.assertIn("Ignored", notice)
            self.assertEqual(lesson.plan_gates["alice"], "pending")
            self.assertEqual(lesson.plan_request_ids["alice"], request_id)

            current_but_unreviewed = {
                **forged,
                "metadata": {"request_id": request_id, "approve": True},
            }
            accepted, _ = lesson.apply_plan_response(
                "alice", current_but_unreviewed
            )
            self.assertFalse(accepted)
            self.assertEqual(lesson.plan_gates["alice"], "pending")

    def test_shutdown_response_must_come_from_requested_teammate(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            lesson.active_teammates.update({"alice": "idle", "bob": "idle"})
            result = lesson.run_request_shutdown("alice")
            request_id = result.split("(")[1].split(")")[0]

            lesson.BUS.send(
                "bob", "lead", "Shutdown acknowledged.",
                "shutdown_response",
                {"request_id": request_id, "approve": True},
            )
            lesson.consume_lead_inbox()

            self.assertEqual(
                lesson.pending_requests[request_id].status, "pending"
            )

    def test_shutdown_request_must_match_active_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            lesson.active_teammates["alice"] = "idle"
            forged = {
                "from": "lead",
                "to": "alice",
                "type": "shutdown_request",
                "content": "Shut down.",
                "metadata": {"request_id": "req_unknown"},
            }

            accepted, notice = lesson.apply_shutdown_request("alice", forged)
            self.assertFalse(accepted)
            self.assertIn("Ignored", notice)
            self.assertEqual(lesson.active_teammates["alice"], "idle")

            result = lesson.run_request_shutdown("alice")
            request_id = result.split("(")[1].split(")")[0]
            request = lesson.BUS.read_inbox("alice")[-1]
            accepted, matched_id = lesson.apply_shutdown_request(
                "alice", request
            )

            self.assertTrue(accepted)
            self.assertEqual(matched_id, request_id)
            self.assertEqual(lesson.active_teammates["alice"], "stopping")
            replayed, _ = lesson.apply_shutdown_request("alice", request)
            self.assertFalse(replayed)

    def test_teammate_emits_result_then_idle_and_shuts_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            lesson.IDLE_SCAN_INTERVAL = 5.0
            pending = lesson.create_task("Do not claim before mailbox delivery")
            seen_tools = set()

            def respond(**kwargs):
                seen_tools.update(tool["name"] for tool in kwargs["tools"])
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    content=[types.SimpleNamespace(
                        type="text", text="Task complete."
                    )],
                )

            lesson.client.messages.create = respond

            lesson.spawn_teammate_thread("alice", "backend", "Inspect auth.")
            lead_inbox = lesson.MAILBOX_DIR / "lead.jsonl"
            self.assertTrue(wait_until(
                lambda: (
                    lead_inbox.exists()
                    and len(lead_inbox.read_text().splitlines()) >= 2
                )
            ))
            events = lesson.consume_lead_inbox()

            self.assertEqual(
                [event["type"] for event in events],
                ["result", "idle_notification"],
            )
            self.assertEqual(lesson.active_teammates["alice"], "idle")
            self.assertTrue(
                {"list_tasks", "claim_task", "complete_task"}
                .issubset(seen_tools)
            )
            self.assertTrue(
                {"create_worktree", "remove_worktree", "keep_worktree"}
                .isdisjoint(seen_tools)
            )

            lesson.run_request_shutdown("alice")
            self.assertTrue(
                wait_until(lambda: "alice" not in lesson.active_teammates)
            )
            shutdown_events = lesson.consume_lead_inbox()
            self.assertEqual(shutdown_events[-1]["type"], "shutdown_response")
            request_id = shutdown_events[-1]["metadata"]["request_id"]
            self.assertEqual(
                lesson.pending_requests[request_id].status, "approved"
            )
            self.assertEqual(lesson.load_task(pending.id).status, "pending")

    def test_downstream_teammates_continue_past_ten_tool_rounds(self):
        for lesson_path in DOWNSTREAM_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    lesson.IDLE_SCAN_INTERVAL = 5.0
                    calls = 0

                    def respond(**kwargs):
                        nonlocal calls
                        calls += 1
                        if calls <= 11:
                            return types.SimpleNamespace(
                                stop_reason="tool_use",
                                content=[types.SimpleNamespace(
                                    type="tool_use",
                                    name="list_tasks",
                                    id=f"list-{calls}",
                                    input={},
                                )],
                            )
                        return types.SimpleNamespace(
                            stop_reason="end_turn",
                            content=[types.SimpleNamespace(
                                type="text", text="Long task complete."
                            )],
                        )

                    lesson.client.messages.create = respond
                    lesson.spawn_teammate_thread(
                        "alice", "backend", "Use more than ten tool rounds."
                    )
                    lead_inbox = lesson.MAILBOX_DIR / "lead.jsonl"
                    self.assertTrue(wait_until(
                        lambda: (
                            lead_inbox.exists()
                            and len(lead_inbox.read_text().splitlines()) >= 2
                        ),
                        timeout=3.0,
                    ))
                    events = lesson.consume_lead_inbox()

                    self.assertEqual(calls, 12)
                    self.assertEqual(
                        [event["type"] for event in events],
                        ["result", "idle_notification"],
                    )
                    self.assertEqual(
                        lesson.active_teammates.get("alice"), "idle"
                    )
                    lesson.run_request_shutdown("alice")
                    self.assertTrue(wait_until(
                        lambda: "alice" not in lesson.active_teammates
                    ))

    def test_teammate_exception_releases_runtime_and_task_ownership(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    lesson = load_lesson(Path(tmp), lesson_path)
                    task = lesson.create_task("Implement auth")
                    calls = 0

                    def respond(**kwargs):
                        nonlocal calls
                        calls += 1
                        tool_name = "claim_task" if calls == 1 else "list_tasks"
                        tool_input = {"task_id": task.id} if calls == 1 else {}
                        return types.SimpleNamespace(
                            stop_reason="tool_use",
                            content=[types.SimpleNamespace(
                                type="tool_use", name=tool_name,
                                id=f"tool-{calls}", input=tool_input,
                            )],
                        )

                    original_dispatch = lesson._run_teammate_tool

                    def crash_after_claim(name, block, handlers):
                        if block.name == "list_tasks":
                            raise RuntimeError("simulated dispatch failure")
                        return original_dispatch(name, block, handlers)

                    lesson.client.messages.create = respond
                    lesson._run_teammate_tool = crash_after_claim
                    lesson.spawn_teammate_thread(
                        "alice", "backend", "Claim and begin work."
                    )

                    self.assertTrue(wait_until(
                        lambda: "alice" not in lesson.active_teammates
                    ))
                    self.assertNotIn("alice", lesson.teammate_assignments)
                    recovered = lesson.load_task(task.id)
                    self.assertEqual(recovered.status, "pending")
                    self.assertIsNone(recovered.owner)
                    events = lesson.consume_lead_inbox()
                    self.assertEqual([event["type"] for event in events], ["error"])
                    self.assertIn("simulated dispatch failure", events[0]["content"])

    def test_s17_completed_background_task_wakes_the_agent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(
                Path(tmp), ROOT / "s17_integrated_harness" / "code.py"
            )
            seen_messages = []

            def respond(messages, context, tools, state, max_tokens):
                seen_messages.append(list(messages))
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    content=[types.SimpleNamespace(
                        type="text", text="Background result handled."
                    )],
                )

            lesson.call_llm = respond
            lesson.background_tasks["bg_0001"] = {
                "tool_use_id": "tool-1",
                "command": "pytest",
                "status": "completed",
            }
            lesson.background_results["bg_0001"] = "all tests passed"
            history = []
            context = {}
            session_state = {"active_user_request": "Run tests"}
            threading.Thread(
                target=lesson.async_event_loop,
                args=(history, context, session_state),
                daemon=True,
            ).start()

            self.assertTrue(wait_until(lambda: bool(seen_messages), timeout=3.0))
            delivered = str(seen_messages[0])
            self.assertIn("<task_notification>", delivered)
            self.assertIn("all tests passed", delivered)
            self.assertFalse(lesson.has_pending_background())
            calls_after_delivery = len(seen_messages)
            time.sleep(1.2)
            self.assertEqual(len(seen_messages), calls_after_delivery)

    def test_teammate_survives_stale_worktree_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            lesson.IDLE_SCAN_INTERVAL = 5.0
            task = lesson.create_task("Implement auth")
            lesson.create_worktree("auth", task.id)
            worktree = lesson.WORKTREES_DIR / "auth"
            calls = 0
            bash_result = []

            def respond(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return types.SimpleNamespace(
                        stop_reason="tool_use",
                        content=[types.SimpleNamespace(
                            type="tool_use", name="claim_task", id="claim-1",
                            input={"task_id": task.id},
                        )],
                    )
                if calls == 2:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force",
                         str(worktree)], cwd=root, check=True,
                    )
                    return types.SimpleNamespace(
                        stop_reason="tool_use",
                        content=[types.SimpleNamespace(
                            type="tool_use", name="bash", id="bash-1",
                            input={"command": "pwd"},
                        )],
                    )
                bash_result.append(
                    kwargs["messages"][-1]["content"][0]["content"]
                )
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    content=[types.SimpleNamespace(
                        type="text", text="Handled stale assignment."
                    )],
                )

            lesson.client.messages.create = respond
            lesson.spawn_teammate_thread("alice", "backend", "Claim the task.")

            self.assertTrue(wait_until(lambda: bool(bash_result)))
            self.assertIn("Invalid task assignment", bash_result[0])
            self.assertIn("alice", lesson.active_teammates)
            lesson.run_request_shutdown("alice")
            self.assertTrue(
                wait_until(lambda: "alice" not in lesson.active_teammates)
            )

    def test_autonomous_claim_is_atomic_across_teammates(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            task = lesson.create_task("Refactor auth")

            barrier = threading.Barrier(3)
            claimed = {}

            def claim(name):
                barrier.wait()
                claimed[name] = lesson.claim_next_task(name)

            threads = [
                threading.Thread(target=claim, args=(name,))
                for name in ("alice", "bob")
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(
                len([result for result in claimed.values() if result is not None]),
                1,
            )
            winner = next(result.owner for result in claimed.values()
                          if result is not None)
            self.assertEqual(lesson.load_task(task.id).owner, winner)

    def test_assignment_enforces_one_task_and_owner_only_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            lesson = load_lesson(Path(tmp))
            first = lesson.create_task("Refactor auth")
            second = lesson.create_task("Refactor login")

            self.assertIn("Claimed", lesson.claim_task(first.id, owner="alice"))
            denied = lesson.claim_task(second.id, owner="alice")
            self.assertIn("must complete", denied)
            self.assertEqual(lesson.load_task(second.id).status, "pending")

            denied = lesson.complete_task(first.id, owner="bob")
            self.assertIn("not bob", denied)
            self.assertEqual(lesson.load_task(first.id).status, "in_progress")

            self.assertIn(
                "Completed", lesson.complete_task(first.id, owner="alice")
            )
            self.assertNotIn("alice", lesson.teammate_assignments)
            self.assertIn("Claimed", lesson.claim_task(second.id, owner="alice"))

    def test_task_worktree_sets_assignment_cwd_and_contains_file_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")

            created = lesson.create_worktree("auth", task.id)
            self.assertIn("created", created)
            worktree = lesson.WORKTREES_DIR / "auth"
            self.assertEqual(lesson.load_task(task.id).worktree, "auth")

            self.assertIn("Claimed", lesson.claim_task(task.id, owner="alice"))
            assignment = lesson.teammate_assignments["alice"]
            self.assertEqual(assignment["task_id"], task.id)
            self.assertEqual(assignment["cwd"], worktree)
            self.assertIn(
                "Wrote", lesson.run_write(
                    "nested/result.txt", "done", cwd=lesson.assignment_cwd("alice")
                )
            )
            self.assertEqual((worktree / "nested" / "result.txt").read_text(),
                             "done")
            escaped = lesson.run_write(
                "../outside.txt", "bad", cwd=lesson.assignment_cwd("alice")
            )
            self.assertIn("escapes workspace", escaped)
            self.assertFalse((lesson.WORKTREES_DIR / "outside.txt").exists())
            missing_cwd = lesson.run_bash("pwd", cwd=worktree / "missing")
            self.assertIn("FileNotFoundError", missing_cwd)

    def test_invalid_or_unregistered_worktree_never_becomes_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")

            invalid = lesson.create_worktree("../escape", task.id)
            self.assertIn("Error", invalid)
            self.assertIsNone(lesson.load_task(task.id).worktree)
            self.assertFalse((root / "escape").exists())

            missing = lesson.create_worktree("auth", "../missing")
            self.assertIn("Error", missing)
            self.assertFalse((lesson.WORKTREES_DIR / "auth").exists())

            bound = lesson.load_task(task.id)
            bound.worktree = "ghost"
            lesson.save_task(bound)
            denied = lesson.claim_task(task.id, owner="alice")
            self.assertIn("not registered", denied)
            self.assertEqual(lesson.load_task(task.id).status, "pending")
            self.assertEqual(lesson.scan_unclaimed_tasks(), [])

    def test_create_validates_branch_and_binds_only_after_git_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            subprocess.run(
                ["git", "branch", "wt/auth"], cwd=root, check=True
            )

            collision = lesson.create_worktree("auth", task.id)
            self.assertIn("already exists", collision)
            self.assertIsNone(lesson.load_task(task.id).worktree)
            self.assertFalse((lesson.WORKTREES_DIR / "auth").exists())

            original_run_git = lesson.run_git

            def fail_add(args, cwd=None):
                if args[:2] == ["worktree", "add"]:
                    return False, "simulated add failure"
                return original_run_git(args, cwd=cwd)

            lesson.run_git = fail_add
            failed = lesson.create_worktree("login", task.id)
            self.assertIn("simulated add failure", failed)
            self.assertIsNone(lesson.load_task(task.id).worktree)
            self.assertFalse((lesson.WORKTREES_DIR / "login").exists())

    def test_failed_git_add_reports_and_preserves_partial_artifacts(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    init_git_repo(root)
                    lesson = load_lesson(root, lesson_path)
                    task = lesson.create_task("Implement auth")
                    original_run_git = lesson.run_git

                    def fail_after_add(args, cwd=None):
                        if args[:2] == ["worktree", "add"]:
                            ok, output = original_run_git(args, cwd=cwd)
                            self.assertTrue(ok, output)
                            return False, "simulated late add failure"
                        return original_run_git(args, cwd=cwd)

                    lesson.run_git = fail_after_add
                    result = lesson.create_worktree("auth", task.id)

                    self.assertIn("Partial operation", result)
                    self.assertIn("simulated late add failure", result)
                    self.assertIn("remains unbound", result)
                    self.assertIn("git worktree list", result)
                    self.assertTrue((lesson.WORKTREES_DIR / "auth").is_dir())
                    self.assertIsNone(lesson.load_task(task.id).worktree)
                    branch = subprocess.run(
                        ["git", "show-ref", "--verify", "--quiet",
                         "refs/heads/wt/auth"],
                        cwd=root,
                    )
                    self.assertEqual(branch.returncode, 0)

    def test_binding_failure_retains_created_git_data_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            original_save_task = lesson.save_task

            def fail_binding(candidate):
                if candidate.worktree == "auth":
                    raise OSError("simulated task persistence failure")
                original_save_task(candidate)

            lesson.save_task = fail_binding
            result = lesson.create_worktree("auth", task.id)

            self.assertIn("Partial success", result)
            self.assertIn("manual recovery", result)
            self.assertTrue((lesson.WORKTREES_DIR / "auth").is_dir())
            self.assertIsNone(lesson.load_task(task.id).worktree)
            branch = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet",
                 "refs/heads/wt/auth"], cwd=root,
            )
            self.assertEqual(branch.returncode, 0)

    def test_remove_worktree_refuses_dirty_checkout_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            lesson.create_worktree("auth", task.id)
            lesson.claim_task(task.id, owner="alice")
            lesson.complete_task(task.id, owner="alice")
            worktree = lesson.WORKTREES_DIR / "auth"
            (worktree / "dirty.txt").write_text("unsaved\n")

            denied = lesson.remove_worktree("auth")

            self.assertIn("uncommitted", denied)
            self.assertTrue(worktree.exists())
            self.assertEqual(lesson.load_task(task.id).worktree, "auth")

    def test_remove_worktree_treats_ignored_files_as_uncommitted_data(self):
        for lesson_path in RUNTIME_LESSONS:
            with self.subTest(lesson=lesson_path.parent.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    init_git_repo(root)
                    (root / ".gitignore").write_text("ignored.log\n")
                    subprocess.run(
                        ["git", "add", ".gitignore"], cwd=root, check=True
                    )
                    subprocess.run(
                        ["git", "commit", "-q", "-m", "ignore runtime log"],
                        cwd=root,
                        check=True,
                    )
                    lesson = load_lesson(root, lesson_path)
                    task = lesson.create_task("Implement auth")
                    lesson.create_worktree("auth", task.id)
                    lesson.claim_task(task.id, owner="alice")
                    lesson.complete_task(task.id, owner="alice")
                    worktree = lesson.WORKTREES_DIR / "auth"
                    (worktree / "ignored.log").write_text("valuable output\n")

                    denied = lesson.run_remove_worktree("auth")

                    self.assertIn("uncommitted", denied)
                    self.assertTrue(worktree.exists())
                    self.assertEqual(lesson.load_task(task.id).worktree, "auth")
                    removed = lesson.remove_worktree(
                        "auth", discard_changes=True
                    )
                    self.assertIn("branch 'wt/auth' retained", removed)
                    self.assertFalse(worktree.exists())

    def test_discard_removes_checkout_but_retains_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            lesson.create_worktree("auth", task.id)
            lesson.claim_task(task.id, owner="alice")
            lesson.complete_task(task.id, owner="alice")
            worktree = lesson.WORKTREES_DIR / "auth"
            (worktree / "dirty.txt").write_text("discard me\n")

            removed = lesson.remove_worktree("auth", discard_changes=True)

            self.assertIn("branch 'wt/auth' retained", removed)
            self.assertFalse(worktree.exists())
            self.assertIsNone(lesson.load_task(task.id).worktree)
            branch = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet",
                 "refs/heads/wt/auth"], cwd=root,
            )
            self.assertEqual(branch.returncode, 0)

    def test_clean_local_commit_survives_non_force_checkout_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            lesson.create_worktree("auth", task.id)
            lesson.claim_task(task.id, owner="alice")
            lesson.complete_task(task.id, owner="alice")
            worktree = lesson.WORKTREES_DIR / "auth"
            (worktree / "feature.txt").write_text("committed work\n")
            subprocess.run(
                ["git", "add", "feature.txt"], cwd=worktree, check=True
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "feature"],
                cwd=worktree, check=True,
            )
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
            ).strip()
            upstream = subprocess.check_output(
                ["git", "for-each-ref", "--format=%(upstream)",
                 "refs/heads/wt/auth"], cwd=root, text=True,
            ).strip()
            self.assertEqual(upstream, "")

            removed = lesson.remove_worktree("auth")

            self.assertIn("branch 'wt/auth' retained", removed)
            self.assertFalse(worktree.exists())
            retained = subprocess.check_output(
                ["git", "rev-parse", "wt/auth"], cwd=root, text=True
            ).strip()
            self.assertEqual(retained, commit)

    def test_active_task_blocks_normal_and_discard_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            lesson = load_lesson(root)
            task = lesson.create_task("Implement auth")
            lesson.create_worktree("auth", task.id)
            worktree = lesson.WORKTREES_DIR / "auth"

            pending_normal = lesson.remove_worktree("auth")
            pending_discard = lesson.remove_worktree(
                "auth", discard_changes=True
            )
            self.assertIn("active task", pending_normal)
            self.assertIn("active task", pending_discard)

            lesson.claim_task(task.id, owner="alice")
            progress_normal = lesson.remove_worktree("auth")
            progress_discard = lesson.remove_worktree(
                "auth", discard_changes=True
            )

            self.assertIn("active task", progress_normal)
            self.assertIn("active task", progress_discard)
            self.assertTrue(worktree.exists())
            self.assertEqual(lesson.load_task(task.id).status, "in_progress")


if __name__ == "__main__":
    unittest.main()
