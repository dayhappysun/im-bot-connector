#!/usr/bin/env python3
"""
Agent Listener for im-bot (v5 - sessions, attachments, model-switch)
=====================================================================
Persistent Socket.io connection to im-bot's /agent namespace. Responds via a
real agent CLI - Hermes Agent OR OpenClaw (auto-detected, CLI-compatible).

FEATURES
  - Per-room persistent sessions (1 room = 1 agent session, real memory).
  - Backend-agnostic: Hermes or OpenClaw (IMBOT_BACKEND / IMBOT_AGENT_BIN).
  - Attachments: file messages from users are surfaced to the agent with
    absolute download URLs so it can fetch and process them.
  - In-chat model switching: a user can say "/model <name>" (or natural
    language). The listener PROBES the candidate model and only applies it
    if the probe succeeds, otherwise it rolls back to the previous model.

Usage:
  python3 hermes_imbot_listener.py [--debug]

Env:
  IMBOT_URL, INVITE_CODE, IMBOT_BACKEND (hermes|openclaw|auto),
  IMBOT_AGENT_BIN, IMBOT_MODEL, IMBOT_TOOLSETS, IMBOT_TIMEOUT, IMBOT_SOURCE
Credentials fall back to ~/.hermes/imbot_agent.json.
"""

import os
import re
import sys
import json
import time
import uuid
import signal
import shutil
import logging
import threading
import subprocess
import socketio

# -- Configuration ----------------------------------
IMBOT_URL = os.environ.get('IMBOT_URL', 'https://im-bot.net')
INVITE_CODE = os.environ.get('INVITE_CODE', 'YOUR_AGENT_INVITE_CODE')
IMBOT_MODEL = os.environ.get('IMBOT_MODEL', '')
IMBOT_TOOLSETS = os.environ.get('IMBOT_TOOLSETS', '')  # empty = profile default
# IMBOT_TIMEOUT NO LONGER KILLS the run. It is the cadence (seconds) at which we
# post a progress / "still working" update to the chat while a long task runs.
# (Set it small, e.g. 60, for a once-a-minute heartbeat.)
IMBOT_TIMEOUT = int(os.environ.get('IMBOT_TIMEOUT', '60'))
# Hard safety backstop. 0 = UNLIMITED (default): a reasonable task may run for
# hours — it is never cut off on a wall clock. Set >0 only as an outer cap to
# reap genuinely-leaked processes.
IMBOT_HARD_TIMEOUT = int(os.environ.get('IMBOT_HARD_TIMEOUT', '0'))
IMBOT_SOURCE = os.environ.get('IMBOT_SOURCE', 'imbot')  # session source tag
# Hermes writes a structured per-session log here; tool events appear in it even
# in -Q mode, so we tail it to stream tool-execution progress into the chat.
AGENT_LOG_FILE = os.path.expanduser(
    os.environ.get('IMBOT_AGENT_LOG', '~/.hermes/logs/agent.log'))

CONFIG_FILE = os.path.expanduser('~/.hermes/imbot_agent.json')
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if not os.environ.get('INVITE_CODE'):
        INVITE_CODE = cfg.get('inviteCode', INVITE_CODE)
    IMBOT_URL = cfg.get('serverUrl', IMBOT_URL)

# -- Agent backend selection (Hermes or OpenClaw) ---
BACKEND_BINS = {
    'hermes':   ['hermes'],
    'openclaw': ['openclaw', 'claw'],
}


def _find_bin(name):
    cand = os.path.expanduser('~/.local/bin/' + name)
    if os.path.exists(cand) and os.access(cand, os.X_OK):
        return cand
    return shutil.which(name)


def _detect_backend():
    forced = os.environ.get('IMBOT_BACKEND', 'auto').strip().lower()
    explicit_bin = os.environ.get('IMBOT_AGENT_BIN', '').strip()

    def _infer_from_path(p):
        return 'openclaw' if 'claw' in os.path.basename(p).lower() else 'hermes'

    if explicit_bin:
        be = forced if forced in BACKEND_BINS else _infer_from_path(explicit_bin)
        return be, os.path.expanduser(explicit_bin)

    search = ['hermes', 'openclaw'] if forced in ('auto', '') else [forced]
    for be in search:
        for binname in BACKEND_BINS.get(be, []):
            path = _find_bin(binname)
            if path:
                return be, path

    fallback_be = forced if forced in BACKEND_BINS else 'hermes'
    return fallback_be, os.path.expanduser('~/.local/bin/' + BACKEND_BINS[fallback_be][0])


