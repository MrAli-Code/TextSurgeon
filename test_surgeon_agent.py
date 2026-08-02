#!/usr/bin/env python3
"""Comprehensive Unit & Integration Test Suite for surgeon_agent.py (Agent Mode).

Covers 100% of Agent Mode scenarios:
1. Workspace Security & Path Traversal Prevention
2. Response Parsing (Markdown Protocol, JSON Protocol, Fences, RTL/Bidi, Malformed)
3. Workspace Operations (Multi-file creation, atomic fsync, file deletion, tree scanning)
4. Execution Engine (Success, failure, timeout, setup commands, artifact discovery)
5. Prompt Builder (Task prompts, runtime presets, verification prompts, truncation)
6. Safe Projects & Backups (Project lifecycle, meta.json, snapshot creation, rollback)
7. Web API Endpoints (All /api/agent/* REST routes)
8. End-to-End Autonomous Agent Loop Simulation (Multi-round goal -> code -> run -> verify -> edit -> rollback)
9. CLI Flags & Project Management
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple

import surgeon_agent as agent
from surgeon_agent import (
    AgentError,
    AgentParseError,
    AgentPlan,
    AgentPromptBuilder,
    AgentWorkspace,
    ExecutionResult,
    FileAction,
    ProjectManager,
    WorkspaceSecurityError,
    parse_agent_response,
    resolve_safe_workspace_path,
    scan_workspace_tree,
)


class TestAgentWorkspaceSecurity(unittest.TestCase):
    """Tests for workspace path safety, sandbox containment, and traversal attacks."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_agent_sec_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_safe_relative_paths(self) -> None:
        p1 = resolve_safe_workspace_path(self.tmp_dir, "main.py")
        self.assertEqual(os.path.normpath(p1), os.path.normpath(os.path.join(self.tmp_dir, "main.py")))

        p2 = resolve_safe_workspace_path(self.tmp_dir, "src/components/button.js")
        self.assertEqual(
            os.path.normpath(p2),
            os.path.normpath(os.path.join(self.tmp_dir, "src", "components", "button.js")),
        )

        p3 = resolve_safe_workspace_path(self.tmp_dir, "./nested/deep/file.txt")
        self.assertEqual(
            os.path.normpath(p3),
            os.path.normpath(os.path.join(self.tmp_dir, "nested", "deep", "file.txt")),
        )

    def test_path_traversal_attacks_blocked(self) -> None:
        bad_paths = [
            "../outside.py",
            "../../etc/passwd",
            "..\\..\\windows\\system32\\cmd.exe",
            "src/../../outside.py",
            "/etc/shadow",
            "C:\\Windows\\System32\\calc.exe",
            "D:\\secret.txt",
        ]
        for bad in bad_paths:
            with self.assertRaises(WorkspaceSecurityError, msg=f"Should block: {bad}"):
                resolve_safe_workspace_path(self.tmp_dir, bad)


class TestAgentResponseParser(unittest.TestCase):
    """Tests for parsing AI Agent responses across Markdown and JSON protocols."""

    def test_parse_full_markdown_protocol(self) -> None:
        reply = """
<EXPLANATION>
We will create a multi-slide presentation builder using python-pptx.
</EXPLANATION>

@@COMMAND pip install python-pptx
@@COMMAND pip install Pillow

@@FILE make_presentation.py
<<<
import sys
print("Building presentation...")
with open("presentation.pptx", "wb") as f:
    f.write(b"PK\\x03\\x04demo_pptx_content")
print("Presentation generated successfully!")
>>>

@@FILE config.json
<<<
{"slides": 5, "theme": "dark"}
>>>

@@RUN python make_presentation.py
@@ARTIFACT presentation.pptx
"""
        plan = parse_agent_response(reply)
        self.assertIsNotNone(plan.explanation)
        self.assertIn("presentation builder", plan.explanation or "")
        self.assertEqual(len(plan.setup_commands), 2)
        self.assertEqual(plan.setup_commands[0], "pip install python-pptx")
        self.assertEqual(plan.setup_commands[1], "pip install Pillow")
        self.assertEqual(plan.run_command, "python make_presentation.py")
        self.assertEqual(plan.expected_artifacts, ["presentation.pptx"])
        self.assertEqual(len(plan.file_actions), 2)

        f1 = plan.file_actions[0]
        self.assertEqual(f1.path, "make_presentation.py")
        self.assertEqual(f1.action_type, "create")
        self.assertIn("Building presentation", f1.content)

        f2 = plan.file_actions[1]
        self.assertEqual(f2.path, "config.json")
        self.assertIn('"slides": 5', f2.content)

    def test_parse_surgical_edit_in_agent_mode(self) -> None:
        reply = """
<EXPLANATION>
Modifying the slide theme to light mode.
</EXPLANATION>

@@EDIT config.json anchor
START-ANCHOR: "theme": "dark"
END-ANCHOR: "theme": "dark"
<<<
"theme": "light"
>>>

@@RUN python make_presentation.py
"""
        plan = parse_agent_response(reply)
        self.assertEqual(len(plan.file_actions), 1)
        act = plan.file_actions[0]
        self.assertEqual(act.path, "config.json")
        self.assertEqual(act.action_type, "edit")
        self.assertEqual(len(act.edit_ops), 1)
        self.assertEqual(act.edit_ops[0].strategy, "anchor")
        self.assertEqual(act.edit_ops[0].replace.strip(), '"theme": "light"')

    def test_parse_json_protocol_plan(self) -> None:
        reply = """
<EXPLANATION>
Structured JSON agent plan.
</EXPLANATION>
```json
{
  "explanation": "Structured JSON agent plan.",
  "commands": ["npm install axios"],
  "files": [
    {
      "path": "scraper.js",
      "action": "create",
      "content": "console.log('Scraper running...');"
    },
    {
      "path": "old_file.tmp",
      "action": "delete"
    }
  ],
  "run": "node scraper.js",
  "artifacts": ["scraped.json"]
}
```
"""
        plan = parse_agent_response(reply)
        self.assertEqual(plan.explanation, "Structured JSON agent plan.")
        self.assertEqual(plan.setup_commands, ["npm install axios"])
        self.assertEqual(plan.run_command, "node scraper.js")
        self.assertEqual(plan.expected_artifacts, ["scraped.json"])
        self.assertEqual(len(plan.file_actions), 2)
        self.assertEqual(plan.file_actions[0].path, "scraper.js")
        self.assertEqual(plan.file_actions[1].path, "old_file.tmp")
        self.assertEqual(plan.file_actions[1].action_type, "delete")

    def test_parse_persian_rtl_explanation(self) -> None:
        reply = """
<EXPLANATION>
این اسکریپت پایتون اسلایدهای پاورپوینت را به طور خودکار تولید می‌کند.
</EXPLANATION>

@@FILE app.py
<<<
print("درود بر جهان")
>>>

@@RUN python app.py
"""
        plan = parse_agent_response(reply)
        self.assertIsNotNone(plan.explanation)
        self.assertIn("پاورپوینت", plan.explanation or "")
        self.assertEqual(len(plan.file_actions), 1)
        self.assertEqual(plan.file_actions[0].path, "app.py")
        self.assertIn("درود بر جهان", plan.file_actions[0].content)

    def test_parse_delete_file_markdown(self) -> None:
        reply = """
<EXPLANATION>Deleting obsolete script.</EXPLANATION>
@@FILE obsolete.py action:delete
"""
        plan = parse_agent_response(reply)
        self.assertEqual(len(plan.file_actions), 1)
        self.assertEqual(plan.file_actions[0].path, "obsolete.py")
        self.assertEqual(plan.file_actions[0].action_type, "delete")

    def test_parse_empty_or_invalid_raises_parse_error(self) -> None:
        with self.assertRaises(AgentParseError):
            parse_agent_response("")

        with self.assertRaises(AgentParseError):
            parse_agent_response("   \n\t  ")

        # Missing closing fence
        bad_fence = "@@FILE broken.py\n<<<\nprint('unfinished')"
        with self.assertRaises(AgentParseError):
            parse_agent_response(bad_fence)


