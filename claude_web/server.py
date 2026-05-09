import asyncio
import ipaddress
import json
import os
import re
import signal
import socket
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_PKG_DIR = Path(__file__).parent
_DATA_DIR = Path(os.environ.get("CLAUDE_WEB_DATA_DIR", "")).resolve() if os.environ.get("CLAUDE_WEB_DATA_DIR") else Path.cwd()

STATIC_DIR = _PKG_DIR / "static"
HISTORY_DIR = _DATA_DIR / "history"
UPLOADS_DIR = _DATA_DIR / "uploads"
DB_PATH = _DATA_DIR / "claude-web.db"

HISTORY_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_UPLOAD_MB = 20
IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache", ".idea", ".vscode"}
KNOWN_TOOL_NAMES = {
    "Bash", "Read", "Write", "Edit", "MultiEdit", "Grep", "Glob",
    "WebFetch", "WebSearch", "TodoWrite", "Task", "NotebookEdit",
}

app = FastAPI(title="Claude Code Web")

_running_processes: Dict[str, asyncio.subprocess.Process] = {}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        ensure_column(conn, "sessions", "pinned", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "sessions", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "sessions", "tags", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "sessions", "manual_title", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "sessions", "remote_session_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "sessions", "remote_ready", "INTEGER NOT NULL DEFAULT 0")


init_db()


def upsert_session(session_id: str, title: str, cwd: str) -> None:
    now = time.time()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT title, manual_title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, title, cwd, now, now),
            )
        else:
            new_title = row["title"]
            if not row["manual_title"] and not new_title:
                new_title = title
            conn.execute(
                "UPDATE sessions SET title = ?, cwd = ?, updated_at = ? WHERE id = ?",
                (new_title, cwd, now, session_id),
            )


def append_event(session_id: str, event: dict) -> None:
    path = HISTORY_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(session_id: str) -> List[dict]:
    path = HISTORY_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    events: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def save_events(session_id: str, events: List[dict]) -> None:
    path = HISTORY_DIR / f"{session_id}.jsonl"
    if not events:
        if path.exists():
            path.unlink()
        return
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def summarize_text_from_events(events: List[dict]) -> str:
    parts: List[str] = []
    for ev in events:
        if ev.get("type") == "user_input":
            parts.append(ev.get("text", ""))
        elif ev.get("type") == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    images: Optional[List[str]] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    display_message: Optional[str] = None
    permission_mode: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    force_new: Optional[bool] = None


class PromptRequest(BaseModel):
    name: str
    content: str


