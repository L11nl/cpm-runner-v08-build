"""Interactive PTY Python/shell runner. Deploy ONE instance per Railway user/account.

WARNING: Python and shell commands are untrusted. This service is not a multi-tenant sandbox.
Each instance must belong to one person, in their own Railway account. Never supply
Railway OAuth tokens or control-plane secrets to this instance.
"""
from __future__ import annotations

import codecs
import hmac
import io
import os
import pathlib
import pty
import resource
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import termios
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from .bootstrap import detect_kind
BOOTSTRAP_PATH = str(pathlib.Path(__file__).with_name("bootstrap.py"))

MAX_SCRIPT_BYTES = 256_000
MAX_ARCHIVE_BYTES = 8_000_000
MAX_EXTRACT_BYTES = 20_000_000
MAX_EXTRACT_FILES = 100
MAX_EVENTS = 256
MAX_LOG_CHARS = 250_000
SESSION_SECONDS = max(60, min(int(os.getenv("SESSION_SECONDS", "3600")), 86400))
MAX_RAM_MB = max(64, min(int(os.getenv("CHILD_RAM_MB", "256")), 2048))
MAX_CPU_SECONDS = max(20, min(int(os.getenv("CHILD_CPU_SECONDS", "120")), 3600))

app = FastAPI(title="Cloud Project Manager PTY Runner", version="0.9.6", docs_url=None, redoc_url=None)


class StartCode(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_SCRIPT_BYTES)
    language: str = "python"


class StartShell(BaseModel):
    command: str = Field(default="", max_length=2000)


class EnterInput(BaseModel):
    text: str = Field(max_length=8000)


@dataclass
class Session:
    id: str
    process: subprocess.Popen
    master_fd: int
    workdir: pathlib.Path
    born: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    events: list[tuple[int, str]] = field(default_factory=list)
    cursor: int = 0
    finished: bool = False
    truncated: bool = False
    char_count: int = 0
    output_updated: float = field(default_factory=time.monotonic)

    def append(self, output: str):
        if not output:
            return
        with self.lock:
            if self.char_count >= MAX_LOG_CHARS:
                if not self.truncated:
                    self.cursor += 1
                    self.events.append((self.cursor, "\n[OUTPUT LIMIT REACHED]\n"))
                    self.truncated = True
                return
            output = output[:MAX_LOG_CHARS - self.char_count]
            self.char_count += len(output)
            self.output_updated = time.monotonic()
            self.cursor += 1
            self.events.append((self.cursor, output))
            if len(self.events) > MAX_EVENTS:
                self.events = self.events[-MAX_EVENTS:]

    def read_loop(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                if time.monotonic() - self.born > SESSION_SECONDS and self.process.poll() is None:
                    self.append("\n[SESSION TIME LIMIT REACHED]\n")
                    self.kill()
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    try:
                        part = os.read(self.master_fd, 8192)
                    except OSError:
                        break
                    if not part:
                        break
                    self.append(decoder.decode(part))
                if self.process.poll() is not None and not ready:
                    # Drain remaining bytes if any on the next select.
                    ready, _, _ = select.select([self.master_fd], [], [], 0)
                    if not ready:
                        break
            self.append(decoder.decode(b"", final=True))
        finally:
            with self.lock:
                self.finished = True
            try:
                os.close(self.master_fd)
            except OSError:
                pass

    def kill(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


sessions: dict[str, Session] = {}
sessions_lock = threading.Lock()


def _require_auth(authorization: Optional[str]):
    token = os.getenv("RUNNER_API_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="Runner token not configured")
    expected = "Bearer " + token
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _child_limits(js=False):
    os.setsid()
    # RLIMIT_AS limits virtual address space, not actual RSS. pip, Python extension
    # modules and V8 can reserve far more virtual memory than their actual RSS.
    # The Railway cgroup enforces actual memory use; child CPU/file/time are capped.
    ram = (8 * 1024 * 1024 * 1024) if js else (4 * 1024 * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_AS, (ram, ram))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))


def _launch(workdir: pathlib.Path, argv: list[str]):
    with sessions_lock:
        for session_id, previous in list(sessions.items()):
            if previous.process.poll() is not None and time.monotonic() - previous.born > SESSION_SECONDS + 300:
                sessions.pop(session_id, None)
                shutil.rmtree(previous.workdir, ignore_errors=True)
        for session in sessions.values():
            if session.process.poll() is None:
                raise HTTPException(429, "Only one active session per runner")
        master, slave = pty.openpty()
        # No local-echo: user inputs such as API tokens will not reappear in program output.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin" + (":"+os.path.dirname(shutil.which("node")) if shutil.which("node") and not shutil.which("node").startswith(("/usr/local/bin/","/usr/bin/","/bin/")) else ""),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "TERM": "dumb",
            "PS1": "$ ",
            "SHELL": "/bin/sh",
        }
        try:
            child = subprocess.Popen(
                argv, cwd=str(workdir), stdin=slave, stdout=slave, stderr=slave,
                env=env, close_fds=True, preexec_fn=lambda: _child_limits(argv[-1]=='javascript'),
            )
        except Exception:
            os.close(master)
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            os.close(slave)
        s = Session(id=str(uuid.uuid4()), process=child, master_fd=master, workdir=workdir)
        sessions[s.id] = s
        threading.Thread(target=s.read_loop, daemon=True).start()
    return {"session_id": s.id, "state": "running"}


def _workspace():
    return pathlib.Path(tempfile.mkdtemp(prefix="cpm_"))


def _get_session(session_id: str) -> Session:
    with sessions_lock:
        session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found (runner might have restarted)")
    return session