BACKEND, AGENT_BIN = _detect_backend()

SESSION_MAP_FILE = os.path.expanduser('~/.hermes/imbot_sessions.json')
MODEL_MAP_FILE = os.path.expanduser('~/.hermes/imbot_room_models.json')

# -- Logging ----------------------------------------
LOG_LEVEL = logging.DEBUG if '--debug' in sys.argv else logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('imbot-agent')

# -- Global state -----------------------------------
sio = socketio.Client(logger=LOG_LEVEL == logging.DEBUG)
agent_id = None
known_rooms = set()
shutting_down = False

room_sessions = {}     # room_id -> session_id (1 room = 1 session)
room_models = {}       # room_id -> model override (set via in-chat command)
_room_locks = {}
_locks_guard = threading.Lock()

_SESSION_ID_RE = re.compile(r'^session_id:\s*([0-9a-zA-Z_]+)\s*$')

# In-chat model-switch command patterns
_MODEL_CMD_RES = [
    re.compile(r'^/model\s*(?P<m>\S.*)?$', re.IGNORECASE),
    re.compile(r'^/switch[- ]?model\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^(?:switch|change)\s+(?:the\s+)?model\s+to\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^use\s+(?:the\s+)?model\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^(?:切换|更换|换)(?:到|成|为)?\s*模型\s*(?:到|成|为)?\s*(?P<m>\S.+)$'),
    re.compile(r'^用\s*(?P<m>\S.+?)\s*模型$'),
]


def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log.warning("Could not load %s: %s" % (path, e))
    return dict(default)


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Could not save %s: %s" % (path, e))


def _room_lock(room_id):
    with _locks_guard:
        lk = _room_locks.get(room_id)
        if lk is None:
            lk = threading.Lock()
            _room_locks[room_id] = lk
        return lk


def build_system_preamble():
    return (
        "[SYSTEM CONTEXT - read once] You are an AI agent chatting with a user "
        "through im-bot, a multi-agent instant-messaging app. This conversation "
        "is one continuous chat session that maps to a single im-bot room - you "
        "keep full memory of it across turns. Be helpful, concise, and friendly. "
        "Your replies are sent as chat messages, so use natural language. "
        "Keep replies reasonably short.\n\n"
        "The user's first message follows:\n"
    )


def _strip_tool_blocks(text):
    """Remove Hermes tool-execution rendering (e.g. a '┊ review diff' header plus
    the git-diff body) from -Q output, keeping only the assistant's prose reply.
    -Q suppresses tool *previews* but still prints patch/write_file diffs."""
    lines = (text or '').split('\n')
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if '┊' in line:                       # tool-rendering gutter marker
            i += 1
            # skip file header line(s):  a/path → b/path
            while i < n and ('→' in lines[i] or lines[i].lstrip().startswith(('a/', 'b/'))):
                i += 1
            # skip diff hunks: @@ headers, then +/-/space (and blank) body lines
            saw_hunk = False
            while i < n:
                l = lines[i]
                if l.startswith('@@'):
                    saw_hunk = True
                    i += 1
                    continue
                if saw_hunk and (l[:1] in '+- ' or l.strip() == ''):
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1
    return '\n'.join(out).strip()