class SessionPatch(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    tags: Optional[str] = None


class ForkRequest(BaseModel):
    event_index: int
    new_text: Optional[str] = None


class RestoreRequest(BaseModel):
    event_index: int


class FetchUrlRequest(BaseModel):
    url: str
    max_chars: Optional[int] = 10000


def build_args(
    message: str,
    session_id: str,
    resume: bool,
    model: Optional[str],
    system_prompt: Optional[str],
    permission_mode: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    use_stdin: bool = False,
) -> List[str]:
    args = ["claude"]
    if use_stdin:
        args += ["-p", "--input-format", "stream-json"]
    else:
        args += ["-p", message]
    args += [
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if resume:
        args += ["--resume", session_id]
    else:
        args += ["--session-id", session_id]
    if model:
        args += ["--model", model]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if permission_mode and permission_mode in ("default", "acceptEdits", "bypassPermissions", "plan"):
        args += ["--permission-mode", permission_mode]
    if allowed_tools:
        args += ["--allowed-tools", ",".join(allowed_tools)]
    if disallowed_tools:
        args += ["--disallowed-tools", ",".join(disallowed_tools)]
    return args


def extract_tool_name(text: str) -> Optional[str]:
    mcp_match = re.search(r"\bmcp__[A-Za-z0-9_:-]+(?:__[A-Za-z0-9_:-]+)*\b", text)
    if mcp_match:
        return mcp_match.group(0)

    for tool in KNOWN_TOOL_NAMES:
        if re.search(rf"\b{re.escape(tool)}\b", text):
            return tool

    patterns = [
        r"(?:MCP tool|mcp tool|tool)\s+[\"'`]?([A-Za-z][A-Za-z0-9_:-]{1,80})[\"'`]?",
        r"[\"'`]([A-Za-z][A-Za-z0-9_:-]{1,80})[\"'`]\s+(?:tool|Tool|MCP tool|mcp tool)",
    ]
    stop_words = {"approval", "permission", "tool", "tools", "mcp", "required", "requires", "non-interactive"}
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        candidate = m.group(1).strip()
        if candidate.lower() not in stop_words:
            return candidate
    return None


def classify_claude_error(message: str) -> dict:
    text = (message or "").strip() or "claude exited with error"
    lower = text.lower()
    tool_name = extract_tool_name(text)
    permissionish = any(k in lower for k in (
        "requires approval", "approval required", "needs approval", "approval",
        "cannot prompt", "non-interactive", "not allowed", "permission denied",
    ))
    if ("permission" in lower and ("tool" in lower or "mcp" in lower or tool_name)) or (permissionish and ("tool" in lower or "mcp" in lower or tool_name)):
        return {
            "type": "permission_error",
            "message": text,
            "tool_name": tool_name,
            "hint": "当前 Web UI 不支持运行中批准工具权限；请预先放行工具后重试本轮，或改用 Claude Code CLI。",
        }
    return {"type": "error", "message": text}


def compose_message(message: str, images: Optional[List[str]]) -> str:
    if not images:
        return message
    return message


def build_image_input_message(message: str, images: List[str]) -> bytes:
    import base64 as b64mod
    content: List[dict] = []
    for img_path in images:
        p = Path(img_path)
        if not p.exists():
            continue
        ext = p.suffix.lower()
        media_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        media_type = media_map.get(ext, "image/png")
        data = b64mod.b64encode(p.read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    content.append({"type": "text", "text": message})
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    return json.dumps(msg, ensure_ascii=False).encode() + b"\n"


async def _git_run(cwd: str, *args: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


async def create_git_checkpoint(cwd: str) -> Optional[dict]:
    if not cwd or not os.path.isdir(cwd):
        return None
    git_dir = await _git_run(cwd, "rev-parse", "--git-dir")
    if git_dir is None:
        return None
    head = await _git_run(cwd, "rev-parse", "HEAD")
    if head is None:
        return None
    stash = await _git_run(cwd, "stash", "create", f"claude-web-checkpoint-{int(time.time())}")
    return {"type": "git", "head": head, "stash": stash or ""}


async def restore_git_checkpoint(cwd: str, cp: dict) -> bool:
    if not cp or cp.get("type") != "git" or not cwd:
        return False
    head = cp.get("head")
    stash = cp.get("stash") or ""
    if not head:
        return False
    if await _git_run(cwd, "reset", "--hard", head) is None:
        return False
    await _git_run(cwd, "clean", "-fd")
    if stash:
        await _git_run(cwd, "stash", "apply", stash)
    return True


def format_context_snippet(events: List[dict], max_chars: int = 6000) -> str:
    lines: List[str] = []
    total = 0
    for ev in events:
        t = ev.get("type")
        if t == "user_input":
            text = (ev.get("text") or "").strip()
            if text:
                chunk = f"用户: {text}"
                lines.append(chunk)
                total += len(chunk)
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        chunk = f"助手: {text[:600]}"
                        lines.append(chunk)
                        total += len(chunk)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    chunk = f"(助手调用了工具: {name})"
                    lines.append(chunk)
                    total += len(chunk)
        if total > max_chars:
            lines.append("...（历史已截断）")
            break
    return "\n\n".join(lines)


def derive_title(message: str) -> str:
    text = message.strip().replace("\n", " ")
    return text[:60] if text else "未命名会话"


def session_has_remote_conversation(events: List[dict]) -> bool:
    for ev in events:
        event_type = ev.get("type")
        if event_type == "assistant":
            return True
        if event_type == "system" and ev.get("subtype") == "init":
            return True
        if event_type == "result" and not ev.get("is_error"):
            return True
    return False


def resolve_remote_session_state(session_id: str, row: Optional[sqlite3.Row], events: List[dict]):
    if row is None:
        return session_id, session_has_remote_conversation(events)
    remote_session_id = (row["remote_session_id"] or "").strip() or session_id
    if (row["remote_session_id"] or "").strip():
        return remote_session_id, bool(row["remote_ready"])
    return remote_session_id, session_has_remote_conversation(events)


def set_session_remote_state(session_id: str, remote_session_id: str, remote_ready: bool) -> None:
    now = time.time()
    with db_connect() as conn:
        conn.execute(
            "UPDATE sessions SET remote_session_id = ?, remote_ready = ?, updated_at = ? WHERE id = ?",
            (remote_session_id, 1 if remote_ready else 0, now, session_id),
        )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    existing_events = load_events(session_id) if req.session_id else []
    with db_connect() as conn:
        row = conn.execute(
            "SELECT cwd, remote_session_id, remote_ready FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

    remote_session_id, remote_ready = resolve_remote_session_state(session_id, row, existing_events)
    if req.force_new is True:
        stored_remote_id = ((row["remote_session_id"] or "").strip() if row else "")
        stored_remote_ready = bool(row["remote_ready"]) if row else False
        if stored_remote_id and not stored_remote_ready:
            remote_session_id = stored_remote_id
        elif row is not None and remote_ready:
            remote_session_id = str(uuid.uuid4())
        remote_ready = False

    is_new = not remote_ready
    work_dir = req.cwd or (row["cwd"] if row and row["cwd"] else os.path.expanduser("~"))
    full_message = compose_message(req.message, req.images)
    display_text = req.display_message if req.display_message is not None else req.message

    checkpoint = await create_git_checkpoint(work_dir)

    user_event = {
        "type": "user_input",
        "text": display_text,
        "images": req.images or [],
        "ts": time.time(),
        "checkpoint": checkpoint,
    }
    append_event(session_id, user_event)
    upsert_session(session_id, derive_title(display_text), work_dir)
    set_session_remote_state(session_id, remote_session_id, remote_ready and not is_new)

    async def generate():
        remote_became_ready = remote_ready and not is_new
        meta = {
            "type": "meta",
            "session_id": session_id,
            "cwd": work_dir,
            "has_checkpoint": checkpoint is not None,
        }
        yield f"data: {json.dumps(meta)}\n\n"

        has_images = bool(req.images)
        args = build_args(
            full_message, remote_session_id,
            resume=not is_new,
            model=req.model,
            system_prompt=req.system_prompt,
            permission_mode=req.permission_mode,
            allowed_tools=req.allowed_tools,
            disallowed_tools=req.disallowed_tools,
            use_stdin=has_images,
        )
        stdin_data: Optional[bytes] = None
        if has_images:
            stdin_data = build_image_input_message(full_message, req.images or [])
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if has_images else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                limit=16 * 1024 * 1024,
            )
            if has_images and stdin_data and process.stdin:
                process.stdin.write(stdin_data)
                process.stdin.close()
        except FileNotFoundError:
            err_event = {"type": "error", "message": "claude CLI not found in PATH"}
            append_event(session_id, err_event)
            yield f"data: {json.dumps(err_event)}\n\n"
            return

        _running_processes[session_id] = process
        try:
            assert process.stdout is not None
            while True:
                try:
                    raw = await process.stdout.readline()
                except ValueError as e:
                    err_event = {"type": "error", "message": f"stdout line too large: {e}"}
                    append_event(session_id, err_event)
                    yield f"data: {json.dumps(err_event)}\n\n"
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = {"type": "raw", "text": line}
                t = obj.get("type")
                if session_has_remote_conversation([obj]):
                    remote_became_ready = True
                if t != "stream_event" and not (t == "system" and obj.get("subtype", "").startswith("hook_")):
                    append_event(session_id, obj)
                yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

            rc = await process.wait()
            err = b""
            if process.stderr is not None:
                err = await process.stderr.read()
            if rc != 0:
                err_event = classify_claude_error(
                    err.decode("utf-8", errors="replace") or f"claude exited with code {rc}"
                )
                append_event(session_id, err_event)
                yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
        finally:
            _running_processes.pop(session_id, None)

        upsert_session(session_id, derive_title(display_text), work_dir)
        if remote_became_ready:
            set_session_remote_state(session_id, remote_session_id, True)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/stop/{session_id}")
async def stop_chat(session_id: str):
    process = _running_processes.get(session_id)
    if process is None:
        raise HTTPException(status_code=404, detail="no running process for this session")
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    stop_event = {"type": "error", "message": "用户中止", "ts": time.time()}
    append_event(session_id, stop_event)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/prepare-fork")
async def prepare_fork(session_id: str, req: ForkRequest):
    events = load_events(session_id)
    user_event_positions = [i for i, e in enumerate(events) if e.get("type") == "user_input"]
    if req.event_index < 0 or req.event_index >= len(user_event_positions):
        raise HTTPException(status_code=400, detail="invalid event_index")

    target_pos = user_event_positions[req.event_index]
    events_before = events[:target_pos]
    original_text = events[target_pos].get("text", "")
    new_text = req.new_text if req.new_text is not None and req.new_text.strip() else original_text

    with db_connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    cwd = row["cwd"] if row else os.path.expanduser("~")

    new_id = str(uuid.uuid4())
    upsert_session(new_id, derive_title(new_text), cwd)

    with db_connect() as conn:
        conn.execute(
            "UPDATE sessions SET tags = ? WHERE id = ?",
            (f"forked-from-{session_id[:8]}", new_id),
        )

    context = format_context_snippet(events_before)
    if context:
        packed_message = (
            "【以下是之前的对话历史，仅作为参考上下文（不要重复回应历史问题）】\n"
            f"{context}\n\n"
            "【请基于以上历史上下文，回应这个新问题】\n"
            f"{new_text}"
        )
    else:
        packed_message = new_text

    return {
        "session_id": new_id,
        "cwd": cwd,
        "sent_message": packed_message,
        "display_message": new_text,
        "forked_from": session_id,
    }


@app.post("/api/sessions/{session_id}/prepare-inline-edit")
async def prepare_inline_edit(session_id: str, req: ForkRequest):
    if session_id in _running_processes:
        raise HTTPException(status_code=409, detail="session is running")

    events = load_events(session_id)
    user_event_positions = [i for i, e in enumerate(events) if e.get("type") == "user_input"]
    if req.event_index < 0 or req.event_index >= len(user_event_positions):
        raise HTTPException(status_code=400, detail="invalid event_index")

    target_pos = user_event_positions[req.event_index]
    events_before = events[:target_pos]
    original_event = events[target_pos]
    original_text = original_event.get("text", "")
    original_images = original_event.get("images", []) or []
    new_text = req.new_text if req.new_text is not None and req.new_text.strip() else original_text

    with db_connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    cwd = row["cwd"] if row else os.path.expanduser("~")

    save_events(session_id, events_before)
    upsert_session(session_id, derive_title(new_text), cwd)
    set_session_remote_state(session_id, str(uuid.uuid4()), False)

    context = format_context_snippet(events_before)
    if context:
        packed_message = (
            "【以下是之前的对话历史，仅作为参考上下文（不要重复回应历史问题）】\n"
            f"{context}\n\n"
            "【请基于以上历史上下文，继续这个对话，并回应下面这条经过编辑的新消息】\n"
            f"{new_text}"
        )
    else:
        packed_message = new_text

    return {
        "session_id": session_id,
        "cwd": cwd,
        "sent_message": packed_message,
        "display_message": new_text,
        "images": original_images,
    }


@app.post("/api/sessions/{session_id}/restore-checkpoint")
async def restore_checkpoint(session_id: str, req: RestoreRequest):
    events = load_events(session_id)
    user_event_positions = [i for i, e in enumerate(events) if e.get("type") == "user_input"]
    if req.event_index < 0 or req.event_index >= len(user_event_positions):
        raise HTTPException(status_code=400, detail="invalid event_index")
    ev = events[user_event_positions[req.event_index]]
    cp = ev.get("checkpoint")
    if not cp:
        raise HTTPException(status_code=400, detail="no checkpoint on this turn")

    with db_connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    cwd = row["cwd"] if row else ""
    if not cwd:
        raise HTTPException(status_code=400, detail="session has no cwd")

    ok = await restore_git_checkpoint(cwd, cp)
    if not ok:
        raise HTTPException(status_code=500, detail="restore failed")
    return {"ok": True, "cwd": cwd}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if file.filename is None:
        raise HTTPException(status_code=400, detail="filename missing")
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")

    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_MB} MB")

    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOADS_DIR / name
    path.write_bytes(data)

    return {
        "path": str(path.absolute()),
        "url": f"/uploads/{name}",
        "name": file.filename,
        "size": len(data),
    }


DOC_EXTS = {".pdf", ".docx", ".csv", ".tsv", ".txt", ".md", ".json", ".log"}
MAX_DOC_MB = 20
MAX_DOC_CHARS = 50000


def _extract_pdf_text(path: Path) -> str:
    import pypdf
    reader = pypdf.PdfReader(str(path))
    parts: List[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(parts)


def _extract_docx_text(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append(" | ".join(cells))
    return "\n".join(parts)


@app.post("/api/upload-doc")
async def upload_doc(file: UploadFile = File(...)):
    if file.filename is None:
        raise HTTPException(status_code=400, detail="filename missing")
    ext = Path(file.filename).suffix.lower()
    if ext not in DOC_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")

    data = await file.read()
    if len(data) > MAX_DOC_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_DOC_MB} MB")

    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOADS_DIR / name
    path.write_bytes(data)

    try:
        if ext == ".pdf":
            text = _extract_pdf_text(path)
        elif ext == ".docx":
            text = _extract_docx_text(path)
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"extract failed: {e}")

    text = text.strip()
    truncated = len(text) > MAX_DOC_CHARS
    if truncated:
        text = text[:MAX_DOC_CHARS]

    return {
        "path": str(path.absolute()),
        "name": file.filename,
        "size": len(data),
        "ext": ext,
        "content": text,
        "length": len(text),
        "truncated": truncated,
    }


class ExecCodeRequest(BaseModel):
    language: str
    code: str
    timeout: Optional[int] = 10


EXEC_LANG_MAP: Dict[str, List[str]] = {
    "python": ["python3", "-c"],
    "python3": ["python3", "-c"],
    "py": ["python3", "-c"],
    "javascript": ["node", "-e"],
    "js": ["node", "-e"],
    "node": ["node", "-e"],
    "bash": ["bash", "-c"],
    "sh": ["bash", "-c"],
    "shell": ["bash", "-c"],
}


@app.post("/api/exec-code")
async def exec_code(req: ExecCodeRequest):
    lang = (req.language or "").lower().strip()
    cmd = EXEC_LANG_MAP.get(lang)
    if cmd is None:
        raise HTTPException(status_code=400, detail=f"unsupported language: {lang}")
    if not req.code or len(req.code) > 100_000:
        raise HTTPException(status_code=400, detail="code empty or too large")

    timeout = max(1, min(int(req.timeout or 10), 30))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, req.code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(UPLOADS_DIR),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"stdout": "", "stderr": f"execution timed out after {timeout}s", "returncode": -1, "timed_out": True}
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"interpreter not found: {e}")

    return {
        "stdout": stdout.decode("utf-8", errors="replace")[:50_000],
        "stderr": stderr.decode("utf-8", errors="replace")[:10_000],
        "returncode": proc.returncode,
        "timed_out": False,
    }


def _row_to_session(r: sqlite3.Row) -> dict:
    tags = [t for t in (r["tags"] or "").split(",") if t]
    return {
        "id": r["id"],
        "title": r["title"] or "未命名会话",
        "cwd": r["cwd"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "pinned": bool(r["pinned"]),
        "archived": bool(r["archived"]),
        "tags": tags,
    }


@app.get("/api/sessions")
async def list_sessions(q: Optional[str] = None, archived: bool = False, tag: Optional[str] = None):
    with db_connect() as conn:
        where = "archived = 1" if archived else "archived = 0"
        rows = conn.execute(
            f"SELECT id, title, cwd, created_at, updated_at, pinned, archived, tags FROM sessions "
            f"WHERE {where} ORDER BY pinned DESC, updated_at DESC LIMIT 500"
        ).fetchall()

    items = [_row_to_session(r) for r in rows]

    if tag:
        items = [i for i in items if tag in i["tags"]]

    if q:
        q_lower = q.lower()
        filtered: List[dict] = []
        for item in items:
            if q_lower in item["title"].lower() or q_lower in ",".join(item["tags"]).lower():
                filtered.append(item)
                continue
            try:
                events = load_events(item["id"])
                content = summarize_text_from_events(events).lower()
                if q_lower in content:
                    filtered.append(item)
            except Exception:
                continue
        items = filtered

    return items


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, title, cwd, created_at, updated_at, pinned, archived, tags FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    data = _row_to_session(row)
    data["events"] = load_events(session_id)
    return data


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: SessionPatch):
    updates: List[str] = []
    params: List = []
    if req.title is not None:
        updates += ["title = ?", "manual_title = 1"]
        params.append(req.title)
    if req.pinned is not None:
        updates.append("pinned = ?")
        params.append(1 if req.pinned else 0)
    if req.archived is not None:
        updates.append("archived = ?")
        params.append(1 if req.archived else 0)
    if req.tags is not None:
        updates.append("tags = ?")
        params.append(req.tags)
    if not updates:
        return {"ok": True}
    params.append(session_id)
    with db_connect() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", params)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    with db_connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    path = HISTORY_DIR / f"{session_id}.jsonl"
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/api/sessions/{session_id}/suggest-title")
async def suggest_title(session_id: str):
    events = load_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="empty session")
    summary = summarize_text_from_events(events)[:3000]
    if not summary.strip():
        raise HTTPException(status_code=400, detail="no textual content")
    prompt = f"根据下面的对话，用中文生成一个不超过15字、不带引号的会话标题（只输出标题本身）：\n\n{summary}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="title generation timeout")
    title = stdout.decode("utf-8", errors="replace").strip().splitlines()[0].strip(' "\'"""''').strip()[:60]
    if not title:
        raise HTTPException(status_code=500, detail="empty title")
    with db_connect() as conn:
        conn.execute("UPDATE sessions SET title = ?, manual_title = 1 WHERE id = ?", (title, session_id))
    return {"title": title}


