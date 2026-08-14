--- 
name: im-bot-connector
version: 2.13.0
description: Manage im-bot agent connectors — configuration, timeouts, progress messages, filtering, troubleshooting, heartbeat-based liveness, and the DeepSeek Harness (dsh) ACP backend
triggers:
  - "connector timeout/offline/restart"
  - "IMBOT_AGENT_TIMEOUT / IMBOT_TIMEOUT / PROGRESS_THROTTLE"
  - "agent not responding / agent offline"
  - "cross-instance agent unregistered"
  - "agent liveness / heartbeat staleness / lastHeartbeatAt"
  - "restore im-bot connection / restore profile connector"
  - "progress message too verbose / filter progress / dedup progress"
---

# im-bot Agent Connector

## Running Files (ACTUAL)

The connector runs via supervisord. **Do NOT edit workspace copies** — they are NOT the running files.

| Profile | Running Path | Supervisord Name |
|---------|-------------|------------------|
| default | `/root/.local/bin/hermes-imbot-listener` | `hermes-imbot` |
| yiman | `~/.hermes/profiles/yiman/home/.local/bin/hermes-imbot-listener` | `hermes-imbot-yiman` |
| NAS | `/home/jet/.local/bin/hermes-imbot-listener` | `hermes-imbot` |

Supervisord configs: `/etc/supervisor/conf.d/*.conf`

## Restart Commands

See also: `references/deployment-workflow.md` for the complete deploy-and-restart workflow.

```bash
# Default
supervisorctl restart hermes-imbot

# Yiman
supervisorctl restart hermes-imbot-yiman

# NAS (requires sudo — user must do this manually)
ssh jet@192.168.1.20
sudo supervisorctl restart hermes-imbot
```

## Backends

The connector is backend-agnostic. Set `IMBOT_BACKEND` to `hermes` (default), `openclaw`, `claude`, or `dsh`. `hermes`/`openclaw`/`claude` are auto-detected from `$PATH`; `dsh` must be set explicitly.

### DeepSeek Harness (dsh) ACP backend

`dsh` connects via the Agent Client Protocol (JSON-RPC over stdio). One long-lived dsh ACP process holds one session per room — multi-turn = repeated `session/prompt` on the same `sessionId`.

Requirements:
- `dsh_acp_client.py` next to the listener (auto-imported at first use).
- A dsh harness install reachable via `docker exec` (defaults: container `dsh-agent`, source `/dsh-src`; override with `DSH_CONTAINER` / `DSH_SRC`).
- `DEEPSEEK_API_KEY` (env) or a `credential_pool.deepseek` entry in `~/.hermes/auth.json`.

```bash
IMBOT_BACKEND=dsh INVITE_CODE=YOUR_AGENT_INVITE_CODE python3 scripts/hermes_imbot_listener.py
```

## Environment Variables

### IMBOT_AGENT_TIMEOUT (default: 3600)
Hard timeout for agent subprocess. If the process doesn't finish within this time, it's killed with "任务超时" message.

```python
IMBOT_AGENT_TIMEOUT = int(os.environ.get('IMBOT_AGENT_TIMEOUT', '3600'))
```

### IMBOT_TIMEOUT (default: 60)
**Legacy** — no longer controls progress timing since v6.4 (progress is now event-driven with `PROGRESS_THROTTLE`). Still used for some timeout calculations.

### PROGRESS_THROTTLE (default: 3)
Minimum seconds between progress message emissions. Prevents flooding when agent produces rapid output.

```python
PROGRESS_THROTTLE = int(os.environ.get('PROGRESS_THROTTLE', '3'))  # min seconds between progress msgs
```

```python
IMBOT_TIMEOUT = int(os.environ.get('IMBOT_TIMEOUT', '60'))  # progress cadence (s)
```

### IMBOT_HARD_TIMEOUT (default: 0 = unlimited)
Overall hard cap for the agent run.

## Common Issues