def _unzip_safely(content: bytes, destination: pathlib.Path) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP archive")
    files = archive.infolist()
    if len(files) > MAX_EXTRACT_FILES:
        raise HTTPException(413, "ZIP has too many entries")
    if sum(f.file_size for f in files) > MAX_EXTRACT_BYTES:
        raise HTTPException(413, "ZIP uncompressed size is too large")
    for item in files:
        p = pathlib.PurePosixPath(item.filename)
        # Disallow absolute paths, traversal, windows separators and symlinks.
        if (not item.filename or p.is_absolute() or ".." in p.parts
                or "\\" in item.filename or item.filename.startswith("/")
                or (item.external_attr >> 16) & 0o170000 == stat.S_IFLNK):
            raise HTTPException(400, "ZIP contains unsafe paths")
        target = destination.joinpath(*p.parts)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    for name in ("main.py", "bot.py", "app.py", "run.py", "index.js", "main.js", "bot.js", "app.js"):
        matches = sorted(destination.rglob(name))
        if matches:
            return str(matches[0].relative_to(destination))
    raise HTTPException(400, "ZIP needs main.py, bot.py, app.py, run.py, index.js, main.js, bot.js or app.js")


@app.get("/health")
def health(authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    return {"ok": True, "version": "0.9.6", "capabilities": ["python3", "pty", "recent_output", "exit_code", "file_upload"]}


@app.post("/sessions")
def start_code(body: StartCode, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    if body.language.lower() not in ("python", "py", "javascript", "js"):
        raise HTTPException(400, "Supported: python, javascript")
    language = 'javascript' if body.language.lower() in ('js','javascript') else 'python'
    workdir = _workspace()
    filename = 'main.js' if language == 'javascript' else 'main.py'
    (workdir / filename).write_text(body.code, encoding='utf-8')
    return _launch(workdir, ['python3','-u',BOOTSTRAP_PATH,str(workdir),filename,language])


@app.post("/sessions/shell")
def start_shell(body: StartShell, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    workdir = _workspace()
    return _launch(workdir, ["/bin/sh", "-lc", body.command] if body.command else ["/bin/sh", "-i"])


@app.post("/sessions/upload")
async def start_file(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    name = (file.filename or "").lower()
    if not name.endswith((".py", ".js", ".txt", ".zip")):
        raise HTTPException(400, "Upload .py, .js, .txt or .zip")
    content = await file.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, "File exceeds 8 MB")
    workdir = _workspace()
    try:
        if not name.endswith('.zip'):
            if len(content) > MAX_SCRIPT_BYTES:
                raise HTTPException(413, 'Script exceeds 256 KB')
            try:
                text = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise HTTPException(400, 'The code file must be UTF-8')
            language = detect_kind(name,text)
            # Preserve the uploaded .py/.js basename so __file__, traceback and
            # relative imports behave like an ordinary Python/Node invocation.
            # .txt needs a language-appropriate extension for Node ESM support.
            original_name = file.filename or ''
            safe_basename = pathlib.Path(original_name).name
            if (original_name==safe_basename and
                    re.fullmatch(r'[\w .()-]{1,100}\.(?:py|js)',safe_basename,re.I)):
                entry=safe_basename
            else:
                entry='main.js' if language=='javascript' else 'main.py'
            (workdir / entry).write_text(text,encoding='utf-8')
        else:
            entry = _unzip_safely(content, workdir)
            language = 'javascript' if entry.endswith('.js') else 'python'
        return _launch(workdir, ['python3','-u',BOOTSTRAP_PATH,str(workdir),entry,language])
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


@app.get("/sessions/{session_id}/events")
def events(session_id: str, after: int = 0, wait_ms: int = 0, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    s = _get_session(session_id)
    cutoff = time.monotonic() + min(max(wait_ms, 0), 1500) / 1000
    while wait_ms > 0 and time.monotonic() < cutoff:
        with s.lock:
            has_changes = s.cursor > after or s.finished or s.process.poll() is not None
        if has_changes:
            break
        time.sleep(0.04)
    with s.lock:
        messages = [{"cursor": n, "text": msg} for n, msg in s.events if n > after]
        state = "finished" if s.finished or s.process.poll() is not None else "running"
        exit_code = s.process.poll()
        last_cursor = s.cursor
        tail = "".join(text for _, text in s.events)[-3500:]
        last_output_age = round(time.monotonic() - s.output_updated, 1)
        uptime_seconds = round(time.monotonic() - s.born, 1)
        output_present = s.char_count > 0
    # A snapshot tail means that manual Telegram refresh remains meaningful even
    # after the event cursor has advanced. There is NO claim that silence = failure.
    return {"session_id": session_id, "state": state, "exit_code": exit_code,
            "events": messages[:80], "cursor": messages[min(len(messages), 80)-1]["cursor"] if messages else after,
            "latest_cursor": last_cursor, "tail": tail,
            "output_age_seconds": last_output_age,
            "uptime_seconds": uptime_seconds,
            "output_present": output_present, "runner_version": "0.9.6"}


@app.post("/sessions/{session_id}/input")
def send_input(session_id: str, body: EnterInput, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    s = _get_session(session_id)
    if s.process.poll() is not None:
        raise HTTPException(409, "Session has finished")
    try:
        os.write(s.master_fd, (body.text.replace("\r", "") + "\n").encode("utf-8"))
    except OSError:
        raise HTTPException(409, "Session stdin closed")
    return {"ok": True}


@app.post("/sessions/{session_id}/stop")
def stop(session_id: str, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    s = _get_session(session_id)
    s.kill()
    return {"ok": True}