class TestAgentWorkspaceOperations(unittest.TestCase):
    """Tests for workspace file operations, atomic writes, diff preview, and tree scanning."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_agent_ops_")
        self.ws = AgentWorkspace(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_and_modify_nested_files(self) -> None:
        plan = AgentPlan(
            explanation="Create nested files",
            file_actions=[
                FileAction(path="src/math/calc.py", action_type="create", content="def add(a, b): return a + b\n"),
                FileAction(path="tests/test_calc.py", action_type="create", content="from src.math.calc import add\nassert add(1, 2) == 3\n"),
            ],
            run_command="python tests/test_calc.py",
        )
        res = self.ws.apply_plan(plan, backup=True)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["written_files"]), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, "src", "math", "calc.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, "tests", "test_calc.py")))

    def test_delete_file_in_workspace(self) -> None:
        # Create a file first
        target = os.path.join(self.tmp_dir, "temp_data.csv")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a,b,c\n1,2,3\n")
        self.assertTrue(os.path.isfile(target))

        # Delete it via plan
        plan = AgentPlan(
            explanation="Remove temp csv",
            file_actions=[FileAction(path="temp_data.csv", action_type="delete")],
        )
        res = self.ws.apply_plan(plan, backup=True)
        self.assertTrue(res["success"])
        self.assertFalse(os.path.isfile(target))

    def test_preview_plan_generates_diffs(self) -> None:
        # Create initial file
        f_path = os.path.join(self.tmp_dir, "doc.txt")
        with open(f_path, "w", encoding="utf-8") as f:
            f.write("Line 1\nLine 2\nLine 3\n")

        plan = AgentPlan(
            explanation="Update doc",
            file_actions=[
                FileAction(path="doc.txt", action_type="modify", content="Line 1\nLine 2 (updated)\nLine 3\n"),
                FileAction(path="new_doc.txt", action_type="create", content="Brand new\n"),
            ],
        )
        preview = self.ws.preview_plan(plan)
        self.assertEqual(len(preview["changes"]), 2)
        c1 = preview["changes"][0]
        self.assertEqual(c1["path"], "doc.txt")
        self.assertEqual(c1["action"], "overwrite")
        self.assertIn("+++", c1["diff"])
        self.assertIn("Line 2 (updated)", c1["diff"])

    def test_scan_workspace_tree(self) -> None:
        # Create regular files
        os.makedirs(os.path.join(self.tmp_dir, "src"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "src", "index.py"), "w", encoding="utf-8") as f:
            f.write("print('hello world')\n" * 10)

        # Create ignored folders
        os.makedirs(os.path.join(self.tmp_dir, ".git"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, ".git", "config"), "w", encoding="utf-8") as f:
            f.write("git config")

        os.makedirs(os.path.join(self.tmp_dir, "__pycache__"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "__pycache__", "index.cpython.pyc"), "w", encoding="utf-8") as f:
            f.write("pyc cache")

        scan = scan_workspace_tree(self.tmp_dir)
        self.assertTrue(scan["exists"])
        paths = [f["path"].replace("\\", "/") for f in scan["files"]]
        self.assertIn("src/index.py", paths)
        self.assertNotIn(".git/config", paths)
        self.assertNotIn("__pycache__/index.cpython.pyc", paths)
        self.assertEqual(scan["total_files"], 1)
        self.assertEqual(scan["total_lines"], 10)
        self.assertGreater(scan["est_tokens"], 0)


class TestAgentExecutionEngine(unittest.TestCase):
    """Tests for executing commands, capturing logs, timeouts, and artifact discovery."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_agent_exec_")
        self.ws = AgentWorkspace(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_execute_successful_python_script(self) -> None:
        script = os.path.join(self.tmp_dir, "generator.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("print('Step 1: Init')\nprint('Step 2: Done')\n")

        res = self.ws.execute_command("python generator.py")
        self.assertTrue(res.success)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Step 1: Init", res.stdout)
        self.assertIn("Step 2: Done", res.stdout)
        self.assertGreater(res.duration_sec, 0)

    def test_execute_failing_script_captures_stderr(self) -> None:
        script = os.path.join(self.tmp_dir, "fail.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('starting...', file=sys.stdout)\nraise ValueError('Intentional Error')\n")

        res = self.ws.execute_command("python fail.py")
        self.assertFalse(res.success)
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("ValueError: Intentional Error", res.stderr)

    def test_execute_discovers_binary_artifacts(self) -> None:
        script = os.path.join(self.tmp_dir, "make_files.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "with open('presentation.pptx', 'wb') as f: f.write(b'PPTX_DATA')\n"
                "with open('report.csv', 'w') as f: f.write('x,y\\n1,2\\n')\n"
            )

        res = self.ws.execute_command("python make_files.py", expected_artifacts=["presentation.pptx", "report.csv"])
        self.assertTrue(res.success)
        self.assertIn("presentation.pptx", res.artifacts_found)
        self.assertIn("report.csv", res.artifacts_found)

    def test_execute_command_timeout(self) -> None:
        script = os.path.join(self.tmp_dir, "infinite.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(10)\n")

        res = self.ws.execute_command("python infinite.py", timeout_sec=1)
        self.assertFalse(res.success)
        self.assertTrue(res.timed_out)
        self.assertIn("timed out", res.stderr.lower())


class TestAgentPromptBuilderScenarios(unittest.TestCase):
    """Tests for prompt builder across multiple runtimes, feedback diagnostics, and token safety."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_agent_pb_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_task_prompt_runtimes(self) -> None:
        # Python
        p_py = AgentPromptBuilder.build_task_prompt(goal="Build PPT", workspace_root=self.tmp_dir, runtime="python")
        self.assertIn("PYTHON", p_py)
        self.assertIn("Build PPT", p_py)

        # Node.js
        p_node = AgentPromptBuilder.build_task_prompt(goal="Build API", workspace_root=self.tmp_dir, runtime="node")
        self.assertIn("NODE", p_node)
        self.assertIn("Build API", p_node)

        # Bash / Shell
        p_bash = AgentPromptBuilder.build_task_prompt(goal="Run script", workspace_root=self.tmp_dir, runtime="bash")
        self.assertIn("BASH", p_bash)

    def test_verification_prompt_on_failure_with_diagnostics(self) -> None:
        plan = AgentPlan(explanation="Generate presentation", run_command="python app.py")
        res = ExecutionResult(
            command="python app.py",
            exit_code=1,
            stdout="Loading modules...",
            stderr="Traceback (most recent call last):\n  File 'app.py', line 5\n    import pptx\nModuleNotFoundError: No module named 'pptx'",
            duration_sec=0.25,
            success=False,
        )
        prompt = AgentPromptBuilder.build_verification_prompt(
            goal="Build PPT",
            plan=plan,
            result=res,
            workspace_root=self.tmp_dir,
        )
        self.assertIn("EXECUTION FAILED", prompt)
        self.assertIn("ModuleNotFoundError: No module named 'pptx'", prompt)
        self.assertIn("REPAIR INSTRUCTIONS", prompt)
        self.assertIn("@@COMMAND", prompt)

    def test_verification_prompt_on_success_with_artifacts(self) -> None:
        plan = AgentPlan(explanation="Generate presentation", expected_artifacts=["presentation.pptx"])
        res = ExecutionResult(
            command="python app.py",
            exit_code=0,
            stdout="Successfully created 5 slides.",
            stderr="",
            duration_sec=1.1,
            artifacts_found=["presentation.pptx"],
            success=True,
        )
        prompt = AgentPromptBuilder.build_verification_prompt(
            goal="Build PPT",
            plan=plan,
            result=res,
            workspace_root=self.tmp_dir,
        )
        self.assertIn("EXECUTION SUCCEEDED", prompt)
        self.assertIn("presentation.pptx", prompt)
        self.assertIn("VERIFICATION TASK", prompt)


class TestSafeProjectManagerAndBackups(unittest.TestCase):
    """Tests for project directory creation, meta.json, backup indexing, and multi-file rollbacks."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_proj_mgr_")
        self.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(self.tmp_dir, "projects")

    def tearDown(self) -> None:
        agent.DEFAULT_PROJECTS_DIR = self.orig_projects_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_and_list_projects(self) -> None:
        p = ProjectManager.create_project("ppt_bot", runtime="python", description="PowerPoint Builder")
        self.assertEqual(p["name"], "ppt_bot")
        self.assertTrue(os.path.isdir(p["path"]))
        self.assertTrue(os.path.isfile(os.path.join(p["path"], "main.py")))

        meta = ProjectManager.get_project_metadata(p["path"])
        self.assertIsNotNone(meta)
        self.assertEqual(meta["runtime"], "python")
        self.assertEqual(meta["description"], "PowerPoint Builder")

        all_p = ProjectManager.list_projects()
        self.assertEqual(len(all_p), 1)
        self.assertEqual(all_p[0]["name"], "ppt_bot")

    def test_multi_round_backup_and_rollback(self) -> None:
        ws_dir = os.path.join(self.tmp_dir, "workspace")
        os.makedirs(ws_dir, exist_ok=True)
        ws = AgentWorkspace(ws_dir)

        # Round 1: Create file v1
        f_path = os.path.join(ws_dir, "script.py")
        with open(f_path, "w", encoding="utf-8") as f:
            f.write("VERSION = 1\n")

        # Round 2: Apply v2 (snapshot 1 made)
        plan2 = AgentPlan(
            explanation="Update to v2",
            file_actions=[FileAction(path="script.py", action_type="modify", content="VERSION = 2\n")],
        )
        res2 = ws.apply_plan(plan2, backup=True)
        self.assertTrue(res2["success"])
        b1_id = res2["backup_id"]
        self.assertIsNotNone(b1_id)

        with open(f_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "VERSION = 2\n")

        # Round 3: Apply v3 (snapshot 2 made)
        plan3 = AgentPlan(
            explanation="Update to v3",
            file_actions=[FileAction(path="script.py", action_type="modify", content="VERSION = 3\n")],
        )
        res3 = ws.apply_plan(plan3, backup=True)
        self.assertTrue(res3["success"])
        b2_id = res3["backup_id"]
        self.assertIsNotNone(b2_id)

        with open(f_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "VERSION = 3\n")

        # Verify backups list has 2 entries
        backups = ws.list_backups()
        self.assertEqual(len(backups), 2)
        b_ids = [b["backup_id"] for b in backups]
        self.assertIn(b1_id, b_ids)
        self.assertIn(b2_id, b_ids)

        # Rollback to b1 (VERSION 1)
        r_res = ws.restore_backup(b1_id)
        self.assertTrue(r_res["success"])
        self.assertIn("script.py", r_res["restored_files"])

        with open(f_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "VERSION = 1\n")


class TestAgentWebServerAPI(unittest.TestCase):
    """Tests for surgeon_web.py Agent Mode HTTP REST endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        import surgeon_web as web

        cls.tmp_dir = tempfile.mkdtemp(prefix="test_agent_api_")
        cls.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(cls.tmp_dir, "projects")

        cls.port = web._pick_port("127.0.0.1", 9960)
        cls.server = web._Server(("127.0.0.1", cls.port), web.SurgeonRequestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        agent.DEFAULT_PROJECTS_DIR = cls.orig_projects_dir
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def _post(self, path: str, data: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> tuple[int, bytes, str]:
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def test_api_projects_crud_and_state(self) -> None:
        # 1. Create project via POST /api/agent/projects
        proj_data = {"name": "web_test_proj", "runtime": "python", "description": "Web API Test"}
        res = self._post("/api/agent/projects", proj_data)
        self.assertTrue(res["success"])
        proj = res["project"]
        self.assertEqual(proj["name"], "web_test_proj")
        ws_dir = proj["path"]

        # 2. Get state via GET /api/agent/state
        q = urllib.parse.urlencode({"dir": ws_dir})
        st, body, _ = self._get(f"/api/agent/state?{q}")
        self.assertEqual(st, 200)
        state = json.loads(body.decode("utf-8"))
        self.assertTrue(state["exists"])
        self.assertEqual(state["workspace_root"], ws_dir)
        self.assertEqual(state["project"]["runtime"], "python")

        # 3. List projects via GET /api/agent/projects
        st, body, _ = self._get("/api/agent/projects")
        self.assertEqual(st, 200)
        p_list = json.loads(body.decode("utf-8"))
        names = [p["name"] for p in p_list.get("projects", [])]
        self.assertIn("web_test_proj", names)

    def test_api_generate_preview_apply_run_and_artifact(self) -> None:
        # Create project workspace
        p_res = self._post("/api/agent/projects", {"name": "data_app", "runtime": "python"})
        ws_dir = p_res["project"]["path"]

        # 1. POST /api/agent/generate
        gen = self._post("/api/agent/generate", {"goal": "Write report.json", "dir": ws_dir, "runtime": "python"})
        self.assertIn("prompt", gen)
        self.assertIn("Write report.json", gen["prompt"])

        # 2. POST /api/agent/preview
        ai_resp = """
<EXPLANATION>Generate report data.</EXPLANATION>
@@FILE generate.py
<<<
import json
with open("report.json", "w") as f:
    json.dump({"status": "complete", "value": 42}, f)
print("Report generated successfully!")
>>>
@@RUN python generate.py
@@ARTIFACT report.json
"""
        prev = self._post("/api/agent/preview", {"response": ai_resp, "dir": ws_dir})
        self.assertEqual(len(prev["changes"]), 1)
        self.assertEqual(prev["run_command"], "python generate.py")

        # 3. POST /api/agent/apply
        apply_res = self._post("/api/agent/apply", {"response": ai_resp, "dir": ws_dir, "execute": True})
        self.assertTrue(apply_res["success"])
        self.assertIsNotNone(apply_res["execution_result"])
        self.assertTrue(apply_res["execution_result"]["success"])
        self.assertIn("report.json", apply_res["execution_result"]["artifacts_found"])

        # 4. POST /api/agent/run (manual command execution)
        run_res = self._post("/api/agent/run", {"command": "python generate.py", "dir": ws_dir})
        self.assertTrue(run_res["execution_result"]["success"])

        # 5. GET /api/agent/artifact (download)
        q_art = urllib.parse.urlencode({"dir": ws_dir, "file": "report.json"})
        st, body, ctype = self._get(f"/api/agent/artifact?{q_art}")
        self.assertEqual(st, 200)
        self.assertIn("application/json", ctype)
        self.assertIn(b'"status": "complete"', body)


class TestEndToEndAutonomousAgentLoop(unittest.TestCase):
    """Full End-to-End Simulation of Autonomous Coding Loop with Iterative Edits & Rollback."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_agent_e2e_")
        self.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(self.tmp_dir, "projects")

    def tearDown(self) -> None:
        agent.DEFAULT_PROJECTS_DIR = self.orig_projects_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_autonomous_powerpoint_builder_lifecycle(self) -> None:
        # Phase 1: User creates a Safe Project
        proj = ProjectManager.create_project("powerpoint_generator", runtime="python", description="Automated PPT bot")
        ws_dir = proj["path"]
        ws = AgentWorkspace(ws_dir)

        # Phase 2: User defines goal -> Generates Prompt
        goal = "Write a python script that builds presentation.pptx with 3 slides and run it"
        prompt = AgentPromptBuilder.build_task_prompt(goal=goal, workspace_root=ws_dir, runtime="python")
        self.assertIn("SURGEON AGENT PROTOCOL", prompt)
        self.assertIn(goal, prompt)

        # Phase 3: Simulated LLM returns multi-file plan
        llm_reply_round1 = """
<EXPLANATION>
Creating presentation builder script that outputs presentation.pptx.
</EXPLANATION>

@@FILE make_ppt.py
<<<
import sys

def create_presentation():
    # 3-slide presentation generator
    slides = ["Title: AI Revolution", "Slide 2: Deep Learning", "Slide 3: Conclusion"]
    with open("presentation.pptx", "wb") as f:
        f.write(b"PK\\x03\\x04" + "\\n".join(slides).encode("utf-8"))
    print(f"Generated {len(slides)} slides successfully.")

if __name__ == "__main__":
    create_presentation()
>>>

@@RUN python make_ppt.py
@@ARTIFACT presentation.pptx
"""
        plan1 = parse_agent_response(llm_reply_round1)
        apply1 = ws.apply_plan(plan1, backup=True)
        self.assertTrue(apply1["success"])
        b1_id = apply1["backup_id"]

        exec1 = ws.execute_command(plan1.run_command, expected_artifacts=plan1.expected_artifacts)
        self.assertTrue(exec1.success)
        self.assertEqual(exec1.exit_code, 0)
        self.assertIn("Generated 3 slides", exec1.stdout)
        self.assertIn("presentation.pptx", exec1.artifacts_found)

        # Phase 4: Generate Verification Prompt for next round
        verif_prompt1 = AgentPromptBuilder.build_verification_prompt(
            goal=goal, plan=plan1, result=exec1, workspace_root=ws_dir
        )
        self.assertIn("EXECUTION SUCCEEDED", verif_prompt1)
        self.assertIn("presentation.pptx", verif_prompt1)

        # Phase 5: Simulated LLM Round 2 (Surgically adds a 4th slide)
        llm_reply_round2 = """
<EXPLANATION>
Adding a 4th slide for Summary & Future Work.
</EXPLANATION>

@@EDIT make_ppt.py anchor
START-ANCHOR: slides = ["Title: AI Revolution",
END-ANCHOR: "Slide 3: Conclusion"]
<<<
slides = ["Title: AI Revolution", "Slide 2: Deep Learning", "Slide 3: Conclusion", "Slide 4: Future Work"]
>>>

@@RUN python make_ppt.py
@@ARTIFACT presentation.pptx
"""
        plan2 = parse_agent_response(llm_reply_round2)
        apply2 = ws.apply_plan(plan2, backup=True)
        self.assertTrue(apply2["success"])
        b2_id = apply2["backup_id"]

        exec2 = ws.execute_command(plan2.run_command, expected_artifacts=plan2.expected_artifacts)
        self.assertTrue(exec2.success)
        self.assertIn("Generated 4 slides", exec2.stdout)

        # Phase 6: User wants to rollback to 3-slide version before round 2
        rollback = ws.restore_backup(b2_id)
        self.assertTrue(rollback["success"])
        self.assertIn("make_ppt.py", rollback["restored_files"])

        # Re-run after rollback to verify 3 slides
        exec_after_rollback = ws.execute_command(plan1.run_command)
        self.assertTrue(exec_after_rollback.success)
        self.assertIn("Generated 3 slides", exec_after_rollback.stdout)


