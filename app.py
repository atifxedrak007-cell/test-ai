"""Multi-AI code review and workflow tool for Termux and Linux."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    """Small .env reader; never overwrites variables supplied by the OS."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", key):
            if value.lower().startswith(("replace-with-", "change-this-")):
                continue
            os.environ.setdefault(key, value)


load_dotenv()

from agents import ask_model, load_config, public_config, save_config
from workflow import MAX_LOG_CHARS, run_workflow


WORKSPACE = Path(os.getenv("WORKSPACE_DIR", ROOT / "workspace")).resolve()
OUTPUTS = Path(os.getenv("OUTPUTS_DIR", ROOT / "outputs")).resolve()
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "history.sqlite3")).resolve()
STATIC = ROOT / "static"
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")
SESSION_COOKIE = "review_session"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UNZIPPED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 1500
RATE_LIMITS: dict[str, list[float]] = {}
RUNS: dict[str, dict[str, Any]] = {}
TUNNEL_PROCESS: asyncio.subprocess.Process | None = None
TUNNEL_READER: asyncio.Task | None = None
TUNNEL_URL = ""
SESSION_KEY = hashlib.sha256(
    (APP_SECRET or APP_PASSWORD or "local-dev-only-review-tool-session").encode()
).digest()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                language TEXT NOT NULL, root_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT, logs TEXT NOT NULL DEFAULT '',
                plan_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                run_id TEXT NOT NULL, agent TEXT NOT NULL, text TEXT NOT NULL,
                model_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                run_id TEXT NOT NULL, filename TEXT NOT NULL, language TEXT NOT NULL,
                size INTEGER NOT NULL, line_count INTEGER NOT NULL, preview TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HTTPException(status_code=404, detail="Project not found.")
    return value


