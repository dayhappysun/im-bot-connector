#!/usr/bin/env python3
"""
im-bot Agent Listener (v6 — unified backend, async socket.io)
===============================================================
Persistent Socket.io connection to im-bot's /agent namespace. Responds via a
real agent CLI — Hermes Agent, OpenClaw, or Claude (auto-detected).

FEATURES (merged from hermes v5 + openclaw v4)
  - Per-room persistent sessions (1 room = 1 agent session).
  - Backend-agnostic: Hermes, OpenClaw, or Claude (IMBOT_BACKEND / IMBOT_AGENT_BIN).
  - Async socket.io (aiohttp) — works reliably across all platforms.
  - MEDIA: file sharing — unified parsing + openclaw mediaUrl JSON extraction.
  - Attachments: file messages surfaced to agent with download URLs.
  - In-chat model switching (/model <name>) with probe + rollback.
  - Interrupt (插话): cancel mid-turn, merge queued messages.
  - Typing indicators + task list integration (task:start/task:end).
  - Self-echo filtering (senderType=agent).
  - Heartbeat self-healing with stale detection + forced reconnect.
  - Content safety output scanning.

Usage:
  python3 hermes_imbot_listener.py [--debug]

Env:
  IMBOT_URL, INVITE_CODE, IMBOT_BACKEND (hermes|openclaw|claude|auto),
  IMBOT_AGENT_BIN, IMBOT_MODEL, IMBOT_TOOLSETS, IMBOT_TIMEOUT,
  IMBOT_HARD_TIMEOUT, IMBOT_SOURCE
Credentials fall back to ~/.hermes/imbot_agent.json.
"""

import os, re, sys, json, time, uuid, signal, shutil, logging, threading
import subprocess, base64, mimetypes, asyncio
import socketio

# ── Configuration ──────────────────────────────────────────────────────────
IMBOT_URL       = os.environ.get('IMBOT_URL',       'https://im-bot.net')
INVITE_CODE     = os.environ.get('INVITE_CODE',     'YOUR_AGENT_INVITE_CODE')
IMBOT_MODEL     = os.environ.get('IMBOT_MODEL',     '')
IMBOT_TOOLSETS  = os.environ.get('IMBOT_TOOLSETS',  'web,browser,terminal,file,code_execution,vision,memory,session_search,skills,todo,delegation')
IMBOT_TIMEOUT   = int(os.environ.get('IMBOT_TIMEOUT', '60'))       # progress cadence (s)
IMBOT_HARD_TIMEOUT = int(os.environ.get('IMBOT_HARD_TIMEOUT', '0')) # 0=unlimited
IMBOT_SOURCE    = os.environ.get('IMBOT_SOURCE',    'imbot')
IMBOT_HB_INTERVAL  = int(os.environ.get('IMBOT_HB_INTERVAL', '25'))
IMBOT_STALE_AFTER  = int(os.environ.get('IMBOT_STALE_AFTER', '120'))
IMBOT_AGENT_TIMEOUT = int(os.environ.get('IMBOT_AGENT_TIMEOUT', '3600'))
AGENT_LOG_FILE  = os.path.expanduser(
    os.environ.get('IMBOT_AGENT_LOG', '~/.hermes/logs/agent.log'))

CONFIG_FILE = os.path.expanduser('~/.hermes/imbot_agent.json')
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if not os.environ.get('INVITE_CODE'):
        INVITE_CODE = cfg.get('inviteCode', INVITE_CODE)
    IMBOT_URL = cfg.get('serverUrl', IMBOT_URL)

