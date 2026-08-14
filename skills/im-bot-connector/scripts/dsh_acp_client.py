#!/usr/bin/env python3
"""Async ACP (Agent Client Protocol) client for the dsh harness.

Manages ONE long-lived dsh ACP subprocess (JSON-RPC over stdio), holding
multiple sessions (one per im-bot room). Multi-turn = repeated session/prompt
on the same sessionId; the dsh harness keeps session context until compaction.

Wire format (verified against @deepseek-ai/dsh-acp):
  initialize     -> {protocolVersion, agentInfo, agentCapabilities, authMethods}
  session/new    -> {sessionId}
  session/prompt -> {stopReason}  (text streams via session/update events)
  session/update -> {sessionId, update:{sessionUpdate:"agent_message_chunk",
                                         content:{type:"text", text:"..."}}}

History replay (survives connector restarts): the ACP bridge is fresh-only
(session/new randomUUID, no resume), so a connector restart loses every dsh
session's in-memory context. To work around this we persist each room's
conversation to disk and, when a room's session is missing (i.e. after a
restart), replay that history into the new session as a prefix context before
sending the real user turn. This preserves the *conversation text* (what the
model sees) but not dsh-internal tool/fs state.
"""
import asyncio
import json
import os


class DshAcpClient:
    def __init__(self, cmd, cwd, log=None, boot_wait=2.0,
                 history_file=None, max_history=20):
        self.cmd = cmd              # shell command to launch the ACP server
        self.cwd = cwd              # absolute cwd for session/new
        self.log = log or (lambda _m: None)
        self.boot_wait = boot_wait
        self.history_file = history_file or os.path.expanduser(
            '~/.hermes/dsh_history.json')
        self.max_history = max_history
        self.proc = None
        self._next_id = 0
        self._pending = {}          # request id -> asyncio.Future
        self._sessions = {}         # room_id -> sessionId
        self._chunk_buffers = {}    # sessionId -> list[str] (agent_message_chunk)
        self._reader_task = None
        self._started = False
        self._history = {}          # room_id -> [{role, text}]
        self._load_history()

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self):
        self.proc = await asyncio.create_subprocess_shell(
            self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd='/tmp',
        )
        # The dsh harness boots a whole Cordis plugin tree — give it time.
        await asyncio.sleep(self.boot_wait)
        self._reader_task = asyncio.create_task(self._read_loop())
        resp = await self._request('initialize', {
            'protocolVersion': 1,
            'clientCapabilities': {},
        }, timeout=60)
        self._started = True
        self.log('dsh ACP initialized: %s' % resp.get('result', {}).get('agentInfo', {}).get('name'))
        return resp

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                self.proc.kill()
        if self._reader_task:
            self._reader_task.cancel()

    # ── JSON-RPC plumbing ────────────────────────────────────────
    async def _read_loop(self):
        while True:
            try:
                line = await self.proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.decode('utf-8', errors='replace').strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('id') is not None:
                fut = self._pending.pop(obj.get('id'), None)
                if fut and not fut.done():
                    fut.set_result(obj)
            elif obj.get('method') == 'session/update':
                self._handle_update(obj)

    def _handle_update(self, obj):
        params = obj.get('params', {})
        sid = params.get('sessionId')
        update = params.get('update', {})
        if update.get('sessionUpdate') == 'agent_message_chunk':
            text = update.get('content', {}).get('text', '')
            if text:
                self._chunk_buffers.setdefault(sid, []).append(text)

    async def _request(self, method, params, timeout=300):
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut
        payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params}
        try:
            self.proc.stdin.write((json.dumps(payload) + '\n').encode())
            await self.proc.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RuntimeError('ACP write failed: %s' % e)
        return await asyncio.wait_for(fut, timeout=timeout)

    # ── history persistence (survives connector restarts) ────────
    def _load_history(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self._history = json.load(f)
        except Exception as e:
            self.log('dsh history load failed: %s' % e)
            self._history = {}

    def _save_history(self):
        try:
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            tmp = self.history_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._history, f, ensure_ascii=False)
            os.replace(tmp, self.history_file)
        except Exception as e:
            self.log('dsh history save failed: %s' % e)

    def _append_history(self, room_id, role, text):
        if not text:
            return
        hist = self._history.setdefault(room_id, [])
        hist.append({'role': role, 'text': text})
        if len(hist) > self.max_history:
            self._history[room_id] = hist[-self.max_history:]

    def _build_history_context(self, history):
        lines = ['[以下是该用户此前的对话历史，用于恢复上下文：]']
        for entry in history:
            role = '用户' if entry['role'] == 'user' else '助手'
            lines.append('%s: %s' % (role, entry['text']))
        return '\n'.join(lines)

    # ── session management ───────────────────────────────────────
    async def session_new(self, room_id):
        resp = await self._request('session/new', {
            'cwd': self.cwd,
            'mcpServers': [],
        })
        if 'error' in resp:
            raise RuntimeError('session/new failed: %s' % resp['error'])
        sid = resp['result']['sessionId']
        self._sessions[room_id] = sid
        self._chunk_buffers.setdefault(sid, [])
        return sid

    async def prompt(self, room_id, text):
        """Send one user turn to the room's session; return the committed text.

        If the room's session is missing (connector restarted → the ACP bridge
        is fresh), replay the persisted conversation as a prefix context so the
        model can pick up where it left off.
        """
        sid = self._sessions.get(room_id)
        effective = text
        if not sid:
            sid = await self.session_new(room_id)
            history = self._history.get(room_id, [])
            if history:
                ctx = self._build_history_context(history)
                effective = ctx + '\n\n---\n\n用户最新消息: ' + text
        self._chunk_buffers[sid] = []
        resp = await self._request('session/prompt', {
            'sessionId': sid,
            'prompt': [{'type': 'text', 'text': effective}],
        })
        if 'error' in resp:
            raise RuntimeError('session/prompt failed: %s' % resp['error'])
        chunks = self._chunk_buffers.get(sid, [])
        result = ''.join(chunks).strip()
        stop_reason = resp.get('result', {}).get('stopReason', 'unknown')
        # Record the *real* turn (not the replayed context) into history.
        self._append_history(room_id, 'user', text)
        self._append_history(room_id, 'assistant', result)
        self._save_history()
        return result, stop_reason

    def has_session(self, room_id):
        return room_id in self._sessions