@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str):
    events = load_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="session not found")
    with db_connect() as conn:
        row = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
    title = row["title"] if row and row["title"] else session_id

    lines: List[str] = [f"# {title}", "", f"_会话 ID: {session_id}_", ""]
    for ev in events:
        t = ev.get("type")
        if t == "user_input":
            lines += ["## 👤 用户", "", ev.get("text", "")]
            for img in ev.get("images", []) or []:
                lines.append(f"![image]({img})")
            lines.append("")
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    lines += ["## 🤖 Claude", "", block.get("text", ""), ""]
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    lines += [f"### 🔧 工具调用: `{name}`", "", "```json",
                              json.dumps(block.get("input", {}), ensure_ascii=False, indent=2), "```", ""]
        elif t == "user":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "tool_result":
                    ct = block.get("content", "")
                    if isinstance(ct, list):
                        ct = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in ct)
                    lines += ["### 📋 工具结果", "", "```", str(ct)[:5000], "```", ""]

    md = "\n".join(lines)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@app.get("/api/prompts")
async def list_prompts():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, name, content, created_at FROM prompts ORDER BY created_at DESC"
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "content": r["content"], "created_at": r["created_at"]}
        for r in rows
    ]


@app.post("/api/prompts")
async def create_prompt(req: PromptRequest):
    pid = uuid.uuid4().hex
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO prompts (id, name, content, created_at) VALUES (?, ?, ?, ?)",
            (pid, req.name, req.content, time.time()),
        )
    return {"id": pid}