# ── Backend detection ──────────────────────────────────────────────────────
BACKEND_BINS = {
    'hermes':   ['hermes'],
    'openclaw': ['openclaw', 'claw'],
    'claude':   ['claude'],
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
        base = os.path.basename(p).lower()
        if 'claude' in base: return 'claude'
        if 'claw' in base:   return 'openclaw'
        return 'hermes'

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
MODEL_MAP_FILE   = os.path.expanduser('~/.hermes/imbot_room_models.json')

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = logging.DEBUG if '--debug' in sys.argv else logging.INFO
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('imbot-agent')

# ── Global state ────────────────────────────────────────────────────────────
sio = None
agent_id = None
known_rooms = set()
shutting_down = False
last_rx_ts = time.time()
last_hb_ts = 0.0

room_sessions = {}      # room_id -> session_id
room_models = {}        # room_id -> model override
_room_locks = {}
_locks_guard = threading.Lock()

# Interrupt support
_pending_msgs = {}      # room_id -> [(content, sender_name, attachments), ...]
_room_turn = {}         # room_id -> {task_id, running: bool}
_room_restart = {}      # room_id -> bool
_turn_guard = threading.Lock()

_running_procs = {}
_cancelled = set()
_procs_guard = threading.Lock()

_SESSION_ID_RE = re.compile(r'^session_id:\s*([0-9a-zA-Z_]+)\s*$')
_CLARIFY_RE    = re.compile(r'\[CLARIFY:(\{.*?\})\]', re.DOTALL)
_MEDIA_RE      = re.compile(r'MEDIA:(/[^\s\n]+)', re.IGNORECASE)

# ── Content safety ──────────────────────────────────────────────────────────
_OUTPUT_BLOCKLIST = [
    'child porn', 'childporn', 'lolita', 'pedo', 'preteen', 'underage',
    'csam', 'isis', 'al-qaeda', 'al qaeda', 'taliban', 'jihadist',
    'mass shooting', 'school shooter', 'genocide',
    'nigger', 'kike', 'faggot',
    'kill yourself', 'kys', 'commit suicide',
]
_OUTPUT_PATTERNS = [re.compile(re.escape(w), re.IGNORECASE) for w in _OUTPUT_BLOCKLIST]

# Model-switch command patterns
_MODEL_CMD_RES = [
    re.compile(r'^/model\s*(?P<m>\S.*)?$', re.IGNORECASE),
    re.compile(r'^/switch[- ]?model\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^(?:switch|change)\s+(?:the\s+)?model\s+to\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^use\s+(?:the\s+)?model\s+(?P<m>\S.+)$', re.IGNORECASE),
    re.compile(r'^(?:切换|更换|换)(?:到|成|为)?\s*模型\s*(?:到|成|为)?\s*(?P<m>\S.+)$'),
    re.compile(r'^用\s*(?P<m>\S.+?)\s*模型$'),
]

# ── System preamble (injected into first message of new sessions) ──────────
def build_system_preamble():
    clarify_timeout = os.environ.get('IMBOT_CLARIFY_TIMEOUT', '1800')
    return (
        "[SYSTEM CONTEXT - read once] You are an AI agent chatting with a user "
        "through im-bot, a multi-agent instant-messaging app. This conversation "
        "is one continuous chat session that maps to a single im-bot room - you "
        "keep full memory of it across turns. Be helpful, concise, and friendly. "
        "Your replies are sent as chat messages, so use natural language. "
        "Keep replies reasonably short.\n\n"
        "CONTENT SAFETY: You MUST NOT generate content that is sexually explicit, "
        "depicts child exploitation (CSAM), promotes terrorism or mass violence, "
        "encourages self-harm or suicide, contains hate speech, or provides "
        "instructions for illegal activities. If asked, politely refuse. "
        "Violating this policy will result in immediate disconnection.\n\n"
        "CLARIFICATION: Do NOT use the built-in clarify tool — it is disabled.\n"
        "When you have a SUBSTANTIVE reply AND want to ask a follow-up question, "
        "just ask naturally as part of your reply text (no special format needed).\n"
        "Only use the [CLARIFY:{...}] format when the ENTIRE response is just a "
        "clarification question with no other content:\n"
        "[CLARIFY:{\"question\":\"<your question>\",\"choices\":[\"A\",\"B\"],\"timeout\":" + clarify_timeout + "}]\n"
        "CRITICAL: If you use [CLARIFY:{...}], output NOTHING else — no text before or after.\n"
        "For open-ended questions without choices, always ask naturally.\n\n"
        "FILE & IMAGE SHARING: You can send images, files, and other media "
        "directly to the chat user. When you generate or save a file, include "
        "MEDIA:/absolute/path/to/file in your reply -- for example: "
        "MEDIA:/root/workspace/chart.png. The connector automatically uploads "
        "the file as an inline attachment and strips the MEDIA: tag from the "
        "visible message. Supported image formats: PNG, JPG, GIF, WebP, SVG. "
        "Other file types are sent as downloadable attachments.\n\n"
        "The user's first message follows:\n"
    )

# ── Helper functions ────────────────────────────────────────────────────────
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
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
            lk = threading.RLock()
            _room_locks[room_id] = lk
        return lk

def _abs_url(url):
    if url and url.startswith('/'):
        return IMBOT_URL.rstrip('/') + url
    return url

def extract_attachments(metadata):
    if not metadata:
        return []
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else metadata
        return meta.get('attachments', []) or []
    except Exception:
        return []

def build_effective_content(content, attachments):
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

def _media_file(file_path):
    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.isfile(abs_path):
        log.warning("MEDIA: file not found: %s" % abs_path)
        return None, None, None
    try:
        mime, _ = mimetypes.guess_type(abs_path)
        mime = mime or 'application/octet-stream'
        with open(abs_path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return mime, data, os.path.basename(abs_path)
    except Exception as e:
        log.warning("MEDIA: read error for %s: %s" % (abs_path, e))
        return None, None, None

def _parse_media(text, room_id):
    matches = _MEDIA_RE.findall(text or '')
    if not matches:
        return text, []
    clean = _MEDIA_RE.sub('', text).strip()
    attachments = []
    seen = set()
    for file_path in matches:
        fp = file_path.strip()
        if fp in seen:
            continue
        seen.add(fp)
        mime, b64, name = _media_file(fp)
        if b64:
            log.info("[%s] MEDIA: encoded %s (%s, %d chars)" % (room_id[:12], name, mime, len(b64)))
            attachments.append({'fileName': name, 'mimeType': mime, 'data': b64})
    return clean, attachments

def _strip_tool_blocks(text):
    lines = (text or '').split('\n')
    out, i, n = [], 0, len(lines)
    def _is_diff_line(l):
        s = l.strip()
        return (l[:1] in '+- \\' or l.strip() == '' or l.startswith('@@')
                or l.lstrip().startswith(('a/', 'b/'))
                or s.startswith('diff --git') or s.startswith('index ')
                or s.startswith('--- ') or s.startswith('+++ ')
                or s == '\\ No newline at end of file')
    while i < n:
        line = lines[i]
        if '┊' in line:
            i += 1
            while i < n and _is_diff_line(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    return '\n'.join(out).strip()

def _scan_output(text):
    if not text or len(text) < 2:
        return False, ''
    lower = text.lower()
    for pat in _OUTPUT_PATTERNS:
        if pat.search(lower):
            return True, pat.pattern.replace('\\', '')
    return False, ''

def _parse_agent_output(stdout, stderr):
    session_id = None
    for line in (stderr or '').splitlines():
        m = _SESSION_ID_RE.match(line.strip())
        if m:
            session_id = m.group(1)
            break
    kept = []
    for line in (stdout or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('Warning:') or stripped.startswith('\u21bb Resumed session'):
            continue
        if _SESSION_ID_RE.match(stripped):
            continue
        kept.append(line)
    return _strip_tool_blocks('\n'.join(kept)), session_id

def _parse_clarify(text):
    blocks = _CLARIFY_RE.findall(text or '')
    if not blocks:
        return text, []
    clean = _CLARIFY_RE.sub('', text).strip()
    parsed = []
    for block in blocks:
        try:
            parsed.append(json.loads(block.strip()))
        except Exception:
            parsed.append({'question': block.strip(), 'choices': [],
                           'timeout': int(os.environ.get('IMBOT_CLARIFY_TIMEOUT', '1800'))})
    return clean, parsed

def _task_summary(content, attachments):
    s = (content or '').strip().replace('\n', ' ')
    if not s and attachments:
        s = "处理附件 %s" % (attachments[0].get('fileName', '文件'))
    s = s or "处理请求"
    return s if len(s) <= 24 else (s[:24] + '…')

def parse_model_command(text):
    t = (text or '').strip()
    for rx in _MODEL_CMD_RES:
        m = rx.match(t)
        if m:
            return (m.groupdict().get('m') or '').strip()
    return None

def probe_model(model):
    """Probe a model with a test query. Returns (ok, err_message)."""
    if BACKEND == 'hermes':
        cmd = [AGENT_BIN, 'chat', '-q', 'Reply with exactly: OK', '-m', model,
               '--source', IMBOT_SOURCE + '-probe', '-Q']
    elif BACKEND == 'openclaw':
        cmd = [AGENT_BIN, 'agent', '--message', 'Reply with exactly: OK',
               '--model', model, '--session-key', 'agent:main:probe-%s' % uuid.uuid4().hex[:8],
               '--json', '--timeout', '30']
    else:
        cmd = [AGENT_BIN, '-p', 'Reply with exactly: OK', '--model', model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return False, 'probe timed out'
    except Exception as e:
        return False, str(e)[:140]
    if r.returncode == 0:
        if BACKEND == 'openclaw':
            try:
                d = json.loads(r.stdout)
                text = ''.join(p.get('text', '') for p in d.get('result', {}).get('payloads', []))
                if text.strip():
                    return True, ''
            except Exception:
                pass
        elif (r.stdout or '').strip():
            return True, ''
    err = ((r.stderr or '') + ' ' + (r.stdout or '')).strip()
    return False, (err[:140] or 'no output')

def handle_model_switch(room_id, target):
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
        kept = prev or IMBOT_MODEL or 'the current model'
        log.warning("Model '%s' probe failed (%s); kept %s" % (target, err, kept))
        return ("Could not switch to `%s` - it failed a quick test (%s). "
                "Keeping %s." % (target, err, ('`%s`' % kept) if prev or IMBOT_MODEL else kept))

def _build_cmd(resume_sid, content, room_id):
    """Build agent CLI command (backend-specific)."""
    model = room_models.get(room_id) or IMBOT_MODEL

    if BACKEND == 'claude':
        cmd = [AGENT_BIN, '-p', content]
        if resume_sid:
            cmd.extend(['--resume', resume_sid])
        if model:
            cmd.extend(['--model', model])
        return cmd

    if BACKEND == 'openclaw':
        # OpenClaw: use 'agent' command with --session-key and --json
        session_key = 'agent:main:%s' % room_id
        cmd = [AGENT_BIN, 'agent', '--message', content,
               '--session-key', session_key, '--json',
               '--timeout', str(IMBOT_AGENT_TIMEOUT)]
        if model:
            cmd.extend(['--model', model])
        return cmd

    # Hermes (default)
    if resume_sid:
        cmd = [AGENT_BIN, '-r', resume_sid, 'chat', '-q', content,
               '--source', IMBOT_SOURCE, '-Q']
    else:
        full = build_system_preamble() + content
        cmd = [AGENT_BIN, 'chat', '-q', full, '--source', IMBOT_SOURCE, '-Q']
    if model:
        cmd.extend(['-m', model])
    if IMBOT_TOOLSETS:
        cmd.extend(['-t', IMBOT_TOOLSETS])
    return cmd


# ── OpenClaw: parse JSON response (handles mediaUrl stripping) ──────────────
def _parse_openclaw_output(stdout, room_id):
    """Parse openclaw --json output. Extract text, mediaUrl, sessionId."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()[:4000], None

    payloads = data.get('result', {}).get('payloads', [])
    texts = []
    seen_urls = set()
    for p in payloads:
        t = p.get('text', '')
        if t:
            texts.append(t)
        # openclaw strips MEDIA: and puts paths in mediaUrl/mediaUrls
        for key in ('mediaUrl', 'mediaUrls'):
            urls = p.get(key)
            if urls:
                if isinstance(urls, str):
                    urls = [urls]
                for url in urls:
                    if url and url.startswith('/') and url not in seen_urls:
                        seen_urls.add(url)
                        texts.append('MEDIA:' + url)

    reply = '\n'.join(texts).strip()
    if not reply:
        reply = '(no response)'

    # Extract session id from meta
    session_id = None
    meta = data.get('result', {}).get('meta', {})
    agent_meta = meta.get('agentMeta', {})
    session_id = agent_meta.get('sessionId')

    return reply, session_id


def call_agent(content, room_id, send_progress=None, task_id=None, _is_retry=False):
    """Generate a reply via the room's persistent agent session (sync)."""
    if send_progress is None:
        send_progress = lambda _text: None

    lock = _room_lock(room_id)
    with lock:
        existing_sid = room_sessions.get(room_id)
        env = os.environ.copy()

        if IMBOT_MODEL and BACKEND == 'hermes' and not room_models.get(room_id):
            env['HERMES_INFERENCE_MODEL'] = IMBOT_MODEL
        if BACKEND == 'claude':
            env['ANTHROPIC_BASE_URL'] = 'https://api.deepseek.com/v1'
            env['ANTHROPIC_AUTH_TOKEN'] = (os.environ.get('DEEPSEEK_API_KEY') or
                                           env.get('DEEPSEEK_API_KEY', ''))
            env.setdefault('ANTHROPIC_AUTH_TOKEN', os.environ.get('ANTHROPIC_API_KEY', ''))

        cmd = _build_cmd(existing_sid, content, room_id)

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
        except FileNotFoundError:
            log.error("Agent binary not found at %s" % AGENT_BIN)
            return "Agent CLI not found. Please check your installation."
        except Exception as e:
            log.error("Agent error: %s" % e)
            return "Internal error: %s" % str(e)[:200]

        if task_id:
            with _procs_guard:
                _running_procs[task_id] = proc

        try:
            stdout, stderr = proc.communicate(timeout=IMBOT_AGENT_TIMEOUT + 30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return "⚠️ 任务超时（%ds），请拆分成更小的步骤。" % IMBOT_AGENT_TIMEOUT

        if task_id:
            with _procs_guard:
                _running_procs.pop(task_id, None)

        if task_id and task_id in _cancelled:
            with _procs_guard:
                _cancelled.discard(task_id)
            return "🛑 任务已取消。"

        if proc.returncode != 0:
            err_msg = stderr.strip() if stderr else 'empty response'
            log.error("Agent exited %s: %s" % (proc.returncode, err_msg[:200]))
            if existing_sid and 'session not found' in err_msg.lower():
                room_sessions.pop(room_id, None)
                _save_json(SESSION_MAP_FILE, room_sessions)
                if not _is_retry:
                    if send_progress:
                        send_progress("🔄 检测到会话过期，正在启动新会话…")
                    return call_agent(content, room_id, send_progress, task_id, _is_retry=True)
            return "Sorry, I had trouble processing that. (%s)" % err_msg[:100]

        # Parse output (backend-specific)
        if BACKEND == 'openclaw':
            reply, parsed_sid = _parse_openclaw_output(stdout, room_id)
        else:
            reply, parsed_sid = _parse_agent_output(stdout, stderr)

        final_sid = parsed_sid or existing_sid
        if final_sid and room_sessions.get(room_id) != final_sid:
            room_sessions[room_id] = final_sid
            _save_json(SESSION_MAP_FILE, room_sessions)
            if not existing_sid:
                log.info("Room %s <-> session %s" % (room_id, final_sid))

        if not reply:
            return "Sorry, I didn't get a response. Could you try again?"
        return reply


# ── Socket.io event handlers + main runner (async) ──────────────────────────
async def _run_turn_async(room_id, effective, task_id, summary, sender_name):
    """Run one agent turn (off the socket read loop, so other rooms stay alive)."""
    def send_progress(text):
        try:
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(
                sio.emit('message:send',
                         {'roomId': room_id, 'content': text, 'msgType': 'progress'},
                         namespace='/agent'),
                loop)
        except Exception:
            pass

    try:
        await sio.emit('typing:start', {'roomId': room_id}, namespace='/agent')
    except Exception:
        pass

    try:
        await sio.emit('task:start',
                       {'roomId': room_id, 'taskId': task_id, 'summary': summary},
                       namespace='/agent')
    except Exception:
        pass

    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, call_agent, effective, room_id, send_progress, task_id)

        # Content safety scan
        blocked, hit = _scan_output(reply)
        if blocked:
            log.warning("BLOCKED output for room %s: matched '%s'" % (room_id, hit))
            await sio.emit('message:send',
                           {'roomId': room_id,
                            'content': '⚠️ My response was blocked by content safety filters.',
                            'msgType': 'text'}, namespace='/agent')
            return

        # MEDIA: parsing + file sending
        clean_reply, attachments = _parse_media(reply, room_id)
        for att in attachments:
            try:
                meta = json.dumps({
                    'fileName': att['fileName'],
                    'mimeType': att['mimeType'],
                    'data': att['data'],
                })
                await sio.emit('message:send', {
                    'roomId': room_id,
                    'content': att.get('fileName', 'file'),
                    'msgType': 'file',
                    'metadata': meta,
                }, namespace='/agent')
                log.info("[%s] Sent file: %s (%s)" % (room_id[:12], att['fileName'], att['mimeType']))
            except Exception as e:
                log.warning("[%s] File send failed: %s" % (room_id[:12], e))

        # CLARIFY parsing
        clean_text, clarifies = _parse_clarify(clean_reply)

        # Send text reply
        if clean_text:
            await sio.emit('message:send', {
                'roomId': room_id, 'content': clean_text, 'msgType': 'text',
            }, namespace='/agent')
            log.info("[%s] Replied: %s..." % (room_id[:12], clean_text[:120]))

        # Send clarify blocks
        for ci, cq in enumerate(clarifies):
            clarify_id = '%s-%d' % (task_id, ci)
            try:
                await sio.emit('message:clarify', {
                    'roomId': room_id, 'clarifyId': clarify_id,
                    'question': cq.get('question', ''),
                    'choices': cq.get('choices', []),
                    'timeout': cq.get('timeout', 120),
                }, namespace='/agent')
            except Exception as e:
                log.warning("[%s] clarify emit failed: %s" % (room_id[:12], e))

        if clarifies and not clean_text:
            old_sid = room_sessions.pop(room_id, None)
            if old_sid:
                _save_json(SESSION_MAP_FILE, room_sessions)

    except Exception as e:
        log.error("[%s] Error generating reply: %s" % (room_id[:12], e))
        try:
            await sio.emit('message:send',
                           {'roomId': room_id, 'content': "Error: %s" % str(e)[:200],
                            'msgType': 'text'}, namespace='/agent')
        except Exception:
            pass
    finally:
        try:
            await sio.emit('typing:stop', {'roomId': room_id}, namespace='/agent')
        except Exception:
            pass
        try:
            await sio.emit('task:end', {'roomId': room_id, 'taskId': task_id},
                           namespace='/agent')
        except Exception:
            pass
        with _procs_guard:
            _cancelled.discard(task_id)

        # Drain-and-restart: if user interrupted with new messages
        restart = False
        with _turn_guard:
            if _room_restart.get(room_id):
                restart = True
        if restart:
            with _turn_guard:
                pending = _pending_msgs.pop(room_id, [])
                _room_restart.pop(room_id, None)
            if pending:
                parts = [effective]
                names = {sender_name}
                for eff, sname, _atts in pending:
                    parts.append(eff)
                    names.add(sname)
                combined = ("\n\n---\n[INTERRUPT: The user sent additional message(s). "
                            "Process ALL of the following together.]\n\n"
                            + "\n\n---\n".join(parts))
                new_task_id = uuid.uuid4().hex[:12]
                new_summary = "interrupt: " + ", ".join(sorted(names))
                with _turn_guard:
                    _room_turn[room_id] = {'task_id': new_task_id, 'running': True}
                await _run_turn_async(room_id, combined, new_task_id, new_summary,
                                      ", ".join(sorted(names)))
                return
            else:
                with _turn_guard:
                    _room_turn.pop(room_id, None)
        else:
            with _turn_guard:
                _room_turn.pop(room_id, None)


# ── Async main ──────────────────────────────────────────────────────────────
async def async_main():
    global sio, agent_id, known_rooms, shutting_down, last_rx_ts, last_hb_ts, room_sessions, room_models

    sio = socketio.AsyncClient(
        reconnection=True, reconnection_attempts=0,
        reconnection_delay=3, reconnection_delay_max=60,
    )

    @sio.on('connect', namespace='/agent')
    async def on_connect():
        global last_rx_ts
        log.info('Connected to im-bot /agent namespace')
        last_rx_ts = time.time()

    @sio.on('disconnect', namespace='/agent')
    async def on_disconnect():
        log.info('Disconnected from /agent (socket.io will auto-reconnect)')

    @sio.on('welcome', namespace='/agent')
    async def on_welcome(data):
        global agent_id, last_rx_ts
        agent_id = data.get('agentId')
        rooms = data.get('rooms', [])
        known_rooms = set(rooms)
        # Restore server-side session map
        server_sessions = data.get('sessions') or {}
        if isinstance(server_sessions, dict) and server_sessions:
            changed = False
            for rid, sid in server_sessions.items():
                if sid and room_sessions.get(rid) != sid:
                    room_sessions[rid] = sid
                    changed = True
            if changed:
                _save_json(SESSION_MAP_FILE, room_sessions)
                log.info("Restored %d session map(s) from server" % len(server_sessions))
        log.info("Welcome — agentId=%s, rooms=%d, backend=%s" % (agent_id, len(known_rooms), BACKEND))
        last_rx_ts = time.time()

    @sio.on('message:new', namespace='/agent')
    async def on_message(msg):
        global shutting_down
        if shutting_down:
            return

        # Ignore messages from agents (including our own echoes)
        sender_type = msg.get('senderType', 'user')
        if sender_type == 'agent':
            return

        content = msg.get('content', '') or ''
        sender_name = msg.get('senderName', 'Unknown')
        room_id = msg.get('roomId')

        attachments = extract_attachments(msg.get('metadata'))
        if not content.strip() and not attachments:
            return

        log.info("[%s] Message from %s: %s%s"
                 % (room_id[:12] if room_id else '?', sender_name, content[:80],
                    (" [+%d attachment(s)]" % len(attachments)) if attachments else ""))

        # In-chat model switch
        target = parse_model_command(content)
        if target is not None and not attachments:
            await sio.emit('typing:start', {'roomId': room_id}, namespace='/agent')
            try:
                reply = handle_model_switch(room_id, target)
                await sio.emit('message:send',
                               {'roomId': room_id, 'content': reply, 'msgType': 'text'},
                               namespace='/agent')
            finally:
                await sio.emit('typing:stop', {'roomId': room_id}, namespace='/agent')
            return

        effective = build_effective_content(content, attachments)

        # Interrupt-aware turn scheduling
        with _turn_guard:
            turn = _room_turn.get(room_id)
            if turn and turn.get('running'):
                log.info("Interrupt: cancelling running turn for room %s" % room_id)
                _pending_msgs.setdefault(room_id, []).append((effective, sender_name, attachments))
                _room_restart[room_id] = True
                tid = turn['task_id']
                with _procs_guard:
                    _cancelled.add(tid)
                    proc = _running_procs.get(tid)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return

            task_id = uuid.uuid4().hex[:12]
            _room_turn[room_id] = {'task_id': task_id, 'running': True}
            _room_restart.pop(room_id, None)

        summary = _task_summary(content, attachments)
        asyncio.create_task(_run_turn_async(room_id, effective, task_id, summary, sender_name))

    @sio.on('heartbeat:ack', namespace='/agent')
    async def on_heartbeat_ack(data):
        global last_rx_ts
        last_rx_ts = time.time()

    @sio.on('task:cancel', namespace='/agent')
    async def on_task_cancel(data):
        tid = (data or {}).get('taskId')
        if not tid:
            return
        with _procs_guard:
            _cancelled.add(tid)
            proc = _running_procs.get(tid)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        log.info("Cancel requested for task %s" % tid)

    @sio.on('clarify:response', namespace='/agent')
    async def on_clarify_response(data):
        room_id = data.get('roomId')
        answer = data.get('answer', '')
        question = data.get('question', '')
        expired = data.get('expired', False)
        if not room_id or expired:
            return
        effective = ("The user answered your clarification:\nQuestion: %s\nAnswer: %s\n\n"
                     "Continue from where you left off." % (question, answer))
        task_id = uuid.uuid4().hex[:12]
        asyncio.create_task(_run_turn_async(room_id, effective, task_id,
                                            "clarify: " + (answer[:20] or "answered"), 'User'))

    @sio.on('*', namespace='/agent')
    async def on_any(event, data):
        global last_rx_ts
        last_rx_ts = time.time()

    @sio.on('error', namespace='/agent')
    async def on_error(data):
        log.error("Server error: %s" % data)

    # ── Connect ─────────────────────────────────────────────────────────────
    try:
        await sio.connect(
            IMBOT_URL, socketio_path='/socket.io',
            transports=['websocket'],
            auth={'token': INVITE_CODE},
            namespaces=['/agent'],
            wait_timeout=30,
        )
    except Exception as e:
        log.critical("Initial connection failed: %s" % e)
        return

    log.info("Listener running. Waiting for messages...")

    # ── Watchdog ────────────────────────────────────────────────────────────
    async def watchdog():
        global last_hb_ts, shutting_down, last_rx_ts
        while not shutting_down:
            await asyncio.sleep(10)
            if shutting_down:
                continue
            now = time.time()

            if sio.connected and now - last_hb_ts >= IMBOT_HB_INTERVAL:
                last_hb_ts = now
                try:
                    await asyncio.wait_for(
                        sio.emit('heartbeat',
                                 {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ')},
                                 namespace='/agent'),
                        timeout=5,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass

            if sio.connected:
                silence = now - last_rx_ts
                if silence > IMBOT_STALE_AFTER:
                    log.warning("No server activity for %ds — forcing reconnect" % int(silence))
                    try:
                        await sio.disconnect()
                    except Exception:
                        pass

    wd_task = asyncio.create_task(watchdog())

    try:
        await sio.wait()
    finally:
        shutting_down = True
        wd_task.cancel()
        try:
            await wd_task
        except asyncio.CancelledError:
            pass
        if sio.connected:
            await sio.disconnect()


# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    global shutting_down

    def graceful_shutdown():
        global shutting_down
        if shutting_down:
            return
        shutting_down = True
        log.info("Shutting down...")
        if sio:
            try:
                asyncio.run_coroutine_threadsafe(sio.disconnect(), loop)
            except Exception:
                pass
        sys.exit(0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, graceful_shutdown)
        except NotImplementedError:
            pass

    log.info("im-bot Agent Listener v6 (unified backend, async)")
    log.info("   Server: %s" % IMBOT_URL)
    log.info("   Invite code: %s..." % INVITE_CODE[:8])
    log.info("   Backend: %s (%s)" % (BACKEND, AGENT_BIN))
    log.info("   Model: %s" % (IMBOT_MODEL or '(profile default)'))
    log.info("   Toolsets: %s" % (IMBOT_TOOLSETS or 'profile default'))
    hardcap = ("%ss" % IMBOT_HARD_TIMEOUT) if IMBOT_HARD_TIMEOUT > 0 else "unlimited"
    log.info("   Progress every: %ss | Hard cap: %s | Source: %s"
             % (IMBOT_TIMEOUT, hardcap, IMBOT_SOURCE))

    if not os.path.exists(AGENT_BIN):
        log.warning("Agent binary not found at %s - set IMBOT_AGENT_BIN or install hermes/openclaw" % AGENT_BIN)

    global room_sessions, room_models
    room_sessions = _load_json(SESSION_MAP_FILE, {})
    room_models = _load_json(MODEL_MAP_FILE, {})
    log.info("   Loaded %d session map(s), %d model override(s)" % (len(room_sessions), len(room_models)))

    if not INVITE_CODE or INVITE_CODE == 'YOUR_AGENT_INVITE_CODE':
        log.error("INVITE_CODE is required. Set IMBOT_URL and INVITE_CODE env vars.")
        sys.exit(1)

    try:
        loop.run_until_complete(async_main())
    except KeyboardInterrupt:
        pass
    log.info("Shutdown complete.")


if __name__ == '__main__':
    main()
