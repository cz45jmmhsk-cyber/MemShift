# -*- coding: utf-8 -*-
"""桌面伙伴（宠物栏）—— 一比一嵌入官方 agent-companion 前端包。

不做任何自绘替代：dist-embed 是上游 MIT 项目构建好的完整产物
（小房子场景 + 动物 + 动画），按官方 EmbedHost 协议
（`window.parent.__AGENT_STUDIO_EMBED_HOST__ = {desktop, listen, invoke,
enableNotifications}`，命令走 `plugin:agent-studio|<name>`）提供宿主桥。
会话数据来自本机 WorkBuddy 的 sessions 表 + hook 事件，合成官方 Snapshot 结构。
"""
import ctypes
import json
import os
import sqlite3
import sys
import threading
import time

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".workbuddy", "workbuddy.db")
HOOKS = os.path.join(HOME, ".wb-switch", "hook-events.jsonl")
CFG = os.path.join(HOME, ".wb-switch", "companion.json")

if getattr(sys, "frozen", False):
    _HERE = os.path.join(sys._MEIPASS, "companion_embed")
else:
    _HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "companion_embed")

HOOK_STATUS = {
    "UserPromptSubmit": "running", "PreToolUse": "running", "PostToolUse": "running",
    "PreCompact": "running", "SubagentStop": "running", "SessionStart": "running",
    "Notification": "wait", "PermissionRequest": "wait",
    "Stop": "done", "FinalStop": "done",
}


# ---------------- 数据：官方 Snapshot ----------------

def _read_hook_states(limit_lines=4000):
    """hook 事件里每个会话最近一次状态与时间戳（毫秒，事件缺 ts 则为 0）。"""
    out = {}
    if not os.path.isfile(HOOKS):
        return out
    try:
        with open(HOOKS, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit_lines:]
    except Exception:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        sid = d.get("session_id")
        st = HOOK_STATUS.get(str(d.get("hook_event_name") or ""))
        if not sid or not st:
            continue
        ts = 0
        for k in ("ts", "timestamp", "_ts"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 10 ** 12:  # 毫秒级
                ts = max(ts, int(v))
        old = out.get(sid)
        if old is None or ts >= old[1]:
            out[sid] = (st, ts)
    return out


def _read_db_sessions(limit=16):
    if not os.path.isfile(DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=3)
        rows = con.execute(
            "SELECT id, title, custom_title, model, status, updated_at, last_activity_at, cwd "
            "FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({"id": r[0], "title": ((r[2] or r[1] or "未命名会话").strip())[:22],
                    "model": r[3] or "", "dbStatus": r[4] or "",
                    "updatedAt": r[5] or 0, "activityAt": r[6] or r[5] or 0,
                    "cwd": r[7] or ""})
    return out


def build_snapshot():
    """官方 Snapshot——hub 实时状态（hub.snapshot()）。

    与官方 collector 完全一致的数据路径：
    WorkBuddy hook → /api/workbuddy-hook → wb_adapter.ingest_hook → hub.
    官方语义：不恢复历史会话（只有 hook 事件出现的会话才进快照）。
    """
    try:
        from .hub import HUB
        return HUB.snapshot()
    except Exception:
        now = int(time.time() * 1000)
        return {"version": 1, "ready": True, "ts": now, "sources": {},
                "sessions": [], "events": []}


def collect(max_items=5, active_hours=3.0):
    """旧接口兼容：扁平会话列表（/api/companion/sessions 仍在用）。"""
    hooks = _read_hook_states()
    rows = _read_db_sessions(40)
    now = time.time() * 1000
    items = []
    for r in rows:
        hk = hooks.get(r["id"])
        st = hk[0] if hk else None
        if not st:
            st = {"working": "running", "completed": "done", "error": "error"}.get(r["dbStatus"], "")
        ts = r["activityAt"] or r["updatedAt"] or 0
        age_h = (now - ts) / 3600000.0 if ts else 999
        if age_h > active_hours:
            continue
        if not st:
            st = "idle"
        if st == "done" and age_h > 0.5:
            st = "sleep"
        items.append({"id": r["id"], "title": r["title"], "model": r["model"],
                      "status": st, "ts": ts, "ageMin": int(age_h * 60)})
    items.sort(key=lambda x: {"wait": 0, "running": 1, "error": 2, "done": 3}.get(x["status"], 4))
    return {"ok": True, "sessions": items[:max_items], "total": len(items),
            "updatedAt": int(now)}