@app.put("/api/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, req: PromptRequest):
    with db_connect() as conn:
        conn.execute(
            "UPDATE prompts SET name = ?, content = ? WHERE id = ?",
            (req.name, req.content, prompt_id),
        )
    return {"ok": True}


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str):
    with db_connect() as conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    return {"ok": True}


@app.post("/api/suggest-followups")
async def suggest_followups(session_id: str = ""):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    events = load_events(session_id)
    if not events:
        return {"suggestions": []}
    snippet = summarize_text_from_events(events[-20:])[-3000:]
    if not snippet.strip():
        return {"suggestions": []}
    prompt = (
        "根据以下对话内容，生成3个用户可能想继续追问的简短问题（每个不超过20字）。"
        "只输出3行，每行一个问题，不要编号、不要引号、不要其他内容。\n\n"
        f"{snippet}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "text", "--model", "haiku",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return {"suggestions": []}
    except Exception:
        return {"suggestions": []}
    lines = [l.strip() for l in stdout.decode("utf-8", errors="replace").splitlines() if l.strip()]
    suggestions = [l.lstrip("0123456789.-、）) ") for l in lines[:3]]
    return {"suggestions": suggestions}


@app.get("/api/cwds")
async def list_cwds():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT cwd, MAX(updated_at) AS last FROM sessions WHERE cwd <> '' GROUP BY cwd ORDER BY last DESC LIMIT 10"
        ).fetchall()
    return [r["cwd"] for r in rows]