class TestTextSurgeonCLI(unittest.TestCase):
    """Tests for text_surgeon.py CLI project and backup flags."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_cli_agent_")
        self.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(self.tmp_dir, "projects")

    def tearDown(self) -> None:
        agent.DEFAULT_PROJECTS_DIR = self.orig_projects_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_project_management(self) -> None:
        import text_surgeon as ts

        # 1. Create project
        code = ts.main(["--new-project", "cli_powerpoint", "--runtime", "python"])
        self.assertEqual(code, 0)

        # 2. List projects
class TestEnvManager(unittest.TestCase):
    """Unit tests for EnvManager (.env parsing and saving)."""

    def test_parse_and_format_env(self) -> None:
        raw_env = (
            "# Database settings\n"
            "DB_HOST=localhost\n"
            "DB_PORT=5432\n"
            "# Auth\n"
            'API_KEY="secret_key_123"\n'
            "EMPTY_VAL=\n"
            "SPACED_KEY = value with spaces \n"
        )
        parsed = agent.EnvManager.parse_env(raw_env)
        self.assertEqual(parsed["DB_HOST"], "localhost")
        self.assertEqual(parsed["DB_PORT"], "5432")
        self.assertEqual(parsed["API_KEY"], "secret_key_123")
        self.assertEqual(parsed["SPACED_KEY"], "value with spaces")
        self.assertEqual(parsed["EMPTY_VAL"], "")

        formatted = agent.EnvManager.format_env(parsed)
        reparsed = agent.EnvManager.parse_env(formatted)
        self.assertEqual(reparsed, parsed)

    def test_load_and_save_env(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="test_env_")
        try:
            ws = AgentWorkspace(tmp_dir)
            self.assertEqual(ws.get_env(), {})

            ws.set_env({"OPENAI_API_KEY": "sk-12345", "PORT": "8000"})
            loaded = ws.get_env()
            self.assertEqual(loaded["OPENAI_API_KEY"], "sk-12345")
            self.assertEqual(loaded["PORT"], "8000")
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, ".env")))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestProjectExportZip(unittest.TestCase):
    """Unit tests for export_project_zip."""

    def test_export_zip_content_and_exclusions(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="test_zip_")
        try:
            # Create project structure
            ws = AgentWorkspace(tmp_dir)
            ws.write_file("main.py", "print('hello world')")
            ws.write_file("data/sample.txt", "sample text content")
            ws.write_file(".surgeon/project.json", '{"name": "test"}')
            os.makedirs(os.path.join(tmp_dir, "__pycache__"), exist_ok=True)
            with open(os.path.join(tmp_dir, "__pycache__", "cache.pyc"), "w") as f:
                f.write("bin")

            zip_bytes = ws.export_zip()
            self.assertIsInstance(zip_bytes, bytes)
            self.assertGreater(len(zip_bytes), 0)

            # Open with zipfile and inspect contents
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                namelist = zf.namelist()
                self.assertIn("main.py", namelist)
                self.assertIn("data/sample.txt", namelist)
                # Excluded directories
                for item in namelist:
                    self.assertFalse(item.startswith(".surgeon"))
                    self.assertFalse(item.startswith("__pycache__"))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestWorkspaceFileCRUD(unittest.TestCase):
    """Unit tests for workspace file CRUD."""

    def test_crud_operations(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="test_crud_")
        try:
            ws = AgentWorkspace(tmp_dir)
            # Write
            res = ws.write_file("src/app.py", "x = 42\n")
            self.assertTrue(res["success"])
            self.assertEqual(res["path"], "src/app.py")

            # Read
            content = ws.read_file("src/app.py")
            self.assertEqual(content, "x = 42\n")

            # Delete
            deleted = ws.delete_file("src/app.py")
            self.assertTrue(deleted)
            self.assertFalse(os.path.isfile(os.path.join(tmp_dir, "src", "app.py")))

            # Delete non-existent
            deleted2 = ws.delete_file("non_existent.py")
            self.assertFalse(deleted2)

            # Read non-existent raises AgentError
            with self.assertRaises(agent.AgentError):
                ws.read_file("non_existent.py")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestProjectTemplates(unittest.TestCase):
    """Unit tests for ProjectManager template starters."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_templates_")
        self.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(self.tmp_dir, "projects")

    def tearDown(self) -> None:
        agent.DEFAULT_PROJECTS_DIR = self.orig_projects_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_powerpoint_template(self) -> None:
        proj = ProjectManager.create_project("my_ppt", runtime="powerpoint")
        self.assertEqual(proj["runtime"], "powerpoint")
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "make_presentation.py")))
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "requirements.txt")))

    def test_scraper_template(self) -> None:
        proj = ProjectManager.create_project("my_scraper", runtime="web_scraper")
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "scraper.py")))

    def test_data_analyzer_template(self) -> None:
        proj = ProjectManager.create_project("my_data", runtime="data_analyzer")
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "analyze_data.py")))

    def test_rest_api_template(self) -> None:
        proj = ProjectManager.create_project("my_api", runtime="rest_api")
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "server.py")))

    def test_html_dashboard_template(self) -> None:
        proj = ProjectManager.create_project("my_dashboard", runtime="html_dashboard")
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "style.css")))
        self.assertTrue(os.path.isfile(os.path.join(proj["path"], "app.js")))


