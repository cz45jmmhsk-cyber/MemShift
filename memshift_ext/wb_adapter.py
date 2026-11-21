# -*- coding: utf-8 -*-
"""官方 workbuddy.rs 适配器 ingest_workbuddy_hook 的一比一 Python 移植。

把 WorkBuddy hook stdin 载荷转换成 hub 事件（start/step/wait/resolve/end/…），
回合、标题、审批等待等语义与官方一致。参考原文：
vendor/agent-companion/crates/agent-studio-core/src/adapters/workbuddy.rs:267-470
"""
import json
import time

from .hub import HUB, _text, _now

HOOK_EVENTS = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
               "PostToolUse", "PostToolUseFailure", "PermissionRequest",
               "Stop", "Notification", "PreCompact", "Interrupt"}

CLOSED = set()   # closed_monitor_sessions
LIVE = {}        # workbuddy_live: sid -> state dict

QUESTION_TOOLS = {"request_user_input", "request_user_input_async",
                  "AskUserQuestion", "ask_user_question", "RequestUserInput"}


def question_tool(name):
    tail = name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    return tail in QUESTION_TOOLS


def content(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(p.get("text", "") for p in v
                         if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def parsed(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def questions(v):
    p = parsed(v) or {}
    out = []
    for q in (p.get("questions") or []) if isinstance(p.get("questions"), list) else []:
        out.append({
            "text": (q.get("question") or q.get("title") or "") if isinstance(q, dict) else "",
            "header": _text(q.get("header")) if isinstance(q, dict) else "",
            "options": [{"label": (o if isinstance(o, str) else
                                   (o.get("label") if isinstance(o, dict) else "")),
                         "description": _text(o.get("description")) if isinstance(o, dict) else ""}
                        for o in (q.get("options") or []) if isinstance(q, dict) or isinstance(o, str)]
            if isinstance(q, dict) else [],
        })
    return out


def question(v):
    s = "\n".join(q["text"] for q in questions(v) if q["text"])
    return s or "等待用户输入"


def wait_text(p):
    message = content(p.get("message"))
    if message.strip():
        return message
    title = p.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "需要你确认"


def confirmation_notification(p):
    kind = (_text(p.get("notification_type")) or _text(p.get("notificationType"))).lower()
    if kind == "auth_success":
        return False
    if kind == "idle_prompt" or any(k in kind for k in
                                    ("permission", "confirm", "approval", "credential")):
        return True
    text = "%s %s" % (content(p.get("message")), p.get("title") or "")
    return ("?" in text or "？" in text or "确认" in text or "Allow " in text
            or "Deny " in text or "允许" in text or "拒绝" in text
            or "credential" in text.lower() or "凭证" in text)


def _merge(base, extra):
    d = dict(base)
    d.update(extra)
    return d


def ingest_hook(p, ts=None):
    """ingest_workbuddy_hook 的移植。返回 True 表示有事件进 hub。"""
    sid = _text(p.get("session_id")) or _text(p.get("sessionId"))
    event = _text(p.get("hook_event_name")) or _text(p.get("hookEventName"))
    if not sid or event not in HOOK_EVENTS:
        return False
    key = "workbuddy:%s" % sid
    if key in CLOSED:
        if event in ("Stop", "SessionEnd", "Interrupt"):
            return False
        if event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest"):
            CLOSED.discard(key)
    ts = ts if isinstance(ts, int) and ts > 0 else _now()
    previous = LIVE.get(sid)
    state = dict(previous) if previous else {"roundId": "", "cwd": "", "calls": {}}
    turn = _text(p.get("turn_id")) or _text(p.get("turnId"))
    begins = event in ("SessionStart", "UserPromptSubmit")
    if turn:
        round_ = turn
    elif begins:
        round_ = "turn:%d" % ts
    else:
        round_ = state["roundId"] or "turn:%d" % ts
    if previous is not None and state["roundId"] != round_ and not begins:
        return False
    if previous is None or state["roundId"] != round_:
        state["calls"] = {}
    state["roundId"] = round_
    cwd = p.get("cwd")
    if isinstance(cwd, str) and cwd:
        state["cwd"] = cwd
    edition = _text(p.get("agent_edition")) or _text(p.get("agentEdition"))
    if edition in ("international", "workbuddy-ai"):
        agent_type = "workbuddy-ai"
    elif edition in ("domestic", "workbuddy"):
        agent_type = "workbuddy"
    else:
        agent_type = _text(state.get("agentType")) or "workbuddy"
    state["agentType"] = agent_type
    LIVE[sid] = state
    base = {"source": "workbuddy", "sessionId": sid, "cwd": state["cwd"],
            "agentType": agent_type, "roundId": round_, "ts": ts}
    emit = lambda ev: HUB.ingest(_merge(base, ev))
    if previous is None or begins:
        emit({"type": "start"})
    title = p.get("session_title")
    title = title.strip() if isinstance(title, str) else ""
    tool = _text(p.get("tool_name")) or _text(p.get("toolName"))
    tid = _text(p.get("tool_use_id")) or _text(p.get("toolUseId"))
    tool_input = p.get("tool_input", p.get("toolInput"))

    if event == "UserPromptSubmit":
        for call, c in (state["calls"].items() if isinstance(state["calls"], dict) else []):
            if isinstance(c, dict) and (c.get("async") is True or c.get("permission") is True):
                emit({"type": "resolve", "callId": call})
        label = title or content(p.get("prompt"))
        if label.strip():
            emit({"type": "meta", "title": label})
    elif event == "PreToolUse":
        if tid and not (state["calls"].get(tid) or {}).get("resolved"):
            is_async = tool.endswith("request_user_input_async")
            if isinstance(state["calls"], dict):
                state["calls"][tid] = {"tool": tool, "resolved": False,
                                       "async": is_async, "ts": ts}
            if question_tool(tool):
                emit({"type": "wait", "callId": tid, "tool": tool,
                      "text": question(tool_input), "questions": questions(tool_input)})
            else:
                emit({"type": "step", "eventId": tid, "label": tool})
    elif event in ("PostToolUse", "PostToolUseFailure"):
        if tid and isinstance(state["calls"], dict):
            c = state["calls"].get(tid) or {}
            is_async = c.get("async") is True or tool.endswith("request_user_input_async")
            state["calls"][tid] = {"tool": tool, "resolved": True,
                                   "async": is_async, "ts": ts}
            if not is_async:
                emit({"type": "resolve", "callId": tid})
    elif event == "PermissionRequest":
        call = tid or "perm:%d" % ts
        if not (state["calls"].get(call) or {}).get("resolved"):
            if isinstance(state["calls"], dict):
                state["calls"][call] = {"tool": tool or "permission", "resolved": False,
                                        "permission": True, "ts": ts}
            emit({"type": "wait", "callId": call, "tool": tool or "permission",
                  "text": wait_text(p)})
    elif event in ("Stop", "SessionEnd", "Interrupt"):
        emit({"type": "end", "status": "aborted" if event == "Interrupt" else "done"})
    elif event == "Notification":
        if title:
            emit({"type": "meta", "title": title})
        if confirmation_notification(p):
            call = tid or "notify:%d" % ts
            if not (state["calls"].get(call) or {}).get("resolved"):
                if isinstance(state["calls"], dict):
                    state["calls"][call] = {"tool": "notification", "resolved": False,
                                            "permission": True, "ts": ts}
                emit({"type": "wait", "callId": call, "tool": "notification",
                      "text": wait_text(p)})
        else:
            emit({"type": "activity"})
    else:
        if title:
            emit({"type": "meta", "title": title})
    if isinstance(state["calls"], dict) and len(state["calls"]) > 256:
        oldest = sorted(state["calls"].items(), key=lambda kv: (kv[1] or {}).get("ts", 0) if isinstance(kv[1], dict) else 0)
        for call, _c in oldest[:len(state["calls"]) - 256]:
            state["calls"].pop(call, None)
    return True


def session_monitor_close(payload):
    """官方 adapters/mod.rs request("session_monitor_close") 的移植。"""
    if not isinstance(payload, dict):
        return {"closed": False}
    sid = _text(payload.get("sessionId"))
    rid = _text(payload.get("roundId"))
    if not sid or not rid:
        return {"closed": False}
    key = "workbuddy:%s" % sid
    s = HUB.sessions.get(key)
    if not s or _text(s.get("roundId")) != rid:
        return {"closed": False}
    HUB.sessions.pop(key, None)
    LIVE.pop(sid, None)
    HUB.events[:] = [e for e in HUB.events
                     if not (e.get("sessionId") == key and _text(e.get("roundId")) == rid)]
    if len(CLOSED) >= 512:
        CLOSED.clear()
    CLOSED.add(key)
    return {"closed": True}
