"""Constrained local workflow planner and subprocess runner.

The model can select step types and project-relative files, but cannot provide
shell command text. Uploaded programs are still executable code and must be
trusted before running; this is not an operating-system sandbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents import ask_model, json_from_model_text

MAX_STEP_SECONDS = 90
MAX_LOG_CHARS = 60_000
STEP_TYPES = {"install", "syntax", "test", "run"}


def detect_languages(root: Path) -> list[str]:
    extensions = {p.suffix.lower() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}
    languages = []
    if ".py" in extensions:
        languages.append("python")
    if extensions & {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
        languages.append("javascript")
    if ".go" in extensions:
        languages.append("go")
    if ".rs" in extensions:
        languages.append("rust")
    if ".java" in extensions:
        languages.append("java")
    return languages or ["unknown"]


def project_files(root: Path, extensions: set[str] | None = None) -> list[str]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if extensions and path.suffix.lower() not in extensions:
            continue
        files.append(path.relative_to(root).as_posix())
        if len(files) >= 250:
            break
    return sorted(files)


def default_plan(root: Path) -> list[dict[str, str]]:
    languages = detect_languages(root)
    plan = []
    if languages[0] == "python":
        if (root / "requirements.txt").is_file():
            plan.append({"type": "install", "target": "requirements.txt", "label": "Install Python dependencies"})
        plan.append({"type": "syntax", "target": "*.py", "label": "Compile Python files"})
        if any((root / name).exists() for name in ("test", "tests")):
            plan.append({"type": "test", "target": "pytest", "label": "Run Python tests"})
        sources = project_files(root, {".py"})
        entry = next((name for name in ("main.py", "app.py", "manage.py") if (root / name).is_file()), None)
        if not entry and len(sources) == 1:
            entry = sources[0]
        if entry:
            plan.append({"type": "run", "target": entry, "label": f"Run {entry}"})
    elif languages[0] == "javascript":
        if (root / "package.json").is_file():
            plan.append({"type": "install", "target": "package.json", "label": "Install Node dependencies"})
        js_files = project_files(root, {".js", ".mjs", ".cjs"})
        if js_files:
            plan.append({"type": "syntax", "target": js_files[0], "label": "Check JavaScript syntax"})
        if (root / "package.json").is_file():
            plan.append({"type": "test", "target": "npm-test", "label": "Run npm tests"})
        entry = next((name for name in ("index.js", "server.js", "app.js", "main.js") if (root / name).is_file()), None)
        if not entry and len(js_files) == 1:
            entry = js_files[0]
        if entry:
            plan.append({"type": "run", "target": entry, "label": f"Run {entry}"})
    else:
        plan.append({"type": "syntax", "target": "none", "label": "No supported syntax check"})
    return plan


async def make_plan(root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    fallback = default_plan(root)
    prompt = {
        "languages": detect_languages(root),
        "files": project_files(root),
        "default_steps": fallback,
        "allowed_types": sorted(STEP_TYPES),
        "format": {"steps": [{"type": "install|syntax|test|run", "target": "a listed safe target", "label": "short label"}]},
    }
    try:
        text, _ = await ask_model(
            "workflow_runner",
            [
                {"role": "system", "content": (
                    "You are a workflow planner. Return JSON only, with at most 8 steps. "
                    "Use only allowed step types and exact targets from the supplied project metadata. "
                    "Never return commands, shell text, options, URLs, or scripts. Include syntax, tests "
                    "when available, and one run step only when a supported entry file is listed."
                )},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            config,
        )
        raw = json_from_model_text(text)
        raw_steps = raw.get("steps", []) if isinstance(raw, dict) else []
        valid_targets = set(project_files(root)) | {"*.py", "pytest", "npm-test", "none"}
        result = []
        for item in raw_steps[:8]:
            if not isinstance(item, dict):
                continue
            kind, target = str(item.get("type", "")), str(item.get("target", ""))
            if kind not in STEP_TYPES or target not in valid_targets:
                continue
            result.append({
                "type": kind, "target": target,
                "label": re.sub(r"[^A-Za-z0-9 ._()/-]", "", str(item.get("label", kind)))[:80] or kind,
            })
        if result and sum(step["type"] == "run" for step in result) <= 1:
            return result
    except Exception:
        pass
    return fallback


def _requirements(root: Path) -> list[str]:
    req = root / "requirements.txt"
    if not req.is_file() or req.stat().st_size > 64_000:
        return []
    pattern = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]{1,2}[A-Za-z0-9.*+!_-]+)?$"
    )
    packages = []
    for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and pattern.fullmatch(line):
            packages.append(line)
        elif line:
            raise ValueError("requirements.txt contains a URL, pip option, or unsupported line. Remove it before installing.")
    if len(packages) > 50:
        raise ValueError("requirements.txt contains more than 50 package entries.")
    return packages


def _project_python(root: Path) -> str:
    candidate = root / ".codepilot-venv" / "bin" / "python"
    if candidate.is_file() and not candidate.is_symlink():
        return str(candidate)
    return sys.executable


def _command_for(step: dict[str, str], root: Path) -> list[str] | None:
    kind, target = step["type"], step["target"]
    if kind == "install":
        if target == "requirements.txt" and (root / target).is_file():
            packages = _requirements(root)
            if not packages:
                return None
            create_and_install = (
                "import pathlib,subprocess,sys,venv;"
                "env=pathlib.Path('.codepilot-venv');"
                "venv.create(env,with_pip=True);"
                "py=env/'bin'/'python';"
                "sys.exit(subprocess.call([str(py),'-m','pip','install',"
                "'--disable-pip-version-check','--no-input','--only-binary=:all:',*sys.argv[1:]]))"
            )
            return [sys.executable, "-c", create_and_install, *packages]
        if target == "package.json" and (root / target).is_file():
            return ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
    if kind == "syntax":
        if target == "*.py":
            files = project_files(root, {".py"})
            return [_project_python(root), "-m", "py_compile", *files] if files else None
        if target not in ("none", "pytest", "npm-test") and (root / target).suffix.lower() in {".js", ".mjs", ".cjs"}:
            return ["node", "--check", target]
        return None
    if kind == "test":
        if target == "pytest" and (root / "tests").exists():
            return [_project_python(root), "-m", "pytest", "-q"]
        if target == "npm-test" and (root / "package.json").is_file():
            return ["npm", "test", "--", "--runInBand"]
        return None
    if kind == "run":
        candidate = (root / target).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        if candidate.suffix.lower() == ".py":
            return [_project_python(root), "-u", candidate.relative_to(root.resolve()).as_posix()]
        if candidate.suffix.lower() in {".js", ".mjs", ".cjs"}:
            return ["node", candidate.relative_to(root.resolve()).as_posix()]
    return None


def _child_environment(root: Path) -> dict[str, str]:
    """Pass only runtime essentials; never pass app/model credentials to code."""
    safe_names = {
        "PATH", "SYSTEMROOT", "WINDIR", "PREFIX", "ANDROID_ROOT", "ANDROID_DATA",
        "LANG", "LC_ALL", "TERM", "COLORTERM",
    }
    env = {key: os.environ[key] for key in safe_names if os.environ.get(key)}
    temp_dir = root / ".runtime-tmp"
    home_dir = root / ".runtime-home"
    temp_dir.mkdir(exist_ok=True)
    home_dir.mkdir(exist_ok=True)
    env.update({
        "HOME": str(home_dir),
        "TMPDIR": str(temp_dir),
        "TMP": str(temp_dir),
        "TEMP": str(temp_dir),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


async def run_workflow(
    root: Path,
    config: dict[str, Any],
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> str:
    plan = await make_plan(root, config)
    await emit({"event": "plan", "steps": plan, "languages": detect_languages(root)})
    all_logs = []
    for index, step in enumerate(plan):
        step_id = f"step-{index + 1}"
        command = _command_for(step, root)
        if command is None:
            await emit({"event": "step", "id": step_id, "label": step["label"], "status": "skipped",
                        "detail": "No safe command could be constructed for this step."})
            continue
        await emit({"event": "step", "id": step_id, "label": step["label"], "status": "running"})
        await emit({"event": "log", "step_id": step_id, "text": "$ " + " ".join(command) + "\n"})
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=str(root), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name != "nt"),
                env=_child_environment(root),
            )
        except FileNotFoundError:
            message = f"Required executable is not installed: {command[0]}\n"
            all_logs.append(message)
            await emit({"event": "log", "step_id": step_id, "text": message})
            await emit({"event": "step", "id": step_id, "label": step["label"], "status": "failed"})
            continue

        log_size = sum(map(len, all_logs))

        async def read_output() -> None:
            nonlocal log_size
            while proc.stdout:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                # Drain stdout even after the display cap so the child cannot
                # block forever on a full pipe.
                if log_size < MAX_LOG_CHARS:
                    allowed = text[:MAX_LOG_CHARS - log_size]
                    all_logs.append(allowed)
                    log_size += len(allowed)
                    await emit({"event": "log", "step_id": step_id, "text": allowed})

        try:
            await asyncio.wait_for(read_output(), timeout=MAX_STEP_SECONDS)
            if proc.returncode is None:
                await asyncio.wait_for(proc.wait(), timeout=MAX_STEP_SECONDS)
            status = "success" if proc.returncode == 0 else "failed"
        except asyncio.TimeoutError:
            await _kill_process(proc)
            await proc.wait()
            status = "failed"
            text = f"Step timed out after {MAX_STEP_SECONDS} seconds.\n"
            all_logs.append(text)
            await emit({"event": "log", "step_id": step_id, "text": text})
        except asyncio.CancelledError:
            if proc.returncode is None:
                await _kill_process(proc)
                await proc.wait()
            raise
        await emit({"event": "step", "id": step_id, "label": step["label"], "status": status})
        if sum(map(len, all_logs)) >= MAX_LOG_CHARS:
            await emit({"event": "log", "step_id": step_id, "text": "\n[Log limit reached.]\n"})
            break
    return "".join(all_logs)[-MAX_LOG_CHARS:]


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """Stop the whole process group, not only its first process."""
    if proc.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