class TestAutoPilotEngine(unittest.TestCase):
    """Unit tests for AutoPilotEngine multi-round loops."""

    def test_autopilot_self_repair_success(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="test_autopilot_")
        try:
            ws = AgentWorkspace(tmp_dir)

            # Round 1 returns buggy code, Round 2 fixes it
            responses = [
                # Round 1: buggy division by zero
                """
<EXPLANATION>Attempt 1</EXPLANATION>
@@FILE script.py
<<<
print("Running step 1")
x = 1 / 0
>>>
@@RUN python script.py
""",
                # Round 2: fixed
                """
<EXPLANATION>Fixed division by zero</EXPLANATION>
@@FILE script.py
<<<
print("Running step 1")
x = 1 / 1
print("Success: x =", x)
with open("done.txt", "w") as f:
    f.write("OK")
>>>
@@RUN python script.py
@@ARTIFACT done.txt
""",
            ]

            resp_iter = iter(responses)

            def mock_send_prompt(prompt, config, system_prompt=None):
                return next(resp_iter)

            orig_send = agent.LLMClient.send_prompt
            agent.LLMClient.send_prompt = mock_send_prompt
            try:
                cfg = agent.LLMConfig(provider="ollama", model="llama3")
                res = agent.AutoPilotEngine.run_autonomous_loop(
                    workspace_root=tmp_dir,
                    goal="Calculate division safely and write done.txt",
                    llm_config=cfg,
                    runtime="python",
                    max_rounds=3,
                )
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["rounds_completed"], 2)
                self.assertIn("done.txt", res["artifacts"])
                self.assertEqual(len(res["rounds"]), 2)

                # History should be stored
                history = ws.get_round_history()
                self.assertEqual(len(history), 2)
            finally:
                agent.LLMClient.send_prompt = orig_send
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestWebAgentExtendedAPI(unittest.TestCase):
    """Unit tests for new Agent Web API endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = tempfile.mkdtemp(prefix="test_agent_web_ext_")
        cls.orig_projects_dir = agent.DEFAULT_PROJECTS_DIR
        agent.DEFAULT_PROJECTS_DIR = os.path.join(cls.tmp_dir, "projects")

        import surgeon_web as web
        cls.port = web._pick_port(web.DEFAULT_HOST, 9800)
        cls.server = web._Server((web.DEFAULT_HOST, cls.port), web.SurgeonRequestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        agent.DEFAULT_PROJECTS_DIR = cls.orig_projects_dir
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> Tuple[int, bytes, str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def test_file_crud_api(self) -> None:
        proj = self._post("/api/agent/projects", {"name": "crud_api_proj"})
        ws_dir = proj["project"]["path"]

        # 1. Write file
        w_res = self._post("/api/agent/file", {"dir": ws_dir, "file": "utils.py", "content": "def add(a, b): return a + b\n"})
        self.assertTrue(w_res["success"])

        # 2. Read file
        q = urllib.parse.urlencode({"dir": ws_dir, "file": "utils.py"})
        st, body, ctype = self._get(f"/api/agent/file?{q}")
        self.assertEqual(st, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["file"], "utils.py")
        self.assertIn("def add", data["content"])

        # 3. Delete file
        del_res = self._post("/api/agent/file/delete", {"dir": ws_dir, "file": "utils.py"})
        self.assertTrue(del_res["success"])
        self.assertTrue(del_res["deleted"])

    def test_env_api(self) -> None:
        proj = self._post("/api/agent/projects", {"name": "env_api_proj"})
        ws_dir = proj["project"]["path"]

        # POST env
        post_env = self._post("/api/agent/env", {"dir": ws_dir, "env": {"PORT": "9090", "API_KEY": "test_key"}})
        self.assertTrue(post_env["success"])
        self.assertEqual(post_env["env"]["PORT"], "9090")

        # GET env
        q = urllib.parse.urlencode({"dir": ws_dir})
        st, body, _ = self._get(f"/api/agent/env?{q}")
        self.assertEqual(st, 200)
        get_env_data = json.loads(body.decode("utf-8"))
        self.assertEqual(get_env_data["env"]["PORT"], "9090")

    def test_export_zip_api(self) -> None:
        proj = self._post("/api/agent/projects", {"name": "export_api_proj"})
        ws_dir = proj["project"]["path"]

        q = urllib.parse.urlencode({"dir": ws_dir})
        st, body, ctype = self._get(f"/api/agent/export?{q}")
        self.assertEqual(st, 200)
        self.assertEqual(ctype, "application/zip")
        self.assertGreater(len(body), 50)

    def test_web_api_security_path_traversal_blocked(self) -> None:
        proj = self._post("/api/agent/projects", {"name": "sec_test_proj"})
        ws_dir = proj["project"]["path"]

        # Traversal in file read
        q = urllib.parse.urlencode({"dir": ws_dir, "file": "../../secret.txt"})
        try:
            st, body, _ = self._get(f"/api/agent/file?{q}")
            self.assertIn(st, (400, 422))
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 422))

        # Traversal in file delete
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/agent/file/delete",
                data=json.dumps({"dir": ws_dir, "file": "../outside.py"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                pass
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 422))


class TestUIAndLocalizationIntegrity(unittest.TestCase):
    """Verifies that surgeon_ui.html has zero broken element IDs and complete translations."""

    @classmethod
    def setUpClass(cls) -> None:
        html_path = os.path.join(os.path.dirname(__file__), "surgeon_ui.html")
        with open(html_path, "r", encoding="utf-8") as fh:
            cls.html_content = fh.read()

    def test_all_js_dom_ids_exist_in_html(self) -> None:
        """Ensures every document.getElementById / $(id) referenced in JS exists in HTML markup."""
        # Extract all $(...) and document.getElementById(...)
        dollar_calls = set(re.findall(r'\$\("([^"]+)"\)', self.html_content))
        get_el_calls = set(re.findall(r'getElementById\("([^"]+)"\)', self.html_content))
        all_referenced_ids = dollar_calls | get_el_calls

        # Extract all id="..." from HTML
        declared_ids = set(re.findall(r'id=["\']([^"\']+)["\']', self.html_content))

        # Check for any missing IDs
        missing = all_referenced_ids - declared_ids
        # Exclude dynamic IDs constructed at runtime if any
        missing = {i for i in missing if not i.startswith("${") and not i.startswith("' +")}
        self.assertEqual(missing, set(), f"JavaScript references IDs not declared in HTML: {missing}")

    def test_i18n_dictionaries_completeness(self) -> None:
        """Verifies that every data-i18n attribute is defined in both English and Persian dictionaries."""
        # Find I18N JS object block
        i18n_match = re.search(r'var I18N\s*=\s*\{([\s\S]*?)\n\};', self.html_content)
        self.assertIsNotNone(i18n_match, "I18N dictionary block not found in surgeon_ui.html")
        i18n_block = i18n_match.group(1)

        # Parse en and fa key blocks
        en_match = re.search(r'en:\s*\{([\s\S]*?)\n\s*\},', i18n_block)
        fa_match = re.search(r'fa:\s*\{([\s\S]*?)\n\s*\}', i18n_block)
        self.assertIsNotNone(en_match, "en dictionary not found")
        self.assertIsNotNone(fa_match, "fa dictionary not found")

        def _extract_keys(js_obj_str: str) -> Set[str]:
            keys = set()
            for m in re.finditer(
                r'(?:^|[,{\n])\s*([a-zA-Z0-9_]+)\s*:\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'|\d+|true|false)',
                js_obj_str,
            ):
                keys.add(m.group(1))
            return keys

        en_keys = _extract_keys(en_match.group(1))
        fa_keys = _extract_keys(fa_match.group(1))

        # 1. En and Fa should have 100% key parity
        diff_en_fa = en_keys ^ fa_keys
        self.assertEqual(diff_en_fa, set(), f"Keys mismatch between en and fa: {diff_en_fa}")

        # 2. All data-i18n and data-i18n-ph in HTML must exist in dictionaries
        used_i18n = set(re.findall(r'data-i18n=["\']([^"\']+)["\']', self.html_content))
        used_i18n_ph = set(re.findall(r'data-i18n-ph=["\']([^"\']+)["\']', self.html_content))
        all_used = used_i18n | used_i18n_ph

        missing_translations = all_used - en_keys
        self.assertEqual(
            missing_translations,
            set(),
            f"HTML tags use data-i18n keys not defined in I18N dictionary: {missing_translations}",
        )


class TestEnvManagerEdgeCases(unittest.TestCase):
    """Tests edge cases for .env parsing, formatting, and atomic persistence."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_env_mgr_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_parse_env_complex(self) -> None:
        raw = """
        # Database Settings
        DB_HOST=127.0.0.1
        DB_PORT=5432
        DB_URL="postgres://user:p@ss=word@localhost:5432/mydb?sslmode=disable"
        
        # API Keys & Secrets
        API_KEY='sk-12345#secret'
        EMPTY_VAL=
        SPACED_VAL = hello world 
        COMMENT_LINE_ONLY
        """
        env = agent.EnvManager.parse_env(raw)
        self.assertEqual(env["DB_HOST"], "127.0.0.1")
        self.assertEqual(env["DB_PORT"], "5432")
        self.assertEqual(env["DB_URL"], "postgres://user:p@ss=word@localhost:5432/mydb?sslmode=disable")
        self.assertEqual(env["API_KEY"], "sk-12345#secret")
        self.assertEqual(env["SPACED_VAL"], "hello world")

    def test_save_and_load_roundtrip(self) -> None:
        original = {
            "APP_NAME": "Text Surgeon",
            "PORT": "8080",
            "SECRET_KEY": "super secret key with spaces and #hash",
        }
        agent.EnvManager.save(self.tmp_dir, original)
        loaded = agent.EnvManager.load(self.tmp_dir)
        self.assertEqual(loaded, original)


