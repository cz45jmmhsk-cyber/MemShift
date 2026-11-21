# -*- coding: utf-8 -*-
"""官方 agent-studio-core hub.rs 的一比一 Python 移植。

只移植本机实际用到的部分（workbuddy 单源），语义与官方保持一致：
- ingest(ev)：start/step/wait/resolve/permission_check/permission_resolve/
  end/meta/activity/tokens 事件驱动会话状态机；
- snapshot()：官方 Snapshot 结构（stale=45min、elapsed 秒、project、events）。

参考原文：vendor/agent-companion/crates/agent-studio-core/src/hub.rs
（_src_wb-switch 仓库，MIT）。
"""
import json
import time

STALE = 45 * 60 * 1000          # hub.rs:4
TERMINAL = {"done", "error", "aborted"}
COPY_KEYS = ["cwd", "title", "folderId", "agentType", "externalId", "webPort",
             "hostKind", "sourceLabel", "parentTitle", "subagent"]
MAX_EVENTS = 500


def _text(v):
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, (int, float, bool)) else str(v)


def _now():
    return int(time.time() * 1000)


class Hub:
    def __init__(self):
        self.sessions = {}          # id("source:sid") -> session dict
        self.events = []            # 官方 push_back 队列
        self.sources = {"workbuddy": {"state": "ok", "detail": "", "checkedAt": _now()}}
        self.ready = True
        self.started = _now()

    def source_state(self, state, detail=""):
        """对齐适配器 poll 里的 sources 汇报（workbuddy.rs:498-505）。"""
        self.sources["workbuddy"] = {"state": state, "detail": detail,
                                     "checkedAt": _now()}

    # ---------------- ingest ----------------

    def ingest(self, ev):
        if not isinstance(ev, dict):
            return
        source = _text(ev.get("source"))
        sid = _text(ev.get("sessionId"))
        if not source or not sid:
            return
        sid_key = "%s:%s" % (source, sid)
        ts = ev.get("ts") if isinstance(ev.get("ts"), int) else _now()
        ty = _text(ev.get("type"))
        round_ = _text(ev.get("roundId"))
        s = self.sessions.get(sid_key)
        if s is None:
            s = {"id": sid_key, "source": source, "sessionId": sid, "cwd": "",
                 "title": "", "roundId": round_ or "observed:%d" % ts,
                 "startedAt": ts, "updatedAt": 0, "status": "unknown",
                 "steps": [], "pending": [], "tokens": None, "endedAt": None}
            self.sessions[sid_key] = s
        for key in COPY_KEYS:
            v = ev.get(key)
            if v is not None and _text(v):
                if key == "title":
                    s[key] = _text(v)[:240]
                elif key == "sourceLabel":
                    s[key] = _text(v)[:60]
                elif key == "parentTitle":
                    s[key] = _text(v)[:80]
                else:
                    s[key] = v
        if ty == "meta":
            if round_ and _text(s["roundId"]).startswith("observed:"):
                s["roundId"] = round_
            return
        # 官方：旧事件直接丢弃（ts 倒退），同回合 codex wait/resolve 例外
        if ts < (s["updatedAt"] or 0):
            return
        if ty == "start" and round_ != _text(s["roundId"]):
            s["roundId"] = round_ or "turn:%d" % ts
            s["startedAt"] = ts
            s["endedAt"] = None
            s["pending"] = []
            s["permissionChecks"] = []
            s["steps"] = []
            s["status"] = "running"
            s["tokens"] = None
            s.pop("endedBy", None)
        if round_ and _text(s["roundId"]).startswith("observed:"):
            s["roundId"] = round_
        if round_ and ty != "start" and round_ != _text(s["roundId"]):
            return
        s["updatedAt"] = max(ts, s["updatedAt"] or 0)
        event = None
        if ty == "start":
            s["status"] = "running" if not s["pending"] else "wait"
        elif ty == "step" and _text(s["status"]) not in TERMINAL:
            if not any(x["id"] == ev.get("eventId") for x in s["steps"]):
                s["steps"].append({"id": ev.get("eventId"), "ts": ts,
                                   "label": _text(ev.get("label"))[:200]})
            if len(s["steps"]) > 20:
                s["steps"].pop(0)
            s["status"] = "running" if not s["pending"] else "wait"
        elif ty == "wait" and _text(s["status"]) not in TERMINAL:
            if not any(x["id"] == ev.get("callId") for x in s["pending"]):
                s["pending"].append({"id": ev.get("callId"), "tool": ev.get("tool"),
                                     "text": _text(ev.get("text"))[:500],
                                     "questions": ev.get("questions") if isinstance(ev.get("questions"), list) else [],
                                     "ts": ts})
            s["status"] = "wait"
            event = ("wait", ev.get("callId"))
        elif ty == "resolve":
            s["pending"] = [p for p in s["pending"] if p["id"] != ev.get("callId")]
            if _text(s["status"]) not in TERMINAL:
                s["status"] = "running" if not s["pending"] else "wait"
        elif ty == "permission_check" and _text(s["status"]) not in TERMINAL:
            checks = s.setdefault("permissionChecks", [])
            if not any(c["id"] == ev.get("callId") for c in checks):
                checks.append({"id": ev.get("callId"), "ts": ts})
        elif ty == "permission_resolve":
            s["permissionChecks"] = [c for c in s.get("permissionChecks", [])
                                     if c["id"] != ev.get("callId")]
        elif ty == "end":
            s["status"] = ev.get("status") if isinstance(ev.get("status"), str) else "done"
            s["endedAt"] = ts
            s["pending"] = []
            s["permissionChecks"] = []
            if _text(ev.get("endedBy")):
                s["endedBy"] = ev.get("endedBy")
            if _text(s["status"]) in ("done", "error"):
                event = (_text(s["status"]), s["roundId"])
        elif ty == "tokens" and isinstance(ev.get("tokens"), (int, float)):
            s["tokens"] = ev.get("tokens")
        if event:
            eid = json.dumps([sid_key, s["roundId"], event[0], event[1]],
                             ensure_ascii=False)
            if not any(e["id"] == eid for e in self.events):
                self.events.append({"id": eid, "sessionId": sid_key,
                                    "roundId": s["roundId"], "kind": event[0],
                                    "ts": ts, "historical": not self.ready or ts < self.started,
                                    "title": s["title"]})
            if len(self.events) > MAX_EVENTS:
                self.events.pop(0)

    # ---------------- snapshot ----------------

    def snapshot(self):
        time_ms = _now()
        sessions = []
        for s in self.sessions.values():
            s = dict(s)
            pend = [p for p in s["pending"]
                    if not (p.get("optional") is True
                            and time_ms - p.get("ts", 0) > 30000)]
            s["pending"] = pend
            if s["status"] == "wait" and not pend:
                s["status"] = "running"
            stale = (_text(s["status"]) not in TERMINAL and not pend
                     and time_ms - (s["updatedAt"] or 0) > STALE)
            cwd = _text(s["cwd"])
            import os
            project = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or s["source"]
            s["project"] = project
            s["stale"] = stale
            if stale:
                s["status"] = "unknown"
            ended = s["endedAt"] if s["endedAt"] is not None else time_ms
            s["elapsed"] = max((ended - (s["startedAt"] or time_ms)) / 1000.0, 0.0)
            s["progress"] = None
            sessions.append(s)
        sessions.sort(key=lambda x: -(x["updatedAt"] or 0))
        events = list(self.events)
        for s in sessions:
            for p in s["pending"]:
                eid = json.dumps([s["id"], s["roundId"], "wait", p["id"]],
                                 ensure_ascii=False)
                if not any(e["id"] == eid for e in events):
                    events.append({"id": eid, "sessionId": s["id"],
                                   "roundId": s["roundId"], "kind": "wait",
                                   "ts": p["ts"], "historical": True,
                                   "title": s["title"]})
        return {"version": 1, "ready": self.ready, "ts": time_ms,
                "sources": self.sources, "sessions": sessions, "events": events}


HUB = Hub()