### Agent shows offline but connector is running
1. Check `supervisorctl status` — verify process is RUNNING
2. Check logs: `tail -20 /tmp/hermes-imbot.log` — if log stopped updating, WebSocket likely dropped
3. Check active connections: `ss -tnp | grep $(pgrep -f hermes-imbot-listener)` — should see both the im-bot server connection AND model API connections. If only API connections exist, the WebSocket is dead
4. Look for `Disconnected from /agent` patterns — normal with auto-reconnect IF logs show a subsequent `Connected` event. If not, auto-reconnect failed — see Pitfall #13
5. **Check heartbeat liveness** (since 2026-08-07): `ssh -p 22022 root@104.207.81.51 "docker exec im-bot-blue node -e \"const{PrismaClient}=require('@prisma/client');const p=new PrismaClient();p.agent.findMany({select:{id:true,name:true,status:true,lastHeartbeatAt:true}}).then(r=>{console.log(JSON.stringify(r,null,2));p.\$disconnect()});\""` — `lastHeartbeatAt` should be within 90s for online agents. If `status='offline'` but `lastHeartbeatAt` is recent, the staleness cron needs investigation. If `lastHeartbeatAt` is old, connector heartbeat isn't reaching the server.
6. **Check server-side**: `ssh -p 22022 root@104.207.81.51 "docker logs im-bot-blue --tail 50 | grep 'Cross-instance agent unregistered'"` — if present with old timestamp, this is Pitfall #17 (fixed by heartbeat liveness)
7. Recovery: `supervisorctl restart hermes-imbot hermes-imbot-yiman` (restart both together)
8. See `references/agent-offline-diagnosis.md` for full workflow

### "Timeout — denying command" in replies
From Hermes Agent CLI (`hermes_cli/callbacks.py:241`, `cli.py:10856`), not the connector. Agent process times out but tries to send one more message. Strip from replies in the connector:

```python
import re
reply = re.sub(r'^\s*⏱\s*Timeout\s*—\s*denying command\s*\n?', '', reply)
```

## Progress Messages — Architecture

### Event-driven delta progress (v6.5+)

Since v6.5, the connector uses **event-driven delta progress** with stateful noise suppression. The agent's entire stdout stream is accumulated and flushed as progress messages whenever new output arrives, throttled to avoid message storms.

1. **Event-driven (not timer-based)**: Progress messages are sent when the agent produces new stdout output — no more blind "🔄 Working…" pulses on a fixed cadence.
2. **Content-rich delta**: Each progress message contains filtered agent output (tool progress lines, meaningful activity) — TUI noise, session metadata, code content, and prompt echoes are all suppressed.
3. **Throttled** (`PROGRESS_THROTTLE` env, default 3s): Minimum interval between progress messages to prevent flooding.
4. **Bounded buffer**: Internal buffer capped at ~100 lines to prevent memory bloat on long runs.
5. **No per-line forwarding**: Lines are batched together — each progress message is a coherent block of output.
6. **Tool-call gating** (`tool_count[0] > 0`): Progress messages are ONLY emitted when at least one tool invocation has been detected. For simple Q&A with zero tool calls, zero progress messages are sent — just the final reply.
7. **Stateful preamble suppression** (`in_query_preamble`): Hermes echoes the entire user input (incl. `[SYSTEM: ...]` block, `[ROOM MEMBERS]`, user message) in a `Query:` preamble. All lines between `Query:` and the first `┊` are suppressed.
8. **Stateful tool-output suppression** (`in_tool_block`): When a `┊` line mentions "diff", "write", "patch", or "code", subsequent content lines are suppressed until the next `┊` — preventing code bodies from appearing in progress.

```
PROGRESS_THROTTLE = int(os.environ.get('PROGRESS_THROTTLE', '3'))  # min seconds between progress msgs
```

### Message format

Progress messages use `msgType: 'progress'` — rendered as transient/thinking bubbles in the client. The content is raw agent stdout, cleaned of TUI noise.

### Filter rules (what's excluded from progress)

Eight layers of filtering prevent noise from reaching the user:

1. **TUI decoration** (`_is_tui_noise`): box-drawing chars, ANSI escapes, TUI borders with embedded text (`╭─ ⚕ Hermes ───╮`), separator lines (repeated `─`/`═`/`━`), overly long lines (>200 chars)
2. **Session metadata** (`_is_tui_noise`): `Session:`, `Duration:`, `Messages:`, `Query:` prompts, `Enter your query (or /help):`, `- 👤` / `- 🤖` user/assistant echo lines, `↻ Resumed session`, `Initializing agent...`
3. **Query preamble suppression** (`in_query_preamble` stateful tracker): Hermes echoes the entire user message (including the `[SYSTEM: ...]` block and `[ROOM MEMBERS]` context) in its TUI as a `Query:` preamble. The tracker sets `in_query_preamble=True` on `Query:` and suppresses ALL subsequent lines until the first `┊` line (which also clears the flag). This catches the SYSTEM preamble, room members, and the user's own message echo — none of which belong in progress.
4. **Diff blocks** (`_is_diff_line` + `in_diff_block` flag): unified diff format (`diff --git`, `@@`, `+`/`-`, `---`/`+++`, `index `, `a/`/`b/` file paths) — entire block suppressed.
5. **Verbose tool output** (`in_tool_block` stateful gate): when a `┊` line contains "diff", "write", "patch", or "code", set `in_tool_block=True`. All subsequent content lines until the next `┊` line are suppressed. This prevents code bodies and file contents from appearing in progress — only the `┊` summary lines (e.g. `┊ review diff`, `┊ 💻 running terminal…`) are shown.
6. **Duplicate `┊` lines**: Hermes reprints the same preparing/running status — deduplicated against previous buffer entry
7. **Tool-call gating** (`tool_count[0] > 0`): Progress flush (both inline and remainder) only fires after at least one tool invocation is detected. For simple Q&A with zero tool calls, the agent's stdout IS the final answer — sending it as progress would duplicate the final reply.
8. **`sent_count` tracking**: only emits lines added since last flush — never re-sends content from previous progress messages

### Deduplication and repetition prevention

Two mechanisms work together:
- **`sent_count` tracker**: only emits lines added since last flush — never re-sends content from previous progress messages
- **Consecutive duplicate filter**: identical adjacent `┊` lines are collapsed to one

### Python 3.10+ pitfall: asyncio.get_event_loop() in threads

**DO NOT use `asyncio.get_event_loop()` in a background thread.** Python 3.10+ raises `RuntimeError: There is no current event loop in thread`. 

**Fix**: capture the main loop at function entry:

```python
async def _run_turn_async(room_id, ...):
    main_loop = asyncio.get_running_loop()  # CAPTURE in async context
    
    def send_progress(text):
        try:
            asyncio.run_coroutine_threadsafe(
                sio.emit('message:send', {...}, namespace='/agent'),
                main_loop)  # USE captured loop, NOT get_event_loop()
        except Exception as e:
            log.error("Progress emit failed: %s" % e)  # NEVER pass silently
```

The old code had `except Exception: pass` which silently swallowed the `RuntimeError: no event loop` for MONTHS. Always log exceptions in progress callbacks.

### proc.communicate() blocks all progress

`proc.communicate(timeout=...)` waits for the ENTIRE process to finish. No intermediate output is captured. Progress is lost.

**Fix**: stream stdout line-by-line for tool counting, then `proc.wait()`:

```python
stdout_lines = []
tool_count = [0]
for line in proc.stdout:
    line = line.rstrip('\n\r')
    stdout_lines.append(line)
    if any(kw in line for kw in ('Tool:', 'tool_call', '<｜｜DSML｜｜tool_calls>', '▌', '┊')):
        tool_count[0] += 1

proc.wait(timeout=IMBOT_AGENT_TIMEOUT + 30)
stdout = '\n'.join(stdout_lines)
```

**Important**: Since v6.4, individual stdout lines are NOT forwarded as-is. Instead, lines are filtered (TUI noise, diffs, duplicates removed), batched into coherent deltas, and sent event-driven with `PROGRESS_THROTTLE` cadence. See Progress Messages — Architecture above.

### IMBOT_TIMEOUT vs IMBOT_AGENT_TIMEOUT

These are DIFFERENT variables — do not confuse them:

| Variable | Default | Purpose |
|----------|---------|---------|
| `IMBOT_AGENT_TIMEOUT` | 3600 | Hard subprocess timeout (seconds) |
| `IMBOT_TIMEOUT` | 60 | Legacy — no longer used for progress timing |
| `PROGRESS_THROTTLE` | 3 | Min seconds between progress msg emissions |
| `IMBOT_HARD_TIMEOUT` | 0 | Overall hard cap (0=unlimited) |

Set in supervisord config:
```
environment=IMBOT_BACKEND="hermes",PROGRESS_THROTTLE="3"
```

### Changing timeout value
Edit the ACTUAL running file (e.g., `/root/.local/bin/hermes-imbot-listener`), NOT the workspace copy. Then restart:

```bash
sed -i "s/IMBOT_AGENT_TIMEOUT.*300/IMBOT_AGENT_TIMEOUT', '3600'/" /root/.local/bin/hermes-imbot-listener
supervisorctl restart hermes-imbot
```

## Pitfalls

1. **Source vs running file**: the git-managed sources (`im-bot/skills/im-bot-connector/scripts/` in the im-bot repo, and `skills/im-bot-connector/scripts/` in im-bot-connector-pub) are COPIES. Changing them does nothing until deployed to the actual running file(s). There are MULTIPLE running files — one per profile:
   ```
   default: /root/.local/bin/hermes-imbot-listener
   yiman:   /root/.hermes/profiles/yiman/home/.local/bin/hermes-imbot-listener
   NAS:     /home/jet/.local/bin/hermes-imbot-listener
   ```
   **After editing, sync ALL active profiles** then restart their supervisord entries.
2. **IMBOT_TIMEOUT ≠ IMBOT_AGENT_TIMEOUT**: One is progress cadence, one is agent timeout. Don't confuse them.
3. **NAS deployment via SSH**: NAS connector can be deployed remotely:\n   ```bash\n   scp hermes_imbot_listener.py jet@192.168.1.20:/tmp/\n   ssh jet@192.168.1.20 \"sudo cp /tmp/hermes_imbot_listener.py /home/jet/.local/bin/hermes-imbot-listener && sudo supervisorctl restart hermes-imbot\"\n   ```
4. **Per-line progress forwarding causes message floods (fixed v6.4)**: As of v6.4, progress messages are event-driven deltas with six-layer filtering — diffs, TUI borders, long lines, duplicate `┊` status, and empty lines are all filtered. `sent_count` tracking prevents message overlap. The old v6.3 periodic pulse pattern is obsolete.
5. **Server-side splitMessage compounds flooding**: Prior to the 2026-07-16 fix, `socket/index.ts` split agent replies into 3500-char chunks with `splitMessage()`, broadcasting each as a separate `message:new` event. Combined with per-line progress forwarding, this created message storms. The fix removed `splitMessage` — messages now store and broadcast as single units with a 500KB cap.
6. **Yiman profile missing hermes binary**: The yiman connector runs with `HOME=/root/.hermes/profiles/yiman`. If `~/.local/bin/hermes` doesn't exist under that home, the connector fails every turn with "Sorry, I had trouble processing that. (session_id: ...)". Fix: `ln -s /root/.local/bin/hermes /root/.hermes/profiles/yiman/home/.local/bin/hermes`. The connector uses `os.path.expanduser('~/.local/bin/...')` to locate the binary — it does NOT search PATH.
7. **Progress messages are now content-rich deltas (v6.4+)**: As of v6.4, progress messages contain the actual agent stdout output (tool calls, thinking, file reads) — not just "🔄 Working…". Messages are event-driven (sent when new output arrives) and throttled to max one every `PROGRESS_THROTTLE` seconds (default 3s). TUI noise is filtered but meaningful content is shown. The old `IMBOT_TIMEOUT` pulse cadence is no longer used for progress timing.
8. **`-Q` flag intentionally REMOVED (v6.1+)**: Previously the connector used `-Q` to suppress TUI noise. However, `-Q` also suppresses Hermes `┊` progress lines (tool previews) which are the primary source of activity information for progress messages. The connector now runs WITHOUT `-Q`, and `_parse_agent_output()` filters all TUI noise (box-drawing, Query:, footer, AND progress lines). The `┊` lines are extracted for progress during stdout streaming but filtered from the final reply text.
9. **Session auto-reset on stale/corrupt sessions**: When an agent exits non-zero with an existing session, the connector should auto-reset and retry. Detect these patterns in stderr: `session not found`, `Resumed session`, or empty stdout + `session_id`. Wire the check in `call_agent()` after `proc.returncode != 0`:
   ```python
   session_lost = existing_sid and (
       'session not found' in err_msg.lower() or
       'resumed session' in err_msg.lower() or
       (not stdout.strip() and 'session_id' in err_msg.lower())
   )
   if session_lost:
       room_sessions.pop(room_id, None)
       _save_json(SESSION_MAP_FILE, room_sessions)
       if not _is_retry:
           return call_agent(content, room_id, send_progress, task_id, _is_retry=True)
   ```
   Without this, sessions with 1000+ messages cause context overflow crashes that repeat forever.

