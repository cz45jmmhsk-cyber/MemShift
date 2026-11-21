# -*- coding: utf-8 -*-
"""把官方 10 事件 hook 合并进 ~/.workbuddy/settings.json。

一比一复刻官方 workbuddy.rs merge_workbuddy_hooks：
- 只移除带本脚本标记（workbuddy-status.py）的旧条目，用户已有 hook 全保留；
- 每个事件追加 {"matcher":"","hooks":[{type,command,timeout:3,statusMessage,async}]}，
  SessionEnd 不加 async（官方同款）。
"""
import json
import os
import shutil
import tempfile

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".workbuddy", "settings.json")
HOOK_SCRIPT = os.path.join(HOME, ".wb-switch", "workbuddy-status.py")

EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
          "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Stop",
          "Notification", "PreCompact"]

MARKER = "workbuddy-status.py"

FORWARDER = '''#!/usr/bin/python3
"""官方 agent-companion/collector/hooks/workbuddy-status.py 的移植版。
把 WorkBuddy 生命周期 hook 转发给本地 MemShift 采集端点。永远退出 0。"""
import json, os, sys, urllib.request

def edition_from_path(path):
    normalized = os.path.abspath(path).replace("\\\\", "/").lower()
    return "international" if "/.workbuddy-ai/" in normalized else "domestic"

def main():
    sys.stdout.write("{}\\n"); sys.stdout.flush()
    url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "astra-office-monitor.url")
    base = "http://127.0.0.1:1889"
    try:
        with open(url_file, encoding="utf-8") as handle:
            base = handle.read().strip() or base
    except OSError:
        pass
    raw = sys.stdin.buffer.read(65536) or b"{}"
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["agent_edition"] = edition_from_path(__file__)
    req = urllib.request.Request(base.rstrip("/") + "/api/workbuddy-hook",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass

if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
'''


def ensure_script():
    """转发脚本 + URL 指向文件落盘（每次覆写，保证与内置版本一致）。"""
    os.makedirs(os.path.dirname(HOOK_SCRIPT), exist_ok=True)
    with open(HOOK_SCRIPT, "w", encoding="utf-8") as f:
        f.write(FORWARDER)
    url_file = os.path.join(os.path.dirname(HOOK_SCRIPT), "astra-office-monitor.url")
    with open(url_file, "w", encoding="utf-8") as f:
        f.write("http://127.0.0.1:%d\n" % PORT)


PORT = 1889


def _is_ours(cmd):
    return MARKER in cmd


def merged_hooks(doc):
    """返回合并后的 hooks dict（不落盘）。doc 为现有 settings dict。"""
    if not isinstance(doc, dict):
        raise ValueError("现有 WorkBuddy Hook 配置无效")
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("现有 WorkBuddy Hook 配置无效")
    command = "python \"%s\"" % HOOK_SCRIPT
    for event in EVENTS:
        groups = hooks.get(event)
        groups = list(groups) if isinstance(groups, list) else []
        cleaned = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            hs = g.get("hooks")
            if isinstance(hs, list):
                hs = [h for h in hs if not (isinstance(h, dict)
                                            and _is_ours(str(h.get("command", ""))))]
                if hs:
                    g = dict(g)
                    g["hooks"] = hs
                    cleaned.append(g)
            elif g.get("command") is not None:
                if not _is_ours(str(g.get("command", ""))):
                    cleaned.append(g)
        h = {"type": "command", "command": command, "timeout": 3,
             "statusMessage": "Agent Studio"}
        if event != "SessionEnd":
            h["async"] = True
        cleaned.append({"matcher": "", "hooks": [h]})
        hooks[event] = cleaned
    return hooks


def install():
    ensure_script()
    if not os.path.isfile(SETTINGS):
        return {"ok": False, "error": "未找到 ~/.workbuddy/settings.json"}
    with open(SETTINGS, "r", encoding="utf-8") as f:
        doc = json.load(f)
    backup = SETTINGS + ".bak-companion"
    if not os.path.isfile(backup):
        shutil.copy2(SETTINGS, backup)
    doc["hooks"] = merged_hooks(doc)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS)
    return {"ok": True, "backup": backup, "events": EVENTS}


def installed_events():
    """返回当前 settings.json 里已挂本脚本的事件清单（自检用）。"""
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    out = []
    for event, groups in (doc.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if _is_ours(str(h.get("command", ""))):
                    out.append(event)
                    break
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(install(), ensure_ascii=False))