@app.get("/api/tags")
async def list_tags():
    with db_connect() as conn:
        rows = conn.execute("SELECT tags FROM sessions WHERE tags <> '' AND archived = 0").fetchall()
    counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counts[t] += 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


@app.get("/api/stats")
async def stats():
    total_cost = 0.0
    total_duration = 0.0
    total_turns = 0
    daily: Dict[str, Dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "turns": 0})
    tool_counts: Dict[str, int] = defaultdict(int)
    with db_connect() as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    total_sessions = len(rows)
    for row in rows:
        events = load_events(row["id"])
        for ev in events:
            t = ev.get("type")
            if t == "result":
                cost = float(ev.get("total_cost_usd") or 0)
                dur = float(ev.get("duration_ms") or 0)
                ts = float(ev.get("ts") or time.time())
                total_cost += cost
                total_duration += dur
                total_turns += 1
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                daily[day]["cost"] += cost
                daily[day]["turns"] += 1
            elif t == "assistant":
                content = (ev.get("message") or {}).get("content") or []
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_counts[block.get("name", "?")] += 1
    daily_sorted = sorted(daily.items(), key=lambda x: x[0])
    return {
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_duration,
        "total_sessions": total_sessions,
        "total_turns": total_turns,
        "daily": [{"date": d, "cost": round(v["cost"], 4), "turns": v["turns"]} for d, v in daily_sorted],
        "tools": sorted(
            [{"name": k, "count": v} for k, v in tool_counts.items()],
            key=lambda x: -x["count"],
        )[:10],
    }