def _parse_agent_output(stdout, stderr):
    """reply on STDOUT (minus the toolset warning), session_id on STDERR."""
    session_id = None
    for line in (stderr or '').splitlines():
        m = _SESSION_ID_RE.match(line.strip())
        if m:
            session_id = m.group(1)
            break
    kept = []
    for line in (stdout or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('Warning:'):
            continue
        if stripped.startswith('\u21bb Resumed session'):
            continue
        if _SESSION_ID_RE.match(stripped):
            continue
        kept.append(line)
    return _strip_tool_blocks('\n'.join(kept)), session_id


def parse_model_command(text):
    """Return the target model name if text is a model-switch command.
    Returns '' for a bare '/model' (query current), or None if not a command."""
    t = (text or '').strip()
    for rx in _MODEL_CMD_RES:
        m = rx.match(t)
        if m:
            val = (m.groupdict().get('m') or '').strip()
            return val
    return None


def _abs_url(url):
    if url and url.startswith('/'):
        return IMBOT_URL.rstrip('/') + url
    return url


def extract_attachments(metadata):
    """Parse attachment list from message metadata (JSON string or dict)."""
    if not metadata:
        return []
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else metadata
        return meta.get('attachments', []) or []
    except Exception:
        return []


def build_effective_content(content, attachments):
    """Augment the user's text with attachment context the agent can act on."""
    if not attachments:
        return content
    lines = ["[The user attached file(s). Download and process them as needed:]"]
    for a in attachments:
        name = a.get('fileName', 'file')
        mime = a.get('mimeType', '')
        size = a.get('fileSize', '?')
        url = _abs_url(a.get('downloadUrl') or '')
        lines.append("- %s (%s, %s bytes): %s" % (name, mime, size, url))
    block = "\n".join(lines)
    return (content + "\n\n" + block) if content else block


def probe_model(model):
    """Run a tiny test query with the candidate model. (ok, err_message)."""
    cmd = [AGENT_BIN, 'chat', '-q', 'Reply with exactly: OK',
           '-m', model, '--source', IMBOT_SOURCE + '-probe', '-Q']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return False, 'probe timed out'
    except Exception as e:
        return False, str(e)[:140]
    # Clean up the throwaway probe session
    _, sid = _parse_agent_output(r.stdout, r.stderr)
    if sid:
        try:
            subprocess.run([AGENT_BIN, 'sessions', 'delete', sid],
                           input='y\n', capture_output=True, text=True, timeout=20)
        except Exception:
            pass
    if r.returncode == 0 and (r.stdout or '').strip():
        return True, ''
    err = ((r.stderr or '') + ' ' + (r.stdout or '')).strip()
    return False, (err[:140] or 'no output')


def handle_model_switch(room_id, target):
    """Probe + apply (or roll back) a model switch for a room. Returns reply."""
    lock = _room_lock(room_id)
    with lock:
        if target == '':
            cur = room_models.get(room_id) or IMBOT_MODEL or '(profile default)'
            return "Current model for this room: %s\n(Use `/model <name>` to switch.)" % cur

        prev = room_models.get(room_id)
        log.info("Probing model '%s' for room %s..." % (target, room_id))
        ok, err = probe_model(target)
        if ok:
            room_models[room_id] = target
            _save_json(MODEL_MAP_FILE, room_models)
            log.info("Room %s model -> %s" % (room_id, target))
            return "Switched to model `%s` for this room." % target
        # Probe failed -> roll back (keep prev / profile default)
        kept = prev or IMBOT_MODEL or 'the current model'
        log.warning("Model '%s' probe failed (%s); kept %s" % (target, err, kept))
        return ("Could not switch to `%s` - it failed a quick test (%s). "
                "Keeping %s." % (target, err, ('`%s`' % kept) if prev or IMBOT_MODEL else kept))


def _build_cmd(resume_sid, content, room_id):
    """Build the agent CLI command (Hermes/OpenClaw share this surface)."""
    if resume_sid:
        cmd = [AGENT_BIN, '-r', resume_sid, 'chat', '-q', content,
               '--source', IMBOT_SOURCE, '-Q']
    else:
        full = build_system_preamble() + content
        cmd = [AGENT_BIN, 'chat', '-q', full, '--source', IMBOT_SOURCE, '-Q']
    model = room_models.get(room_id) or IMBOT_MODEL
    if model:
        cmd.extend(['-m', model])
    if IMBOT_TOOLSETS:
        cmd.extend(['-t', IMBOT_TOOLSETS])
    return cmd


# -- Live tool-execution progress (tail ~/.hermes/logs/agent.log) ----
# Hermes logs one structured line per tool/turn, tagged with [session_id],
# even in -Q mode. We tail that file during a run and relay tool activity to
# the chat so a long task shows its work instead of looking frozen.
_AGENT_LOG_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2} [\d:,]+ \w+ \[(?P<sid>[0-9A-Za-z_]+)\] '
    r'(?P<comp>[\w.]+): (?P<msg>.*)$'
)
_TOOL_DONE_RE = re.compile(r'^tool (?P<name>\S+) completed \((?P<dur>[\d.]+)s')