10. **TUI parsing uses `╭╰` block tracking**: Since `-Q` is no longer used, Hermes outputs TUI with `╭╰` reply blocks. When sessions are resumed, there are MULTIPLE blocks (one per tool turn). `_parse_agent_output()` now only keeps the LAST `╭`…`╰` block — all earlier blocks are intermediate thinking steps and are discarded. Inside each block:
    - Progress lines containing `┊` set `in_diff=True` and are skipped
    - Diff lines (unified diff format patterns: `a/`, `b/`, `@@`, `--- `, `+++ `, `index `, `diff --git`, `+`, `-`, `\ No newline`) are skipped while `in_diff=True`
    - Non-diff lines reset `in_diff=False` and are appended
    
    Everything OUTSIDE `╭╰` blocks is discarded (TUI borders, progress lines, footers, Query:, Initializing agent..., separators, `- 👤` / `- 🤖` header lines, session summary). Footer lines (`Resume this session with:`, `hermes --resume`, `Session:`, `Duration:`, `Messages:`) are also filtered inside blocks for defense-in-depth.

    **CRITICAL: session_id moves to stdout when `-Q` is removed.** With `-Q`, Hermes outputs `session_id: xxx` to stderr. Without `-Q`, it outputs `Session: xxx` to stdout footer. `_parse_agent_output()` MUST check BOTH:
    - stderr: match `_SESSION_ID_RE` (`session_id: xxx`)
    - stdout (fallback): parse `Session: xxx` lines (skip `Session ended:` system messages)
    
    Without this fallback, `parsed_sid` is always None, `room_sessions` never saved, and "New Session" after a conversation round shows no "Session ended" message because there's nothing to pop.

11. **NO BACKGROUND TASKS — three-layer defense + retry loop**: The agent model may claim it will work in the background and come back later. The connector does NOT support async background tasks — after replying, the agent goes idle until the next user message. Three layers prevent this, plus a retry loop for weak models. See `references/holding-patterns.md` for the complete regex pattern catalog.

    **Defense layers:**
    - **System preamble** (new sessions only): `build_system_preamble()` rule #6 explicitly forbids background promises
    - **Per-turn `[SYSTEM]` block** (every message): injected in `_run_turn_async` before `call_agent`. Tells the model to output ONLY the finished answer, never status narration
    - **`_is_holding_reply()` detection**: regex patterns match "hold on" language (正在调研, 稍等, let me research, working on it, 后台执行, etc.) in Chinese and English. Max reply length 300 chars to avoid false positives on real answers.

    **Retry loop** (`MAX_HOLDING_RETRIES = 2`): When `_is_holding_reply()` matches:
    1. Reply sent as `msgType: 'progress'` (not final text) — user sees transient progress indicator
    2. Connector nudges agent: `[SYSTEM: Continue working. Output your FINAL answer NOW — no narration, no status updates, just the deliverable.]`
    3. `call_agent()` is called AGAIN, resuming the same session (session ID was saved from first call)
    4. Up to 2 retries — if agent still outputs holding replies, the last one is sent as text and the turn ends
    5. Good models never trigger the retry — they pass `_is_holding_reply()` on the first attempt with zero overhead

    The retry loop is specifically designed for weaker models (deepseek-v4-flash) that tend to narrate their process. Stronger models (v4-pro, MiniMax-M3) rarely produce holding replies.