# ---------------- 官方默认配置（settings-config.js 一字不差） ----------------

def default_settings():
    """官方 settings-config.js defaultSettings() 一字不差。"""
    return {
        "version": 1,
        "sources": {"codex": {"enabled": True, "path": ""},
                    "workbuddy": {"enabled": True, "path": "", "logWatch": True},
                    "codebuddy-ide": {"enabled": True, "path": ""},
                    "codeg": {"enabled": True, "path": ""}},
        "monitor": {"avatarStyle": "animal", "railVisibleCount": 8, "autoDiscover": True,
                    "retentionHours": 0.5, "assignment": "auto", "seats": ["auto"] * 8},
        "scene": {"light": "day", "weather": "clear", "lightning": True, "door": False,
                  "ceiling": False, "playing": True, "speed": 1, "maxFps": 60,
                  "renderResolution": "native", "showPerformance": False,
                  "reducedMotion": False, "defaultView": "program"},
        "notifications": {"desktop": False, "wait": True, "error": True, "done": True,
                          "sound": False},
        "general": {"mode": "live", "rememberView": True},
        "schedule": {"enabled": False, "start": "09:00", "end": "18:00", "deferBusy": True},
    }


def default_rail_prefs():
    return {"avatarStyle": "animal", "visibleCount": 8, "animation": True,
            "size": "standard", "autostartSupported": False,
            "autostartManaged": False, "autostart": False}