# Cross-run state so concurrent rooms attribute log lines to the right session.
_inflight_ids = set()
_inflight_guard = threading.Lock()
_new_session_gate = threading.Lock()  # serialize NEW-session id acquisition

# Cancellable in-flight turns: task_id -> Popen, plus task_ids the user asked to
# stop. on_task_cancel kills the process; call_agent then returns a cancel notice.
_running_procs = {}
_cancelled = set()
_procs_guard = threading.Lock()


class _LogTail:
    """Follow an append-only (possibly rotating) log from its current end."""

    def __init__(self, path):
        self.path = path
        self._buf = ''
        try:
            st = os.stat(path)
            self.ino, self.pos = st.st_ino, st.st_size  # start at EOF
        except OSError:
            self.ino, self.pos = None, 0

    def read_new_lines(self):
        out = []
        try:
            st = os.stat(self.path)
        except OSError:
            return out
        if self.ino is None:
            self.ino, self.pos, self._buf = st.st_ino, 0, ''
        elif st.st_ino != self.ino or st.st_size < self.pos:  # rotated/truncated
            self.ino, self.pos, self._buf = st.st_ino, 0, ''
        if st.st_size <= self.pos:
            return out
        try:
            with open(self.path, 'r', errors='replace') as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return out
        self._buf += data
        *full, self._buf = self._buf.split('\n')
        return full


def _format_tool_progress(events):
    """events: list of (name, dur) -> compact, deduped, ordered chat line."""
    from collections import Counter
    names = [n for (n, _d) in events]
    counts = Counter(names)
    parts = []
    for name in dict.fromkeys(names):  # preserve first-seen order, dedupe
        n = counts[name]
        parts.append("%s%s" % (name, (" ×%d" % n) if n > 1 else ""))
    return "🔧 正在处理：" + "、".join(parts) + " …"


def _drain(stream, sink):
    try:
        sink.append(stream.read())
    except Exception:
        pass


def _report_session_map(room_id, session_id):
    """Persist this room's session id to the server (per agent, per room) so a
    fresh connector can recover its sessions after a reinstall/migration."""
    try:
        if sio.connected:
            sio.emit('session:map', {'roomId': room_id, 'sessionId': session_id},
                     namespace='/agent')
    except Exception as e:
        log.debug("session:map emit failed: %s" % e)


def _task_summary(content, attachments):
    """A few-word task overview for the room's running-task list."""
    s = (content or '').strip().replace('\n', ' ')
    if not s and attachments:
        s = "处理附件 %s" % (attachments[0].get('fileName', '文件'))
    s = s or "处理请求"
    return s if len(s) <= 24 else (s[:24] + '…')


