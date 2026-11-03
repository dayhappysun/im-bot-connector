#!/usr/bin/env python3
"""Verify the fallback-notice strip works on real Hermes reply-block output.

Bug (2026-11-03): Hermes injects
    🔄 Switched to fallback model: A via B → C via D
into the ╭...╰ reply block when the primary model fails. The connector's
_parse_agent_output keeps every line inside that block, so the notice was
sent to the chat room as part of the agent's reply.

This test feeds the EXACT stdout captured from a real failing run on NAS
(agent.log 2026-11-03 15:57:54, model ksyun/mog-8-s → glm-5.3-flash) plus
the healthy no-fallback case, and asserts the notice is gone but the real
reply survives.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(
    os.path.dirname(HERE), "scripts", "hermes_imbot_listener.py"
)
if not os.path.exists(SRC):
    SRC = HERE

spec = importlib.util.spec_from_file_location("hl", SRC)
hl = importlib.util.module_from_spec(spec)
sys.modules["hl"] = hl
spec.loader.exec_module(hl)

# ── Real captured stdout, fallback fired (hermes -m ksyun/mog-8-s) ─────────
FALLBACK_STDOUT = """Warning: Unknown toolsets: messaging
Query: say hi
Initializing agent...
────────────────────────────────────────────


╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────╮
🔄 Switched to fallback model: ksyun/mog-8-s via custom → glm-5.3-flash via ksyun
    Hi! I'm Hermes — what can I help you with today?
╰────────────────────────────────────────────────────────────────────────────╯

Resume this session with:
  hermes --resume 20261103_161719_04c9b5

Session:        20261103_161719_04c9b5
Duration:       4s
Messages:       2 (1 user, 0 tool calls)
"""

# ── Real captured stdout, healthy run (no fallback) ───────────────────────
HEALTHY_STDOUT = """Warning: Unknown toolsets: messaging
Query: say hi
Initializing agent...
────────────────────────────────────────────


╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────╮
    Hi! 👋 How can I help you today?
╰────────────────────────────────────────────────────────────────────────────╯

Resume this session with:
  hermes --resume 20261103_161704_e63e27

Session:        20261103_161704_e63e27
Duration:       4s
Messages:       2 (1 user, 0 tool calls)
"""

# ── Legacy/alternate notice format (↻ + "Switched to fallback:") ──────────
LEGACY_STDOUT = """╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────╮
↻ Switched to fallback: glm-5.3-flash (ksyun)
    answer text
╰────────────────────────────────────────────────────────────────────────────╯
"""

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        fails.append(name)


print("test_fallback_notice_stripped")
reply, sid = hl._parse_agent_output(FALLBACK_STDOUT, "")
check("notice absent from reply", "Switched to fallback" not in reply,
      repr(reply[:160]))
check("emoji arrow absent", "\U0001F504" not in reply)
check("real reply preserved",
      "Hi! I'm Hermes" in reply, repr(reply[:160]))
check("model names absent", "mog-8-s" not in reply and "glm-5.3-flash" not in reply)
check("no leading blank lines", reply == reply.strip())

print("test_healthy_reply_untouched")
reply2, _ = hl._parse_agent_output(HEALTHY_STDOUT, "")
check("healthy reply preserved",
      "Hi! 👋 How can I help you today?" in reply2, repr(reply2[:160]))
check("no fallback wording added", "Switched to fallback" not in reply2)

print("test_legacy_notice_format")
reply3, _ = hl._parse_agent_output(LEGACY_STDOUT, "")
check("legacy notice absent", "Switched to fallback" not in reply3)
check("legacy answer preserved", "answer text" in reply3, repr(reply3[:160]))

print("test_regex_direct")
check("matches 🔄 Switched to fallback model:",
      bool(hl._FALLBACK_NOTICE_RE.match(
          "\U0001F504 Switched to fallback model: a via b → c via d")))
check("matches ↻ Switched to fallback:",
      bool(hl._FALLBACK_NOTICE_RE.match("↻ Switched to fallback: m (p)")))
check("does not match normal prose",
      not hl._FALLBACK_NOTICE_RE.match("Hi there, how can I help?"))
check("does not match mid-line text",
      not hl._FALLBACK_NOTICE_RE.match("see: Switched to fallback model"))

print()
if fails:
    print("FAILED: %d" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("ALL PASS")