def _load_cfg():
    try:
        return json.load(open(CFG, encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(patch):
    d = _load_cfg()
    d.update(patch)
    try:
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        json.dump(d, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return d


def get_settings():
    d = _load_cfg()
    base = default_settings()
    saved = d.get("settings") or {}
    for g in ("monitor", "scene", "notifications", "general", "schedule"):
        if isinstance(saved.get(g), dict):
            base[g].update(saved[g])
    if isinstance(saved.get("sources"), dict):
        base["sources"].update(saved["sources"])
    return base


def get_rail_prefs():
    d = default_rail_prefs()
    d.update(_load_cfg().get("railPrefs") or {})
    return d


# ---------------- 宿主桥页面（iframe + EmbedHost 协议） ----------------

WRAPPER = r"""<!doctype html><html><head><meta charset="utf-8">
<title>桌面伙伴</title><style>
html,body{margin:0;width:100%%;height:100%%;background:transparent;overflow:hidden;
  -webkit-user-select:none;user-select:none}
iframe{position:fixed;inset:0;width:100%%;height:100%%;border:0;background:transparent}
</style></head><body>
<iframe id="f" src="__EMBED_SRC__"></iframe>
<script>
const BASE="__BASE__";
const handlers={};
window.__AGENT_STUDIO_EMBED_HOST__={
  desktop:true,
  listen:async(ev,h)=>{(handlers[ev]=handlers[ev]||[]).push(h);
    return()=>{const a=handlers[ev]||[],i=a.indexOf(h);if(i>=0)a.splice(i,1);};},
  invoke:async(cmd,args)=>{const name=String(cmd).split("|").pop();
    return await call(name,args||{});},
  enableNotifications:async()=>"granted",
};
async function call(name,a){
  switch(name){
    case "monitor_state":{const r=await fetch(BASE+"/api/companion/snapshot",{cache:"no-store"});
      return {snapshot:await r.json(),connected:true};}
    case "collector_request":{
      const c=a.command;
      if(c==="settings_get"||c==="settings_check")
        return await(await fetch(BASE+"/api/companion/settings",{cache:"no-store"})).json();
      if(c==="settings_set")
        return await(await fetch(BASE+"/api/companion/settings_save",{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify(a.payload||{})})).json();
      if(c==="integrations_get")return{};
      if(c==="integrations_set")return a.payload||{};
      if(c==="session_monitor_close")
        return await(await fetch(BASE+"/api/workbuddy-close",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(a.payload||{})})).json();
      return{};}
    case "set_hit_regions":return null;
    case "rail_settings_get":
      return await(await fetch(BASE+"/api/companion/rail_prefs",{cache:"no-store"})).json();
    case "rail_settings_set":
      return await(await fetch(BASE+"/api/companion/rail_prefs_save",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(a.preferences||{})})).json();
    case "rail_reset_position":
      await fetch(BASE+"/api/companion/reset_pos",{method:"POST"});return null;
    case "open_view":
      if(a.view==="settings"){try{window.pywebview.api.open_main();}catch(e){}}
      return null;
    case "open_session_url":
      await fetch(BASE+"/api/companion/open_url",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({url:a.url})});
      return null;
    case "open_integration_folder":return null;
    default:throw new Error("未知命令 "+name);
  }
}
function emit(ev,p){(handlers[ev]||[]).forEach(h=>{try{h({payload:p});}catch(e){}});}
emit("monitor-connection","connected");
setInterval(async()=>{try{
  emit("monitor-state",await(await fetch(BASE+"/api/companion/snapshot",
    {cache:"no-store"})).json());
}catch(e){}},3000);
const f=document.getElementById("f");
/* 窗口尺寸跟随官方 rail 实际渲染矩形（含悬浮卡片/通知，官方窗 368×600 同源） */
setInterval(()=>{try{
  const d=f.contentDocument;if(!d||!d.body)return;
  let l=1e9,r=0,b=0;
  const push=el=>{if(!el||el.hidden||el.getClientRects().length===0)return;
    const rc=el.getBoundingClientRect();
    l=Math.min(l,rc.left);r=Math.max(r,rc.right);b=Math.max(b,rc.bottom);};
  push(d.querySelector(".desktop-strip"));
  d.querySelectorAll(".desktop-card,.desktop-notice,.desktop-context-menu").forEach(push);
  if(l>r)return;
  const w=Math.ceil(r-l+48),h=Math.ceil(b+40);
  if(w>100&&h>60&&window.pywebview&&window.pywebview.api)
    window.pywebview.api.fit(Math.ceil(r-l+48),h);
}catch(e){}},2000);
/* 拖拽：官方 grip / 拖拽区 → pywebview 移窗 */
setInterval(()=>{try{
  const doc=f.contentDocument;if(!doc)return;
  doc.querySelectorAll("[data-tauriDragRegion],.desktop-grip").forEach(el=>{
    if(el.__dragged)return;el.__dragged=true;el.style.cursor="grab";
    el.addEventListener("pointerdown",ev=>{
      ev.preventDefault();
      let lx=ev.screenX,ly=ev.screenY;
      const mv=e2=>{const dx=e2.screenX-lx,dy=e2.screenY-ly;lx=e2.screenX;ly=e2.screenY;
        if(dx||dy){try{window.pywebview.api.move_by(dx,dy);}catch(e){}}};
      const up=()=>{window.removeEventListener("pointermove",mv);
        window.removeEventListener("pointerup",up);};
      window.addEventListener("pointermove",mv);window.addEventListener("pointerup",up);
    });
  });
}catch(e){}},1500);
</script></body></html>"""


def wrapper_html(app_url=""):
    src = app_url.rstrip("/") + "/companion/embed/desktop.html"
    return WRAPPER.replace("__EMBED_SRC__", src).replace("__BASE__", app_url.rstrip("/"))


def html():
    """兼容旧路由：返回宿主桥页面。"""
    return wrapper_html("")


_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
         ".svg": "image/svg+xml", ".json": "application/json", ".webp": "image/webp",
         ".woff2": "font/woff2", ".woff": "font/woff", ".map": "application/json"}


def embed_file(rel):
    """读取官方包内静态文件，返回 (bytes, content_type)；路径逃逸一律拒绝。"""
    rel = (rel or "").lstrip("/").replace("\\", "/")
    root = os.path.abspath(_HERE)
    path = os.path.abspath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) and path != root:
        return None, None
    if not os.path.isfile(path):
        return None, None
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        return f.read(), _MIME.get(ext, "application/octet-stream")


# ---------------- 窗口 ----------------

def _work_area():
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    r = RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    try:
        return 0, 0, ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        return 0, 0, 1920, 1080


def _apply_rail_style():
    """把悬浮栏设成 WS_EX_TOOLWINDOW —— 不进任务栏 / Alt-Tab。

    官方 create_rail 用 Tauri 的 skip_taskbar=true（lib.rs:498-517），
    pywebview 无此参数，故用 Win32 直接改扩展样式等价实现。幂等，可反复调用。
    """
    try:
        win = _state.get("window")
        if win is None:
            return False
        native = getattr(win, "native", None)
        if native is None:
            return False
        handle = getattr(native, "Handle", None)
        if handle is None:
            return False
        try:
            hwnd = int(handle.ToInt64())   # pythonnet System.IntPtr
        except Exception:
            hwnd = int(handle)
        if not hwnd:
            return False
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if new != ex:
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
        return True
    except Exception:
        return False


class RailApi:
    """暴露给页面 JS 的极简接口。

    属性必须以下划线开头：pywebview 对 js_api 公开属性做递归自省，
    公开的 Window 对象会把日志刷爆（见 util.get_functions）。
    """

    def __init__(self):
        self._window = None

    def hide(self):
        try:
            if self._window is not None:
                self._window.hide()
        except Exception:
            pass
        return True

    def open_main(self):
        """rail 里点「设置」→ 拉起主窗口（同一进程 HTTP 已在跑）。"""
        try:
            import webbrowser
            port = _state.get("port") or 1889
            webbrowser.open("http://127.0.0.1:%d/#set" % port)
        except Exception:
            pass
        return True

    def fit(self, w, h):
        """调整窗口大小并锁定右缘。

        2026-11-20 修复：pywebview frameless 窗口的 `.x/.y` 在部分时机返回
        32767（屏幕外哨兵值），旧的 `win.x + (old_w-w)` 会把它永久卡在 x=32767
        （任务栏预览里那个空白深色窗）。改为用 Python 侧自维护坐标，绝不读 win.x。
        """
        try:
            w = max(120, min(int(w), 1000))
            h = max(80, min(int(h), 1000))
            win = self._window
            if win is not None:
                old_w = int(_state.get("rw") or w)
                win.resize(w, h)
                rx = int(_state.get("rx") or 0) + (old_w - w)
                ry = int(_state.get("ry") or 0)
                win.move(rx, ry)
                _state["rx"], _state["ry"], _state["rw"] = rx, ry, w
        except Exception:
            pass
        _apply_rail_style()  # 幂等：确保不进任务栏（对齐官方 skip_taskbar=true）
        return True

    def move_by(self, dx, dy):
        try:
            win = self._window
            if win is not None:
                rx = int(_state.get("rx") or 0) + int(dx)
                ry = int(_state.get("ry") or 0) + int(dy)
                win.move(rx, ry)
                _state["rx"], _state["ry"] = rx, ry
        except Exception:
            pass
        return True


_state = {"window": None, "api": None, "port": 1889}
_lock = threading.Lock()


def is_open():
    return _state["window"] is not None


def launch(app_url="http://127.0.0.1:1889"):
    """打开（或聚焦）官方 Agent Companion 悬浮窗。"""
    with _lock:
        w = _state["window"]
        if w is not None:
            try:
                w.show()
                return {"ok": True, "reused": True}
            except Exception:
                _state["window"] = None
                return {"ok": False, "error": "窗口已失效，请重开 MemShift"}

    import webview

    # 验收钩子：MEMSHIFT_CDP=端口 → WebView2 开远程调试（只影响 rail 窗口）
    _cdp = os.environ.get("MEMSHIFT_CDP")
    if _cdp and str(_cdp).isdigit():
        try:
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(_cdp)
        except Exception:
            pass

    try:
        port = int(app_url.rsplit(":", 1)[-1])
    except Exception:
        port = 1889
    _state["port"] = port

    prefs = get_rail_prefs()
    n = max(3, min(16, int(prefs.get("visibleCount") or 8)))
    # 官方 create_rail：368×600、透明、无边框、置顶、不进任务栏（lib.rs:498-517）。
    # pywebview 等价实现；后续 fit() 会把窗口收缩到内容实际矩形。
    width, height = 368, 600
    l, t, r, b = _work_area()
    x = max(l + 8, r - width - 28)
    y = max(t + 8, t + 8)

    api = RailApi()
    win = webview.create_window("", url=app_url.rstrip("/") + "/companion/rail",
                                width=width, height=height,
                                x=x, y=y, frameless=True, on_top=True, resizable=False,
                                transparent=True, js_api=api)
    api._window = win
    _state["window"] = win
    _state["api"] = api
    # 自维护坐标：绝不读 pywebview 的 win.x（frameless 下是 32767 哨兵值）
    _state["rx"], _state["ry"], _state["rw"] = x, y, width

    def _on_closed():
        _state["window"] = None
    try:
        win.events.closed += _on_closed
    except Exception:
        pass
    return {"ok": True, "width": width, "height": height}


def reset_position(app_url="http://127.0.0.1:1889"):
    """回主屏右上角。"""
    w = _state.get("window")
    if w is None:
        return {"ok": False, "error": "窗口未打开"}
    try:
        width, height = w.width, w.height
        l, t, r, b = _work_area()
        w.move(max(l + 8, r - width - 28), t + 8)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def close():
    w = _state.get("window")
    if w is None:
        return {"ok": True}
    try:
        w.destroy()
    except Exception:
        pass
    _state["window"] = None
    return {"ok": True}


def selftest():
    rows = []

    def ck(name, ok, info=""):
        rows.append((name, bool(ok), info))

    snap = build_snapshot()
    ck("Snapshot version=1", snap.get("version") == 1)
    ck("Snapshot ready", snap.get("ready") is True)
    ck("events 为数组", isinstance(snap.get("events"), list))
    ss = snap.get("sessions") or []
    ck("会话 <= 官方模型上限", len(ss) <= 64, str(len(ss)))
    need = {"id", "source", "sessionId", "cwd", "title", "roundId", "startedAt",
            "updatedAt", "status", "steps", "pending", "tokens", "endedAt",
            "project", "stale", "elapsed", "progress"}
    ck("Session 字段与官方类型一致", all(need <= set(s) for s in ss),
       str(sorted(need - set(ss[0])) if ss and need - set(ss[0]) else ""))
    legal = {"unknown", "running", "wait", "done", "error", "aborted"}
    ck("状态在官方枚举内", all(s["status"] in legal for s in ss),
       str(sorted({s["status"] for s in ss})))
    old = collect()
    ck("旧接口 collect 兼容", old.get("ok") is True)
    st = get_settings()
    ck("官方默认设置（settings-config.js 一字不差）",
       st["monitor"]["avatarStyle"] == "animal"
       and st["monitor"]["railVisibleCount"] == 8
       and st["monitor"]["retentionHours"] == 0.5
       and len(st["monitor"]["seats"]) == 8
       and st["scene"]["maxFps"] == 60 and st["scene"]["light"] == "day")
    rp = get_rail_prefs()
    ck("railPrefs 结构（rail_settings.rs Default）", rp["avatarStyle"] == "animal"
       and rp["visibleCount"] == 8 and rp["animation"] is True
       and rp["size"] == "standard")
    page = wrapper_html("http://127.0.0.1:1889")
    ck("宿主桥含 EmbedHost", "__AGENT_STUDIO_EMBED_HOST__" in page)
    ck("iframe 指向官方包", "/companion/embed/desktop.html" in page)
    ck("monitor-state 轮询存在", "monitor-state" in page and "/api/companion/snapshot" in page)
    ck("官方命令全覆盖", all(k in page for k in
       ("monitor_state", "collector_request", "set_hit_regions", "rail_settings_get",
        "rail_settings_set", "rail_reset_position", "open_view", "open_session_url",
        "open_integration_folder")))
    ck("session_monitor_close 走真实端点", "/api/workbuddy-close" in page)
    ck("拖拽桥接存在", "move_by" in page and "desktop-grip" in page)
    ck("尺寸自适应存在", "api.fit" in page)
    import ast as _ast
    _tree = _ast.parse(open(__file__, encoding="utf-8").read())
    _reads_winx = any(
        isinstance(n, _ast.Attribute) and n.attr == "x"
        and isinstance(n.value, _ast.Name) and n.value.id in ("win", "w")
        for n in _ast.walk(_tree))
    ck("不读 win.x 哨兵值（防跑飞 x=32767）",
       not _reads_winx and "_state.get(\"rx\")" in
       open(__file__, encoding="utf-8").read())
    ck("skip_taskbar 等价实现存在",
       callable(_apply_rail_style) and "WS_EX_TOOLWINDOW" in
       open(__file__, encoding="utf-8").read())
    ck("官方包存在 desktop.html", os.path.isfile(os.path.join(_HERE, "desktop.html")))
    ck("官方包 assets 完整", os.path.isfile(os.path.join(_HERE, "assets", "desktop-B8gZhaEY.js"))
       and os.path.isfile(os.path.join(_HERE, "assets", "client-BYatEnVG.js")))
    wa = _work_area()
    ck("工作区可读", wa[2] > wa[0] and wa[3] > wa[1], str(wa))
    try:
        from . import hook_install as _hi
        ev = sorted(set(_hi.installed_events()))
        ck("官方 10 事件 hook 已挂", len(ev) == 10, ",".join(ev))
    except Exception as e:
        ck("官方 10 事件 hook 已挂", False, repr(e))
    ok = all(p for _, p, _ in rows)
    for name, passed, info in rows:
        print(("PASS" if passed else "FAIL"), name, ("| " + info if info else ""))
    print("companion: %d/%d" % (sum(1 for _, p, _ in rows if p), len(rows)))
    return ok, rows


if __name__ == "__main__":
    selftest()