def call_agent(content, room_id, send_progress=None, task_id=None):
    """Generate a reply via the room's persistent agent session.

    Streams real tool-execution progress to the chat while running, posts a
    "still working" notice on quiet stretches, and only hard-kills a run after
    IMBOT_HARD_TIMEOUT (never on the soft IMBOT_TIMEOUT cadence).
    """
    if send_progress is None:
        send_progress = lambda _text: None

    lock = _room_lock(room_id)
    with lock:
        existing_sid = room_sessions.get(room_id)
        env = os.environ.copy()
        # HERMES_INFERENCE_MODEL only meaningful for hermes; -m handles per-room
        if IMBOT_MODEL and BACKEND == 'hermes' and not room_models.get(room_id):
            env['HERMES_INFERENCE_MODEL'] = IMBOT_MODEL

        cmd = _build_cmd(existing_sid, content, room_id)

        # Progress streaming only when we can read Hermes' structured log.
        stream_ok = (BACKEND == 'hermes' and os.path.exists(AGENT_LOG_FILE))
        tail = _LogTail(AGENT_LOG_FILE) if stream_ok else None

        # For a NEW session we don't know the session id up-front; serialize
        # new-session claiming so "first unclaimed id in the log" is unambiguous.
        new_session = existing_sid is None
        claimed = existing_sid
        gate_held = False
        if stream_ok and new_session:
            _new_session_gate.acquire()
            gate_held = True
        if claimed:
            with _inflight_guard:
                _inflight_ids.add(claimed)

        def _release():
            nonlocal gate_held
            if gate_held:
                try:
                    _new_session_gate.release()
                finally:
                    gate_held = False
            if claimed:
                with _inflight_guard:
                    _inflight_ids.discard(claimed)

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
        except FileNotFoundError:
            _release()
            log.error("Agent binary not found at %s" % AGENT_BIN)
            return "Agent CLI not found. Please check your installation."
        except Exception as e:
            _release()
            log.error("Agent error: %s" % e)
            return "Internal error: %s" % str(e)[:200]

        if task_id:
            with _procs_guard:
                _running_procs[task_id] = proc

        out_buf, err_buf = [], []
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)
        t_out.start()
        t_err.start()

        start = time.time()
        last_update = start
        pending = []      # (tool_name, dur) since last forwarded progress
        killed = False

        try:
            while True:
                rc = proc.poll()

                # 1) Relay any new tool activity from the structured log.
                if tail is not None:
                    for line in tail.read_new_lines():
                        m = _AGENT_LOG_RE.match(line)
                        if not m:
                            continue
                        sid = m.group('sid')
                        if claimed is None and new_session:
                            with _inflight_guard:
                                if sid not in _inflight_ids:
                                    claimed = sid
                                    _inflight_ids.add(sid)
                            if claimed == sid and gate_held:
                                _new_session_gate.release()
                                gate_held = False
                        if claimed is None or sid != claimed:
                            continue
                        if m.group('comp') == 'agent.tool_executor':
                            tm = _TOOL_DONE_RE.match(m.group('msg'))
                            if tm:
                                pending.append((tm.group('name'), tm.group('dur')))

                # 2) Cadence: one progress / "still working" update per interval.
                now = time.time()
                if rc is None and (now - last_update) >= IMBOT_TIMEOUT:
                    if pending:
                        send_progress(_format_tool_progress(pending))
                        pending = []
                    else:
                        send_progress("⏳ 仍在处理中，请稍候…")
                    last_update = now

                if rc is not None:
                    break

                # 3) Hard backstop — DISABLED by default (IMBOT_HARD_TIMEOUT=0).
                # A reasonable task may run for hours and is never cut off on a
                # wall clock; >0 is only an outer cap to reap leaked processes.
                if IMBOT_HARD_TIMEOUT > 0 and (now - start) >= IMBOT_HARD_TIMEOUT:
                    proc.kill()
                    killed = True
                    break

                time.sleep(0.4)
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            _release()
            if task_id:
                with _procs_guard:
                    _running_procs.pop(task_id, None)

        stdout = ''.join(out_buf)
        stderr = ''.join(err_buf)

        if task_id and task_id in _cancelled:
            if claimed:
                room_sessions[room_id] = claimed
                _save_json(SESSION_MAP_FILE, room_sessions)
                _report_session_map(room_id, claimed)
            log.info("Turn cancelled by user (room %s, task %s)" % (room_id, task_id))
            return "🛑 任务已取消。"

        if killed:
            mins = IMBOT_HARD_TIMEOUT // 60
            log.error("Hard timeout %ss (room %s) — killed" % (IMBOT_HARD_TIMEOUT, room_id))
            return ("⚠️ 这个任务运行超过 %d 分钟仍未结束，我先停下了。"
                    "可以把它拆成更小的步骤再试一次。" % mins)

        if proc.returncode != 0:
            err_msg = stderr.strip() if stderr else 'empty response'
            log.error("Agent exited %s: %s" % (proc.returncode, err_msg[:200]))
            if existing_sid and 'session' in err_msg.lower():
                log.warning("Dropping stale session %s for room %s" % (existing_sid, room_id))
                room_sessions.pop(room_id, None)
                _save_json(SESSION_MAP_FILE, room_sessions)
            return "Sorry, I had trouble processing that. (%s)" % err_msg[:100]

        reply, parsed_sid = _parse_agent_output(stdout, stderr)
        final_sid = parsed_sid or claimed
        if final_sid and room_sessions.get(room_id) != final_sid:
            room_sessions[room_id] = final_sid
            _save_json(SESSION_MAP_FILE, room_sessions)
            _report_session_map(room_id, final_sid)
            if not existing_sid:
                log.info("Room %s <-> session %s" % (room_id, final_sid))

        if not reply:
            log.error("Empty reply after cleaning (room %s)" % room_id)
            return "Sorry, I didn't get a response. Could you try again?"
        return reply