@app.get("/api/files")
async def list_files(cwd: str = Query(...), q: str = Query(default=""), limit: int = Query(default=30)):
    base = Path(os.path.expanduser(cwd)).resolve()
    if not base.exists() or not base.is_dir():
        return []
    q_lower = q.lower()
    results: List[dict] = []
    for root, dirs, files in os.walk(str(base)):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            full = Path(root) / f
            try:
                rel = str(full.relative_to(base))
            except ValueError:
                continue
            if q_lower and q_lower not in rel.lower():
                continue
            results.append({"path": str(full), "rel": rel})
            if len(results) >= limit:
                return results
    return results


@app.get("/api/git")
async def git_status(cwd: str = Query(...)):
    target = os.path.expanduser(cwd)
    if not os.path.isdir(target):
        return {"branch": "", "dirty": 0, "available": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", target, "status", "--porcelain=v1", "--branch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        return {"branch": "", "dirty": 0, "available": False}
    if proc.returncode != 0:
        return {"branch": "", "dirty": 0, "available": False}
    branch = ""
    dirty = 0
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("##"):
            header = line[2:].strip()
            branch = header.split("...")[0].strip()
        else:
            dirty += 1
    return {"branch": branch, "dirty": dirty, "available": True}


app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        chunk = data.strip()
        if chunk:
            self._parts.append(chunk)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        collapsed = re.sub(r"[ \t]+", " ", raw)
        collapsed = re.sub(r"\n\s*", "\n", collapsed)
        return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def _is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
            return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        except Exception:
            return True


@app.post("/api/fetch-url")
async def fetch_url(req: FetchUrlRequest):
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="only http/https allowed")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid url")
    if _is_private_host(parsed.hostname):
        raise HTTPException(status_code=400, detail="refusing to fetch private/internal host")

    def _do_fetch() -> Dict[str, str]:
        request = urllib.request.Request(
            req.url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ClaudeWeb/1.0)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=", 1)[1].split(";")[0].strip()
            raw = resp.read(2 * 1024 * 1024)
        html = raw.decode(charset, errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else req.url
        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()
        return {"title": title, "content": text}

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _do_fetch)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"remote {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    limit = max(500, min(req.max_chars or 10000, 50000))
    content = result["content"][:limit]
    return {
        "url": req.url,
        "title": result["title"] or req.url,
        "content": content,
        "truncated": len(result["content"]) > limit,
        "length": len(result["content"]),
    }


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


def main():
    """CLI entry point for `claude-web` command."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Claude Code Web - Web UI for Claude Code CLI")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("PORT", "8765")), help="Port to listen on (default: 8765)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--open", action="store_true", help="Open browser after starting")
    parser.add_argument("--version", "-v", action="store_true", help="Show version")
    args = parser.parse_args()

    if args.version:
        from claude_web import __version__
        print(f"claude-web {__version__}")
        return

    print(f"Claude Code Web v{__import__('claude_web').__version__}")
    print(f"  → http://{args.host}:{args.port}")
    print(f"  → Data: {_DATA_DIR}")
    print()

    if args.open:
        import webbrowser
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