def project_root(project_id: str) -> Path:
    project_id = valid_id(project_id)
    with db() as conn:
        row = conn.execute("SELECT root_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    path = Path(row["root_path"]).resolve()
    try:
        path.relative_to(WORKSPACE)
    except ValueError:
        raise HTTPException(status_code=404, detail="Project not found.")
    return path


def _client_key(request: Request) -> str:
    # Do not trust arbitrary forwarded-for headers. The local tunnel is behind
    # a proxy; remote IP granularity is not required for this basic throttle.
    return request.client.host if request.client else "unknown"


def _rate_limited(request: Request, limit: int) -> bool:
    now = time.monotonic()
    key = _client_key(request)
    hits = [stamp for stamp in RATE_LIMITS.get(key, []) if now - stamp < 60]
    if len(hits) >= limit:
        RATE_LIMITS[key] = hits
        return True
    hits.append(now)
    RATE_LIMITS[key] = hits
    if len(RATE_LIMITS) > 2000:
        for old_key in list(RATE_LIMITS)[:500]:
            RATE_LIMITS.pop(old_key, None)
    return False


def _session_token() -> str:
    expires = str(int(time.time()) + 60 * 60 * 24 * 7)
    sig = hmac.new(SESSION_KEY, expires.encode(), hashlib.sha256).hexdigest()
    return expires + "." + sig


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        expires, signature = token.split(".", 1)
        if int(expires) < int(time.time()):
            return False
        expected = hmac.new(SESSION_KEY, expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def _secure_cookie(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
        or "https" in request.headers.get("cf-visitor", "").lower()
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global TUNNEL_PROCESS, TUNNEL_READER
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    init_db()
    cloudflared = shutil.which("cloudflared")
    if cloudflared:
        try:
            TUNNEL_PROCESS = await asyncio.create_subprocess_exec(
                cloudflared, "tunnel", "--url", "http://localhost:8000",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            TUNNEL_READER = asyncio.create_task(_read_tunnel(TUNNEL_PROCESS))
        except OSError:
            print("Could not start cloudflared; local server remains available.", flush=True)
    else:
        install = "pkg install cloudflared" if "com.termux" in os.getenv("PREFIX", "") else "Install cloudflared for your Linux distribution"
        print(f"cloudflared not found. {install}. Continuing on http://0.0.0.0:8000", flush=True)
    try:
        yield
    finally:
        for info in list(RUNS.values()):
            task = info.get("task")
            if task and not task.done():
                task.cancel()
        if TUNNEL_PROCESS and TUNNEL_PROCESS.returncode is None:
            TUNNEL_PROCESS.terminate()
            try:
                await asyncio.wait_for(TUNNEL_PROCESS.wait(), timeout=5)
            except asyncio.TimeoutError:
                TUNNEL_PROCESS.kill()
                await TUNNEL_PROCESS.wait()
        if TUNNEL_READER and not TUNNEL_READER.done():
            TUNNEL_READER.cancel()


async def _read_tunnel(proc: asyncio.subprocess.Process) -> None:
    global TUNNEL_URL
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
    while proc.stdout:
        line = await proc.stdout.readline()
        if not line:
            return
        match = pattern.search(line.decode("utf-8", errors="replace"))
        if match:
            TUNNEL_URL = match.group(0)
            print(f"Public URL: {TUNNEL_URL}", flush=True)


app = FastAPI(
    title="Multi-AI Code Review Tool",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        limit = 8 if path == "/api/login" else 120
        if _rate_limited(request, limit):
            return JSONResponse({"detail": "Rate limit reached. Try again in a minute."}, status_code=429)
        if APP_PASSWORD and path not in {"/api/login", "/api/session"}:
            if not _valid_session(request.cookies.get(SESSION_COOKIE)):
                return JSONResponse({"detail": "Login required."}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/session")
async def session_status(request: Request):
    authenticated = not APP_PASSWORD or _valid_session(request.cookies.get(SESSION_COOKIE))
    return {
        "auth_enabled": bool(APP_PASSWORD),
        "authenticated": authenticated,
        "tunnel_url": TUNNEL_URL,
        "warning": "" if APP_PASSWORD else "Password protection is off. Anyone with the public link can use configured API tokens.",
    }


@app.post("/api/login")
async def login(request: Request):
    if not APP_PASSWORD:
        return {"ok": True, "auth_enabled": False}
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request.")
    supplied = str(body.get("password", "")) if isinstance(body, dict) else ""
    if not hmac.compare_digest(supplied.encode(), APP_PASSWORD.encode()):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, _session_token(), httponly=True, samesite="strict",
        secure=_secure_cookie(request), max_age=60 * 60 * 24 * 7, path="/",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _safe_relative(raw: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Unsafe archive path.")
    if re.match(r"^[A-Za-z]:", normalized) or "\x00" in normalized:
        raise ValueError("Unsafe archive path.")
    return path


def _write_project_file(root: Path, relative: PurePosixPath, content: bytes) -> None:
    if relative.parts[0] in {".codepilot-venv", ".runtime-home", ".runtime-tmp"}:
        raise ValueError("Upload uses a reserved runtime directory name.")
    target = (root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ValueError("File path escapes the project workspace.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


async def _save_uploads(files: list[UploadFile], root: Path) -> int:
    total = 0
    uncompressed = 0
    count = 0
    for upload in files:
        raw = await upload.read(MAX_UPLOAD_BYTES + 1)
        total += len(raw)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError("Uploads exceed the 25 MB limit.")
        original = upload.filename or "upload.bin"
        if original.lower().endswith(".zip"):
            try:
                archive = zipfile.ZipFile(BytesIO(raw))
            except zipfile.BadZipFile:
                raise ValueError(f"{Path(original).name} is not a valid ZIP file.")
            for item in archive.infolist():
                if item.is_dir():
                    continue
                count += 1
                uncompressed += item.file_size
                if count > MAX_ARCHIVE_FILES or uncompressed > MAX_UNZIPPED_BYTES:
                    raise ValueError("ZIP exceeds the extracted file or size limit.")
                mode = item.external_attr >> 16
                if (mode & 0o170000) == 0o120000:
                    raise ValueError("Symbolic links are not allowed inside ZIP files.")
                relative = _safe_relative(item.filename)
                if item.flag_bits & 0x1:
                    raise ValueError("Encrypted ZIP files are not supported.")
                content = archive.read(item)
                if len(content) != item.file_size:
                    raise ValueError("ZIP entry size mismatch.")
                _write_project_file(root, relative, content)
        else:
            count += 1
            if count > MAX_ARCHIVE_FILES:
                raise ValueError("Upload contains more than 1,500 files.")
            relative = PurePosixPath(Path(original.replace("\\", "/")).name or "upload.bin")
            _write_project_file(root, relative, raw)
    if count == 0:
        raise ValueError("Choose at least one non-empty file.")
    return count


def _project_language(root: Path) -> str:
    exts = {path.suffix.lower() for path in root.rglob("*") if path.is_file()}
    if ".py" in exts:
        return "Python"
    if exts & {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
        return "JavaScript"
    if ".go" in exts:
        return "Go"
    if ".rs" in exts:
        return "Rust"
    if ".java" in exts:
        return "Java"
    return "Unknown"


@app.post("/api/projects")
async def upload_project(
    files: list[UploadFile] = File(...),
    name: str = Form(""),
):
    project_id = uuid.uuid4().hex
    root = (WORKSPACE / project_id).resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        count = await _save_uploads(files, root)
        display_name = (name.strip() or Path(files[0].filename or "Project").stem)[:100]
        display_name = re.sub(r"[\x00-\x1f]", "", display_name) or "Project"
        language = _project_language(root)
        with db() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,created_at,language,root_path) VALUES(?,?,?,?,?)",
                (project_id, display_name, utc_now(), language, str(root)),
            )
        return {"id": project_id, "name": display_name, "language": language, "file_count": count}
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/projects")
async def list_projects():
    with db() as conn:
        rows = conn.execute(
            "SELECT id,name,created_at,language FROM projects ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return {"projects": [dict(row) for row in rows]}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    project_id = valid_id(project_id)
    with db() as conn:
        project = conn.execute("SELECT id,name,created_at,language FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")
        runs = conn.execute(
            "SELECT id,status,created_at,finished_at FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 30",
            (project_id,),
        ).fetchall()
        reviews = conn.execute(
            "SELECT run_id,agent,text,model_name,created_at FROM reviews WHERE project_id=? ORDER BY id DESC LIMIT 6",
            (project_id,),
        ).fetchall()
    return {"project": dict(project), "runs": [dict(r) for r in runs], "reviews": [dict(r) for r in reviews]}


@app.get("/api/history")
async def history():
    with db() as conn:
        rows = conn.execute("""
            SELECT r.id AS run_id,r.project_id,p.name AS project_name,r.status,r.created_at,r.finished_at
            FROM runs r JOIN projects p ON p.id=r.project_id
            ORDER BY r.created_at DESC LIMIT 100
        """).fetchall()
    return {"runs": [dict(row) for row in rows]}


@app.get("/api/settings")
async def get_settings():
    return public_config()


@app.put("/api/settings")
async def put_settings(request: Request):
    try:
        body = await request.json()
        config = save_config(body)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return public_config(config)


def _project_context(root: Path, max_chars: int = 65_000) -> str:
    chunks, total = [], 0
    include = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".html", ".css", ".yml", ".yaml", ".toml"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in include:
            continue
        if any(part in {"node_modules", ".git", "__pycache__", ".venv"} for part in path.parts):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _redact_sensitive(text)
        room = max_chars - total
        if room <= 0:
            break
        text = text[:room]
        relative = path.relative_to(root).as_posix()
        chunks.append(f"\n--- {relative} ---\n{text}")
        total += len(text)
    return "".join(chunks) or "(No readable source files.)"


def _redact_sensitive(text: str) -> str:
    patterns = [
        (r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]"),
        (r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^'\"\s,;}]+", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(?:postgres(?:ql)?|mysql)://[^:\s/]+:[^@\s/]+@", "[REDACTED_DATABASE_URL]@"),
        (r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts", "tsx": ".tsx", "jsx": ".jsx",
    "html": ".html", "css": ".css", "json": ".json", "java": ".java",
    "c": ".c", "cpp": ".cpp", "go": ".go", "rust": ".rs", "rs": ".rs",
    "php": ".php", "bash": ".sh", "sh": ".sh", "sql": ".sql",
}


def _attribute(attrs: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*(['\"])(.*?)\1", attrs, re.I)
    return match.group(2)[:120] if match else ""


def persist_long_code(project_id: str, run_id: str, text: str) -> tuple[str, list[dict[str, Any]]]:
    project_id = valid_id(project_id)
    output_root = OUTPUTS.resolve()
    out_dir = (output_root / project_id).resolve()
    try:
        out_dir.relative_to(output_root)
    except ValueError:
        raise ValueError("Output path escapes the configured output directory.")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    pattern = re.compile(r"<code\b([^>]*)>([\s\S]*?)</code\s*>", re.I)

    def replace(match: re.Match) -> str:
        attrs, code = match.group(1), html.unescape(match.group(2))
        lines = code.splitlines()
        if len(lines) <= 150 and len(code.encode("utf-8")) <= 8 * 1024:
            return match.group(0)
        lang = (_attribute(attrs, "lang") or "txt").lower()
        extension = EXTENSIONS.get(lang, ".txt")
        requested = Path(_attribute(attrs, "file") or ("generated" + extension)).name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(requested).stem).strip("._")[:70] or "generated"
        filename = f"{stem}-{uuid.uuid4().hex[:8]}{extension}"
        destination = (out_dir / filename).resolve()
        try:
            destination.relative_to(out_dir)
        except ValueError:
            return match.group(0)
        destination.write_text(code, encoding="utf-8")
        preview = "\n".join(lines[:15])
        item = {
            "filename": filename, "language": lang, "size": destination.stat().st_size,
            "line_count": len(lines), "preview": preview,
            "download_url": f"/api/projects/{project_id}/outputs/{filename}",
        }
        saved.append(item)
        with db() as conn:
            conn.execute(
                """INSERT INTO outputs(project_id,run_id,filename,language,size,line_count,preview,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, run_id, filename, lang, item["size"], item["line_count"], preview, utc_now()),
            )
        note = f"Saved as {filename} ({len(lines)} lines); download below."
        return f"<code{attrs}>{html.escape(note)}</code>"

    return pattern.sub(replace, text), saved


async def _review_agent(
    role: str, label: str, root: Path, logs: str, config: dict[str, Any]
) -> tuple[str, str]:
    prompt = (
        f"Review the uploaded project and workflow output as the {label}. "
        "Be specific, prioritize real bugs and security risks, and do not claim tests passed "
        "unless the logs prove it. Wrap special notes in <note>...</note>. "
        "Wrap suggested code only in <code lang=\"python\" file=\"example.py\">...</code> "
        "(use the correct language and filename). Plain explanation must not be wrapped. "
        "Never repeat secrets you encounter.\n\n"
        f"WORKFLOW LOGS (tail):\n{_redact_sensitive(logs[-25_000:])}\n\n"
        f"PROJECT FILES:\n{_project_context(root)}"
    )
    return await ask_model(role, [
        {"role": "system", "content": "You are a careful software reviewer. Do not execute code or invent test results."},
        {"role": "user", "content": prompt},
    ], config)


async def _emit(run_id: str, event: dict[str, Any]) -> None:
    state = RUNS.get(run_id)
    if not state:
        return
    state["events"].append(event)
    queue: asyncio.Queue = state["queue"]
    await queue.put(event)


async def _execute_run(run_id: str, project_id: str, root: Path) -> None:
    state = RUNS[run_id]
    logs = ""
    config = load_config()
    try:
        logs = await run_workflow(root, config, lambda event: _emit(run_id, event))
        state["logs"] = logs
        await _emit(run_id, {"event": "review_status", "status": "running"})
        roles = [
            ("backend_reviewer", "Backend Reviewer"),
            ("syntax_reviewer", "Syntax Reviewer"),
        ]
        results = await asyncio.gather(
            *(_review_agent(role, label, root, logs, config) for role, label in roles),
            return_exceptions=True,
        )
        for (role, label), result in zip(roles, results):
            if isinstance(result, BaseException):
                text, model_name = f"Review unavailable: {str(result)}", ""
            else:
                text, model_name = result
            display_text, files = persist_long_code(project_id, run_id, text)
            with db() as conn:
                conn.execute(
                    "INSERT INTO reviews(project_id,run_id,agent,text,model_name,created_at) VALUES(?,?,?,?,?,?)",
                    (project_id, run_id, role, display_text, model_name, utc_now()),
                )
            await _emit(run_id, {
                "event": "review", "agent": role, "label": label,
                "text": display_text, "model": model_name, "outputs": files,
            })
        state["status"] = "completed"
        await _emit(run_id, {"event": "review_status", "status": "completed"})
    except asyncio.CancelledError:
        state["status"] = "stopped"
        await _emit(run_id, {"event": "stopped"})
        raise
    except Exception as exc:
        state["status"] = "failed"
        safe_message = str(exc)[:1000]
        await _emit(run_id, {"event": "error", "message": safe_message})
    finally:
        with db() as conn:
            conn.execute(
                "UPDATE runs SET status=?,finished_at=?,logs=? WHERE id=?",
                (state["status"], utc_now(), state.get("logs", logs)[-MAX_LOG_CHARS:], run_id),
            )
        await _emit(run_id, {"event": "done", "status": state["status"]})


@app.post("/api/projects/{project_id}/run")
async def start_run(project_id: str):
    project_id = valid_id(project_id)
    root = project_root(project_id)
    run_id = uuid.uuid4().hex
    state = {
        "project_id": project_id, "queue": asyncio.Queue(), "events": [],
        "status": "running", "logs": "", "task": None,
    }
    RUNS[run_id] = state
    with db() as conn:
        conn.execute(
            "INSERT INTO runs(id,project_id,status,created_at) VALUES(?,?,?,?)",
            (run_id, project_id, "running", utc_now()),
        )
    state["task"] = asyncio.create_task(_execute_run(run_id, project_id, root))
    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id) or run_id not in RUNS:
        raise HTTPException(status_code=404, detail="Run not found.")
    task = RUNS[run_id].get("task")
    if task and not task.done():
        task.cancel()
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run stream is no longer available. Start a new run.")
    queue: asyncio.Queue = state["queue"]

    async def stream():
        cursor = 0
        # Events are retained so a browser reconnect does not miss early output.
        while True:
            events = state["events"]
            while cursor < len(events):
                event = events[cursor]
                cursor += 1
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event.get("event") == "done":
                    return
            try:
                await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/api/projects/{project_id}/outputs/{filename}")
async def download_output(project_id: str, filename: str):
    project_id = valid_id(project_id)
    project_root(project_id)
    if Path(filename).name != filename or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", filename):
        raise HTTPException(status_code=404, detail="File not found.")
    folder = (OUTPUTS / project_id).resolve()
    path = (folder / filename).resolve()
    try:
        path.relative_to(folder)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, filename=filename)


@app.get("/api/projects/{project_id}/outputs")
async def list_outputs(project_id: str):
    project_id = valid_id(project_id)
    project_root(project_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT filename,language,size,line_count,preview,created_at FROM outputs WHERE project_id=? ORDER BY id DESC LIMIT 100",
            (project_id,),
        ).fetchall()
    return {"outputs": [
        {**dict(row), "download_url": f"/api/projects/{project_id}/outputs/{row['filename']}"}
        for row in rows
    ]}


@app.post("/api/chat")
async def chat(request: Request):
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        project_id = str(body.get("project_id", ""))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request.")
    if not message or len(message) > 8000:
        raise HTTPException(status_code=400, detail="Message must be 1 to 8000 characters.")
    context = ""
    if project_id:
        root = project_root(project_id)
        context = "\n\nProject source context:\n" + _project_context(root, 25_000)
    try:
        text, model = await ask_model(
            "workflow_runner",
            [
                {"role": "system", "content": (
                    "You are a practical coding assistant. Do not claim to have changed or run files. "
                    "Wrap special notes in <note>...</note> and code in <code lang=\"python\" file=\"name.py\">...</code>. "
                    "Use appropriate language/filename. Plain explanations get no tags."
                )},
                {"role": "user", "content": message + context},
            ],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if project_id:
        display_text, files = persist_long_code(project_id, "chat", text)
    else:
        display_text, files = text, []
    return {"text": display_text, "model": model, "outputs": files}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