def _run_turn(room_id, effective, task_id, summary, sender_name):
    """Run one agent turn OFF the socket read loop so task:cancel (and other
    rooms' messages) can still be received while this turn is running."""
    def send_progress(text):
        try:
            sio.emit('message:send',
                     {'roomId': room_id, 'content': text, 'msgType': 'text'},
                     namespace='/agent')
            sio.emit('typing:start', {'roomId': room_id}, namespace='/agent')
        except Exception as e:
            log.warning("progress emit failed: %s" % e)

    sio.emit('typing:start', {'roomId': room_id}, namespace='/agent')
    try:
        sio.emit('task:start',
                 {'roomId': room_id, 'taskId': task_id, 'summary': summary},
                 namespace='/agent')
    except Exception:
        pass
    try:
        reply = call_agent(effective, room_id, send_progress, task_id)
        sio.emit('message:send', {'roomId': room_id, 'content': reply, 'msgType': 'text'},
                 namespace='/agent')
        log.info("Replied to %s (%d chars)" % (sender_name, len(reply)))
    except Exception as e:
        log.error("Error generating reply: %s" % e)
        sio.emit('message:send', {'roomId': room_id, 'content': "Error: %s" % str(e)[:200],
                                  'msgType': 'text'}, namespace='/agent')
    finally:
        sio.emit('typing:stop', {'roomId': room_id}, namespace='/agent')
        try:
            sio.emit('task:end', {'roomId': room_id, 'taskId': task_id}, namespace='/agent')
        except Exception:
            pass
        with _procs_guard:
            _cancelled.discard(task_id)


# -- Socket.io Event Handlers -----------------------
@sio.on('connect', namespace='/agent')
def on_connect():
    log.info("Connected to im-bot /agent namespace at %s" % IMBOT_URL)


@sio.on('disconnect', namespace='/agent')
def on_disconnect():
    log.warning("Disconnected from im-bot (agent: %s)" % agent_id)


@sio.on('welcome', namespace='/agent')
def on_welcome(data):
    global agent_id, known_rooms
    agent_id = data.get('agentId')
    rooms = data.get('rooms', [])
    known_rooms = set(rooms)
    # Restore server-side per-(room,agent) session map so a fresh connector
    # recovers its sessions (memory) without starting over.
    server_sessions = data.get('sessions') or {}
    if isinstance(server_sessions, dict) and server_sessions:
        changed = False
        for _rid, _sid in server_sessions.items():
            if _sid and room_sessions.get(_rid) != _sid:
                room_sessions[_rid] = _sid
                changed = True
        if changed:
            _save_json(SESSION_MAP_FILE, room_sessions)
            log.info("Restored %d session map(s) from server" % len(server_sessions))
    log.info("Welcome: %s" % data.get('message'))
    log.info("   Agent ID: %s" % agent_id)
    log.info("   Rooms: %s" % rooms)
    log.info("   Mode: per-room sessions | backend: %s | model-switch: on | attachments: on"
             % BACKEND)
    for room_id in rooms:
        sio.emit('room:join', room_id, namespace='/agent')


@sio.on('message:new', namespace='/agent')
def on_message(msg):
    content = msg.get('content', '') or ''
    sender_name = msg.get('senderName', 'Unknown')
    room_id = msg.get('roomId')
    sender_type = msg.get('senderType', 'user')

    if sender_type == 'agent':
        return

    attachments = extract_attachments(msg.get('metadata'))

    # Allow attachment-only messages; ignore truly empty ones
    if not content.strip() and not attachments:
        return

    log.info("Message from %s in room %s: %s%s"
             % (sender_name, room_id, content[:80],
                (" [+%d attachment(s)]" % len(attachments)) if attachments else ""))

    # ── In-chat model switch command (probe + rollback) ──
    target = parse_model_command(content)
    if target is not None and not attachments:
        sio.emit('typing:start', {'roomId': room_id}, namespace='/agent')
        try:
            reply = handle_model_switch(room_id, target)
            sio.emit('message:send', {'roomId': room_id, 'content': reply, 'msgType': 'text'},
                     namespace='/agent')
        finally:
            sio.emit('typing:stop', {'roomId': room_id}, namespace='/agent')
        return

    # ── Normal agent turn — run in a worker thread so the socket read loop
    #    stays free to receive task:cancel (and other rooms) mid-turn.
    effective = build_effective_content(content, attachments)
    task_id = uuid.uuid4().hex[:12]
    summary = _task_summary(content, attachments)
    threading.Thread(target=_run_turn,
                     args=(room_id, effective, task_id, summary, sender_name),
                     daemon=True).start()