class TestProjectZipExporterIntegrity(unittest.TestCase):
    """Tests project ZIP archive packaging, exclusion rules, and unpack fidelity."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_zip_mgr_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_export_and_unzip_fidelity(self) -> None:
        # Create structure
        os.makedirs(os.path.join(self.tmp_dir, "src", "utils"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp_dir, ".surgeon", "backups"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp_dir, "__pycache__"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp_dir, ".git"), exist_ok=True)

        with open(os.path.join(self.tmp_dir, "main.py"), "w", encoding="utf-8") as fh:
            fh.write("print('Hello World! 🚀')\n")
        with open(os.path.join(self.tmp_dir, "src", "utils", "calc.py"), "w", encoding="utf-8") as fh:
            fh.write("def add(x, y): return x + y\n")
        with open(os.path.join(self.tmp_dir, ".surgeon", "backups", "old.bak"), "w", encoding="utf-8") as fh:
            fh.write("backup data")
        with open(os.path.join(self.tmp_dir, "__pycache__", "temp.pyc"), "w", encoding="utf-8") as fh:
            fh.write("cache data")

        zip_bytes = agent.export_project_zip(self.tmp_dir)
        self.assertGreater(len(zip_bytes), 100)

        # Inspect ZIP contents
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            namelist = zf.namelist()
            self.assertIn("main.py", namelist)
            self.assertIn("src/utils/calc.py", namelist)
            # Excluded directories
            for name in namelist:
                self.assertNotIn(".surgeon", name)
                self.assertNotIn("__pycache__", name)
                self.assertNotIn(".git", name)

            # Verify UTF-8 content
            content = zf.read("main.py").decode("utf-8")
            self.assertIn("Hello World! 🚀", content)


class TestLLMClientMockProviders(unittest.TestCase):
    """Tests LLMClient provider formatting and error parsing against local mock HTTP endpoints."""

    def test_openai_compatible_call(self) -> None:
        mock_response = json.dumps({
            "choices": [{"message": {"content": "<EXPLANATION>Done</EXPLANATION>\n@@FILE app.py\n<<<print(1)>>>"}}]
        }).encode("utf-8")

        # Mock urllib.request.urlopen
        class MockHTTPResponse:
            status = 200
            def read(self) -> bytes:
                return mock_response
            def __enter__(self) -> "MockHTTPResponse":
                return self
            def __exit__(self, *args: Any) -> None:
                pass

        orig_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=None: MockHTTPResponse()
            config = agent.LLMConfig(provider="openai", api_key="sk-test", model="gpt-4o")
            reply = agent.LLMClient.send_prompt("Build an app", config)
            self.assertIn("@@FILE app.py", reply)
        finally:
            urllib.request.urlopen = orig_urlopen

    def test_anthropic_call(self) -> None:
        mock_response = json.dumps({
            "content": [{"text": "<EXPLANATION>Claude</EXPLANATION>\n@@FILE bot.py\n<<<pass>>>"}]
        }).encode("utf-8")

        class MockHTTPResponse:
            status = 200
            def read(self) -> bytes:
                return mock_response
            def __enter__(self) -> "MockHTTPResponse":
                return self
            def __exit__(self, *args: Any) -> None:
                pass

        orig_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=None: MockHTTPResponse()
            config = agent.LLMConfig(provider="anthropic", api_key="sk-ant", model="claude-3-5-sonnet-20241022")
            reply = agent.LLMClient.send_prompt("Build a bot", config)
            self.assertIn("@@FILE bot.py", reply)
        finally:
            urllib.request.urlopen = orig_urlopen

    def test_gemini_call(self) -> None:
        mock_response = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "<EXPLANATION>Gemini</EXPLANATION>\n@@FILE gem.py\n<<<pass>>>"}]}}]
        }).encode("utf-8")

        class MockHTTPResponse:
            status = 200
            def read(self) -> bytes:
                return mock_response
            def __enter__(self) -> "MockHTTPResponse":
                return self
            def __exit__(self, *args: Any) -> None:
                pass

        orig_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=None: MockHTTPResponse()
            config = agent.LLMConfig(provider="gemini", api_key="ai-key", model="gemini-1.5-pro")
            reply = agent.LLMClient.send_prompt("Build gemini", config)
            self.assertIn("@@FILE gem.py", reply)
        finally:
            urllib.request.urlopen = orig_urlopen


class TestSkillManager(unittest.TestCase):
    """Tests for discovering, parsing, matching, and injecting modular skills."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_skills_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_builtin_skills_discovered(self) -> None:
        skills = agent.SkillManager.discover_skills()
        names = [s.name for s in skills]
        self.assertIn("powerpoint_maker", names)
        self.assertIn("excel_data_analyst", names)
        self.assertIn("web_scraper", names)

    def test_custom_skill_creation_and_discovery(self) -> None:
        created = agent.SkillManager.create_custom_skill(
            workspace_root=self.tmp_dir,
            name="sound_effects",
            title="Audio & Sound Effects Generator",
            description="Generates synth sounds and audio effects using pydub",
            keywords=["sound", "audio", "wav", "mp3", "synth"],
            packages=["pydub", "scipy"],
            content="Use pydub to generate 440Hz sine wave tones.",
        )
        self.assertEqual(created.name, "sound_effects")
        self.assertEqual(created.source, "custom")

        discovered = agent.SkillManager.discover_skills(self.tmp_dir)
        names = [s.name for s in discovered]
        self.assertIn("sound_effects", names)

        # Retrieve specific skill
        skill = agent.SkillManager.get_skill("sound_effects", self.tmp_dir)
        self.assertIsNotNone(skill)
        self.assertIn("pydub", skill.packages)
        self.assertIn("audio", skill.keywords)

    def test_keyword_matching(self) -> None:
        # Match PPTX prompt to powerpoint_maker
        matches = agent.SkillManager.match_skills_to_goal("Make a 5-slide PowerPoint deck with python-pptx")
        names = [s.name for s in matches]
        self.assertTrue("powerpoint_maker" in names or "officecli" in names)

        # Match CSV pandas prompt to excel_data_analyst
        matches = agent.SkillManager.match_skills_to_goal("Read sales.csv, calculate mean and plot charts")
        names = [s.name for s in matches]
        self.assertIn("excel_data_analyst", names)

        # Match scraper prompt to web_scraper
        matches = agent.SkillManager.match_skills_to_goal("Scrape prices from website using beautifulsoup4")
        names = [s.name for s in matches]
        self.assertIn("web_scraper", names)

    def test_prompt_injection_with_skills(self) -> None:
        builder = agent.AgentPromptBuilder()
        prompt = builder.build_task_prompt(
            goal="Create an excel sheet with openpyxl",
            workspace_root=self.tmp_dir,
            skill_names=["powerpoint_maker"],
        )
        self.assertIn("SPECIALIZED SKILLS & DOMAIN GUIDELINES", prompt)
        self.assertIn("PowerPoint Presentation Deck Maker", prompt)
        self.assertIn("python-pptx", prompt)



