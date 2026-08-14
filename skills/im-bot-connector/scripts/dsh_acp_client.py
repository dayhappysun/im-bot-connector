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
"""
import asyncio
import json
import os
import subprocess
import time


class DshAcpClient:
    def __init__(self, cmd, cwd, log=None, boot_wait=2.0):
        self.cmd = cmd              # shell command to launch the ACP server
        self.cwd = cwd              # absolute cwd for session/new
        self.log = log or (lambda _m: None)
        self.boot_wait = boot_wait
        self.proc = None
        self._next_id = 0
        self._pending = {}          # request id -> asyncio.Future
        self._sessions = {}         # room_id -> sessionId
        self._chunk_buffers = {}    # sessionId -> list[str] (agent_message_chunk)
        self._reader_task = None
        self._started = False

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
        """Send one user turn to the room's session; return the committed text."""
        sid = self._sessions.get(room_id)
        if not sid:
            sid = await self.session_new(room_id)
        self._chunk_buffers[sid] = []
        resp = await self._request('session/prompt', {
            'sessionId': sid,
            'prompt': [{'type': 'text', 'text': text}],
        })
        if 'error' in resp:
            raise RuntimeError('session/prompt failed: %s' % resp['error'])
        chunks = self._chunk_buffers.get(sid, [])
        stop_reason = resp.get('result', {}).get('stopReason', 'unknown')
        return ''.join(chunks).strip(), stop_reason

    def has_session(self, room_id):
        return room_id in self._sessions