@sio.on('session:state', namespace='/agent')
def on_session_state(data):
    log.debug("Session state: %s" % str(data)[:200])


@sio.on('heartbeat:ack', namespace='/agent')
def on_heartbeat_ack(data):
    log.debug("Heartbeat ack: %s" % data.get('timestamp'))


@sio.on('error', namespace='/agent')
def on_error(data):
    log.error("Server error: %s" % data)


@sio.on('*', namespace='/agent')
def on_any(event, data):
    if event not in ('message:new', 'heartbeat:ack', 'session:state'):
        log.debug("  Event '%s': %s" % (event, str(data)[:200]))


@sio.on('task:cancel', namespace='/agent')
def on_task_cancel(data):
    tid = (data or {}).get('taskId')
    if not tid:
        return
    with _procs_guard:
        _cancelled.add(tid)
        proc = _running_procs.get(tid)
    if proc:
        try:
            proc.kill()
        except Exception as e:
            log.debug("cancel kill failed: %s" % e)
    log.info("🛑 Cancel requested for task %s" % tid)


# -- Connection -------------------------------------
def connect():
    max_retries = 10
    retry_delay = 3
    for attempt in range(max_retries):
        try:
            log.info("Connecting to %s/agent (attempt %d)..." % (IMBOT_URL, attempt + 1))
            sio.connect(
                IMBOT_URL,
                socketio_path='/socket.io',
                transports=['websocket'],
                auth={'token': INVITE_CODE},
                namespaces=['/agent'],
                wait_timeout=10,
            )
        except Exception:
            pass
        sio.sleep(1)
        if sio.connected:
            log.info("Connected to im-bot /agent namespace")
            return True
        delay = min(retry_delay * (2 ** attempt), 60)
        log.error("Connection attempt %d failed. Retrying in %ss..." % (attempt + 1, delay))
        time.sleep(delay)
    log.critical("Failed to connect after max retries")
    return False


def main():
    global shutting_down, room_sessions, room_models

    def graceful_shutdown(signum, frame):
        global shutting_down
        if shutting_down:
            return
        shutting_down = True
        log.info("Received signal %s, shutting down..." % signum)
        sio.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    log.info("im-bot Agent Listener v5 (sessions + attachments + model-switch)")
    log.info("   Server: %s" % IMBOT_URL)
    log.info("   Invite code: %s..." % INVITE_CODE[:8])
    log.info("   Backend: %s (%s)" % (BACKEND, AGENT_BIN))
    log.info("   Model: %s" % (IMBOT_MODEL or '(profile default)'))
    log.info("   Toolsets: %s" % (IMBOT_TOOLSETS or 'profile default'))
    _hardcap = ("%ss" % IMBOT_HARD_TIMEOUT) if IMBOT_HARD_TIMEOUT > 0 else "unlimited"
    log.info("   Progress every: %ss | Hard cap: %s | Source: %s"
             % (IMBOT_TIMEOUT, _hardcap, IMBOT_SOURCE))

    if not os.path.exists(AGENT_BIN):
        log.warning("Agent binary not found at %s - set IMBOT_AGENT_BIN or install hermes/openclaw" % AGENT_BIN)

    room_sessions = _load_json(SESSION_MAP_FILE, {})
    room_models = _load_json(MODEL_MAP_FILE, {})
    log.info("   Loaded %d session map(s), %d model override(s)" % (len(room_sessions), len(room_models)))

    if not connect():
        sys.exit(1)

    try:
        while not shutting_down:
            sio.sleep(30)
            try:
                sio.emit('heartbeat', {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ')},
                         namespace='/agent')
            except Exception:
                pass
    except KeyboardInterrupt:
        pass
    log.info("Shutdown complete.")


if __name__ == '__main__':
    main()