class TestKeyManager(unittest.TestCase):
    """Tests for multi-key rotation, failover, and health telemetry."""

    def test_parse_keys(self) -> None:
        raw = "sk-key1, sk-key2\nsk-key3; sk-key4"
        keys = agent.KeyManager.parse_keys(raw)
        self.assertEqual(keys, ["sk-key1", "sk-key2", "sk-key3", "sk-key4"])

    def test_key_rotation_and_failover(self) -> None:
        keys = ["sk-k1", "sk-k2", "sk-k3"]
        k1 = agent.KeyManager.get_active_key(keys)
        self.assertEqual(k1, "sk-k1")

        # Simulate 429 Rate Limit on k1
        agent.KeyManager.record_key_status("sk-k1", 429, cooldown_sec=10)
        
        # Next active key should automatically fail over to k2
        k2 = agent.KeyManager.get_active_key(keys)
        self.assertEqual(k2, "sk-k2")

        # Simulate 401 Invalid Key on k2
        agent.KeyManager.record_key_status("sk-k2", 401)

        # Next active key should fail over to k3
        k3 = agent.KeyManager.get_active_key(keys)
        self.assertEqual(k3, "sk-k3")

        # Verify pool status
        status = agent.KeyManager.get_pool_status(keys)
        self.assertEqual(status["total"], 3)
        self.assertEqual(status["ready"], 1)
        self.assertEqual(status["cooldown"], 1)
        self.assertEqual(status["invalid"], 1)