12. **NEVER clear session maps blindly**: Session maps (`~/.hermes/.../imbot_sessions.json`) track active room→session mappings. When the user clicks "New Session" in the UI, the server broadcasts `session:reset` which the connector handles properly (pops the room's session). NEVER manually clear the session map file — this destroys all active sessions across all rooms. If a session needs resetting, tell the user to use the "New Session" button in the UI.

13. **WebSocket drop without auto-reconnect** (observed 2026-07-24): Process stays alive (supervisord RUNNING), agent subprocess is still making API calls, but the socket.io WebSocket to im-bot server dropped and auto-reconnect never fired. Symptoms:
    - `/tmp/hermes-imbot.log` stops updating (no progress pulses, no message logs)
    - `ss -tnp | grep <pid>` shows only model API connections (DeepSeek), NO connection to im-bot server
    - Server marks agent as `offline` (check via: `agent.status` in DB)
    - Recovery: `supervisorctl restart hermes-imbot` (restart, not just reload)
    Root cause suspected: socket.io AsyncClient `reconnection_attempts=0` (infinite) but event loop may not process reconnect under certain conditions during long `run_in_executor` tasks.

14. **Session restore (switch-back)** — see also `references/session-lifecycle.md`

14. **Session restore (switch-back)** — see also `references/session-lifecycle.md` for the complete event flow: When `session:reset` occurs, the old session ID is saved to `SESSION_HISTORY_FILE` (`~/.hermes/imbot_session_history.json`). A system message is emitted with `msgType: 'system'` and metadata `{sessionId, action: 'session:ended'}`. The frontend renders a "↩ Switch back" button. On click:
    1. Frontend emits `session:restore` on user namespace `/`
    2. **Server relays** the event to `/agent` namespace (see im-bot skill, server socket section)
    3. Connector saves current session to history, restores `room_sessions[roomId] = old_sid`
    4. Emits both "Session ended: <cur>" and "Session restored: <old>" system messages
    5. Next user message resumes that session via `-r`
    **CRITICAL**: Without the server relay, `session:restore` never reaches the connector. The server MUST forward this event from `/` to `/agent` namespace.

15. **Agent→agent file sharing: metadata format gap (fixed M25)** — Agent connectors send files as `msgType:'file'` with inline base64 `metadata: {fileName, mimeType, data:"<base64>"}`. The receiving connector's `extract_attachments()` only reads `metadata.attachments` (Attachment record references), NOT `metadata.data`. This means receiving agents could never access files sent by other agents — they only saw the filename text. The fix is SERVER-SIDE (not connector): `socket/index.ts` extracts inline base64 → uploads to R2 → creates Attachment record → rewrites metadata to `{attachments:[{...}]}` format. The connector needs ZERO changes — `extract_attachments` and `build_effective_content` already handle the `attachments` format. See `im-bot-development` skill `references/agent-file-sharing.md`.

    **Post-M25 frontend pitfall**: After server rewrites metadata from `{fileName, mimeType, data}` to `{attachments: [...]}`, the top-level `meta.mimeType` is gone. `renderFileBubble()` must check `meta.attachments?.[0]?.mimeType` as fallback, otherwise ALL files get `application/octet-stream` and images won't preview.

17. **Cross-instance agent unregister causes false offline** (observed 2026-07-29, root cause found 2026-08-07): 

    **Symptoms:**
    - `supervisorctl status` shows RUNNING, logs show recent `Welcome` / message processing
    - DB shows agent `status='offline'` with `updatedAt` AFTER the connector's `Welcome` timestamp
    - `ss -tnp | grep <pid>` shows model API connections but im-bot connection may be on a different Cloudflare IP
    - Recovery: `supervisorctl restart hermes-imbot hermes-imbot-yiman` (restart both together)
    
    **Root cause (2026-08-07):** The July 2026 cross-instance guard (`agentRegistry.isOnline()`) only protects the in-memory registry. The **DB race** still happens because `AgentRegistry.unregister()` directly calls `prisma.agent.update({status:'offline'})` in EVERY instance's disconnect handler. When an old connector process is killed (supervisord restart), its disconnect on the old instance writes DB='offline' — this can arrive AFTER the new connector's connect on the new instance wrote DB='online', overwriting it.
    
    ```
    Green: connect → register() → DB='online'        (t=0)
    Blue:  disconnect → unregister() → DB='offline'   (t=1)  ← overwrites!
    Green: 收到 unregister → isOnline()=true → skip   (t=2)  ← guard works, but too late
    ```
    
    **Permanent fix — heartbeat-based liveness (deployed 2026-08-07):** Don't derive status from connect/disconnect events. Instead use the agent's heartbeat signal:
    - Connector sends heartbeat every 25s → server records `lastHeartbeatAt` in DB (debounced, ≤1 write/30s)
    - Server cron every 30s: marks agents with `lastHeartbeatAt > 90s` ago as 'offline'
    - `register()`/`unregister()` no longer touch DB — they only manage the in-memory registry
    - Presence endpoint (`GET /agents/presence`) checks both in-memory + `lastHeartbeatAt` recency
    
    See `references/heartbeat-liveness.md` for the full design, schema changes, and implementation details.
    
    See `references/agent-offline-diagnosis.md` for the full diagnostic workflow.

18. **DB mismatch after Neon migration**: `docker-compose.env` still has the old local `DATABASE_URL`, but the running container uses `-e` flags to override it to Neon. Always verify with `docker exec im-bot-green printenv DATABASE_URL` before querying agent status. Querying `docker exec imbot-db psql` returns stale pre-migration data. Use the active container's Prisma client instead — see `references/agent-offline-diagnosis.md` step 5.

16. **OpenClaw backend (jet_claw, etc.)**: Not all agents use Hermes. Some run OpenClaw with `openclaw-imbot-listener` (a separate Python script) and the OpenClaw gateway (`openclaw gateway --port 18789`). These run via **systemd user service** (not supervisord): `/home/jet/.config/systemd/user/openclaw-imbot.service`. Restart: `systemctl --user restart openclaw-imbot.service` (requires `--privileged` Docker container or direct host access). Log file: `/tmp/imbot-listener.log` (shared with Hermes connectors). Key differences: uses `openclaw` binary at `/home/jet/.npm-global/bin/openclaw`, invite code in systemd env, agent sessions map to OpenClaw sessions (not Hermes `-r`).

19. **VPS restart → Cloudflare 520/521 → supervisord FATAL loop** (observed 2026-08-08): When the VPS reboots, **sun-port** (reverse proxy) must auto-start via systemd. As of v2.16.0 the landing page is served from the im-bot container itself (no separate Python HTTP server). Only sun-port needs systemd management.

20. **tool_count detection MUST include `┊` for Hermes**: Hermes uses `┊` (not `Tool:` or `tool_call`) for its progress lines:
    ```
    ┊ 📖 preparing read_file…
    ┊ 📖 read    /root/workspace/hermes_imbot_listener.py  0.7s
    ```
    The `tool_count` increment keyword list MUST include `'┊'` alongside `'Tool:'`, `'tool_call'`, `'<｜｜DSML｜｜tool_calls>'`, `'▌'`. Without `'┊'`, `tool_count` stays 0 for the entire run, the tool-call gate never opens, and ALL progress messages are suppressed — including for tool-heavy runs. The fix is a one-character addition to the keyword tuple.

21. **`[SYSTEM]` preamble leaks into progress via Hermes TUI echo**: The per-turn `[SYSTEM: ...]` block (see Pitfall #11) is prepended to the agent's input. Hermes echoes the ENTIRE input in its TUI as a `Query:` block — including the SYSTEM preamble, `[ROOM MEMBERS]` context, and the user's original message. Without the `in_query_preamble` stateful tracker, all of this leaks into progress messages as raw text. The tracker suppresses everything between `Query:` and the first `┊` line. If the preamble is ever restructured, this tracker must be updated in sync — otherwise the SYSTEM block reappears in progress.

    **Diagnosis:**
    ```bash
    # 1. Is Cloudflare reachable?
    curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 https://im-bot.net/api/health
    # 521 = origin unreachable. 200 = origin OK, connector problem.

    # 2. Is sun-port listening on 443/80?
    ssh -p 22022 root@104.207.81.51 "ss -tlnp | grep sun-port"
    # Should show :80, :443, :9090. Empty = sun-port not running.

    # 3. Is the container healthy locally?
    ssh -p 22022 root@104.207.81.51 "curl -s localhost:3001/api/health"
    # {"status":"ok"} = container is fine, just not reachable from outside.
    ```

    **Fix — start sun-port:**
    ```bash
    ssh -p 22022 root@104.207.81.51 "
    # Kill any stale processes
    pkill -9 sun-port 2>/dev/null; sleep 1

    # Ensure cert symlinks exist (sun-port reads from /run/sun-port/certs/)
    mkdir -p /run/sun-port/certs
    ln -sf /opt/im-bot/sun-port-run/certs/cert.pem /run/sun-port/certs/cert.pem
    ln -sf /opt/im-bot/sun-port-run/certs/key.pem /run/sun-port/certs/key.pem

    # Start
    cd /opt/im-bot/sun-port-run && nohup ./sun-port >> sun-port.log 2>&1 &
    "
    ```

    **After sun-port is back (verify 443/80 listening):**
    ```bash
    ssh -p 22022 root@104.207.81.51 "ss -tlnp | grep sun-port"
    ```

    **Start connectors (supervisord may need explicit 'start' after FATAL):**
    ```bash
    supervisorctl start hermes-imbot hermes-imbot-yiman
    ```

    **Permanent fix — systemd auto-start (deployed 2026-08-08):**

    Both sun-port and landing-page now have systemd services created by `vps-setup.sh`:

    ```bash
    # sun-port (reverse proxy, TLS termination on :443)
    cat > /etc/systemd/system/sun-port.service << 'UNIT'
    [Unit]
    Description=sun-port reverse proxy
    After=network-online.target docker.service
    Wants=network-online.target
    StartLimitIntervalSec=0

    [Service]
    Type=simple
    Restart=always
    RestartSec=5
    WorkingDirectory=/opt/im-bot/sun-port-run
    ExecStartPre=/bin/bash -c 'mkdir -p /run/sun-port/certs && ln -sf /opt/im-bot/sun-port-run/certs/cert.pem /run/sun-port/certs/cert.pem && ln -sf /opt/im-bot/sun-port-run/certs/key.pem /run/sun-port/certs/key.pem'
    ExecStart=/opt/im-bot/sun-port-run/sun-port
    StandardOutput=append:/opt/im-bot/sun-port-run/sun-port.log
    StandardError=append:/opt/im-bot/sun-port-run/sun-port.log

    [Install]
    WantedBy=multi-user.target
    UNIT

    # landing-page (static site on 127.0.0.1:9998)
    cat > /etc/systemd/system/landing-page.service << 'UNIT'
    [Unit]
    Description=im-bot landing page (Python HTTP server)
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    Restart=always
    RestartSec=5
    WorkingDirectory=/opt/im-bot/landing
    ExecStart=/usr/bin/python3 -m http.server 9998 --bind 127.0.0.1
    StandardOutput=append:/tmp/landing.log
    StandardError=append:/tmp/landing.log

    [Install]
    WantedBy=multi-user.target
    UNIT

    systemctl daemon-reload
    systemctl enable sun-port landing-page
    ```

    **IMPORTANT:** Do NOT use nginx or LiteSpeed. The VPS reverse proxy is **sun-port** — a custom Pingora-based Go binary at `/opt/im-bot/sun-port-run/sun-port` with config at `/opt/im-bot/sun-port-run/config.yaml`. It round-robins between im-bot-blue (3001) and im-bot-green (3002), terminates TLS with certs from `/run/sun-port/certs/`, and serves the admin UI on port 9090. The certs live at `/opt/im-bot/sun-port-run/certs/` and are symlinked to `/run/sun-port/certs/` on startup.