class TestAutoPilotEngineAdvanced(unittest.TestCase):
    """Tests for multi-round autonomous loop and automatic pip dependency repair."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_ap_adv_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_autopilot_self_repair_loop(self) -> None:
        # Mock LLM that returns a buggy script in round 1 and fixes it in round 2
        calls = []

        def mock_send(prompt: str, config: agent.LLMConfig, system_prompt: Optional[str] = None) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                # Round 1: buggy script that raises ZeroDivisionError
                return """
<EXPLANATION>Initial attempt</EXPLANATION>
@@FILE main.py
<<<
def compute():
    return 10 / 0

if __name__ == '__main__':
    compute()
>>>
@@COMMAND python main.py
"""
            else:
                # Round 2: fixed script
                return """
<EXPLANATION>Fixed zero division error</EXPLANATION>
@@FILE main.py
<<<
def compute():
    return 10 / 2

if __name__ == '__main__':
    result = compute()
    print(f"Computed: {result}")
    with open("result.txt", "w") as f:
        f.write(str(result))
>>>
@@COMMAND python main.py
"""

        orig_send = agent.LLMClient.send_prompt
        try:
            agent.LLMClient.send_prompt = mock_send
            res = agent.AutoPilotEngine.run_autonomous_loop(
                workspace_root=self.tmp_dir,
                goal="Compute division safely and write result.txt",
                llm_config=agent.LLMConfig(provider="ollama", model="llama3"),
                runtime="python",
                max_rounds=3,
            )

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["rounds_completed"], 2)
            self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "result.txt")))
            with open(os.path.join(self.tmp_dir, "result.txt")) as f:
                self.assertEqual(f.read().strip(), "5.0")
        finally:
            agent.LLMClient.send_prompt = orig_send


class TestWebAPISkillsAndKeys(unittest.TestCase):
    """Tests for Web API REST endpoints supporting skills, keys, and packages."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_web_sk_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_skills_api_and_key_status(self) -> None:
        import surgeon_web as web

        port = web._pick_port(web.DEFAULT_HOST, 9850)
        server = web._Server((web.DEFAULT_HOST, port), web.SurgeonRequestHandler)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        time.sleep(0.3)
        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. GET /api/agent/skills
            req = urllib.request.urlopen(f"{base_url}/api/agent/skills")
            skills_res = json.loads(req.read().decode("utf-8"))
            self.assertGreaterEqual(skills_res["total"], 3)

            # 2. POST /api/agent/skills/detect
            post_data = json.dumps({"goal": "Make a presentation with python-pptx"}).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/agent/skills/detect",
                data=post_data,
                headers={"Content-Type": "application/json"},
            )
            resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
            matched_names = [s["name"] for s in resp.get("matched_skills", [])]
            self.assertTrue("powerpoint_maker" in matched_names or "officecli" in matched_names)

            # 3. POST /api/agent/keys/status
            post_data = json.dumps({"api_keys": ["sk-test1", "sk-test2"]}).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/agent/keys/status",
                data=post_data,
                headers={"Content-Type": "application/json"},
            )
            key_res = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
            self.assertEqual(key_res["summary"]["total"], 2)
            self.assertEqual(key_res["summary"]["ready"], 2)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()



