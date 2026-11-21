# -*- coding: utf-8 -*-
"""
忆迁 MemShift —— 像素记忆移植工作台 v1（反推自开源 workbuddy-switch + 我们的完善）
================================================================
复刻的核心机制（源码反推，crates/wb-switch-core）：
  1. 认证文件 ~/AppData/Local/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
     四段 JSON: {account, auth, accounts, allAccounts}；写后校验 accessToken；清 .logged-out 标记
  2. 账号库 ~/.wb-switch/accounts.json（与官方工具兼容共存，直接读同一份）
  3. 会话三层: ~/.workbuddy/workbuddy.db sessions 表 + projects/ 正文 + edge-sync-mapping-vN.db

界面照官方 WorkBuddy Switch 结构 1:1：账号管理 / 关联会话 / Token统计 / 积分统计 / 设置。
数据全部来自本机真数据（accounts.json + workbuddy.db sessions / session_usage）。

我们的完善（官方没有的）：
  A. 切换前自动全量备份会话（db+wal/shm+projects+映射库）→ 记忆移植保险层
  B. 切换后自动跑换号链：08_一键验收 → 写 bridge/inbox 换号事件（供 WorkBuddy 自动化接续）
  C. 像素风桌面 webui（127.0.0.1:1889，缝合像素字体 OFL 授权可商用）

CLI:
  python wb_switch_pro.py status            当前账号快照
  python wb_switch_pro.py list              账号列表（脱敏）
  python wb_switch_pro.py backup            手动全量备份会话
  python wb_switch_pro.py switch --id <账号id> [--no-backup]   真切换（危险操作，需 --yes）
  python wb_switch_pro.py serve             启动 webui (127.0.0.1:1889)
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 扩展能力包（对齐 workbuddy-switch 的基础功能：IDE/VS Code 切换、进程控制、限额台账等）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memshift_ext import aesgcm, ext_sessions, extras as ext_extras  # noqa: E402
from memshift_ext import ide_switch, limits_board, proc_ctl, secret_storage  # noqa: E402
from memshift_ext import pet_rail  # noqa: E402
from memshift_ext import wb_adapter, hook_install  # noqa: E402

# 控制台输出统一 UTF-8（打包成 exe 后在 GBK 控制台跑，emoji 昵称会崩）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOME = os.path.expanduser("~")
AUTH_DIR = os.path.join(HOME, "AppData", "Local", "CodeBuddyExtension", "Data", "Public", "auth")
AUTH_FILE = {"cn": os.path.join(AUTH_DIR, "workbuddy-desktop.info"),
             "ai": os.path.join(AUTH_DIR, "workbuddy-desktop-ai.info")}
ACCOUNTS_JSON = os.path.join(HOME, ".wb-switch", "accounts.json")
WB_ROOT = os.path.join(HOME, ".workbuddy")
WB_DB = os.path.join(WB_ROOT, "workbuddy.db")
# ---- 可分发路径（2026-11-21 一步到位改造）：exe 所在目录即工作包目录 ----
PACK = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
VERIFY08 = os.path.join(PACK, "08_一键验收.py")
BACKUP_ROOT = (r"G:/WorkBuddy/downloads/wb_session_backups" if os.path.isdir(r"G:/WorkBuddy")
               else os.path.join(PACK, "backups"))
BRIDGE = (r"G:/WorkBuddy/node-red/bridge" if os.path.isdir(r"G:/WorkBuddy/node-red/bridge")
          else os.path.join(PACK, "bridge"))
INBOX = os.path.join(BRIDGE, "inbox")
LOGF = os.path.join(BRIDGE, "wb_switch_pro.log")
PORT = 1889


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(BRIDGE, exist_ok=True)
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def init_runtime_io():
    """窗口模式(exe 无控制台)下的 stdout/stderr 兜底：
    ① 双击启动：没有控制台 → 所有输出静默，崩溃写 memshift_crash.log；
    ② 命令行启动（MemShift.exe verify 等）：挂到父控制台 → 正常打印。"""
    if os.name == "nt":
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                for name in ("stdout", "stderr"):
                    if getattr(sys, name) is None:
                        try:
                            setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                                    errors="replace", buffering=1))
                        except Exception:
                            pass
                if sys.stdin is None:
                    try:
                        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
                    except Exception:
                        pass
        except Exception:
            pass
    if sys.stdout is None or sys.stderr is None:
        try:
            os.makedirs(BRIDGE, exist_ok=True)
            fh = open(os.path.join(BRIDGE, "memshift_stdio.log"), "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = fh
            if sys.stderr is None:
                sys.stderr = fh
        except Exception:
            pass

    def _excepthook(t, v, tb):
        try:
            import traceback
            txt = "".join(traceback.format_exception(t, v, tb))
            os.makedirs(BRIDGE, exist_ok=True)
            with open(os.path.join(BRIDGE, "memshift_crash.log"), "a", encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (datetime.datetime.now().isoformat(" "), txt))
            log("未捕获异常: %r" % (v,))
        except Exception:
            pass
    sys.excepthook = _excepthook
    try:
        threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


# ---------------- 账号库 ----------------

def load_accounts():
    if not os.path.isfile(ACCOUNTS_JSON):
        return []
    with open(ACCOUNTS_JSON, encoding="utf-8") as f:
        d = json.load(f)
    return d if isinstance(d, list) else d.get("accounts", [])


def mask(s):
    if not isinstance(s, str) or len(s) < 10:
        return str(s)
    return s[:6] + "…" + s[-4:]


def account_brief(a):
    return {
        "id": a.get("id"), "uid": mask(a.get("uid", "")),
        "key": a.get("uid", ""),
        "nickname": a.get("nickname"), "domain": a.get("domain"),
        "variant": a.get("variant", "cn"),
        "email": a.get("email"),
        "enterpriseName": a.get("enterpriseName"),
        "expiresAt": a.get("expiresAt"),
        "refreshExpiresAt": a.get("refreshExpiresAt"),
        "hasToken": bool(a.get("access_token") or a.get("auth_raw")),
    }


def current_uid():
    """从认证文件读当前登录账号 uid。"""
    for _, p in AUTH_FILE.items():
        try:
            d = json.load(open(p, encoding="utf-8"))
            acc = d.get("account", {})
            if acc.get("uid"):
                return acc["uid"]
        except Exception:
            pass
    return None


# ---------------- 切换（复刻机制） ----------------

def _atomic_write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def build_account_obj(acc):
    obj = dict(acc.get("profile_raw") or {})
    obj["uid"] = acc.get("uid", "")
    obj["nickname"] = acc.get("nickname", "")
    obj.setdefault("type", "personal")
    return obj


def build_auth_obj(acc):
    raw = acc.get("auth_raw") or {}
    inner = raw.get("auth") if isinstance(raw.get("auth"), dict) else raw
    obj = dict(inner or {})
    now = int(datetime.datetime.now().timestamp() * 1000)
    # token 可能是明文串或 $wbEncrypted 信封对象——原样写回，由 WorkBuddy 自行解密
    at, rt = acc.get("access_token"), acc.get("refresh_token")
    if at:
        obj["accessToken"] = at
    if rt:
        obj["refreshToken"] = rt
    obj["tokenType"] = acc.get("token_type", "Bearer")
    obj["domain"] = acc.get("domain", "")
    obj["lastRefreshTime"] = now
    obj.setdefault("scope", "openid profile offline_access email")
    exp = acc.get("expiresAt")
    if exp:
        obj["expiresAt"] = exp
        obj["expiresIn"] = max(0, (exp - now) // 1000)
        rexp = acc.get("refreshExpiresAt") or exp
        obj.setdefault("refreshExpiresAt", rexp)
        obj["refreshExpiresIn"] = max(0, (rexp - now) // 1000)
    else:
        obj.setdefault("expiresIn", 0)
        obj.setdefault("refreshExpiresIn", 0)
    obj.setdefault("notBeforePolicy", 0)
    obj.setdefault("sessionState", "")
    return obj


def switch_account(acc_id, do_backup=True, do_chain=True, copy_ids=None, sync_ids=None,
                   restart=None):
    """切换 WorkBuddy 登录态。restart=None 时读配置 auto_close_client（默认开）。

    P1：复制/同步会话要求客户端未运行，过去靠人工关；现在由本流程自动
    「关闭 → 写库 → 重开」，关闭失败即中止（不写半截）。
    """
    accs = load_accounts()
    acc = next((a for a in accs if a.get("id") == acc_id), None)
    if not acc:
        return {"ok": False, "error": "账号不存在: %s" % acc_id}
    variant = acc.get("variant", "cn")
    path = AUTH_FILE.get(variant)
    if not path:
        return {"ok": False, "error": "未知档位: %s" % variant}

    if restart is None:
        try:
            restart = bool(ext_extras.load().get("auto_close_client", True))
        except Exception:
            restart = True

    steps = []
    closed_client = False
    # 0) 自动关闭客户端（P1）：复制/同步/写库都要求它不在运行
    if restart and proc_ctl.is_running("workbuddy"):
        ok, msg = proc_ctl.close("workbuddy", 25)
        steps.append("自动关闭 WorkBuddy：%s" % msg)
        if not ok:
            return {"ok": False, "error": msg, "steps": steps}
        closed_client = True
    elif restart:
        steps.append("WorkBuddy 未在运行，跳过关闭")

    # 1) 认证文件备份
    if os.path.isfile(path):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = os.path.join(BACKUP_ROOT, "auth_backups")
        os.makedirs(bdir, exist_ok=True)
        bpath = os.path.join(bdir, os.path.basename(path) + "." + ts)
        shutil.copy2(path, bpath)
        steps.append("auth 备份 → %s" % bpath)
    # 2) 会话全量备份（聊天记录保险层）
    if do_backup:
        r = backup_sessions(tag="pre_switch")
        steps.append("会话备份 → %s" % (r.get("dest") if r.get("ok") else r.get("error")))
    prev_uid = current_uid()
    prev_acc = next((a for a in accs if a.get("uid") == prev_uid), None)
    # 2.5) 会话接力：复制勾选会话 + 同步已关联会话（目标账号写侧，官方同款门禁：
    #      客户端运行中不写库；复制/同步失败不阻断切换本身，只如实上报）
    relay = {}
    if copy_ids:
        relay["copy"] = copy_sessions(prev_acc or {}, acc, copy_ids)
        steps.append("复制会话 %d 个（成功 %d/已关联 %d/失败 %d）" % (
            len(copy_ids), len(relay["copy"].get("copied", [])),
            len(relay["copy"].get("alreadyLinked", [])),
            len(relay["copy"].get("errors", []))))
    if sync_ids:
        relay["sync"] = sync_sessions(prev_acc or {}, acc, sync_ids)
        steps.append("同步关联会话 %d 个（成功 %d/一致 %d/失败 %d）" % (
            len(sync_ids), len(relay["sync"].get("synced", [])),
            len(relay["sync"].get("skipped", [])),
            len(relay["sync"].get("errors", []))))
    # 3) 构造四段 JSON 并原子写
    existing = {}
    try:
        existing = json.load(open(path, encoding="utf-8"))
    except Exception:
        existing = {}
    all_accounts = existing.get("allAccounts") or existing.get("accounts") or []
    if not isinstance(all_accounts, list):
        all_accounts = []
    acc_obj = build_account_obj(acc)
    auth_obj = build_auth_obj(acc)
    merged = [a for a in all_accounts
              if (a.get("uid") or a.get("id")) not in ("", acc.get("uid"))]
    merged.append(acc_obj)
    session = {"account": acc_obj, "auth": auth_obj,
               "accounts": merged, "allAccounts": merged}
    _atomic_write(path, json.dumps(session, ensure_ascii=False, indent=2))
    steps.append("认证文件已写入 (%d 个 allAccounts)" % len(merged))
    # 4) 写后校验
    try:
        w = json.load(open(path, encoding="utf-8"))
        if w.get("auth", {}).get("accessToken") != auth_obj.get("accessToken"):
            return {"ok": False, "error": "写后校验失败", "steps": steps}
        steps.append("写后校验通过")
    except Exception as e:
        return {"ok": False, "error": "写后校验异常: %r" % e, "steps": steps}
    # 5) 清退出标记
    marker = path + ".logged-out"
    if os.path.isfile(marker):
        os.remove(marker)
        steps.append("已清理退出标记")
    steps.append("✅ 已切换到: %s (%s)" % (acc.get("nickname"), mask(acc.get("uid", ""))))
    # 7) 自动重开客户端（P1）：读新登录态，免得用户还惦记「要不要手动开回来」
    restarted = False
    if restart and closed_client:
        ok, msg = proc_ctl.launch("workbuddy")
        steps.append("自动重开 WorkBuddy：%s" % msg)
        restarted = ok
        if not ok:
            steps.append("⚠ 登录态已生效，但客户端未能自动打开，请手动启动")
    log(" | ".join(steps))
    result = {"ok": True, "steps": steps, "nickname": acc.get("nickname"),
              "uid": acc.get("uid"), "restarted": restarted}
    if relay:
        result["relay"] = relay
    # 6) 换号自动链（我们的完善）：蓝本落位 → Basic Memory 体检 → 主脑广播 → 验收 → inbox 事件
    if do_chain:
        result["chain"] = run_chain(prev=prev_uid, target_acc=acc)
    return result


# ---------------- 会话备份（聊天记录保险层） ----------------

def backup_sessions(tag="manual"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_ROOT, "%s_%s" % (ts, tag))
    try:
        os.makedirs(dest, exist_ok=True)
        n = 0
        for f in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
            p = os.path.join(WB_ROOT, f)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(dest, f))
                n += 1
        mapping_n = 0
        for f in os.listdir(WB_ROOT):
            if f.startswith("edge-sync-mapping") and f.endswith(".db"):
                shutil.copy2(os.path.join(WB_ROOT, f), os.path.join(dest, f))
                mapping_n += 1
        proj_n = 0
        pdir = os.path.join(WB_ROOT, "projects")
        if os.path.isdir(pdir):
            shutil.copytree(pdir, os.path.join(dest, "projects"), dirs_exist_ok=True)
            proj_n = sum(len(fs) for _, _, fs in os.walk(os.path.join(dest, "projects")))
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dest) for f in fs)
        log("会话备份 %s: db%d 映射%d projects文件%d 共%.1fMB"
            % (dest, n, mapping_n, proj_n, size / 1048576))
        return {"ok": True, "dest": dest, "db": n, "mapping": mapping_n,
                "project_files": proj_n, "size_mb": round(size / 1048576, 1)}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


# ---------------- 换号自动链（我们的完善 · 一键全自动） ----------------

BLUEPRINT = os.path.join(PACK, "02_引导层蓝本.md")
BM_CANDIDATES = [r"G:/WorkBuddy/tools/bin/basic-memory.exe",
                 os.path.join(HOME, ".local", "bin", "basic-memory.exe"),
                 os.path.join(PACK, "tools", "basic-memory.exe")]
BM_EXE = next((p for p in BM_CANDIDATES if os.path.isfile(p)), BM_CANDIDATES[0])


def _resolve_brain():
    """主脑目录解析（2026-11-21 可分发改造）：老机器沿用既有 G 盘主脑；新机器用 exe 旁 memory-brain。"""
    if os.path.isdir(os.path.join(r"G:/WorkBuddy/memory-brain", "notes")):
        return r"G:/WorkBuddy/memory-brain"
    return os.path.join(PACK, "memory-brain")


BRAIN = _resolve_brain()
BROADCAST = os.path.join(BRAIN, "brain-broadcast.md")

MINIMAL_BLUEPRINT = """# 用户级长期记忆 — 引导层

> 本文件由 MemShift 在换号时自动落位。它只做一件事：告诉新会话去哪里找完整记忆。

## 记忆主脑位置
- 完整记忆库：MemShift 程序目录下 `memory-brain/`（MEMORY.md 索引 / notes/ 记忆卡 / brain-broadcast.md 广播）
- 任何新会话开工前先读它；本文件只保留全局铁律与路径指引，请在此补充你自己的固定规则。
"""


def bm_health():
    """Basic Memory 健康体检（可选件：未安装则整体跳过，不拖垮换号链结论）。"""
    h = {"exe": os.path.isfile(BM_EXE)}
    try:
        mcp = open(os.path.join(HOME, ".workbuddy", "mcp.json"),
                   encoding="utf-8", errors="ignore").read()
        h["mcp"] = "basic-memory" in mcp
    except Exception:
        h["mcp"] = False
    h["brain"] = os.path.isdir(os.path.join(BRAIN, "notes"))
    if not h["mcp"] and not h["exe"]:
        h["skip"] = "Basic Memory 未安装（可选件），跳过"
        h["state"] = "skip"
        h["ok"] = True
    elif h["mcp"] and h["exe"]:
        h["state"] = "ok"
        h["ok"] = True
    else:
        h["state"] = "broken"
        h["ok"] = False
    return h


def bm_install():
    """Basic Memory 一键安装（2026-11-21）：探测 uv/pip → 安装 → 定位 exe →
    复制到包内 tools/bin（随 exe 分发）→ 注册 ~/.workbuddy/mcp.json（先备份）→ 复检。"""
    r = {"ok": False}
    h0 = bm_health()
    if h0.get("state") == "ok":
        r.update(ok=True, message="已安装且注册，无需重装", health=h0)
        return r
    exe, way = None, None
    uv = shutil.which("uv")
    if not uv:
        for c in (r"D:/Program Files/AiPyPro/resources/app.asar.unpacked/resources/bin/uv.exe",
                  os.path.join(HOME, ".local", "bin", "uv.exe"),
                  os.path.join(PACK, "tools", "uv.exe")):
            if os.path.isfile(c):
                uv = c
                break
    if uv:
        way = "uv"
        try:
            subprocess.run([uv, "tool", "install", "basic-memory"], capture_output=True,
                           text=True, timeout=900, encoding="utf-8", errors="replace")
        except Exception as e:
            r["install_error"] = repr(e)
        cands = [os.path.join(HOME, ".local", "bin", "basic-memory.exe"),
                 os.path.join(os.environ.get("APPDATA", ""), "uv", "tools",
                              "basic-memory", "Scripts", "basic-memory.exe")]
        exe = next((p for p in cands if os.path.isfile(p)), None)
    if exe is None:
        pip = shutil.which("pip") or shutil.which("pip3")
        if pip:
            way = "pip"
            try:
                subprocess.run([pip, "install", "basic-memory"], capture_output=True,
                               text=True, timeout=900, encoding="utf-8", errors="replace")
            except Exception as e:
                r["install_error"] = repr(e)
            try:
                import sysconfig
                p = os.path.join(sysconfig.get_path("scripts"), "basic-memory.exe")
                if os.path.isfile(p):
                    exe = p
            except Exception:
                pass
    if exe is None:
        r["message"] = ("未能自动安装（未找到 uv/pip）。请先安装 uv 后重试一键安装，"
                        "或将 basic-memory.exe 放到程序旁 tools/bin/ 目录。")
        return r
    dest = os.path.join(PACK, "tools", "bin")
    os.makedirs(dest, exist_ok=True)
    dest_exe = os.path.join(dest, "basic-memory.exe")
    try:
        shutil.copy2(exe, dest_exe)
    except Exception:
        dest_exe = exe
    mcp_path = os.path.join(HOME, ".workbuddy", "mcp.json")
    try:
        os.makedirs(os.path.dirname(mcp_path), exist_ok=True)
        cfg = {}
        if os.path.isfile(mcp_path):
            shutil.copy2(mcp_path, mcp_path + ".bak-bm")
            with open(mcp_path, encoding="utf-8") as f:
                cfg = json.load(f)
        servers = cfg.setdefault("mcpServers", {})
        servers["basic-memory"] = {"command": dest_exe.replace("\\", "/"),
                                   "args": ["mcp", "--transport", "stdio", "--project", "brain"]}
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        r["registered"] = mcp_path
    except Exception as e:
        r["register_error"] = repr(e)
    h = bm_health()
    r.update(ok=bool(h.get("ok")), way=way, exe=dest_exe, health=h)
    r["message"] = ("Basic Memory 安装并注册完成" if r["ok"]
                    else "安装流程已执行，但体检仍未全过，请看 health 详情")
    return r


def run_chain(prev=None, target_acc=None):
    out = {"verify_rc": None, "inbox": None, "memory": None, "broadcast": None,
           "basic_memory": None}
    tgt_uid = (target_acc or {}).get("uid", "")
    tgt_nick = (target_acc or {}).get("nickname", "?")
    # 1) 引导层蓝本 → 新账号用户目录 MEMORY.md（uid=用户目录 uuid，已实证）
    #    蓝本缺失时自动生成（2026-11-21 一步到位）：优先快照旧账号现有引导层，否则最小模板
    if target_acc and not os.path.isfile(BLUEPRINT):
        try:
            src = (os.path.join(HOME, ".workbuddy", "user-%s-personal" % prev, "MEMORY.md")
                   if prev else "")
            if prev and os.path.isfile(src):
                txt = (datetime.date.today().strftime("# 引导层蓝本（%Y-%m-%d 自动快照自旧账号 %s）\n\n" % prev)
                       + open(src, encoding="utf-8", errors="ignore").read())
                out["blueprint_source"] = "旧账号快照"
            else:
                txt = MINIMAL_BLUEPRINT
                out["blueprint_source"] = "最小模板"
            with open(BLUEPRINT, "w", encoding="utf-8") as f:
                f.write(txt)
            log("蓝本缺失，已自动生成（%s）→ %s" % (out["blueprint_source"], BLUEPRINT))
        except Exception as e:
            out["blueprint_error"] = repr(e)
    if target_acc and os.path.isfile(BLUEPRINT):
        try:
            udir = os.path.join(HOME, ".workbuddy", "user-%s-personal" % tgt_uid)
            os.makedirs(udir, exist_ok=True)
            mem = os.path.join(udir, "MEMORY.md")
            stamp = datetime.datetime.now().strftime("%Y-%m-%d")
            txt = open(BLUEPRINT, encoding="utf-8").read()
            txt = txt.replace("YYYY-MM-DD", stamp)
            if os.path.isfile(mem):
                bakm = mem + ".pre_switch_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(mem, bakm)
                out["memory_prev"] = bakm
            open(mem, "w", encoding="utf-8").write(txt)
            out["memory"] = mem
            log("引导层蓝本已落位 → %s" % mem)
        except Exception as e:
            out["memory_error"] = repr(e)
    # 2) Basic Memory 体检（未安装则自动尝试一键安装，失败不阻断换号链）
    h_bm = bm_health()
    if h_bm.get("state") == "skip":
        try:
            h_bm = bm_install()
        except Exception as e:
            h_bm["install_error"] = repr(e)
    out["basic_memory"] = h_bm
    # 3) 主脑广播换号条（新账号首个会话跑 preflight 即见）；主脑缺失则自动创建最小骨架
    try:
        if not os.path.isdir(os.path.join(BRAIN, "notes")):
            os.makedirs(os.path.join(BRAIN, "notes"), exist_ok=True)
            out["brain_created"] = BRAIN
            log("主脑目录不存在，已自动创建最小骨架 → %s" % BRAIN)
        if not os.path.isfile(BROADCAST):
            with open(BROADCAST, "w", encoding="utf-8") as f:
                f.write("# 主脑广播\n")
        brain_mem = os.path.join(BRAIN, "MEMORY.md")
        if not os.path.isfile(brain_mem):
            with open(brain_mem, "w", encoding="utf-8") as f:
                f.write("# 记忆主脑索引\n\n- 本目录由 MemShift 于换号时自动创建（最小骨架）。\n"
                        "- 结构：MEMORY.md 索引 / notes/ 记忆卡 / brain-broadcast.md 广播。\n"
                        "- 请把你的长期规则沉淀到本目录；用户级 MEMORY.md 只保留铁律与本路径指引。\n")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = ("\n### [%s] 换号事件 · MemShift 一键切换 → %s\n"
                 "- 旧账号 uid: `%s` → 新账号: %s（uid `%s`）\n"
                 "- 来源: MemShift 一键切换（备份/登录态/蓝本落位已由程序自动完成）\n"
                 "- 新账号会话: 跑 preflight --health 即接续；验收见 bridge/inbox 换号事件\n"
                 % (ts, tgt_nick, prev or "未知", tgt_nick, tgt_uid or "未知"))
        with open(BROADCAST, "a", encoding="utf-8") as f:
            f.write(entry)
        out["broadcast"] = "appended"
        log("主脑广播已追加换号条 → %s" % tgt_nick)
    except Exception as e:
        out["broadcast_error"] = repr(e)
    # 4) 08 一键验收（脚本在包内才跑；裸 exe 分发形态自动跳过）
    if os.path.isfile(VERIFY08):
        try:
            r = subprocess.run([sys.executable, VERIFY08], capture_output=True,
                               text=True, timeout=900, encoding="utf-8", errors="replace")
            out["verify_rc"] = r.returncode
            lines = [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()]
            out["verify_tail"] = "\n".join(lines[-15:])
        except Exception as e:
            out["verify_tail"] = repr(e)
    else:
        out["verify_rc"] = "skipped"
        out["verify_tail"] = "08_一键验收.py 不在包内，跳过（裸 exe 分发形态）"
    # 5) 换号事件 inbox（WorkBuddy 自动化/新会话读到即自动接续，免手动贴指令包）
    #    bridge/inbox 存在（本机）用它；分发形态落 exe 旁 inbox/
    inbox_dir = INBOX if os.path.isdir(BRIDGE) else os.path.join(PACK, "inbox")
    os.makedirs(inbox_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    inbox_path = os.path.join(inbox_dir, "换号事件_%s.md" % ts)
    with open(inbox_path, "w", encoding="utf-8") as f:
        f.write(
            "# 换号事件 @ %s（MemShift 一键切换自动生成）\n\n"
            "- 旧账号 uid: `%s`\n- 新账号: %s（uid `%s`）\n\n"
            "## 已由 MemShift 自动完成\n"
            "1. 全量会话备份 + auth 备份\n"
            "2. 登录态写入 + 校验 + 清退出标记\n"
            "3. 引导层蓝本已落位: `%s/.workbuddy/user-%s-personal/MEMORY.md`\n"
            "4. 主脑广播换号条已追加（主脑目录: %s）\n\n"
            "## 待执行（新会话读到本文件即接续，无需人工贴指令包）\n"
            "1. 验收: `%s` 存在则跑 `python %s`，否则确认引导层可读即可\n"
            "2. 定时任务: 若包内有 automations-blueprint.json，用 automation_update 工具按蓝本重建；没有则向用户确认是否需要\n"
            "3. 汇报完即停，等用户指示\n"
            % (ts, prev or "未知", tgt_nick, tgt_uid or "未知", HOME, tgt_uid,
               BRAIN, VERIFY08, VERIFY08))
    out["inbox"] = inbox_path
    log("换号链完成: 蓝本=%s bm=%s 广播=%s 验收rc=%s inbox=%s"
        % (bool(out["memory"]), out["basic_memory"].get("ok"), out["broadcast"],
           out["verify_rc"], out["inbox"]))
    return out


# ---------------- 会话接力（复制/关联，反推官方 session.rs + session_link.rs） ----------------
# 机制（源码反推 1:1）：
#   · 同一逻辑会话的各账号副本归入同一「接力组」，组存 ~/.wb-switch/memshift_links.json
#   · 复制 = 新 uuid 副本正文（projects/{workspace}/{new_cid}.jsonl）+ sessions 行插副本
#     （id=new_cid、user_id=目标 uid，其余照抄源行）+ 组内登记源/目标双成员 + 配对基线
#   · 云端 edge-sync-mapping 故意不预写（官方 design：预写会令客户端判定已迁移跳过上传，
#     导致云端无会话、后续 RENAME/ACTIVITY 404）——副本由客户端下次启动自行迁移
#   · 同步 = 对比源正文与配对基线的记录数差，把新增行追加到目标正文并推进基线
#   · 已复制过的（组内已有目标 active 成员）不再重复复制（AlreadyLinked）
import uuid as _uuid

LINKS_FILE = os.path.join(HOME, ".wb-switch", "memshift_links.json")
PROJECTS_DIR = os.path.join(WB_ROOT, "projects")
COPY_BAK_ROOT = os.path.join(BACKUP_ROOT, "session_copy_backups")


def _wb_client_running():
    """WorkBuddy 桌面客户端是否在运行（运行中禁止会话写库，与官方门禁同语义）。"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace")
        names = (r.stdout or "").lower()
        for key in ("workbuddy.exe", "workbuddy desktop", "codebuddy.exe"):
            if key in names:
                return True
        return False
    except Exception:
        return False


def _find_jsonl(cid):
    direct = os.path.join(PROJECTS_DIR, cid + ".jsonl")
    if os.path.isfile(direct):
        return direct
    if os.path.isdir(PROJECTS_DIR):
        for name in os.listdir(PROJECTS_DIR):
            p = os.path.join(PROJECTS_DIR, name, cid + ".jsonl")
            if os.path.isfile(p):
                return p
    return None


def _read_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def _pair_key(src_uid, src_cid, tgt_uid, tgt_cid):
    return "%s|%s::%s|%s" % (src_uid, src_cid, tgt_uid, tgt_cid)


# ---------------- 会话身份改写（对齐 workbuddy-switch 的 find_session_identities / rewrite_identity_to） ----------------
# 会话正文里身份不止一处写法：JSON 主字段 "sessionId"，baggage 里的 codebuddy.session_id=。
# 且被复制过的会话正文可能残留更早一手的历史 id（与数据库 id 不一致），
# 因此不能只替换数据库 id，必须把正文里出现的所有会话身份统一改写成本副本 id。

_UUID_RE = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F\-])'
_ID_FIELD_RE = re.compile(r'"sessionId"\s*:\s*"(' + _UUID_RE + r')"')
_ID_BAG_RE = re.compile(r'codebuddy\.session_id=(' + _UUID_RE + r')')


def find_session_identities(text):
    """返回正文里出现的所有会话身份（去重、保持出现顺序）。

    只认严格的标准 uuid（8-4-4-4-12）。截断的短串（如输出打印里的 id[:8]）不算身份，
    否则会把聊天正文里的文字一起改掉，等于篡改历史。
    """
    found = []
    for rx in (_ID_FIELD_RE, _ID_BAG_RE):
        for m in rx.finditer(text):
            v = m.group(1)
            if v not in found:
                found.append(v)
    return found


def rewrite_identity(text, target_cid):
    """把正文里所有会话身份统一改写成 target_cid，返回 (改写后文本, 被替换的旧身份列表)。"""
    target = (target_cid or "").strip()
    if not target:
        return text, []
    olds = [v for v in find_session_identities(text) if v != target]
    out = text
    for old in sorted(olds, key=len, reverse=True):
        out = out.replace(old, target)
    return out, olds


def residual_identities(text, target_cid):
    """改写后仍存在的非本副本身份；为空表示干净。"""
    target = (target_cid or "").strip()
    return [v for v in find_session_identities(text) if v != target]


def load_links():
    try:
        d = json.load(open(LINKS_FILE, encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("groups"), list):
            return d
    except Exception:
        pass
    return {"version": 1, "revision": 0, "groups": []}


def save_links(store):
    store["revision"] = int(store.get("revision", 0)) + 1
    _atomic_write(LINKS_FILE, json.dumps(store, ensure_ascii=False, indent=2))


def _find_group(store, uid, cid):
    for g in store.get("groups", []):
        for m in g.get("members", []):
            if (m.get("uid") == uid and m.get("session_id") == cid
                    and m.get("state", "active") == "active"):
                return g
    return None


def _group_target_member(g, tgt_uid):
    if not g:
        return None
    for m in g.get("members", []):
        if m.get("uid") == tgt_uid and m.get("state", "active") == "active":
            return m
    return None


def _backup_db_for_copy():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(COPY_BAK_ROOT, ts)
    os.makedirs(dest, exist_ok=True)
    n = 0
    for f in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
        p = os.path.join(WB_ROOT, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dest, f))
            n += 1
    if n == 0:
        raise RuntimeError("workbuddy.db 不存在，未复制")
    return dest


def relay_preview(source_uid, target_uid):
    """切换弹窗数据源：当前账号会话 × 关联状态（可同步/内容一致/未关联）。"""
    con = _db_readonly()
    try:
        rows = con.execute(
            "SELECT id, cwd, title, custom_title, updated_at, is_playground "
            "FROM sessions WHERE user_id=? AND deleted_at IS NULL ORDER BY updated_at DESC",
            (source_uid,)).fetchall()
    finally:
        con.close()
    store = load_links()
    out = []
    for r in rows:
        cid = r["id"]
        item = {
            "id": cid,
            "title": (r["custom_title"] or r["title"] or cid[:8]),
            "updatedAt": r["updated_at"],
            "isPlayground": bool(r["is_playground"]),
            "hasHistory": _find_jsonl(cid) is not None,
            "linked": False, "syncable": False, "newRecords": 0,
        }
        g = _find_group(store, source_uid, cid)
        tgt = _group_target_member(g, target_uid)
        if tgt:
            item["linked"] = True
            item["targetSessionId"] = tgt.get("session_id")
            base = (g.get("pair_bases") or {}).get(
                _pair_key(source_uid, cid, target_uid, tgt.get("session_id")))
            n_src = len(_read_lines(_find_jsonl(cid))) if item["hasHistory"] else 0
            base_n = (base or {}).get("record_count", 0)
            item["newRecords"] = max(0, n_src - base_n)
            item["syncable"] = item["newRecords"] > 0
        out.append(item)
    return out


def copy_sessions(source_acc, target_acc, ids):
    """把勾选会话复制给目标账号（复制后归属目标账号；已复制过的自动跳过）。"""
    src_uid = (source_acc or {}).get("uid", "")
    tgt_uid = (target_acc or {}).get("uid", "")
    if not src_uid or not tgt_uid:
        return {"ok": False, "error": "源/目标账号缺少 uid"}
    if src_uid == tgt_uid:
        return {"ok": False, "error": "源账号与目标账号相同，无需复制"}
    if not ids:
        return {"ok": True, "copied": [], "alreadyLinked": [], "errors": []}
    try:
        bak = _backup_db_for_copy()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    store = load_links()
    copied, linked, errors = [], [], []
    con = None
    try:
        con = sqlite3.connect(WB_DB, timeout=20)
        con.row_factory = sqlite3.Row
        now = int(datetime.datetime.now().timestamp() * 1000)
        for cid in ids:
            try:
                row = con.execute("SELECT * FROM sessions WHERE id=? AND deleted_at IS NULL",
                                  (cid,)).fetchone()
                if not row:
                    raise RuntimeError("数据库中找不到源会话记录")
                if row["user_id"] != src_uid:
                    raise RuntimeError("源会话不属于当前账号")
                body = _find_jsonl(cid)
                if not body:
                    raise RuntimeError("会话正文不存在")
                g = _find_group(store, src_uid, cid)
                if _group_target_member(g, tgt_uid):
                    linked.append({"id": cid, "groupId": (g or {}).get("id", "")})
                    continue
                lines = _read_lines(body)
                new_cid = str(_uuid.uuid4())
                new_path = os.path.join(os.path.dirname(body), new_cid + ".jsonl")
                # 正文里的会话身份必须统一改写成本副本 id：源正文可能残留更早一手的历史 id，
                # 只替换数据库 id 会漏改，导致新副本被判为「记录对不上」。
                new_text, replaced_ids = rewrite_identity("".join(lines), new_cid)
                resid = residual_identities(new_text, new_cid)
                if resid:
                    raise RuntimeError("复制后正文仍残留非本副本身份（%s），已停止复制"
                                       % ", ".join(resid[:3]))
                with open(new_path, "w", encoding="utf-8", errors="replace") as f:
                    f.write(new_text)
                new_row = dict(row)
                new_row["id"] = new_cid
                new_row["user_id"] = tgt_uid
                if "deleted_at" in new_row:
                    new_row["deleted_at"] = None
                cols = list(new_row.keys())
                con.execute(
                    "INSERT INTO sessions (%s) VALUES (%s)"
                    % (", ".join(cols), ", ".join("?" * len(cols))),
                    [new_row[c] for c in cols])
                if not g:
                    g = {"id": str(_uuid.uuid4()), "variant": target_acc.get("variant", "cn"),
                         "created_at": now, "members": [], "pair_bases": {}}
                    store["groups"].append(g)
                members = g.setdefault("members", [])
                if not any(m.get("uid") == src_uid and m.get("session_id") == cid
                           for m in members):
                    members.append({"member_id": str(_uuid.uuid4()), "uid": src_uid,
                                    "session_id": cid, "state": "active", "linked_at": now})
                members.append({"member_id": str(_uuid.uuid4()), "uid": tgt_uid,
                                "session_id": new_cid, "state": "active", "linked_at": now})
                digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
                g.setdefault("pair_bases", {})[
                    _pair_key(src_uid, cid, tgt_uid, new_cid)] = {
                    "record_count": len(lines), "total_digest": digest, "synced_at": now}
                copied.append({"id": cid, "newId": new_cid, "groupId": g["id"],
                               "backup": bak})
            except Exception as e:
                msg = str(e)
                if "locked" in msg.lower():
                    msg = "数据库被占用（WorkBuddy 客户端在运行？），请关闭客户端后重试"
                errors.append({"id": cid, "error": msg})
        con.commit()
    except Exception as e:
        return {"ok": False, "error": repr(e), "copied": copied, "errors": errors}
    finally:
        if con:
            con.close()
    save_links(store)
    log("会话复制: 源%s → 目标%s 复制%d 已关联%d 失败%d"
        % (mask(src_uid), mask(tgt_uid), len(copied), len(linked), len(errors)))
    return {"ok": not errors, "copied": copied, "alreadyLinked": linked, "errors": errors}


def sync_sessions(source_acc, target_acc, ids):
    """把关联会话中源账号的新内容（基线之后的记录）追加到目标会话。"""
    src_uid = (source_acc or {}).get("uid", "")
    tgt_uid = (target_acc or {}).get("uid", "")
    store = load_links()
    synced, skipped, errors = [], [], []
    con = None
    try:
        con = sqlite3.connect(WB_DB, timeout=20)
        con.row_factory = sqlite3.Row
        now = int(datetime.datetime.now().timestamp() * 1000)
        for cid in ids:
            try:
                g = _find_group(store, src_uid, cid)
                tgt = _group_target_member(g, tgt_uid)
                if not tgt:
                    raise RuntimeError("未建立关联（先复制会话）")
                s_body, t_body = _find_jsonl(cid), _find_jsonl(tgt.get("session_id"))
                if not s_body or not t_body:
                    raise RuntimeError("源或目标正文不存在")
                s_lines, t_lines = _read_lines(s_body), _read_lines(t_body)
                base = (g.get("pair_bases") or {}).get(
                    _pair_key(src_uid, cid, tgt_uid, tgt.get("session_id")))
                if not base:
                    raise RuntimeError("该对副本无共同基线，请重新复制建立关联")
                n_done = int(base.get("record_count", 0))
                if len(s_lines) <= n_done:
                    skipped.append({"id": cid, "reason": "内容一致"})
                    continue
                new = s_lines[n_done:]
                # 同步追加的行带的是源会话身份，必须改写成目标副本 id，否则目标正文出现两套身份。
                new_text, replaced_ids = rewrite_identity("".join(new), tgt.get("session_id"))
                resid = residual_identities(new_text, tgt.get("session_id"))
                if resid:
                    raise RuntimeError("同步内容仍残留非本副本身份（%s），已停止同步"
                                       % ", ".join(resid[:3]))
                with open(t_body, "a", encoding="utf-8", errors="replace") as f:
                    f.write(new_text)
                digest = hashlib.sha256("".join(s_lines).encode("utf-8")).hexdigest()
                base["record_count"] = len(s_lines)
                base["total_digest"] = digest
                base["synced_at"] = now
                for m in g.get("members", []):
                    if m.get("uid") == tgt_uid and m.get("session_id") == tgt.get("session_id"):
                        m["last_synced_at"] = now
                con.execute("UPDATE sessions SET updated_at=? WHERE id=? AND user_id=?",
                            (now, tgt.get("session_id"), tgt_uid))
                synced.append({"id": cid, "newId": tgt.get("session_id"),
                               "added": len(new)})
            except Exception as e:
                errors.append({"id": cid, "error": str(e)})
        con.commit()
    except Exception as e:
        return {"ok": False, "error": repr(e), "synced": synced, "errors": errors}
    finally:
        if con:
            con.close()
    save_links(store)
    log("会话同步: 源%s → 目标%s 同步%d 一致%d 失败%d"
        % (mask(src_uid), mask(tgt_uid), len(synced), len(skipped), len(errors)))
    return {"ok": not errors, "synced": synced, "skipped": skipped, "errors": errors}


# ---------------- 本机真数据统计（界面数据源） ----------------

STATS_CACHE = {"t": 0, "data": None}


def _db_readonly():
    con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def collect_stats(force=False):
    """汇总本机真数据：每账号会话数/最近活动/token 用量/积分用量。30s 缓存。"""
    now = datetime.datetime.now().timestamp()
    if not force and STATS_CACHE["data"] and now - STATS_CACHE["t"] < 30:
        return STATS_CACHE["data"]
    out = {"sessions": {}, "usage": {}, "sessions_list": {}}
    if os.path.isfile(WB_DB):
        try:
            con = _db_readonly()
            cur = con.cursor()
            # 每账号会话统计
            for r in cur.execute(
                "SELECT user_id uid, COUNT(*) n, MAX(COALESCE(updated_at,created_at)) last "
                "FROM sessions WHERE deleted_at IS NULL GROUP BY user_id"):
                out["sessions"][r["uid"]] = {"n": r["n"], "last": r["last"]}
            # token 用量（session_usage join sessions 归到账号）
            for r in cur.execute(
                "SELECT s.user_id uid, COUNT(*) n, SUM(u.used) used, SUM(u.size) size, "
                "MAX(u.updated_at) last FROM session_usage u "
                "LEFT JOIN sessions s ON s.id = u.session_id GROUP BY s.user_id"):
                out["usage"][r["uid"] or ""] = {
                    "n": r["n"], "used": r["used"] or 0, "size": r["size"] or 0, "last": r["last"]}
            # 每账号最近 60 个会话（关联会话页）
            for r in cur.execute(
                "SELECT user_id uid, id, COALESCE(custom_title,title) title, model, status, "
                "created_at, updated_at FROM sessions WHERE deleted_at IS NULL "
                "ORDER BY COALESCE(updated_at,created_at) DESC LIMIT 400"):
                out["sessions_list"].setdefault(r["uid"], []).append({
                    "id": r["id"], "title": r["title"] or "(未命名)", "model": r["model"] or "-",
                    "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"]})
            # 积分用量：credit_json 是 {模型hash: 积分}，按账号归集
            credits = {}
            for r in cur.execute(
                "SELECT s.user_id uid, u.credit_json cj FROM session_usage u "
                "LEFT JOIN sessions s ON s.id = u.session_id WHERE u.credit_json IS NOT NULL"):
                try:
                    for mk, v in json.loads(r["cj"]).items():
                        d = credits.setdefault(r["uid"] or "", {})
                        d[mk] = round(d.get(mk, 0) + float(v), 2)
                except Exception:
                    pass
            out["credits"] = credits
            con.close()
        except Exception as e:
            out["error"] = repr(e)
    STATS_CACHE["t"] = now
    STATS_CACHE["data"] = out
    return out


# ---------------- P2：Token/积分加深 + 会话分页 + 分歧处理 ----------------


def collect_stats_deep(days=30):
    """Token 趋势（按天）/ Top10 模型 / 请求明细（会话级）。全部来自本机库，不联网。"""
    out = {"daily": [], "top_models": [], "requests": [], "totals": {}, "days": days}
    if not os.path.isfile(WB_DB):
        out["error"] = "未找到 workbuddy.db"
        return out
    try:
        con = _db_readonly()
        cur = con.cursor()
        # 每日 token（按 session_usage.updated_at 落到本地日期）
        buckets = {}
        for r in cur.execute(
                "SELECT u.used, u.size, u.updated_at, s.user_id uid "
                "FROM session_usage u LEFT JOIN sessions s ON s.id = u.session_id"):
            ts = r["updated_at"]
            if not ts:
                continue
            day = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            b = buckets.setdefault(day, {"date": day, "used": 0, "size": 0, "sessions": 0})
            b["used"] += r["used"] or 0
            b["size"] += r["size"] or 0
            b["sessions"] += 1
        ordered = sorted(buckets.values(), key=lambda x: x["date"])[-days:]
        out["daily"] = ordered

        # Top10 模型（按 token 用量）
        models = {}
        for r in cur.execute(
                "SELECT COALESCE(s.model,'-') model, COUNT(*) n, SUM(u.used) used "
                "FROM session_usage u LEFT JOIN sessions s ON s.id = u.session_id "
                "GROUP BY COALESCE(s.model,'-') ORDER BY used DESC"):
            models[r["model"]] = {"model": r["model"], "sessions": r["n"], "used": r["used"] or 0}
        out["top_models"] = list(models.values())[:10]

        # 请求明细（最近 200 个有用量记录的会话）
        rows = []
        for r in cur.execute(
                "SELECT u.session_id id, COALESCE(s.custom_title,s.title) title, "
                "COALESCE(s.model,'-') model, s.user_id uid, u.used, u.size, u.updated_at "
                "FROM session_usage u LEFT JOIN sessions s ON s.id = u.session_id "
                "ORDER BY u.updated_at DESC LIMIT 200"):
            rows.append({
                "id": r["id"], "title": r["title"] or "(未命名)", "model": r["model"],
                "uid": r["uid"], "used": r["used"] or 0, "size": r["size"] or 0,
                "updated_at": r["updated_at"],
            })
        out["requests"] = rows

        total_used = sum(x["used"] for x in out["daily"])
        total_size = sum(x["size"] for x in out["daily"])
        out["totals"] = {
            "used": total_used, "size": total_size,
            "sessions": sum(x["sessions"] for x in out["daily"]),
            "models": len(models),
            "pct": round(total_used / total_size * 100, 1) if total_size else 0,
        }
        con.close()
    except Exception as e:
        out["error"] = repr(e)
    return out


def credits_overview():
    """积分总体汇总：今日/近7天/本月/近30天消耗 + 每账号构成。

    源：session_usage.credit_json（本机真实用量，按 updated_at 落本地日期）+ 余额 API。
    与官方「账号消耗构成」同口径：周期切换、每账号积分 + 占比条。
    不联网、不编数字；无库/无用量时返回零值与 has_data=False。
    """
    out = {"ok": True, "has_data": False, "source": "本机 session_usage.credit_json",
           "today": 0.0, "week7": 0.0, "month30": 0.0, "month": 0.0,
           "remaining": 0.0, "unlimited": False, "accounts": [], "updated": None}
    if not os.path.isfile(WB_DB):
        out["error"] = "未找到 workbuddy.db"
        return out
    now = datetime.datetime.now()
    today_s = now.strftime("%Y-%m-%d")
    month_s = now.strftime("%Y-%m")
    d30 = (now - datetime.timedelta(days=29)).strftime("%Y-%m-%d")
    d7 = (now - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    # uid -> {day: amount}
    per = {}
    try:
        con = _db_readonly()
        for r in con.execute(
                "SELECT s.user_id uid, u.credit_json cj, u.updated_at ts "
                "FROM session_usage u LEFT JOIN sessions s ON s.id = u.session_id "
                "WHERE u.credit_json IS NOT NULL"):
            ts = r["ts"]
            if not ts:
                continue
            try:
                day = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            except Exception:
                continue
            try:
                amt = sum(float(v) for v in json.loads(r["cj"]).values()
                          if isinstance(v, (int, float)))
            except Exception:
                continue
            if amt <= 0:
                continue
            uid = r["uid"] or ""
            per.setdefault(uid, {})
            per[uid][day] = round(per[uid].get(day, 0.0) + amt, 2)
        con.close()
    except Exception as e:
        out["error"] = repr(e)
        return out
    # 账号显示名：优先账号库 nickname，回退 uid
    try:
        names = {a.get("uid"): a.get("nickname") or (a.get("uid") or "")[:10]
                 for a in load_accounts()}
    except Exception:
        names = {}
    # 汇总周期
    def _sum(days_from):
        return round(sum(v for days in per.values()
                         for dd, v in days.items() if dd >= days_from), 2)
    out["today"] = _sum(today_s)
    out["week7"] = _sum(d7)
    out["month30"] = _sum(d30)
    out["month"] = round(sum(v for days in per.values()
                             for dd, v in days.items() if dd.startswith(month_s)), 2)
    out["has_data"] = bool(per)
    out["updated"] = now.strftime("%Y-%m-%d %H:%M")
    # 余额：走 5min 缓存，企业不限量单列
    try:
        bals = fetch_all_credits()
        rem = 0.0
        for b in bals.values():
            if b.get("unlimited"):
                out["unlimited"] = True
            elif b.get("ok"):
                rem += b.get("remaining") or 0.0
        out["remaining"] = round(rem, 2)
    except Exception:
        pass
    # 每账号构成（默认按近 7 天排序，前端可切周期本地重排）
    rows = []
    for uid, days in per.items():
        rows.append({
            "key": uid, "name": names.get(uid, uid[:10] or "未知账号"),
            "today": round(sum(v for dd, v in days.items() if dd >= today_s), 2),
            "week7": round(sum(v for dd, v in days.items() if dd >= d7), 2),
            "month30": round(sum(v for dd, v in days.items() if dd >= d30), 2),
            "month": round(sum(v for dd, v in days.items() if dd.startswith(month_s)), 2),
        })
    rows.sort(key=lambda x: x["week7"], reverse=True)
    out["accounts"] = rows
    return out


def sessions_page(account_key="", page=1, size=50, query=""):
    """会话分页查询（服务端分页 + 关键词）。"""
    page = max(1, int(page or 1))
    size = max(5, min(200, int(size or 50)))
    where = ["deleted_at IS NULL"]
    args = []
    if account_key:
        where.append("user_id = ?")
        args.append(account_key)
    if query:
        where.append("(COALESCE(custom_title,title) LIKE ? OR COALESCE(model,'') LIKE ?)")
        args.extend(["%" + query + "%", "%" + query + "%"])
    clause = " AND ".join(where)
    out = {"page": page, "size": size, "total": 0, "rows": []}
    if not os.path.isfile(WB_DB):
        out["error"] = "未找到 workbuddy.db"
        return out
    try:
        con = _db_readonly()
        cur = con.cursor()
        out["total"] = cur.execute("SELECT COUNT(*) c FROM sessions WHERE " + clause,
                                   args).fetchone()["c"]
        rows = cur.execute(
            "SELECT id, user_id uid, COALESCE(custom_title,title) title, COALESCE(model,'-') model, "
            "status, created_at, updated_at FROM sessions WHERE " + clause +
            " ORDER BY COALESCE(updated_at,created_at) DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
        out["rows"] = [dict(r) for r in rows]
        out["pages"] = max(1, (out["total"] + size - 1) // size)
        con.close()
    except Exception as e:
        out["error"] = repr(e)
    return out


def _count_lines(path):
    if not path or not os.path.isfile(path):
        return 0
    n = 0
    try:
        with open(path, "rb") as f:
            for _ in f:
                n += 1
    except OSError:
        return 0
    return n


def detect_divergence():
    """关联会话分歧检测：源/目标都在基线之后新增 → diverged。"""
    store = load_links()
    accs = {a.get("uid"): a for a in load_accounts()}

    def nick(uid):
        return (accs.get(uid) or {}).get("nickname") or mask(uid or "")

    rows = []
    for g in store.get("groups", []):
        members = [m for m in g.get("members", []) if m.get("state", "active") == "active"]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if not a.get("session_id") or not b.get("session_id"):
                    continue
                base = (g.get("pair_bases") or {}).get(
                    _pair_key(a.get("uid"), a.get("session_id"),
                              b.get("uid"), b.get("session_id")))
                if base is None:
                    base = (g.get("pair_bases") or {}).get(
                        _pair_key(b.get("uid"), b.get("session_id"),
                                  a.get("uid"), a.get("session_id")))
                a_count = _count_lines(_find_jsonl(a.get("session_id")))
                b_count = _count_lines(_find_jsonl(b.get("session_id")))
                base_count = int((base or {}).get("record_count") or 0)
                if base_count <= 0:
                    base_count = min(a_count, b_count)
                a_new = max(0, a_count - base_count)
                b_new = max(0, b_count - base_count)
                if a_new > 0 and b_new > 0:
                    state = "diverged"
                elif a_new > 0:
                    state = "a_ahead"
                elif b_new > 0:
                    state = "b_ahead"
                else:
                    state = "in_sync"
                rows.append({
                    "groupId": g.get("id"),
                    "aUid": a.get("uid"), "aSession": a.get("session_id"),
                    "aNick": nick(a.get("uid")), "aCount": a_count, "aNew": a_new,
                    "bUid": b.get("uid"), "bSession": b.get("session_id"),
                    "bNick": nick(b.get("uid")), "bCount": b_count, "bNew": b_new,
                    "base": base_count, "state": state,
                    "syncedAt": (base or {}).get("synced_at"),
                })
    rows.sort(key=lambda x: ({"diverged": 0, "a_ahead": 1, "b_ahead": 2}.get(x["state"], 3), x["aCount"] + x["bCount"]), reverse=False)
    return {"pairs": rows,
            "diverged": len([r for r in rows if r["state"] == "diverged"]),
            "ahead": len([r for r in rows if r["state"] in ("a_ahead", "b_ahead")])}


def resolve_divergence(group_id, a_uid, a_session, b_uid, b_session, action):
    """处理分歧：keep_a（以源为准重置基线）/ keep_b（以目标为准）/ overwrite（真覆盖，危险）。

    - keep_a / keep_b：只把基线更新为对应一侧的当前行数，**不动任何会话正文**；
    - overwrite：把源全文覆盖到目标（先备份目标），并重建基线。
    """
    store = load_links()
    g = next((x for x in store.get("groups", []) if x.get("id") == group_id), None)
    if not g:
        return {"ok": False, "error": "关联组不存在"}
    key_ab = _pair_key(a_uid, a_session, b_uid, b_session)
    key_ba = _pair_key(b_uid, b_session, a_uid, a_session)
    base = (g.get("pair_bases") or {}).get(key_ab) or (g.get("pair_bases") or {}).get(key_ba)
    if base is None:
        base = {"record_count": 0, "total_digest": "", "synced_at": 0}
    a_path, b_path = _find_jsonl(a_session), _find_jsonl(b_session)
    now = int(datetime.datetime.now().timestamp() * 1000)

    if action == "keep_a":
        n = _count_lines(a_path)
        base["record_count"] = n
        base["synced_at"] = now
    elif action == "keep_b":
        n = _count_lines(b_path)
        base["record_count"] = n
        base["synced_at"] = now
    elif action == "overwrite":
        if not a_path or not b_path:
            return {"ok": False, "error": "源或目标正文不存在"}
        bak = b_path + ".bak-%d" % now
        try:
            shutil.copy2(b_path, bak)
            a_text = open(a_path, encoding="utf-8", errors="replace").read()
            b_text, _ = rewrite_identity(a_text, b_session)
            resid = residual_identities(b_text, b_session)
            if resid:
                return {"ok": False, "error": "覆盖后仍残留非本副本身份（%s）" % ", ".join(resid[:2])}
            with open(b_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(b_text)
        except Exception as e:
            return {"ok": False, "error": "覆盖失败：%s" % e}
        base["record_count"] = _count_lines(a_path)
        base["synced_at"] = now
        base["overwritten_from"] = a_session
        log("分歧覆盖：%s → %s（备份 %s）" % (mask(a_session), mask(b_session), os.path.basename(bak)))
    else:
        return {"ok": False, "error": "未知动作：%s" % action}

    g.setdefault("pair_bases", {})[key_ab] = base
    save_links(store)
    return {"ok": True, "action": action, "base": base.get("record_count")}



# ---------------- 签到（反推自官方 checkin.rs / refresh.rs，机制 1:1） ----------------
# 国内版才有签到（supports_checkin）；国际版不发任何请求，直接 skipped。
# POST {endpoint}{billing_prefix}/daily-checkin  body={}
# 状态:  cn 先 /checkin-activity-status 再 /checkin-status 回落
# 信封 token（$wbEncrypted 开头）不能调 API，直接短路给可读错误（官方 issue #94 同款）
# 401 时若有 refresh_token 则刷新一次重试: POST {endpoint}{api_prefix}/auth/token/refresh
#      头必须带 X-Refresh-Token + X-Auth-Refresh-Source: plugin（缺了国际版会 invalid_grant）

API_ENDPOINT = {"cn": "https://www.codebuddy.cn", "ai": "https://www.workbuddy.ai"}
API_PREFIX = {"cn": "/v2/plugin", "ai": "/v2/plugin"}
BILLING_PREFIX = {"cn": "/v2/billing/meter", "ai": "/billing/meter"}
CHECKIN_LOGF = os.path.join(HOME, ".wb-switch", "memshift_checkin_log.json")
CHECKIN_LOG_MAX = 500


def _is_envelope(acc):
    at = acc.get("access_token")
    if isinstance(at, str):
        return at.startswith("$wbEncrypted")
    if isinstance(at, dict):  # 信封对象形态
        return True
    return False


def _api_headers(acc):
    h = {"Authorization": "Bearer %s" % (acc.get("access_token") or ""),
         "Accept": "application/json", "Content-Type": "application/json",
         # 官方 reqwest 客户端带 Chrome UA（config.rs DEFAULT_HTTP_USER_AGENT）；
         # 缺 UA 会被网关 403（实测 get-user-resource-summary）
         "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"}
    if acc.get("uid"):
        h["X-User-Id"] = acc["uid"]
    eid = acc.get("enterpriseId")
    if eid:
        h["X-Enterprise-Id"] = str(eid)
        h["X-Tenant-Id"] = str(eid)
    if acc.get("domain"):
        h["X-Domain"] = acc["domain"]
    return h


def _api_post(acc, path, extra_headers=None, timeout=20):
    import urllib.request as _u
    url = API_ENDPOINT.get(acc.get("variant", "cn")) + path
    h = _api_headers(acc)
    if extra_headers:
        h.update(extra_headers)
    req = _u.Request(url, data=json.dumps({}).encode(), headers=h, method="POST")
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body), r.status
            except Exception:
                return {"code": -1, "message": "响应非 JSON: %s" % body[:120]}, r.status
    except _u.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace")), e.code
        except Exception:
            return {"code": e.code, "message": "HTTP %d" % e.code}, e.code
    except Exception as e:
        return {"code": -1, "message": repr(e)}, 0


def _billing_post(acc, suffix):
    """按档位生成路径候选，只有 404 才回落（官方 checkin_request 同款语义）。"""
    v = acc.get("variant", "cn")
    if v == "ai":
        paths = [BILLING_PREFIX["ai"] + suffix, "/v2/billing/meter" + suffix]
    else:
        paths = [BILLING_PREFIX["cn"] + suffix]
    last = {"code": -1, "message": "无可用签到路径"}
    for i, p in enumerate(paths):
        resp, status = _api_post(acc, p)
        last = resp
        if i + 1 == len(paths) or status != 404:
            break
    return last


def _refresh_token(acc):
    """POST /v2/plugin/auth/token/refresh；成功落盘 accounts.json 并返回新 acc。"""
    rt = acc.get("refresh_token") or ""
    if not rt:
        return None, "缺少 refresh token"
    v = acc.get("variant", "cn")
    extra = {"X-Refresh-Token": rt, "X-Auth-Refresh-Source": "plugin"}
    resp, _ = _api_post(acc, API_PREFIX[v] + "/auth/token/refresh", extra_headers=extra)
    code = resp.get("code", -1)
    if code not in (0, 200):
        return None, resp.get("message") or resp.get("msg") or ("code=%s" % code)
    data = resp.get("data") or {}
    new_at = data.get("accessToken") or data.get("access_token")
    if not new_at:
        return None, "刷新响应缺少 accessToken"
    acc = dict(acc)
    acc["access_token"] = new_at
    if data.get("refreshToken") or data.get("refresh_token"):
        acc["refresh_token"] = data.get("refreshToken") or data.get("refresh_token")
    exp = data.get("expiresAt") or data.get("expires_at")
    ins = data.get("expiresIn")
    if exp:
        acc["expiresAt"] = exp if isinstance(exp, (int, float)) else exp
    elif isinstance(ins, (int, float)):
        acc["expiresAt"] = int(datetime.datetime.now().timestamp() * 1000 + ins * 1000)
    _upsert_account(acc)
    log("token 已刷新: %s" % acc.get("nickname"))
    return acc, None


def _upsert_account(acc):
    """按 id 回写 accounts.json（与官方工具同库，原子写）。"""
    try:
        with open(ACCOUNTS_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        accs = raw if isinstance(raw, list) else raw.get("accounts", [])
        for i, a in enumerate(accs):
            if a.get("id") == acc.get("id"):
                accs[i] = acc
                break
        else:
            accs.append(acc)
        _atomic_write(ACCOUNTS_JSON, json.dumps(raw, ensure_ascii=False, indent=2))
    except Exception as e:
        log("accounts.json 回写失败: %r" % e)


def _checkin_auto_loop():
    """自动签到线程：每 10 分钟巡一次，只对今天还没成功的国内版账号提交。"""
    while True:
        try:
            cfg = load_cfg()
            if cfg.get("checkin_auto") and _in_window(cfg):
                for a in load_accounts():
                    if a.get("variant", "cn") != "cn":
                        continue
                    r0, _ = checkin_today_state(a.get("id"))
                    if r0 in ("ok", "already"):
                        continue
                    checkin_account(a.get("id"))
        except Exception as e:
            log("自动签到异常: %r" % e)
        threading.Event().wait(600)


CHECKIN_THREAD_STARTED = [False]


def ensure_checkin_thread():
    if not CHECKIN_THREAD_STARTED[0]:
        t = threading.Thread(target=_checkin_auto_loop, daemon=True)
        t.start()
        CHECKIN_THREAD_STARTED[0] = True
        log("自动签到线程已启动")


def _checkin_log():
    try:
        return json.load(open(CHECKIN_LOGF, encoding="utf-8"))
    except Exception:
        return []


def _checkin_log_append(entry):
    logs = _checkin_log()
    logs.append(entry)
    _atomic_write(CHECKIN_LOGF, json.dumps(logs[-CHECKIN_LOG_MAX:], ensure_ascii=False, indent=2))


def _today_str():
    return datetime.date.today().isoformat()


def checkin_today_state(acc_id):
    """从本地签到日志读今天该账号状态: ok/already/None/error。"""
    for e in _checkin_log():
        if e.get("accountId") == acc_id and e.get("day") == _today_str():
            return e.get("result"), e
    return None, None


def checkin_status(acc):
    """查签到状态（cn 专用）：todayCheckedIn / 不支持时返回 statusUnsupported。"""
    if acc.get("variant", "cn") != "cn":
        return {"ok": False, "statusUnsupported": True}
    for suffix in ("/checkin-activity-status", "/checkin-status"):
        resp = _billing_post(acc, suffix)
        code = resp.get("code", -1)
        if code in (0, 200):
            data = resp.get("data") or {}
            return {"ok": True, "todayCheckedIn": bool(data.get("todayCheckedIn"))}
        if resp.get("message") or resp.get("msg"):
            last_msg = resp.get("message") or resp.get("msg")
        else:
            last_msg = "code=%s" % code
    return {"ok": False, "error": last_msg}


def checkin_account(acc_id, force=False):
    """完整签到流程：信封短路 → 查状态 → 未签则提交 →（401 刷新重试）→ 写日志。"""
    acc = next((a for a in load_accounts() if a.get("id") == acc_id), None)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    entry = {"ts": int(datetime.datetime.now().timestamp() * 1000), "day": _today_str(),
             "accountId": acc_id, "name": acc.get("nickname"), "variant": acc.get("variant", "cn")}
    if acc.get("variant", "cn") != "cn":
        entry["result"] = "skipped"
        entry["error"] = "国际版无签到接口"
        _checkin_log_append(entry)
        return {"ok": False, "skipped": True, "error": "国际版没有签到接口"}
    if _is_envelope(acc):
        entry["result"] = "error"
        entry["error"] = "加密信封凭据，无法调用签到接口（需重新登录换取明文 token）"
        _checkin_log_append(entry)
        return {"ok": False, "error": entry["error"]}
    if not force:
        st = checkin_status(acc)
        if st.get("ok") and st.get("todayCheckedIn"):
            entry["result"] = "already"
            _checkin_log_append(entry)
            return {"ok": True, "already": True, "message": "今日已签到"}
        if st.get("ok") is False and not st.get("statusUnsupported"):
            # 状态查询失败也允许直接提交（daily-checkin 幂等，官方同款防御）
            pass
    resp = _billing_post(acc, "/daily-checkin")
    code = resp.get("code", -1)
    if code in (401, 403):
        acc2, err = _refresh_token(acc)
        if acc2:
            resp = _billing_post(acc2, "/daily-checkin")
            code = resp.get("code", -1)
        else:
            entry["result"] = "error"
            entry["error"] = "token 失效且刷新失败: %s" % err
            _checkin_log_append(entry)
            return {"ok": False, "error": entry["error"]}
    msg = (resp.get("message") or resp.get("msg") or ("code=%s" % code))
    if code in (0, 200):
        entry["result"] = "ok"
        _checkin_log_append(entry)
        return {"ok": True, "message": "签到成功", "raw": resp.get("data") or {}}
    if "已签到" in str(msg) or "repeat" in str(msg).lower():
        entry["result"] = "already"
        _checkin_log_append(entry)
        return {"ok": True, "already": True, "message": msg}
    entry["result"] = "error"
    entry["error"] = str(msg)
    _checkin_log_append(entry)
    return {"ok": False, "error": str(msg)}


def checkin_all(force=False):
    out = []
    for a in load_accounts():
        if a.get("variant", "cn") != "cn":
            continue
        r = checkin_account(a.get("id"), force=force)
        r["name"] = a.get("nickname")
        out.append(r)
    log("一键签到: %s" % json.dumps(out, ensure_ascii=False)[:300])
    return out


def _in_window(cfg):
    s, e = str(cfg.get("checkin_start") or ""), str(cfg.get("checkin_end") or "")
    if not s or not e:
        return True
    now = datetime.datetime.now().strftime("%H:%M")
    return s <= now <= e


# ---------------- OAuth 扫码添加（1:1 复刻官方 workbuddy-switch oauth.rs）----------------
# 官方源码: crates/wb-switch-core/src/modules/oauth.rs
#   POST {endpoint}/v2/plugin/auth/state?platform={workbuddy|workbuddy-ai}  (无 token)
#   GET  {endpoint}/v2/plugin/auth/token?state={state}                      (无 token)
#   GET  {endpoint}/v2/plugin/login/account?state={state}  + Bearer token + X-Domain
# 无 client_id / app_id —— state 由官方下发，二维码内容 = 返回的 authUrl。
OAUTH_PLATFORM = {"cn": "workbuddy", "ai": "workbuddy-ai"}
OAUTH_TIMEOUT = 300  # 秒，官方 OAUTH_TIMEOUT_SECONDS
_OAUTH_STATES = {}   # login_id -> {state, variant, expires_at, done, result, error}


def _oauth_http(url, method="GET", body=None, headers=None, timeout=20):
    """OAuth 专用裸请求（不带账号 token，官方 http_request 等价）。"""
    import urllib.request as _u
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = _u.Request(url, data=data, headers=h, method=method)
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                return json.loads(txt)
            except Exception:
                return {"code": -1, "message": "非 JSON: %s" % txt[:160]}
    except _u.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": e.code, "message": "HTTP %d" % e.code}
    except Exception as e:
        return {"code": -1, "message": repr(e)}


def _oauth_domain_ok(variant, domain):
    """档位一致性校验：AI 必须 .workbuddy.ai；CN 不校验（官方 matches_domain 行为）。"""
    if variant == "ai":
        return (domain or "").endswith(".workbuddy.ai") or (domain or "") == "workbuddy.ai"
    return True


def oauth_start(variant="cn"):
    """发起扫码：返回 loginId / verificationUri(authUrl) / expiresIn。"""
    import uuid as _uuid, time as _time
    variant = variant if variant in API_ENDPOINT else "cn"
    login_id = "wb_%s" % _uuid.uuid4().hex
    url = "%s%s/auth/state?platform=%s" % (
        API_ENDPOINT[variant], API_PREFIX[variant], OAUTH_PLATFORM[variant])
    resp = _oauth_http(url, "POST", {})
    data = resp.get("data") or {}
    state = data.get("state") or ""
    if not state:
        return {"error": "auth/state 未返回 state: %s" % json.dumps(resp, ensure_ascii=False)[:200]}
    auth_url = (data.get("authUrl") or data.get("auth_url") or data.get("url")
                or ("%s/login?state=%s" % (API_ENDPOINT[variant], state)))
    _OAUTH_STATES[login_id] = {"state": state, "variant": variant,
                               "expires_at": _time.time() + OAUTH_TIMEOUT,
                               "done": False, "result": None, "error": None}
    log("OAuth 发起: %s (%s)" % (login_id, variant))
    return {"loginId": login_id, "verificationUri": auth_url, "expiresIn": OAUTH_TIMEOUT}


def oauth_poll(login_id):
    """轮询扫码结果：未扫={'done':False}；成功={'done':True,'result':账号}。"""
    import uuid as _uuid, time as _time
    info = _OAUTH_STATES.get(login_id)
    if not info:
        return {"done": True, "error": "登录请求不存在"}
    if info["done"]:
        return {"done": True, "result": info.get("result"), "error": info.get("error")}
    if _time.time() > info["expires_at"]:
        info["done"] = True
        info["error"] = "登录超时（5 分钟）"
        return {"done": True, "error": info["error"]}
    variant, state = info["variant"], info["state"]
    tok_url = "%s%s/auth/token?state=%s" % (API_ENDPOINT[variant], API_PREFIX[variant], state)
    resp = _oauth_http(tok_url, "GET")
    code = resp.get("code", -1)
    if code not in (0, 200):
        return {"done": False}
    data = resp.get("data") or {}
    access_token = data.get("accessToken") or data.get("access_token") or ""
    if not access_token:
        return {"done": False}
    domain = data.get("domain") or ""
    if not _oauth_domain_ok(variant, domain):
        info["done"] = True
        info["error"] = "登录响应 domain（%s）与 %s 档位不符，已拒绝入库" % (
            domain, "国际版" if variant == "ai" else "国内版")
        return {"done": True, "error": info["error"]}
    # 拉账号信息
    acc_url = "%s%s/login/account?state=%s" % (API_ENDPOINT[variant], API_PREFIX[variant], state)
    hdr = {"Authorization": "Bearer %s" % access_token}
    if domain:
        hdr["X-Domain"] = domain
    acc_resp = _oauth_http(acc_url, "GET", headers=hdr)
    acc_data = acc_resp.get("data") or {}
    if not _oauth_domain_ok(variant, acc_data.get("domain") or ""):
        info["done"] = True
        info["error"] = "账号信息 domain 与档位不符，已拒绝入库"
        return {"done": True, "error": info["error"]}
    acc = {
        "id": str(_uuid.uuid4()),
        "uid": acc_data.get("uid"),
        "nickname": acc_data.get("nickname"),
        "email": (acc_data.get("email") or "").strip() or None,
        "enterpriseName": acc_data.get("enterpriseName"),
        "enterpriseId": acc_data.get("enterpriseId"),
        "access_token": access_token,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token"),
        "token_type": data.get("tokenType") or data.get("token_type") or "Bearer",
        "domain": domain,
        "variant": variant,
        "expiresAt": data.get("expiresAt") or data.get("expires_at"),
        "refreshExpiresAt": data.get("refreshExpiresAt") or data.get("refresh_expires_at"),
        "auth_raw": data, "profile_raw": acc_data,
        "createdAt": int(_time.time() * 1000),
    }
    try:
        with open(ACCOUNTS_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        is_dict = isinstance(raw, dict)
        accs = raw.get("accounts", []) if is_dict else raw
        # 同一 uid 已存在则更新，不重复入库
        for i, a in enumerate(accs):
            if a.get("uid") and a.get("uid") == acc.get("uid"):
                accs[i] = acc
                break
        else:
            accs.append(acc)
        out = {"accounts": accs} if is_dict else accs
        _atomic_write(ACCOUNTS_JSON, json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        info["done"] = True
        info["error"] = "保存账号失败: %r" % e
        return {"done": True, "error": info["error"]}
    meta = account_brief(acc)
    info["done"] = True
    info["result"] = meta
    log("OAuth 扫码成功: %s (%s)" % (meta.get("nickname"), meta.get("uid")))
    return {"done": True, "result": meta}


# ---------------- 积分余额（反推自官方 credits.rs：summary/enterprise/legacy 三路） ----------------
# 个人版: POST {billing}/get-user-resource-summary body={} → data.Packages[]（6 种嵌套形态）
# 企业版: POST /v2/billing/meter/get-enterprise-user-usage body={} → limitNum/credit(-1=不限量)
# 旧接口回退: POST {billing}/get-user-resource（分页+ProductCode p_tcaca）
# 401 刷新一次重试；信封短路同签到。包字段兼容 camel/snake 双命名。

CREDITS_TTL = 300  # 秒；余额查询有真实 API 成本，5 分钟缓存
CREDITS_CACHE = {}  # id -> (ts, result)


def _gval(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _gnum(d, keys):
    v = _gval(d, keys)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except Exception:
            return None
    return None


def _gts(d, keys):
    v = _gval(d, keys)
    if isinstance(v, (int, float)) and v > 10 ** 12:  # ms 级
        return float(v)
    if isinstance(v, (int, float)):
        return float(v) * 1000
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(v[:19], fmt).timestamp() * 1000
            except Exception:
                pass
    return None


def _packages_of(resp):
    if not isinstance(resp, dict):
        return []
    for path in (("data", "Packages"), ("data", "data", "Packages"),
                 ("data", "Response", "Data", "Packages"),
                 ("data", "data", "Response", "Data", "Packages"),
                 ("data", "packages"), ("data", "data", "packages")):
        cur = resp
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            return cur
    return []


NOW_MS = lambda: int(datetime.datetime.now().timestamp() * 1000)


def _parse_package(raw, now):
    slice0 = None
    spd = _gval(raw, ["SlicePeriodUsageDetails", "slicePeriodUsageDetails"])
    if isinstance(spd, list) and spd:
        slice0 = spd[0]
    T = ["CycleCapacitySizePrecise", "CycleCapacitySize", "CycleTotalCapacity",
         "CapacitySizePrecise", "CapacitySize", "SlicePeriodCapacitySizePrecise",
         "SlicePeriodCapacitySize"]
    R = ["CycleCapacityRemainPrecise", "CycleCapacityRemain", "CycleRemainCapacity",
         "CapacityRemainPrecise", "CapacityRemain", "SlicePeriodCapacityRemainPrecise",
         "SlicePeriodCapacityRemain"]
    U = ["CycleCapacityUsedPrecise", "CycleCapacityUsed", "CycleUsedCapacity",
         "CapacityUsedPrecise", "CapacityUsed", "SlicePeriodCapacityUsedPrecise",
         "SlicePeriodCapacityUsed"]
    rt = _gnum(raw, T) or (slice0 and _gnum(slice0, T))
    rr = _gnum(raw, R) or (slice0 and _gnum(slice0, R))
    ru = _gnum(raw, U) or (slice0 and _gnum(slice0, U))
    total = rt if rt is not None else (
        (rr + ru) if (rr is not None and ru is not None) else (rr or ru or 0.0))
    total = max(0.0, total)
    remaining = max(0.0, rr if rr is not None else max(0.0, total - (ru or 0.0)))
    used = max(0.0, ru if ru is not None else max(0.0, total - remaining))
    # 到期：DeductionEndTime/ExpiredTime 优先；CycleEnd 晚 365 天以上视为长期占位改用 Cycle
    ded = _gts(raw, ["DeductionEndTime", "deductionEndTime", "ExpiredTime", "expiredTime"])
    cyc = _gts(raw, ["CycleEndTime", "cycleEndTime"])
    override_ms = 365 * 24 * 3600 * 1000
    exp = (cyc if (ded is not None and cyc is not None and ded - cyc > override_ms)
           else (ded if ded is not None else cyc))
    if exp is not None and exp - now > 730 * 24 * 3600 * 1000:
        exp = None  # 2049 类占位 → 长期有效
    return {"packageName": _gval(raw, ["PackageName", "packageName"]) or "积分包",
            "packageCode": _gval(raw, ["PackageCode", "packageCode"]),
            "total": round(total, 2), "remaining": round(remaining, 2), "used": round(used, 2),
            "expireAt": exp,
            "expired": bool(exp and exp <= now),
            "expiringSoon": bool(exp and now < exp <= now + 7 * 24 * 3600 * 1000)}


def _api_resp_ok(resp):
    if not isinstance(resp, dict):
        return False
    code = resp.get("code")
    if isinstance(code, str) and code.strip().lstrip("-").isdigit():
        code = int(code)
    if code is None:
        code = (resp.get("data") or {}).get("code") if isinstance(resp.get("data"), dict) else None
    return code in (0, 200)


def fetch_account_credits(acc, refreshed=False):
    """单账号积分余额。返回 {ok, remaining, total, packages[], unlimited?, error?}"""
    now = NOW_MS()
    cached = CREDITS_CACHE.get(acc.get("id"))
    if cached and now - cached[0] < CREDITS_TTL * 1000:
        return cached[1]
    result = _fetch_credits_uncached(acc, now, refreshed)
    if result.get("ok"):
        CREDITS_CACHE[acc.get("id")] = (now, result)
    return result


def _fetch_credits_uncached(acc, now, refreshed):
    if _is_envelope(acc):
        return {"ok": False, "error": "加密信封凭据，无法查询积分（需重新登录换明文 token）"}
    # 企业版分流（issue #167）：有 enterpriseId 走企业配额接口
    eid = acc.get("enterpriseId")
    if eid:
        resp, status = _api_post(acc, "/v2/billing/meter/get-enterprise-user-usage")
        if status in (401, 403) and not refreshed:
            acc2, err = _refresh_token(acc)
            if acc2:
                return _fetch_credits_uncached(acc2, now, True)
            return {"ok": False, "error": "token 失效且刷新失败: %s" % err}
        if _api_resp_ok(resp):
            payload = None
            data = resp.get("data")
            for cand in ((data or {}).get("data") if isinstance(data, dict) else None,
                         data, resp):
                if isinstance(cand, dict) and _gnum(cand, ["limitNum", "limit_num"]) is not None:
                    payload = cand
                    break
            if payload is not None:
                limit = _gnum(payload, ["limitNum", "limit_num"])
                if limit == -1.0:
                    return {"ok": True, "unlimited": True, "remaining": None, "total": None,
                            "packages": [{"packageName": "企业配额（不限量）",
                                          "remaining": None, "total": None,
                                          "expireAt": _gts(payload, ["cycleResetTime", "CycleResetTime"]),
                                          "expired": False, "expiringSoon": False}]}
                used = (_gnum(payload, ["credit", "used_num"]) or 0.0)
                reset = _gts(payload, ["cycleResetTime", "CycleResetTime"])
                rem = max(0.0, limit - used)
                return {"ok": True, "remaining": round(rem, 2), "total": round(limit, 2),
                        "packages": [{"packageName": "企业配额", "remaining": round(rem, 2),
                                      "total": round(limit, 2), "expireAt": reset,
                                      "expired": False, "expiringSoon": False}]}
        return {"ok": False, "error": (resp.get("message") or resp.get("msg")
                                       or "企业配额查询失败（code=%s）" % resp.get("code", -1))}
    # 个人版：summary → 空则旧接口回退
    # 注意路径规则（官方 variant.rs billing_paths）：summary 是 /billing/meter/...（无 /v2），
    # 与签到 /v2/billing/meter/... 不同；国际版先 /billing/meter 404 才回落 /v2。
    v = acc.get("variant", "cn")
    if v == "ai":
        sum_paths = ["/billing/meter/get-user-resource-summary",
                     "/v2/billing/meter/get-user-resource-summary"]
    else:
        sum_paths = ["/billing/meter/get-user-resource-summary"]
    resp, status = {}, 0
    for i, p in enumerate(sum_paths):
        resp, status = _api_post(acc, p)
        if i + 1 == len(sum_paths) or status != 404:
            break
    if status in (401, 403) and not refreshed:
        acc2, err = _refresh_token(acc)
        if acc2:
            return _fetch_credits_uncached(acc2, now, True)
        return {"ok": False, "error": "token 失效且刷新失败: %s" % err}
    pkgs = _packages_of(resp) if _api_resp_ok(resp) else []
    if not pkgs:
        legacy_body = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
                       "Status": [0, 3],
                       "PackageEndTimeRangeBegin": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "PackageEndTimeRangeEnd": (datetime.datetime.now()
                                                  + datetime.timedelta(days=365 * 101)).strftime("%Y-%m-%d %H:%M:%S")}
        import urllib.request as _u
        url = API_ENDPOINT.get(v) + "/v2/billing/meter/get-user-resource"
        h = _api_headers(acc)
        req = _u.Request(url, data=json.dumps(legacy_body).encode(), headers=h, method="POST")
        try:
            with _u.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            pkgs = _packages_of(resp)
        except Exception as e:
            if not pkgs:
                return {"ok": False, "error": "积分查询失败: %r" % e}
    if not pkgs:
        return {"ok": False, "error": (resp.get("message") or resp.get("msg")
                                       or "积分查询失败（code=%s）" % resp.get("code", -1))}
    parsed = [_parse_package(p, now) for p in pkgs]
    remaining = round(sum(p["remaining"] for p in parsed), 2)
    total = round(sum(p["total"] for p in parsed), 2)
    exps = [p["expireAt"] for p in parsed if p["expireAt"] and not p["expired"]]
    return {"ok": True, "remaining": remaining, "total": total, "packages": parsed,
            "expiringSoon": any(p["expiringSoon"] for p in parsed),
            "expired": all(p["expired"] for p in parsed) if parsed else False,
            "soonestExpireAt": min(exps) if exps else None,
            "updatedAt": now}


def fetch_all_credits():
    out = {}
    for a in load_accounts():
        out[a.get("id")] = fetch_account_credits(a)
    return out


# ---------------- 官方功能一比一：删除/导入本机/导出导入账号库/CLI切换/自动轮换 ----------------
# 反推自官方 account.rs / export_import.rs / codebuddy_cli.rs / rotate.rs

def _secret_value(d, key):
    """官方 secret_value：字符串原样；$wbEncrypted 信封(dict)原样；空串/缺失=None。"""
    v = (d or {}).get(key)
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        return v
    return None


def _get_str(d, keys):
    """官方 get_str：只收非空字符串（信封不强转）。"""
    for k in keys:
        v = (d or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _load_store_raw():
    with open(ACCOUNTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def upsert_collected(acc):
    """官方 save_collected_account 口径：uid 优先去重；信封不覆盖未过期明文；保留本地 id/createdAt/variant。"""
    raw = _load_store_raw()
    is_dict = isinstance(raw, dict)
    accs = raw.get("accounts", []) if is_dict else raw
    uid = acc.get("uid")
    hit = next((a for a in accs if uid and a.get("uid") == uid), None)
    if hit is None and not uid and acc.get("email"):
        em = acc["email"].lower()
        hit = next((a for a in accs if (a.get("email") or "").lower() == em), None)
    if hit is not None:
        new_env = isinstance(acc.get("access_token"), dict)
        old_plain = not isinstance(hit.get("access_token"), dict)
        if new_env and old_plain:
            return hit  # 信封保护：不让加密信封覆盖明文
        acc["id"] = hit.get("id") or acc.get("id") or str(__import__("uuid").uuid4())
        acc.setdefault("createdAt", hit.get("createdAt"))
        if not acc.get("variant"):
            acc["variant"] = hit.get("variant", "cn")
        for k in ("note", "displayField"):
            if k not in acc and k in hit:
                acc[k] = hit[k]
        accs[accs.index(hit)] = acc
    else:
        if not acc.get("id"):
            acc["id"] = str(__import__("uuid").uuid4())
        accs.append(acc)
    out = {"accounts": accs} if is_dict else accs
    _atomic_write(ACCOUNTS_JSON, json.dumps(out, ensure_ascii=False, indent=2))
    return acc


def delete_account(acc_id):
    """官方 delete_account：仅从 accounts.json 按 id 移除记录，不动本机登录态文件。"""
    raw = _load_store_raw()
    is_dict = isinstance(raw, dict)
    accs = raw.get("accounts", []) if is_dict else raw
    before = len(accs)
    accs = [a for a in accs if a.get("id") != acc_id]
    if len(accs) == before:
        return {"error": "账号不存在"}
    out = {"accounts": accs} if is_dict else accs
    _atomic_write(ACCOUNTS_JSON, json.dumps(out, ensure_ascii=False, indent=2))
    log("账号已删除: %s" % mask(acc_id))
    return {"ok": True, "count": before - len(accs)}


def import_local_account(variant="cn"):
    """官方 import_local：从本机 WorkBuddy 登录态文件读当前账号入库（字段提取 1:1 auth_file.rs）。"""
    import uuid as _uuid
    variant = variant if variant in AUTH_FILE else "cn"
    path = AUTH_FILE[variant]
    if not os.path.isfile(path):
        return {"error": "未读取到本机 %s 登录文件" % ("国际版" if variant == "ai" else "国内版")}
    try:
        root = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return {"error": "登录文件解析失败: %r" % e}
    if not isinstance(root, dict):
        return {"error": "登录文件结构异常"}
    account = root.get("account") if isinstance(root.get("account"), dict) else {}
    auth = root.get("auth") if isinstance(root.get("auth"), dict) else {}
    access_token = (_secret_value(auth, "accessToken") or _secret_value(auth, "access_token")
                    or _secret_value(root, "accessToken") or _secret_value(root, "access_token"))
    if access_token is None:
        return {"error": "登录文件无 accessToken（空串/缺失不导入，官方守卫）"}
    ent_keys = (("enterpriseName", "enterprise_name"), ("enterpriseId", "enterprise_id"))
    ent = {}
    for ck, sk in ent_keys:
        ent[ck] = (_get_str(root, [ck, sk]) or _get_str(account, [ck, sk]))
    acc = {
        "id": str(_uuid.uuid4()),
        "uid": root.get("uid") or account.get("uid") or account.get("id"),
        "nickname": _get_str(root, ["nickname", "name"]) or _get_str(account, ["nickname", "label"]),
        "email": _get_str(root, ["email"]) or _get_str(account, ["email"]) or _get_str(auth, ["email"]),
        "enterpriseName": ent["enterpriseName"], "enterpriseId": ent["enterpriseId"],
        "access_token": access_token,
        "refresh_token": (_secret_value(auth, "refreshToken") or _secret_value(auth, "refresh_token")
                          or _secret_value(root, "refreshToken") or _secret_value(root, "refresh_token")),
        "token_type": _get_str(auth, ["tokenType", "token_type"]) or "Bearer",
        "domain": root.get("domain") or auth.get("domain"),
        "variant": variant,
        "expiresAt": root.get("expiresAt") or root.get("expires_at") or auth.get("expiresAt") or auth.get("expires_at"),
        "refreshExpiresAt": root.get("refreshExpiresAt") or auth.get("refreshExpiresAt"),
        "auth_raw": root, "profile_raw": account,
        "createdAt": int(datetime.datetime.now().timestamp() * 1000),
    }
    saved = upsert_collected(acc)
    log("导入本机登录态: %s (%s)" % (saved.get("nickname"), variant))
    return {"ok": True, "account": account_brief(saved), "envelope": isinstance(saved.get("access_token"), dict)}


def export_accounts_file(ids):
    """官方 export_accounts：完整记录含 token 不脱敏，wb-switch-accounts-YYYY-MM-DD.json，pretty 数组。"""
    accs = load_accounts()
    if ids:
        want = set(ids)
        recs = [a for a in accs if a.get("id") in want]
    else:
        recs = list(accs)
    if not recs:
        return {"error": "未找到要导出的账号"}
    out_dir = os.path.join(os.path.dirname(BACKUP_ROOT), "wb_accounts_export")
    os.makedirs(out_dir, exist_ok=True)
    fname = "wb-switch-accounts-%s.json" % datetime.date.today().isoformat()
    path = os.path.join(out_dir, fname)
    _atomic_write(path, json.dumps(recs, ensure_ascii=False, indent=2))
    log("导出账号 %d 个 → %s" % (len(recs), path))
    return {"ok": True, "path": path, "count": len(recs)}


def import_accounts_text(text):
    """官方 import_accounts：数组校验→凭据判定→uid 去重覆盖→补 id→计数→原子写回。"""
    import uuid as _uuid
    if not text or not str(text).strip():
        return {"error": "文件内容为空"}
    try:
        items = json.loads(text)
    except Exception as e:
        return {"error": "文件不是合法的 JSON: %r" % e}
    if not isinstance(items, list):
        return {"error": "文件内容应为 JSON 数组（账号列表）"}
    raw = _load_store_raw()
    is_dict = isinstance(raw, dict)
    accs = raw.get("accounts", []) if is_dict else raw
    imported = overwritten = skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if _secret_value(item, "access_token") is None:
            skipped += 1  # 缺凭据/空串跳过；明文或信封均可入库
            continue
        uid = item.get("uid")
        hit = next((a for a in accs if uid and a.get("uid") == uid), None)
        if hit is not None:
            overwritten += 1
            imported += 1
            item["id"] = item.get("id") or hit.get("id") or str(_uuid.uuid4())
            for k in ("note", "displayField"):
                if k not in item and k in hit:
                    item[k] = hit[k]  # 备份未携带时保留本地值
            accs[accs.index(hit)] = item
        else:
            imported += 1
            if not item.get("id"):
                item["id"] = str(_uuid.uuid4())  # issue #111：缺 id 会被反复追加
            accs.append(item)
    out = {"accounts": accs} if is_dict else accs
    _atomic_write(ACCOUNTS_JSON, json.dumps(out, ensure_ascii=False, indent=2))
    log("导入账号库: 入库 %d / 覆盖 %d / 跳过 %d" % (imported, overwritten, skipped))
    return {"ok": True, "imported": imported, "overwritten": overwritten, "skipped": skipped}


def _clean_bearer(tok):
    t = (tok or "").strip()
    return t[7:].strip() if t.lower().startswith("bearer ") else t


def _cli_running():
    out, _ = subprocess.run('tasklist /FI "IMAGENAME eq codebuddy.exe" /NH', shell=True,
                            capture_output=True, text=True).stdout or "", 0
    return "codebuddy.exe" in (out or "")


def switch_cli_account(acc_id):
    """官方 codebuddy_cli.rs Windows 模式：写 settings.json env 三字段 + rotate state.json。"""
    acc = next((a for a in load_accounts() if a.get("id") == acc_id), None)
    if not acc:
        return {"error": "账号不存在"}
    at = acc.get("access_token")
    if isinstance(at, dict):
        return {"error": "该账号 token 为加密信封，无法写入 CLI（请用 OAuth 扫码获取明文 token）"}
    tok = _clean_bearer(at)
    if not tok:
        return {"error": "账号无明文 token"}
    running = _cli_running()
    settings_p = os.path.join(HOME, ".codebuddy", "settings.json")
    os.makedirs(os.path.dirname(settings_p), exist_ok=True)
    try:
        cfg = json.load(open(settings_p, encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    env = cfg.setdefault("env", {})
    env["CODEBUDDY_AUTH_TOKEN"] = tok
    if acc.get("variant") == "ai":
        env["CODEBUDDY_INTERNET_ENVIRONMENT"] = "public"
        env["CODEBUDDY_BASE_URL"] = "https://www.codebuddy.ai/v2"
    else:
        env["CODEBUDDY_INTERNET_ENVIRONMENT"] = "internal"
        env.pop("CODEBUDDY_BASE_URL", None)
    _atomic_write(settings_p, json.dumps(cfg, ensure_ascii=False, indent=2))
    rot_dir = os.path.join(HOME, ".codebuddy-rotate")
    os.makedirs(rot_dir, exist_ok=True)
    state_p = os.path.join(rot_dir, "state.json")
    try:
        state = json.load(open(state_p, encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    state["activeAccountId"] = acc.get("id")
    state["updatedAt"] = int(datetime.datetime.now().timestamp() * 1000)
    _atomic_write(state_p, json.dumps(state, ensure_ascii=False, indent=2))
    msg = "CLI 已切到「%s」" % (acc.get("nickname") or mask(acc.get("uid") or ""))
    if running:
        msg += "（CLI 正在运行：本次未杀进程，重启 CodeBuddy CLI 后生效）"
    log("CLI 账号切换: %s" % acc.get("nickname"))
    return {"ok": True, "message": msg, "cliRunning": running, "settings": settings_p}


def rotate_cli_account():
    """官方 rotate：把积分最紧迫（快到期优先→余额最少）的账号设为 CLI 启动账号。"""
    balances = fetch_all_credits()
    cands = []
    for a in load_accounts():
        b = balances.get(a.get("id"))
        if not b or not b.get("ok") or b.get("unlimited"):
            continue
        at = a.get("access_token")
        if isinstance(at, dict) or not (at or "").strip():
            continue
        cands.append({"id": a["id"], "nick": a.get("nickname") or "未名",
                      "remaining": b.get("remaining") or 0, "expiring": bool(b.get("expiringSoon"))})
    if not cands:
        return {"error": "没有可轮换账号（全部信封态/无余额数据）"}
    cands.sort(key=lambda x: ((not x["expiring"]), x["remaining"]))
    pick = cands[0]
    r = switch_cli_account(pick["id"])
    if r.get("error"):
        return r
    r["picked"] = pick
    r["ranked"] = [[c["nick"], c["remaining"], c["expiring"]] for c in cands[:5]]
    return r




APP_NAME = "忆迁 MemShift"
APP_VER = "v2.9.0"
FONT_DIR = os.environ.get("MEMSHIFT_FONT_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_fonts")
if not os.path.isdir(FONT_DIR) and hasattr(sys, "_MEIPASS"):
    FONT_DIR = os.path.join(sys._MEIPASS, "_fonts")

FONTS = {}
def _load_fonts():
    for name, fn in (("zh", "fusion-pixel-12px-proportional-zh_hans.otf.woff2"),
                     ("la", "fusion-pixel-12px-proportional-latin.otf.woff2")):
        p = os.path.join(FONT_DIR, fn)
        if os.path.isfile(p):
            FONTS[name] = open(p, "rb").read()
# ---------------- JetBrains 插件切换（1:1 官方 jetbrains.rs：secret-storage.xml upsert）----------------
JB_CONFIG_ROOT = os.path.join(os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming"), "JetBrains")
JB_PREFIXES = ("IntelliJIdea", "IdeaIC", "PyCharm", "PyCharmCE")
JB_SECRET_KEY = "Tencent-Cloud.coding-copilot.new.accessToken"
JB_STATE = os.path.join(HOME, ".wb-switch", "jetbrains.json")


def _jb_versioned(name):
    """目录名 = 前缀 + 数字版本号（如 IntelliJIdea2026.2），官方口径：前缀后必须跟数字。"""
    for p in JB_PREFIXES:
        if name.startswith(p):
            rest = name[len(p):]
            return bool(rest) and rest[0].isdigit()
    return False


def jb_list_targets():
    """扫描全部装有 CodeBuddy 插件（plugins/coding-copilot*）的 JetBrains 配置目录。"""
    targets = []
    if not os.path.isdir(JB_CONFIG_ROOT):
        return targets
    for name in sorted(os.listdir(JB_CONFIG_ROOT)):
        d = os.path.join(JB_CONFIG_ROOT, name)
        if not os.path.isdir(d) or not _jb_versioned(name):
            continue
        plugins = os.path.join(d, "plugins")
        has_plugin = False
        if os.path.isdir(plugins):
            has_plugin = any(p.startswith("coding-copilot") for p in os.listdir(plugins))
        if has_plugin:
            targets.append({"dirName": name, "path": d,
                            "xml": os.path.join(d, "options", "secret-storage.xml")})
    return targets


def _jb_running():
    out, _ = subprocess.run("tasklist /NH", shell=True, capture_output=True,
                            text=True, errors="replace").stdout or "", 0
    low = (out or "").lower()
    return [p for p in ("idea64.exe", "pycharm64.exe") if p in low]


def _build_ext_session_json(acc, existing=None):
    """官方 build_ext_session_json：merge 保留扩展私有键；accounts[] 按 uid upsert。"""
    import uuid as _uuid, time as _time
    uid = acc.get("uid") or ""
    tok = _clean_bearer(_secret_value(acc, "access_token") or "")
    rt = _clean_bearer(_secret_value(acc, "refresh_token") or "")
    domain = "www.codebuddy.cn" if acc.get("variant", "cn") == "cn" else "www.workbuddy.ai"
    now_ms = int(_time.time() * 1000)
    root = dict(existing) if isinstance(existing, dict) else {}
    root["id"] = "Tencent-Cloud.coding-copilot"
    root["accessToken"] = "%s+%s" % (uid, tok)
    root["token"] = tok
    if rt:
        root["refreshToken"] = rt
    root["expiresAt"] = acc.get("expiresAt") or now_ms + 30 * 86400000
    root["domain"] = domain
    acct = {"id": uid or acc.get("id"), "uid": uid, "label": acc.get("nickname"),
            "nickname": acc.get("nickname"), "enterpriseId": acc.get("enterpriseId"),
            "enterpriseName": acc.get("enterpriseName"), "pluginEnabled": True, "lastLogin": True}
    old_acct = root.get("account") if isinstance(root.get("account"), dict) else {}
    acct.update({k: old_acct[k] for k in old_acct if k not in acct})
    root["account"] = acct
    accs = root.get("accounts") if isinstance(root.get("accounts"), list) else []
    accs = [a for a in accs if isinstance(a, dict)]
    hit = next((a for a in accs if a.get("uid") == uid), None)
    if hit is not None:
        accs[accs.index(hit)] = acct
    else:
        accs.append(acct)
    for a in accs:
        if a.get("uid") != uid:
            a["lastLogin"] = False
    root["accounts"] = accs
    auth = dict(root.get("auth")) if isinstance(root.get("auth"), dict) else {}
    auth["accessToken"] = "%s+%s" % (uid, tok)
    auth["token"] = tok
    if rt:
        auth["refreshToken"] = rt
    auth["tokenType"] = acc.get("token_type") or "Bearer"
    auth["domain"] = domain
    auth["expiresAt"] = root["expiresAt"]
    auth["lastRefreshTime"] = now_ms
    root["auth"] = auth  # merge：scope/sessionState 等扩展私有键自动保留
    return root


def switch_jetbrains(acc_id):
    """官方 jetbrains.rs：遍历装有插件的 IDE → secret-storage.xml Entry upsert（写前 .wb-switch-bak）。"""
    import xml.etree.ElementTree as ET
    acc = next((a for a in load_accounts() if a.get("id") == acc_id), None)
    if not acc:
        return {"error": "账号不存在"}
    if isinstance(acc.get("access_token"), dict):
        return {"error": "该账号 token 为加密信封，无法写入插件（请 OAuth 扫码取明文）"}
    if not _clean_bearer(acc.get("access_token") or ""):
        return {"error": "账号无明文 token"}
    targets = jb_list_targets()
    if not targets:
        return {"error": "未检测到装有 CodeBuddy 插件的 JetBrains IDE（%s 下无匹配）" % JB_CONFIG_ROOT}
    running = _jb_running()
    existing_secret = None
    results = []
    for t in targets:
        xml_path = t["xml"]
        os.makedirs(os.path.dirname(xml_path), exist_ok=True)
        tree = None
        if os.path.isfile(xml_path):
            try:
                tree = ET.parse(xml_path)
            except Exception:
                tree = None
        if tree is None:
            root_el = ET.Element("application")
            comp = ET.SubElement(root_el, "component", {"name": "SecretStorage"})
            ET.SubElement(comp, "Scores")
        else:
            root_el = tree.getroot()
        comp = root_el.find("component[@name='SecretStorage']")
        if comp is None:
            comp = ET.SubElement(root_el, "component", {"name": "SecretStorage"})
        scores = comp.find("Scores")
        if scores is None:
            scores = ET.SubElement(comp, "Scores")
        entry = next((e for e in scores.findall("Entry") if e.get("key") == JB_SECRET_KEY), None)
        if entry is not None and os.path.isfile(xml_path):
            existing_secret = entry.get("value")
        session = _build_ext_session_json(acc,
                    json.loads(existing_secret) if existing_secret else None)
        if entry is None:
            entry = ET.SubElement(scores, "Entry", {"key": JB_SECRET_KEY})
        entry.set("value", json.dumps(session, ensure_ascii=False))
        # 写前备份（官方 .wb-switch-bak）
        if os.path.isfile(xml_path):
            bak = xml_path + ".wb-switch-bak"
            try:
                open(bak, "wb").write(open(xml_path, "rb").read())
            except Exception:
                pass
        ET.ElementTree(root_el).write(xml_path, encoding="UTF-8", xml_declaration=True)
        results.append({"ide": t["dirName"], "xml": xml_path, "ok": True})
    os.makedirs(os.path.dirname(JB_STATE), exist_ok=True)
    _atomic_write(JB_STATE, json.dumps(
        {"activeAccountId": acc.get("id"), "updatedAt": int(datetime.datetime.now().timestamp() * 1000),
         "targets": [r["ide"] for r in results]}, ensure_ascii=False, indent=2))
    msg = "JetBrains 已切到「%s」，写入 %d 个 IDE%s" % (
        acc.get("nickname") or "", len(results),
        ("（检测到 IDE 运行中 %s：本次未杀进程，重启 IDE 后插件读到新账号）" % ",".join(running)) if running else "")
    log("JetBrains 切换: %s → %s" % (acc.get("nickname"), ",".join(r["ide"] for r in results)))
    return {"ok": True, "message": msg, "results": results, "running": running}



_load_fonts()
# ---------------- webui ----------------

PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>忆迁 MemShift · 记忆移植工作台</title>
<style>
/* ============================================================
   忆迁 MemShift · Design System  v2.5
   设计 DNA：专业 / 精致 / 克制
   参考：Material 3 色彩与动效 · Apple HIG 空间与排版 · Fluent 深度
   8pt 网格 · 系统化圆角/阴影 · 内联 SVG 线性图标 · 有意义的微交互
   ============================================================ */
:root{
  /* 品牌色阶 */
  --brand:#5b8cff; --brand-2:#7ba2ff; --brand-3:#9ab8ff; --brand-deep:#3d6ae0;
  --brand-soft:rgba(91,140,255,.13); --brand-line:rgba(91,140,255,.30);
  /* 中性灰阶（表面层级） */
  --bg:#0e1117; --surface:#171c26; --surface-2:#1e2531; --surface-3:#28303f;
  --line:#2b3444; --line-soft:#222a38;
  --text:#eef2fa; --text-2:#a7b3c9; --text-3:#6d7a92;
  /* 语义色 */
  --ok:#37d39a; --ok-soft:rgba(55,211,154,.13);
  --warn:#f5b544; --warn-soft:rgba(245,181,68,.13);
  --danger:#ff6b81; --danger-soft:rgba(255,107,129,.13);
  /* 圆角系统 */
  --radius:14px; --radius-sm:10px; --radius-xs:7px; --radius-pill:999px;
  /* 阴影系统：克制，一两层精准阴影 */
  --shadow:0 1px 2px rgba(0,0,0,.20),0 8px 24px rgba(0,0,0,.26);
  --shadow-hover:0 2px 6px rgba(0,0,0,.24),0 16px 40px rgba(0,0,0,.34);
  --shadow-brand:0 6px 20px rgba(91,140,255,.26);
  /* 8pt 间距 */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px;
  --sp-6:24px; --sp-8:32px; --sp-10:40px; --sp-12:48px;
  /* 动效曲线 */
  --ease-spring:cubic-bezier(.34,1.56,.64,1);
  --ease-standard:cubic-bezier(.2,0,0,1);
  --ease-decel:cubic-bezier(0,0,.2,1);
  --dur-1:150ms; --dur-2:200ms; --dur-3:300ms;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.6;background:var(--bg);color:var(--text);
  display:flex;overflow:hidden;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:6px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--line);background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}
/* SVG 图标基础 */
svg.ic{width:18px;height:18px;flex:0 0 auto;stroke:currentColor;fill:none;stroke-width:1.8;
  stroke-linecap:round;stroke-linejoin:round;vertical-align:-3px;transition:transform var(--dur-1) var(--ease-standard)}
svg.ic.sm{width:15px;height:15px}
svg.ic.lg{width:22px;height:22px}

/* ============ 侧栏 ============ */
#side{width:244px;min-width:244px;background:linear-gradient(180deg,var(--surface) 0%,var(--bg) 100%);
  border-right:1px solid var(--line-soft);display:flex;flex-direction:column;
  padding:var(--sp-6) var(--sp-3) var(--sp-4);position:relative;z-index:2}
#logo{display:flex;align-items:center;gap:12px;padding:0 10px var(--sp-6);position:relative}
#logo:after{content:"";position:absolute;left:10px;right:10px;bottom:0;height:1px;
  background:linear-gradient(90deg,var(--brand-line),transparent)}
#logo .seal{width:42px;height:42px;border-radius:12px;flex:0 0 42px;
  background:linear-gradient(145deg,var(--brand-2),var(--brand-deep));
  display:flex;align-items:center;justify-content:center;color:#fff;
  box-shadow:0 6px 18px rgba(91,140,255,.34),inset 0 1px 0 rgba(255,255,255,.24)}
#logo .seal svg{width:24px;height:24px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#logo b{font-size:16px;color:var(--text);letter-spacing:.4px;font-weight:700;display:block;line-height:1.3}
#logo small{display:block;font-size:10.5px;color:var(--text-3);margin-top:3px;font-weight:500;letter-spacing:.2px}
.nav{margin-top:var(--sp-6);display:flex;flex-direction:column;gap:3px;position:relative}
.nav a{display:flex;align-items:center;gap:12px;padding:11px 13px;margin:0;color:var(--text-2);
  background:transparent;border-radius:var(--radius-sm);cursor:pointer;user-select:none;font-size:13.5px;
  font-weight:500;position:relative;transition:background var(--dur-1),color var(--dur-1),transform var(--dur-1)}
.nav a:hover{background:var(--surface-2);color:var(--text);transform:translateX(2px)}
.nav a.on{background:var(--brand-soft);color:var(--brand-2);font-weight:650}
.nav a.on:before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:3px;height:18px;border-radius:0 3px 3px 0;background:var(--brand);box-shadow:0 0 10px var(--brand)}
.nav a .ic{font-size:15px;width:18px;text-align:center;display:flex;align-items:center;justify-content:center;
  color:var(--text-3);transition:color var(--dur-1)}
.nav a.on .ic,.nav a:hover .ic{color:var(--brand-2)}
.nav a .ic svg{width:18px;height:18px}
#side .foot{margin-top:auto;font-size:10.5px;color:var(--text-3);line-height:1.85;
  padding:var(--sp-4) 10px 0;border-top:1px solid var(--line-soft)}
#side .foot b{color:var(--brand-2);font-weight:700;letter-spacing:.4px}
#side .foot .foot-brand{display:flex;align-items:center;gap:6px;color:var(--text-2);font-weight:600;margin-bottom:4px}

/* ============ 主区 ============ */
#main{flex:1;overflow-y:auto;padding:0 0 var(--sp-12);scroll-behavior:smooth}
#topbar{display:flex;align-items:center;gap:10px;
  background:rgba(23,28,38,.82);backdrop-filter:blur(16px) saturate(1.4);
  border-bottom:1px solid var(--line-soft);padding:var(--sp-4) var(--sp-8);
  position:sticky;top:0;z-index:10}
#topbar h2{font-size:19px;color:var(--text);margin:0;font-weight:700;letter-spacing:-.2px}
#topbar .sp{flex:1}
#topbar .btn:first-of-type{box-shadow:var(--shadow-brand)}
/* 顶栏图标按钮（刷新等无文字按钮） */
#topbar .btn.icon-only{padding:8px;width:36px;display:inline-flex;align-items:center;justify-content:center}
.page{padding:var(--sp-7,28px) var(--sp-8) var(--sp-2);max-width:1440px;animation:pageIn var(--dur-3) var(--ease-decel) both}
@keyframes pageIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.sub{color:var(--text-3);font-size:12.5px;margin:0 0 var(--sp-6);line-height:1.7}
.tag{display:inline-flex;align-items:center;gap:4px;background:var(--brand-soft);color:var(--brand-2);
  border:1px solid var(--brand-line);font-size:11px;padding:3px 10px;margin-left:6px;
  border-radius:var(--radius-pill);font-weight:600;vertical-align:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(348px,1fr));gap:18px}
/* ============ 账号卡片 ============ */
.card{background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--radius);
  box-shadow:var(--shadow);position:relative;transition:border-color var(--dur-2),box-shadow var(--dur-2),transform var(--dur-2)}
.card:hover{border-color:var(--line);box-shadow:var(--shadow-hover);transform:translateY(-3px)}
.card.cur{border-color:var(--brand);box-shadow:0 0 0 1px var(--brand),var(--shadow-brand)}
.card.cur:before{content:"当前";position:absolute;top:-1px;right:16px;background:var(--brand);color:#fff;
  font-size:10px;font-weight:700;padding:3px 10px;border-radius:0 0 7px 7px;letter-spacing:.5px}
.chead{display:flex;align-items:center;gap:11px;padding:var(--sp-4) var(--sp-4);border-bottom:1px solid var(--line-soft)}
.av{width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;
  font-size:15px;color:#fff;flex:0 0 38px;font-weight:700;letter-spacing:-.5px;
  background:linear-gradient(145deg,var(--brand),var(--brand-deep));box-shadow:inset 0 1px 0 rgba(255,255,255,.2)}
.av svg{width:20px;height:20px}
.nm{font-size:15px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  max-width:170px;font-weight:650;letter-spacing:-.1px}
.chead .sp{flex:1}
.pill{font-size:10.5px;padding:3px 10px;border:1px solid var(--line);color:var(--text-2);
  border-radius:var(--radius-pill);background:var(--surface-2);font-weight:600;white-space:nowrap}
.pill.ok,.pill.grn{color:var(--ok);background:var(--ok-soft);border-color:rgba(55,211,154,.3)}
.pill.red{color:var(--danger);background:var(--danger-soft);border-color:rgba(255,107,129,.3)}
.pill.warn{color:var(--warn);background:var(--warn-soft);border-color:rgba(245,181,68,.3)}
.cbody{padding:var(--sp-4)}
.bignum{font-size:27px;color:var(--text);margin:0 0 4px;font-weight:750;letter-spacing:-1px;line-height:1.1}
.bignum small{font-size:11.5px;color:var(--text-3);margin-left:7px;font-weight:500;letter-spacing:0}
.row{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text-2);margin:7px 0}
.row .lb{flex:0 0 84px;color:var(--text-3)}
.row .vl{color:var(--text);font-weight:550}
.row .dt{margin-left:auto;color:var(--text-3);font-size:11px}
.bar{height:5px;background:var(--surface-3);border-radius:4px;margin:6px 0 2px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--brand-deep),var(--brand-2));border-radius:4px;
  box-shadow:0 0 8px rgba(91,140,255,.4)}
.bar.warn i{background:linear-gradient(90deg,var(--warn),var(--danger))}
/* ============ 积分总览 · Bento 杂志式排版（差异化设计，非官方平铺） ============ */
/* DNA：唯一视觉焦点（hero）+ 阶梯次要指标 + 条形排行榜；8pt 网格、克制阴影、交错入场 */
.ov-wrap{margin-bottom:var(--sp-6)}
@keyframes ovRise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.ov-bento{display:grid;grid-template-columns:1.15fr 1fr;gap:var(--sp-4);margin-bottom:var(--sp-4)}
/* — 主焦点卡：剩余积分 — */
.ov-hero{position:relative;overflow:hidden;border-radius:var(--radius);padding:var(--sp-6) var(--sp-6);
  min-height:168px;display:flex;flex-direction:column;justify-content:space-between;
  background:linear-gradient(135deg,var(--brand-deep) 0%,var(--brand) 55%,#6f9bff 100%);
  color:#fff;box-shadow:var(--shadow-brand);animation:ovRise var(--dur-3) var(--ease-decel) both}
.ov-hero:before{content:"";position:absolute;width:230px;height:230px;right:-70px;top:-90px;border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,.28),rgba(255,255,255,0) 68%);pointer-events:none}
.ov-hero:after{content:"";position:absolute;width:150px;height:150px;left:-60px;bottom:-80px;border-radius:50%;
  border:1px solid rgba(255,255,255,.22);pointer-events:none}
.ov-hero .eyebrow{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:650;
  letter-spacing:.4px;color:rgba(255,255,255,.85);position:relative;z-index:1}
.ov-hero .eyebrow svg{width:17px;height:17px;stroke:#fff}
.ov-hero .hero-num{font-size:46px;font-weight:800;letter-spacing:-2px;line-height:1;
  position:relative;z-index:1;text-shadow:0 2px 12px rgba(0,0,0,.12)}
.ov-hero .hero-num small{font-size:13px;font-weight:600;letter-spacing:0;margin-left:8px;color:rgba(255,255,255,.8)}
.ov-hero .hero-foot{display:flex;align-items:center;gap:8px;font-size:11.5px;color:rgba(255,255,255,.8);
  position:relative;z-index:1}
.ov-hero .hero-foot .dot{width:6px;height:6px;border-radius:50%;background:#b7f7dc;box-shadow:0 0 8px #b7f7dc}
/* — 阶梯次要指标 — */
.ov-metrics{display:flex;flex-direction:column;gap:var(--sp-3)}
.ov-metric{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);
  background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--radius-sm);
  box-shadow:var(--shadow);transition:border-color var(--dur-1),transform var(--dur-1);
  animation:ovRise var(--dur-3) var(--ease-decel) both}
.ov-metric:nth-child(1){animation-delay:60ms}
.ov-metric:nth-child(2){animation-delay:120ms}
.ov-metric:nth-child(3){animation-delay:180ms}
.ov-metric:hover{border-color:var(--line);transform:translateX(3px)}
.ov-metric .mi{width:38px;height:38px;flex:0 0 38px;border-radius:11px;display:flex;align-items:center;
  justify-content:center;background:var(--brand-soft);color:var(--brand-2);border:1px solid var(--brand-line)}
.ov-metric .mi svg{width:19px;height:19px}
.ov-metric .mt{flex:1;min-width:0}
.ov-metric .mt .lb{font-size:11.5px;color:var(--text-3);font-weight:600;letter-spacing:.2px}
.ov-metric .mt .num{font-size:21px;font-weight:750;color:var(--text);letter-spacing:-.8px;line-height:1.25}
.ov-metric .mt .num small{font-size:11px;color:var(--text-3);font-weight:500;margin-left:4px;letter-spacing:0}
.ov-metric .trend{font-size:10.5px;color:var(--text-3);text-align:right;line-height:1.4}
/* — 条形排行榜 — */
.ov-rank{background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:var(--sp-5) var(--sp-6);animation:ovRise var(--dur-3) var(--ease-decel) both;
  animation-delay:220ms}
.ov-rank .rhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:var(--sp-5)}
.ov-rank .rhead h3{font-size:14px;margin:0;color:var(--text);font-weight:700;letter-spacing:.2px}
.ov-rank .rhead .src{color:var(--text-3);font-size:11px}
.ov-rank .rhead .sp{flex:1}
.ov-tabs{display:inline-flex;gap:2px;background:var(--surface-2);border:1px solid var(--line-soft);
  border-radius:var(--radius-pill);padding:3px}
.ov-tabs a{font-size:11.5px;color:var(--text-3);padding:4px 11px;border-radius:var(--radius-pill);
  cursor:pointer;user-select:none;font-weight:600;transition:all var(--dur-1)}
.ov-tabs a:hover{color:var(--text-2)}
.ov-tabs a.on{background:var(--brand);color:#fff;box-shadow:0 2px 8px rgba(91,140,255,.32)}
.ov-rank .rsum{font-size:11.5px;color:var(--text-3);margin-bottom:var(--sp-3)}
.ov-rank .rsum b{color:var(--brand-2);font-size:13px;font-weight:700}
.ov-row{display:grid;grid-template-columns:24px 1fr 92px;align-items:center;gap:var(--sp-3);
  padding:9px 0;border-bottom:1px solid var(--line-soft)}
.ov-row:last-child{border-bottom:none}
.ov-row .idx{width:24px;height:24px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;color:var(--text-3);background:var(--surface-2)}
.ov-row:nth-child(1) .idx{background:linear-gradient(145deg,#f5b544,#ff9f43);color:#1a1206}
.ov-row:nth-child(2) .idx{background:linear-gradient(145deg,#b8c4d8,#8fa0bb);color:#101624}
.ov-row:nth-child(3) .idx{background:linear-gradient(145deg,#c98f5b,#a86b3c);color:#1a1206}
.ov-row .body{min-width:0}
.ov-row .body .line{display:flex;align-items:center;gap:8px;font-size:12.5px}
.ov-row .body .nm{font-size:12.5px;max-width:220px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov-row .body .pct{margin-left:auto;color:var(--text-2);font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.ov-row .body .track{height:7px;border-radius:5px;background:var(--surface-3);margin-top:7px;overflow:hidden}
.ov-row .body .track i{display:block;height:100%;border-radius:5px;
  background:linear-gradient(90deg,var(--brand-deep),var(--brand-2));box-shadow:0 0 8px rgba(91,140,255,.4)}
.ov-row .val{font-size:13px;font-weight:700;color:var(--text);text-align:right;font-variant-numeric:tabular-nums}
.ov-row .val small{display:block;font-size:10px;color:var(--text-3);font-weight:500}
@media(max-width:760px){
  .ov-bento{grid-template-columns:1fr}
  .ov-hero .hero-num{font-size:38px}
}
.acts{display:flex;gap:7px;margin-top:var(--sp-4);flex-wrap:wrap}
/* ============ 按钮系统 ============ */
.btn{font-family:inherit;font-size:12.5px;border:1px solid var(--line);cursor:pointer;
  padding:7px 13px;background:var(--surface-2);color:var(--text-2);border-radius:var(--radius-xs);
  transition:all var(--dur-1) var(--ease-standard);font-weight:600;display:inline-flex;align-items:center;
  gap:6px;justify-content:center;white-space:nowrap;line-height:1.4}
.btn:hover{background:var(--surface-3);border-color:var(--line);color:var(--text);transform:translateY(-1px)}
.btn:active{transform:translateY(0) scale(.97)}
.btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.btn svg.ic{width:15px;height:15px}
.btn.amber{background:linear-gradient(145deg,var(--brand),var(--brand-deep));color:#fff;
  border-color:transparent;box-shadow:0 4px 14px rgba(91,140,255,.28)}
.btn.amber:hover{filter:brightness(1.1);box-shadow:0 6px 20px rgba(91,140,255,.4);color:#fff}
.btn.grn{background:var(--ok-soft);color:var(--ok);border-color:rgba(55,211,154,.32)}
.btn.grn:hover{background:rgba(55,211,154,.22);color:var(--ok)}
.btn.red{background:var(--danger-soft);color:var(--danger);border-color:rgba(255,107,129,.32)}
.btn.red:hover{background:rgba(255,107,129,.2);color:var(--danger)}
.btn.sm{padding:5px 10px;font-size:11.5px}
/* ============ 表格 ============ */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px;background:var(--surface);
  border:1px solid var(--line-soft);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
th{background:var(--surface-2);color:var(--text-3);text-align:left;padding:12px 15px;
  border-bottom:1px solid var(--line);font-weight:650;font-size:11.5px;letter-spacing:.3px;text-transform:uppercase}
td{padding:12px 15px;border-bottom:1px solid var(--line-soft);color:var(--text-2)}
td:first-child{color:var(--text);font-weight:550}
tr:last-child td{border-bottom:0}
tbody tr{transition:background var(--dur-1)}
tbody tr:hover td{background:var(--surface-2)}
/* ============ 表单控件 ============ */
select,input[type=text],input[type=time]{font-family:inherit;font-size:12.5px;background:var(--surface-2);
  color:var(--text);border:1px solid var(--line);padding:8px 11px;border-radius:var(--radius-xs);outline:none;
  transition:border-color var(--dur-1),box-shadow var(--dur-1)}
select:hover,input[type=text]:hover{border-color:var(--surface-3)}
select:focus,input:focus{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-soft)}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--brand);cursor:pointer}
/* ============ Toast ============ */
#toast{position:fixed;right:26px;bottom:26px;z-index:100;background:var(--surface-2);
  border:1px solid var(--brand-line);border-left:3px solid var(--brand);border-radius:var(--radius-sm);
  box-shadow:var(--shadow-hover);color:var(--text);padding:13px 17px;font-size:13px;display:none;max-width:460px;
  white-space:pre-wrap;animation:toastIn var(--dur-2) var(--ease-spring)}
@keyframes toastIn{from{opacity:0;transform:translateY(12px) scale(.96)}to{opacity:1;transform:none}}
/* ============ 设置卡片 ============ */
.mut{color:var(--text-3);font-size:12px;line-height:1.7}
.setcard{background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:var(--sp-5) var(--sp-6);margin-bottom:18px;max-width:720px}
.setcard h3{color:var(--text);font-size:15px;margin:0 0 var(--sp-4);font-weight:700;
  border-bottom:1px solid var(--line-soft);padding-bottom:13px;display:flex;align-items:center;gap:8px}
.setrow{display:flex;align-items:center;gap:11px;padding:11px 0;border-bottom:1px solid var(--line-soft);font-size:13px}
.setrow:last-child{border-bottom:0}
.kv{font-size:12.5px;color:var(--text-2);line-height:2.1}
.kv b{color:var(--brand-2);font-weight:600;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
/* ============ 模态框 ============ */
#modalBg{position:fixed;inset:0;background:rgba(6,9,14,.74);backdrop-filter:blur(6px);z-index:90;
  display:flex;align-items:center;justify-content:center;animation:fadeIn var(--dur-2) both}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
#modal{width:640px;max-width:92vw;max-height:84vh;display:flex;flex-direction:column;
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:0 28px 80px rgba(0,0,0,.6);padding:0;overflow:hidden;
  animation:modalIn var(--dur-3) var(--ease-spring)}
@keyframes modalIn{from{opacity:0;transform:translateY(16px) scale(.97)}to{opacity:1;transform:none}}
.mhead{display:flex;align-items:center;padding:var(--sp-4) var(--sp-5);border-bottom:1px solid var(--line);background:var(--surface-2)}
.mhead b{color:var(--text);font-size:15px;font-weight:700}
.mhead span{margin-left:auto;cursor:pointer;color:var(--text-3);font-size:20px;line-height:1;transition:color var(--dur-1)}
.mhead span:hover{color:var(--danger)}
.mtabs{display:flex;gap:3px;background:var(--surface-2);padding:0 var(--sp-5);border-bottom:1px solid var(--line)}
.mtabs a{padding:12px 16px;cursor:pointer;color:var(--text-3);font-size:13px;font-weight:550;
  border-bottom:2px solid transparent;transition:all var(--dur-1)}
.mtabs a:hover{color:var(--text-2)}
.mtabs a.on{color:var(--brand-2);border-bottom-color:var(--brand);font-weight:700}
.mpane{overflow-y:auto;padding:var(--sp-5);flex:1}
.minfo{background:var(--brand-soft);border:1px solid var(--brand-line);border-radius:var(--radius-sm);
  padding:13px 15px;font-size:12.5px;color:var(--text-2);line-height:1.8;margin-bottom:14px}
.mrow{display:flex;align-items:center;gap:9px;padding:9px 0;font-size:13px;color:var(--text)}
.mrow label{display:flex;align-items:center;gap:9px;cursor:pointer}
.mlist{border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface-2);margin-bottom:12px;overflow:hidden}
.mlist .li{display:flex;align-items:center;gap:11px;padding:12px 14px;border-bottom:1px solid var(--line-soft);font-size:13px}
.mlist .li:last-child{border-bottom:0}
.mlist .li .t{color:var(--text);max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:550}
.mlist .li .sub{font-size:11px;color:var(--text-3);margin-top:3px}
/* 减少动态效果偏好 */
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}

/* ==================================================================
   v2.7 全局设计语言升级（覆盖式，不改任何 DOM/JS，功能零风险）
   设计 DNA：杂志式眉题 + 品牌顶部色条 + 精致表格 + 统一间距节奏
   区别于 workbuddy-switch 的朴素平铺，形成自有视觉体系
   ================================================================== */
:root{
  --brand-glow:rgba(91,140,255,.18);
  --hair:rgba(43,52,68,.7);
}
/* 顶栏：加品牌小标 + 页标题字重层级 */
#topbar{padding-top:var(--sp-3);padding-bottom:var(--sp-3)}
#topbar h2{font-size:20px;letter-spacing:-.4px;position:relative;padding-left:14px}
#topbar h2:before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:4px;height:20px;border-radius:0 4px 4px 0;background:linear-gradient(180deg,var(--brand-2),var(--brand-deep))}
/* 页面说明：升级为眉题条，带序号圆点与左引导线 */
.page>.sub{position:relative;padding:9px 0 9px 16px;margin-bottom:var(--sp-5);
  border-left:2px solid var(--brand-line);border-radius:0 4px 4px 0;
  background:linear-gradient(90deg,var(--brand-soft),transparent 78%)}
.page>.sub:before{content:"";position:absolute;left:-4px;top:14px;width:6px;height:6px;border-radius:50%;
  background:var(--brand);box-shadow:0 0 0 3px var(--bg),0 0 10px var(--brand)}
/* 账号/数据卡片：顶部品牌渐变发丝条，悬浮更克制 */
.card:before{content:"";position:absolute;left:0;right:0;top:0;height:2px;
  background:linear-gradient(90deg,var(--brand),var(--brand-2),transparent);
  opacity:.55;border-radius:var(--radius) var(--radius) 0 0}
.card.cur:before{content:"";top:0;height:2px;left:0;right:0;width:auto;
  background:linear-gradient(90deg,var(--brand),var(--brand-2),var(--brand-3));opacity:1;
  border-radius:var(--radius) var(--radius) 0 0}
.chead{padding-top:13px;padding-bottom:13px}
/* 分区卡片（设置/趋势/明细）：标题带图标胶囊 */
.setcard{position:relative;overflow:hidden}
.setcard:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--brand-2),var(--brand-deep));opacity:.5}
.setcard h3{font-size:14.5px;letter-spacing:-.1px}
.setcard h3>svg,.setcard h3>img{width:22px;height:22px;padding:4px;border-radius:7px;
  background:var(--brand-soft);color:var(--brand-2);box-sizing:border-box}
/* 表格：表头深色胶囊 + 斑马纹 + 首列强调点 */
table{border-color:var(--line);box-shadow:var(--shadow)}
table thead th{background:linear-gradient(180deg,var(--surface-3),var(--surface-2));
  color:var(--text-2);font-size:11px;font-weight:700;letter-spacing:.5px;
  text-transform:none;border-bottom:1px solid var(--line)}
tbody tr:nth-child(even) td{background:rgba(255,255,255,.012)}
td:first-child{position:relative}
/* 徽章/标签统一 */
.tag{letter-spacing:.2px}
/* 空态统一 */
.mut{padding-left:2px}
/* 按钮焦点环（无障碍）统一 */
.btn:focus-visible,select:focus-visible,input:focus-visible{
  outline:2px solid var(--brand-2);outline-offset:2px}

/* ============================================================
   v2.8 外壳重构 · 顶部导航工作台
   架构级差异化：旧版=左侧竖栏+顶栏；本版=顶部工作栏+全宽内容
   仅改布局与外观，不动任何 DOM id / onclick / JS 渲染逻辑
   ============================================================ */
body{flex-direction:column}

/* 顶部工作栏：由左侧竖栏旋转为横向 header */
#side{width:auto;min-width:0;flex:0 0 auto;
  flex-direction:row;align-items:center;gap:18px;
  min-height:66px;padding:10px 26px;
  background:linear-gradient(180deg,rgba(33,41,55,.97),rgba(14,18,25,.97));
  border-right:0;border-bottom:1px solid var(--line);
  box-shadow:0 4px 22px rgba(0,0,0,.30);z-index:20}
#logo{padding:0;gap:10px;flex:0 0 auto}
#logo:after{display:none}
#logo .seal{width:36px;height:36px;flex-basis:36px;border-radius:11px}
#logo .seal svg{width:21px;height:21px}
#logo b{font-size:15px;line-height:1.1}
#logo small{display:none}

/* 横向胶囊导航 */
.nav{margin:0;flex:1;flex-direction:row;align-items:center;gap:5px;
  overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav a{padding:9px 13px;gap:7px;font-size:12.8px;border-radius:9px;
  border:1px solid transparent;white-space:nowrap;flex:0 0 auto}
.nav a:hover{transform:translateY(-1px);background:var(--surface-2);border-color:var(--line)}
.nav a.on{background:linear-gradient(180deg,rgba(91,140,255,.20),rgba(91,140,255,.07));
  border-color:var(--brand-line);color:var(--brand-2);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.nav a.on:before{content:"";left:50%;top:auto;bottom:-1px;transform:translateX(-50%);
  width:22px;height:2.5px;border-radius:3px 3px 0 0;
  background:var(--brand);box-shadow:0 0 10px var(--brand)}
.nav a .ic,.nav a .ic svg{width:16px;height:16px}

/* 右侧状态胶囊 */
#side .foot{margin:0 0 0 auto;padding:0;border:0;flex:0 0 auto;
  font-size:0;line-height:1;display:flex;align-items:center;gap:8px}
#side .foot br{display:none}
#side .foot b{font-size:10.5px;color:var(--brand-2);background:var(--brand-soft);
  border:1px solid var(--brand-line);padding:5px 9px;border-radius:var(--radius-pill);
  font-weight:700;letter-spacing:.5px}
#side .foot:before{content:"";width:7px;height:7px;border-radius:50%;
  background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft),0 0 10px var(--ok)}

/* 全宽内容区 + 页面工具条 */
#main{padding:0}
#topbar{padding:14px 30px}
#topbar h2{font-size:18px;display:flex;align-items:center;gap:9px}
#topbar h2:before{content:"";width:3px;height:17px;border-radius:2px;
  background:linear-gradient(180deg,var(--brand-2),var(--brand-deep))}
.page{padding:22px 30px 40px;max-width:1600px;margin:0 auto}

/* 用量统计：双视角 tab（区分于原版两个独立页面） */
.uv-tabs{display:inline-flex;gap:4px;padding:4px;margin-bottom:20px;
  background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--radius-pill)}
.uv-tabs a{display:inline-flex;align-items:center;gap:7px;padding:9px 18px;border-radius:var(--radius-pill);
  color:var(--text-3);font-size:13px;font-weight:600;cursor:pointer;user-select:none;
  transition:all var(--dur-2) var(--ease-standard)}
.uv-tabs a svg.ic{width:16px;height:16px}
.uv-tabs a:hover{color:var(--text);background:var(--surface-3)}
.uv-tabs a.on{color:#fff;background:linear-gradient(135deg,var(--brand-deep),var(--brand));
  box-shadow:0 4px 14px rgba(91,140,255,.30)}
.uv-view{animation:uvIn var(--dur-3) var(--ease-decel)}
@keyframes uvIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

</style></head><body>

<!-- ===== 内联 SVG 图标精灵（线性 24x24，统一风格，离线可用）===== -->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
<symbol id="i-user" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></symbol>
<symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></symbol>
<symbol id="i-puzzle" viewBox="0 0 24 24"><path d="M9 3H5a2 2 0 0 0-2 2v4h3a2 2 0 1 1 0 4H3v6a2 2 0 0 0 2 2h4v-3a2 2 0 1 1 4 0v3h6a2 2 0 0 0 2-2v-4h-3a2 2 0 1 1 0-4h3V5a2 2 0 0 0-2-2h-6v3a2 2 0 1 1-4 0z"/></symbol>
<symbol id="i-hourglass" viewBox="0 0 24 24"><path d="M6 2h12M6 22h12M8 2v4l4 4 4-4V2M8 22v-4l4-4 4 4v4"/></symbol>
<symbol id="i-chart" viewBox="0 0 24 24"><path d="M3 3v18h18M7 15l4-5 3 3 5-7"/></symbol>
<symbol id="i-diamond" viewBox="0 0 24 24"><path d="M6 3h12l4 6-10 12L2 9zM2 9h20M6 3l6 18M18 3l-6 18"/></symbol>
<symbol id="i-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v0a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"/></symbol>
<symbol id="i-checkin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12l2.5 2.5L16 9"/></symbol>
<symbol id="i-backup" viewBox="0 0 24 24"><path d="M4 7h16v13H4zM4 7l2-4h12l2 4M9 12h6M12 9v6"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6z"/><path d="M9 12l2 2 4-4"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></symbol>
<symbol id="i-switch" viewBox="0 0 24 24"><path d="M7 7h13l-3-3M17 17H4l3 3"/></symbol>
<symbol id="i-logo" viewBox="0 0 24 24"><path d="M4 8h16M4 16h16M8 4l-4 4 4 4M16 12l4 4-4 4"/></symbol>
<symbol id="i-export" viewBox="0 0 24 24"><path d="M12 16V3M7 8l5-5 5 5M4 15v5h16v-5"/></symbol>
<symbol id="i-import" viewBox="0 0 24 24"><path d="M12 3v13M7 11l5 5 5-5M4 20h16"/></symbol>
<symbol id="i-qr" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v3M14 20h3M20 20h1"/></symbol>
<symbol id="i-rotate" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></symbol>
<symbol id="i-coin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M9 10h5a2 2 0 0 1 0 4H9"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 11v6M14 11v6"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-close" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5"/></symbol>
<symbol id="i-diff" viewBox="0 0 24 24"><path d="M7 7h10M12 4v6M17 17H7M12 14v6"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M4 12l5 5L20 6"/></symbol>
</svg>


<div id="side">
  <div id="logo"><div class="seal"><svg><use href="#i-logo"/></svg></div><div><b>忆迁</b><small>MemShift · 记忆移植工作台</small></div></div>
  <div class="nav">
    <a data-p="acc" class="on"><span class="ic"><svg class="ic"><use href="#i-user"/></svg></span>账号管理</a>
    <a data-p="sess"><span class="ic"><svg class="ic"><use href="#i-folder"/></svg></span>关联会话</a>
    <a data-p="ide"><span class="ic"><svg class="ic"><use href="#i-puzzle"/></svg></span>编辑器账号</a>
    <a data-p="lim"><span class="ic"><svg class="ic"><use href="#i-hourglass"/></svg></span>限额台账</a>
    <a data-p="usage"><span class="ic"><svg class="ic"><use href="#i-chart"/></svg></span>用量统计</a>
    <a data-p="set"><span class="ic"><svg class="ic"><use href="#i-settings"/></svg></span>设置</a>
  </div>
  <div class="foot"><b id="ver"></b><br>记忆随账号移植 · 签到/移植一键完成<br>本地运行 · 数据不出机</div>
</div>

<div id="main">
  <div id="topbar"><h2 id="pgTitle">账号管理</h2><div class="sp"></div>
    <button class="btn amber sm" onclick="doCheckinAll()"><svg class="ic"><use href="#i-checkin"/></svg> 一键签到全部</button>
    <button class="btn sm" onclick="doBackup()"><svg class="ic"><use href="#i-backup"/></svg> 备份记忆</button>
    <button class="btn sm" onclick="bmCheck()"><svg class="ic"><use href="#i-shield"/></svg> 记忆体检</button>
    <button class="btn sm" onclick="refresh()"><svg class="ic"><use href="#i-refresh"/></svg> 刷新</button>
  </div>

  <div class="page" id="pg-acc">
    <div class="sub">与官方 workbuddy-switch 共用账号库 · 每日签到自动跑 · 切换 = 备份记忆 → 写登录态 → 校验 → 验收链 <span class="tag">记忆移植</span></div>
    <div class="setcard" style="max-width:none;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <div style="flex:1;min-width:220px"><h3 style="border:0;padding:0;margin:0">添加与迁移账号</h3>
        <div class="mut">官方 OAuth 扫码接入新账号，或从已有环境恢复 · 与官方共用账号库</div></div>
      <select id="oauthVariant" style="width:100px"><option value="cn">国内版</option><option value="ai">国际版</option></select>
      <button class="btn grn sm" onclick="oauthBegin()"><svg class="ic"><use href="#i-qr"/></svg> OAuth 扫码添加</button>
      <button class="btn grn sm" onclick="importLocal()"><svg class="ic"><use href="#i-import"/></svg> 导入本机登录态</button>
      <button class="btn sm" onclick="exportAcc([])"><svg class="ic"><use href="#i-export"/></svg> 导出全部</button>
      <button class="btn sm" onclick="importAccountsPick()"><svg class="ic"><use href="#i-import"/></svg> 导入账号库</button>
      <input type="file" id="importFile" accept=".json,application/json" style="display:none" onchange="importAccountsFile(this)">
      <button class="btn amber sm" onclick="rotateCli()"><svg class="ic"><use href="#i-rotate"/></svg> 自动轮换 CLI</button>
      <button class="btn sm" onclick="refresh()"><svg class="ic"><use href="#i-refresh"/></svg> 刷新</button>
    </div>
    <div class="grid" id="accGrid">加载中…</div>
    <p class="mut" style="margin-top:14px">签到仅国内版账号支持（国际版接口未开放，自动跳过）。切换后建议重启 WorkBuddy 客户端读取新登录态。</p>
  </div>

  <div class="page" id="pg-sess" style="display:none">
    <div class="sub">本机全部会话（聊天记录真身）· 服务端分页 · 关键词搜索 · 关联分歧检测</div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <select id="sessSel" onchange="sessPage=1;renderSess()"></select>
      <input id="sessQ" placeholder="搜索标题 / 模型" style="width:190px" onkeydown="if(event.key==='Enter'){sessPage=1;renderSess()}">
      <select id="sessSize" onchange="sessPage=1;renderSess()">
        <option value="30">30 条/页</option><option value="50" selected>50 条/页</option><option value="100">100 条/页</option>
      </select>
      <button class="btn sm" onclick="sessPage=1;renderSess()"><svg class="ic"><use href="#i-refresh"/></svg> 查询</button>
      <span class="mut" id="sessCount"></span>
      <div style="flex:1"></div>
      <button class="btn sm" onclick="loadDivergence()"><svg class="ic"><use href="#i-diff"/></svg> 检查分歧</button>
    </div>
    <table><thead><tr><th style="width:46%">标题</th><th>模型</th><th>状态</th><th>最近活动</th></tr></thead>
    <tbody id="sessRows"></tbody></table>
    <div style="display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px">
      <button class="btn sm" onclick="sessJump(-1)">‹ 上一页</button>
      <span class="mut" id="sessPageInfo"></span>
      <button class="btn sm" onclick="sessJump(1)">下一页 ›</button>
    </div>
    <div id="divBox" style="margin-top:18px"></div>
  </div>

  <div class="page" id="pg-ide" style="display:none">
    <div class="sub">CodeBuddy IDE / VS Code 插件独立切换 · 与 WorkBuddy 登录态互不影响 · 可勾选先复制会话</div>
    <div class="grid" id="ideGrid">加载中…</div>
    <p class="mut" style="margin-top:14px">切换流程：校验账号 → 关闭客户端 → （可选）复制会话 → 注入登录态 → 重开。
      客户端在运行时会自动关闭，可在「设置」里关掉这项行为。</p>
  </div>

  <div class="page" id="pg-lim" style="display:none">
    <div class="sub">被限流时这里直接给出「哪个账号 · 哪个模型 · 官方给的恢复时刻」· 数据来自 hook 事件流与客户端日志</div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <button class="btn amber sm" onclick="loadLimits(true)"><svg class="ic"><use href="#i-refresh"/></svg> 重新扫描日志</button>
      <span class="mut" id="limSummary">加载中…</span>
    </div>
    <div id="limActive"></div>
    <div id="limExpired" style="margin-top:16px"></div>
  </div>

  <div class="page" id="pg-usage" style="display:none">
    <div class="sub">Token 用量与积分消耗 · 同一份本机用量（session_usage）的两个视角 · 切换下方标签查看</div>
    <div class="uv-tabs">
      <a data-v="tok" class="on" onclick="setUsageView('tok')"><svg class="ic"><use href="#i-chart"/></svg>Token 用量</a>
      <a data-v="cred" onclick="setUsageView('cred')"><svg class="ic"><use href="#i-coin"/></svg>积分消耗</a>
    </div>

    <!-- 视角一：Token 用量 -->
    <div class="uv-view" id="uv-tok">
      <div class="grid" id="tokGrid"></div>
      <div class="setcard" style="margin-top:16px;max-width:none">
        <h3><svg class="ic"><use href="#i-chart"/></svg> 近 30 天用量趋势</h3>
        <div id="tokTrend"><span class="mut">加载中…</span></div>
      </div>
      <div class="setcard" style="max-width:none">
        <h3><svg class="ic"><use href="#i-refresh"/></svg> Top10 模型（按 token 用量）</h3>
        <div id="tokModels"><span class="mut">加载中…</span></div>
      </div>
      <div class="setcard" style="max-width:none">
        <h3><svg class="ic"><use href="#i-folder"/></svg> 请求明细（最近 200 个有用量的会话）</h3>
        <div style="max-height:380px;overflow:auto">
          <table><thead><tr><th style="width:40%">会话</th><th>模型</th><th>Token</th><th>额度</th><th>时间</th></tr></thead>
          <tbody id="tokDetail"></tbody></table>
        </div>
      </div>
    </div>

    <!-- 视角二：积分消耗 -->
    <div class="uv-view" id="uv-cred" style="display:none">
      <div class="credOverviewSlot"></div>
      <div class="grid" id="credGrid"></div>
    </div>
  </div>

  <div class="page" id="pg-set" style="display:none">
    <div class="sub">签到、移植链路与路径配置</div>
    <div class="setcard">
      <h3><svg class="ic"><use href="#i-checkin"/></svg> 每日签到</h3>
      <div class="setrow"><input type="checkbox" id="cfgCheckin"><label for="cfgCheckin">自动签到（国内版账号，每 10 分钟巡检一次，今日已签自动跳过）</label></div>
      <div class="setrow">签到时间窗 <input type="time" id="cfgCkStart" style="width:110px"> 至
        <input type="time" id="cfgCkEnd" style="width:110px"> <span class="mut">留空 = 全天可签</span></div>
      <div class="setrow"><button class="btn amber sm" onclick="saveCfg()">保存设置</button>
        <span class="mut" id="ckLast"></span></div>
    </div>
    <div class="setcard">
      <h3><svg class="ic"><use href="#i-switch"/></svg> 记忆移植链路</h3>
      <div class="setrow"><input type="checkbox" id="cfgBackup" checked><label for="cfgBackup">切换前自动全量备份会话（记忆保险层）</label></div>
      <div class="setrow"><input type="checkbox" id="cfgChain" checked><label for="cfgChain">切换后自动跑换号验收链 + 写 bridge/inbox 事件</label></div>
      <div class="setrow"><input type="checkbox" id="cfgAutoClose" checked><label for="cfgAutoClose">切换时自动关闭 / 重开 WorkBuddy 客户端（复制会话必须先关，不再需要人工关）</label></div>
      <div class="setrow"><button class="btn grn sm" onclick="saveCfg()">保存设置</button></div>
    </div>
    <div class="setcard">
      <h3>🎨 外观与网络</h3>
      <div class="setrow">主题
        <select id="cfgTheme" style="width:130px">
          <option value="pixel">像素（默认）</option>
          <option value="dark">深色</option>
          <option value="light">浅色</option>
        </select>
        <span class="mut">切换立即生效并记住</span></div>
      <div class="setrow">代理 <input id="cfgProxy" placeholder="http://127.0.0.1:7890（空=直连）" style="width:260px">
        <span class="mut">仅影响本软件的联网请求（签到 / 积分 / 更新检查）</span></div>
      <div class="setrow"><button class="btn grn sm" onclick="saveExtras()">保存</button></div>
    </div>
    <div class="setcard">
      <h3>🧰 支持工具开关</h3>
      <div class="setrow" id="featBox"></div>
      <div class="setrow"><button class="btn sm" onclick="saveExtras()">保存</button>
        <span class="mut">关掉的功能只隐藏入口，不动任何数据</span></div>
    </div>
    <div class="setcard">
      <h3>🐾 桌面伙伴</h3>
      <div class="sub" style="margin:-4px 0 10px">无边框置顶小窗：每个在跑的会话变成一只小动物。
        绿点＝在做任务 · 琥珀点＝等你确认 · 打勾＝刚做完 · 睡着了＝空闲。左边把手可拖动位置。</div>
      <div class="setrow">
        <button class="btn grn sm" onclick="petOpen()">打开伙伴</button>
        <button class="btn sm" onclick="petClose()">收起</button>
        <span class="mut" id="petState">—</span>
      </div>
    </div>
    <div class="setcard">
      <h3>🔄 更新与通知</h3>
      <div class="setrow"><button class="btn sm" onclick="checkUpdate()">检查更新</button>
        <button class="btn sm" onclick="loadNotif()">刷新通知</button>
        <span class="mut" id="updInfo"></span></div>
      <div id="notifBox" style="max-height:240px;overflow:auto;margin-top:8px"></div>
    </div>
    <div class="setcard">
      <h3>⚙ 路径</h3>
      <div class="kv">
        账号库 <b>~/.wb-switch/accounts.json</b>（与官方工具兼容）<br>
        登录态 <b>~/AppData/Local/CodeBuddyExtension/Data/Public/auth/</b><br>
        会话库 <b>~/.workbuddy/workbuddy.db + projects/</b><br>
        备份根 <b id="setBackupRoot"></b><br>
        签到日志 <b>~/.wb-switch/memshift_checkin_log.json</b><br>
        webui <b id="setPort"></b> · 日志 <b id="setLogFile"></b>
      </div>
      <p class="mut">界面：现代排版 · 系统无衬线字体 · 本软件反推自开源 workbuddy-switch（MIT）并独立重写。</p>
    </div>
  </div>
</div>
<div id="toast"></div>

<div id="modalBg" style="display:none">
  <div id="modal">
    <div class="mhead"><b id="mTitle">切换</b><span id="mClose">${ic('close')}</span></div>
    <div class="mtabs">
      <a id="mtabSync" class="on">关联会话 <span id="mSyncN">0</span></a>
      <a id="mtabCopy">复制会话</a>
    </div>
    <div id="mSyncPane" class="mpane">
      <div class="minfo"><b>什么是关联会话?</b><br>通过本工具复制到其他账号的会话，会自动建立关联。切换时，可选择将当前账号的后续内容同步到对应会话。</div>
      <div id="mSyncClientWarn" class="minfo" style="display:none;border-color:#ff6b81">⚠ WorkBuddy 客户端正在运行——会话写库需要先关闭客户端，否则复制/同步会失败（切换本身不受影响）。</div>
      <div class="mrow" id="mSelAllRow" style="display:none"><label><input type="checkbox" id="mSelAllSync"> 全选可直接同步的会话</label><span class="mut" id="mSyncCount"></span></div>
      <div id="mSyncList"></div>
    </div>
    <div id="mCopyPane" class="mpane" style="display:none">
      <div class="minfo">把当前账号勾选的会话复制给目标账号（复制后归属目标账号）；已经复制过的不会重复复制。</div>
      <div id="mCopyList"></div>
    </div>
    <div class="mfoot">
      <div class="mut" id="mSummary"></div>
      <div style="flex:1"></div>
      <button class="btn sm" onclick="closeModal()">取消</button>
      <button class="btn grn sm" id="mGo">确认切换</button>
    </div>
  </div>
</div>

<script>
/* 内联 SVG 图标快捷函数（引用 sprite symbol，离线可用） */
function ic(n,cls){return '<svg class="ic '+(cls||'')+'"><use href="#i-'+n+'"/></svg>';}
let ACCS=[], CUR=null, STATS=null, CK=null, CFG={}, BALS=null, OV=null, ovPeriod='week7';
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const COLORS=['#5b8cff','#3ecf8e','#7ba2ff','#ff6b81','#a78bfa','#f5b544','#38bdf8','#f472b6'];
const acol=s=>{let h=0;for(const c of String(s))h=(h*31+c.charCodeAt(0))>>>0;return COLORS[h%COLORS.length]};
const TITLES={acc:'账号管理',sess:'关联会话',ide:'编辑器账号',lim:'限额台账',usage:'用量统计',set:'设置'};
const PAGES=['acc','sess','ide','lim','usage','set'];
/* 兼容旧 hash：tok/cred 都落到合并后的 usage 页 */
const ALIAS={tok:'usage',cred:'usage'};
function setUsageView(v){
  document.querySelectorAll('.uv-tabs a').forEach(x=>x.classList.toggle('on',x.dataset.v===v));
  $('uv-tok').style.display=(v==='tok')?'':'none';
  $('uv-cred').style.display=(v==='cred')?'':'none';
  if(v==='cred'&&OV)renderOverview();
}
function fmtN(n){if(n==null)return '0';if(n>=1e8)return (n/1e8).toFixed(2)+'亿';if(n>=1e4)return (n/1e4).toFixed(1)+'万';return String(Math.round(n))}
function fmtC(n){if(n==null)return '-';if(n>=1e8)return (n/1e8).toFixed(2)+'亿';return n.toLocaleString('zh-CN',{maximumFractionDigits:2})}
function fmtT(ms){if(!ms)return '-';const d=new Date(ms>1e12?ms:ms*1000);const p=x=>String(x).padStart(2,'0');
  return `${d.getMonth()+1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`}
function fmtD(ms){if(!ms)return '-';const d=new Date(ms>1e12?ms:ms*1000);return `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()}`}
function fmtTime(ts){if(!ts)return '-';const d=new Date(ts>1e12?ts:ts*1000);const p=x=>String(x).padStart(2,'0');
  return `${d.getMonth()+1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`}
function days(ms){if(!ms)return null;return Math.ceil((ms-Date.now())/864e5)}
async function api(p,opt){const r=await fetch(p,opt);return r.json()}
function toast(m,ok=true){const t=$('toast');t.textContent=m;t.style.borderColor=ok?'#5b8cff':'#ff6b81';
  t.style.color=ok?'#7ba2ff':'#ff6b81';t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',7000)}

/* ---- 侧栏 ---- */
function go(p){
  p=ALIAS[p]||p;
  document.querySelectorAll('.nav a').forEach(x=>x.classList.toggle('on',x.dataset.p===p));
  for(const q of PAGES)$('pg-'+q).style.display=(q===p)?'':'none';
  $('pgTitle').textContent=TITLES[p];
  location.hash=p;
  if(p==='usage'&&OV)renderOverview();
  if(p==='ide')loadIde();
  if(p==='lim')loadLimits(false);
}
document.querySelectorAll('.nav a').forEach(a=>a.onclick=()=>go(a.dataset.p));

/* ---- OAuth 扫码添加 ---- */
let _oauthTimer=null;
async function oauthBegin(){
  const variant=$('oauthVariant').value;
  let r;
  try{r=await api('/api/oauth/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({variant:variant})});}catch(e){toast('发起扫码失败: '+e,false);return;}
  if(r.error){toast('发起扫码失败: '+r.error,false);return;}
  const url=r.verificationUri;
  showOAuthModal(url,r.expiresIn||300,r.loginId);
  if(_oauthTimer)clearInterval(_oauthTimer);
  _oauthTimer=setInterval(()=>oauthPoll(r.loginId),2000);
}
function showOAuthModal(url,secs,lid){
  const bg=document.createElement('div');bg.id='oauthBg';bg.style.cssText=
    'position:fixed;inset:0;background:rgba(5,3,18,.82);z-index:95;display:flex;align-items:center;justify-content:center';
  bg.innerHTML='<div style="width:560px;max-width:92vw;background:#0f1219;border:3px solid #000;box-shadow:6px 6px 0 #000,0 0 0 2px #2a3142;padding:0">'+
    '<div class="mhead"><b><svg class="ic sm" style="vertical-align:-2px"><use href="#i-qr"/></svg> OAuth 扫码添加</b><span id="oauthClose"><svg class="ic sm" style="vertical-align:-2px"><use href="#i-close"/></svg></span></div>'+
    '<div class="mpane" style="text-align:center">'+
    '<p class="mut" style="text-align:left">在 WorkBuddy 客户端打开下方链接 → 用手机扫码授权 → 本窗口自动检测结果并入库。</p>'+
    '<div id="oauthQr" style="margin:14px auto;width:240px;height:240px;background:#fff;border:4px solid #5b8cff;display:flex;align-items:center;justify-content:center"></div>'+
    '<a id="oauthLink" href="#" target="_blank" rel="noopener" style="color:#5b8cff;font-size:12px;word-break:break-all"></a>'+
    '<p class="mut" id="oauthStatus" style="margin-top:12px">等待扫码…（<span id="oauthSec">'+secs+'</span>s）</p>'+
    '<button class="btn amber sm" onclick="window.open(document.getElementById(\'oauthLink\').href)">在浏览器打开授权页</button>'+
    '</div></div>';
  document.body.appendChild(bg);
  $('oauthLink').href=url;$('oauthLink').textContent=url;
  // 二维码用在线 API 生成（仅把官方 authUrl 编码成图，不含任何 token）
  try{$('oauthQr').innerHTML='<img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data='+
    encodeURIComponent(url)+'" alt="扫码授权" width="220" height="220">';}
  catch(e){$('oauthQr').innerHTML='<span style="color:#333;font-size:13px">扫码请打开上方链接</span>';}
  let left=secs;const tick=setInterval(()=>{left--;if($('oauthSec'))$('oauthSec').textContent=left;
    if(left<=0)clearInterval(tick);},1000);
  $('oauthClose').onclick=()=>{clearInterval(_oauthTimer);bg.remove();};
}
async function oauthPoll(lid){
  let r;try{r=await api('/api/oauth/status?id='+encodeURIComponent(lid));}catch(e){return;}
  if(r.error||r.done){
    clearInterval(_oauthTimer);
    if($('oauthStatus'))$('oauthStatus').textContent=r.error||('✅ 已添加 '+((r.result||{}).nickname||''));
    if(!r.error){toast('扫码添加成功：'+((r.result||{}).nickname||''));setTimeout(()=>{
      const bg=$('oauthBg');if(bg)bg.remove();location.reload();},1200);}
    else toast('扫码失败: '+r.error,false);
  } else if($('oauthStatus'))$('oauthStatus').textContent='等待扫码确认…';
}

/* ---- 官方功能一比一：删除/导入导出/CLI切换/轮换 ---- */
async function importLocal(){
  const v=$('oauthVariant').value;
  const r=await api('/api/import_local',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({variant:v})});
  if(r.error){toast('导入失败: '+r.error,false);return;}
  toast('已导入本机登录态：'+(r.account.nickname||'')+(r.envelope?'（信封态，可切换但不可写CLI）':''));refresh();
}
async function exportAcc(ids){
  const r=await api('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:ids||[]})});
  if(r.error){toast('导出失败: '+r.error,false);return;}
  toast('已导出 '+r.count+' 个账号（含token）→ '+r.path);
}
function importAccountsPick(){$('importFile').click();}
async function importAccountsFile(inp){
  const f=inp.files[0];if(!f)return;
  const text=await f.text();inp.value='';
  const r=await api('/api/import_accounts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text})});
  if(r.error){toast('导入失败: '+r.error,false);return;}
  toast('导入完成：入库 '+r.imported+'（其中覆盖同uid '+r.overwritten+'）· 跳过 '+r.skipped);refresh();
}
async function switchCli(id,nick){
  const r=await api('/api/switch_cli',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})});
  toast(r.error?('CLI 切换失败: '+r.error):(r.message||('CLI 已切到 '+nick)),!r.error);
}
async function rotateCli(){
  const r=await api('/api/rotate_cli',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})});
  if(r.error){toast('轮换失败: '+r.error,false);return;}
  const rk=r.ranked.map(x=>x[0]+' 余'+x[1]).join(' / ');
  toast('已把积分最紧迫的「'+r.picked.nick+'」（余 '+r.picked.remaining+(r.picked.expiring?',即将到期':'')+'）设为 CLI 账号'+(r.cliRunning?'（CLI 运行中，重启生效）':'')+'\n紧迫排序：'+rk);
}
async function switchJb(id,nick){
  const r=await api('/api/switch_jb',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})});
  toast(r.error?('JetBrains 切换失败: '+r.error):(r.message||('JetBrains 已切到 '+nick)),!r.error);
}
async function delAcc(id,nick){
  if(!confirm('确定删除账号「'+nick+'」？\n仅从账号库移除记录，不动本机登录态文件（官方同款行为）。'))return;
  const r=await api('/api/account_delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})});
  if(r.error){toast('删除失败: '+r.error,false);return;}
  toast('已删除「'+nick+'」');refresh();
}

/* ---- 账号管理 ---- */
function ckBadge(a){
  const s=CK&&CK.states[a.id];if(!s)return '';
  if(!s.supported)return '<span class="pill">国际版·无签到</span>';
  if(s.envelope)return '<span class="pill red" title="需重新登录换明文 token">信封态</span>';
  if(s.today==='ok')return '<span class="pill grn">今日已签✓</span>';
  if(s.today==='already')return '<span class="pill grn">今日已签✓</span>';
  if(s.today==='error')return '<span class="pill red" title="'+esc(s.msg||'')+'">签到失败</span>';
  return '<span class="pill">今日未签</span>';
}
function balBadge(a){
  if(!BALS)return '<span class="pill">积分查询中…</span>';
  const b=BALS[a.id];
  if(!b)return '';
  if(b.unlimited)return '<span class="pill grn">企业不限量</span>';
  if(!b.ok)return '<span class="pill red" title="'+esc(b.error||'')+'">积分查询失败</span>';
  let h='';
  if(b.expiringSoon)h+='<span class="pill" style="color:#ffd97a;border-color:#ffd97a" title="有积分包 7 天内到期">即将到期</span>';
  if(b.expired)h+='<span class="pill red">已过期</span>';
  return h;
}
function accCard(a){
  const st=STATS?STATS.sessions[a.key]||{n:0,last:0}:{n:0,last:0};
  const us=STATS?STATS.usage[a.key]:null;
  const cur=a.key===CUR;
  const dleft=days(a.expiresAt);
  let pct=50,barc='';
  if(dleft!=null){pct=Math.max(0,Math.min(100,dleft/30*100));if(dleft<=0)barc='warn';else if(dleft<=7)barc='warn';}
  const cn=a.variant!=='ai';
  const b=BALS?BALS[a.id]:null;
  let big;
  if(b&&b.ok){
    const npk=(b.packages||[]).length;
    big=`<div class="bignum">${b.unlimited?'∞':fmtC(b.remaining)}<small>积分${b.unlimited?' · 企业配额':(npk?' · '+npk+' 个积分包':'')}</small>
      <span style="float:right;font-size:11px;color:#9d92c9">${us?fmtN(us.used)+' token':'会话 '+fmtN(st.n)}</span></div>`;
  }else{
    big=`<div class="bignum">${fmtN(st.n)}<small>个会话</small>
      <span style="float:right;font-size:11px;color:#9d92c9">${BALS?(b&&b.error?'积分查询失败':'积分查询中…'):''}</span></div>`;
  }
  return `<div class="card ${cur?'cur':''}">
   <div class="chead">
     <div class="av" style="background:${acol(a.key)}">${esc((a.nickname||'?').slice(0,2))}</div>
     <div class="nm">${esc(a.nickname||'-')}</div>
     <div class="sp"></div>
     ${ckBadge(a)}
     ${cur?'<span class="pill ok">当前</span>':''}
     ${a.enterpriseName?`<span class="pill">${esc(a.enterpriseName)}</span>`:''}
   </div>
   <div class="cbody">
     ${big}
     ${balBadge(a)}
     <div class="row" style="margin-top:6px"><span class="lb">会话 / 活动</span><span class="vl">${fmtN(st.n)} 个 · ${fmtT(st.last)}</span>
       <span class="dt">${esc(a.email||a.domain||'')}</span></div>
     <div class="row"><span class="lb">Token 到期</span><span class="vl">${fmtD(a.expiresAt)}</span>
       <span class="dt">${dleft!=null?(dleft>0?dleft+' 天':'已过期'):''}</span></div>
     <div class="bar ${barc}"><i style="width:${pct}%"></i></div>
     <div class="acts">
       ${cn&&!cur?'<button class="btn amber sm" onclick="doSwitch(\''+a.id+'\',\''+esc(a.nickname)+'\')"><svg class=\"ic\"><use href=\"#i-switch\"/></svg> 移植到此</button>':''}
       ${cn?'<button class="btn grn sm" onclick="doCheckin(\''+a.id+'\',\''+esc(a.nickname)+'\')"><svg class=\"ic\"><use href=\"#i-checkin\"/></svg> 签到</button>':''}
       <button class="btn sm" onclick="switchCli('${a.id}','${esc(a.nickname)}')" title="写入 CodeBuddy CLI 登录态">${ic('switch')} 切CLI</button>
       <button class="btn sm" onclick="switchJb('${a.id}','${esc(a.nickname)}')" title="写入 JetBrains 插件（自动遍历装了插件的 IDE）">${ic('switch')} 切JB</button>
       <button class="btn sm" onclick="exportAcc(['${a.id}'])" title="导出该账号完整记录（含token）">${ic('export')} 导出</button>
       <button class="btn red sm" onclick="delAcc('${a.id}','${esc(a.nickname)}')" title="仅删账号库记录，不动登录态">${ic('trash')} 删除</button>
       <button class="btn sm" onclick="reloadBal('${a.id}')">${ic('refresh')} 积分</button>
       ${cur?'<span class="pill grn">移植=先备份再写登录态</span>':''}
     </div>
   </div>
  </div>`;
}
async function loadBals(){
  try{BALS=await api('/api/credits_balances');}catch(e){BALS=null;}
  if(ACCS.length){$('accGrid').innerHTML=ACCS.map(accCard).join('');renderCred();}
  try{OV=await api('/api/credits_overview');renderOverview();}catch(e){}
}
async function reloadBal(id){
  toast('重新查询积分…');
  const d=await api('/api/credits_balances?force=1&id='+encodeURIComponent(id));
  if(BALS&&d&&d[id])BALS[id]=d[id];
  $('accGrid').innerHTML=ACCS.map(accCard).join('');renderCred();
  const b=d&&d[id];
  toast(b?(b.ok?(b.unlimited?'✅ 企业不限量':('✅ 余额 '+fmtC(b.remaining)+' 积分')):'❌ '+(b.error||'查询失败')):'❌ 查询失败',b&&b.ok);
}
async function refresh(){
  const [d,s,c,ck]=await Promise.all([api('/api/accounts'),api('/api/stats'),api('/api/config'),api('/api/checkin')]);
  ACCS=d.accounts;CUR=d.current_uid;STATS=s;CFG=c;CK=ck;
  $('accGrid').innerHTML=ACCS.map(accCard).join('')||'<div class="mut">账号库为空</div>';
  let opts='<option value="">全部账号</option>';
  for(const a of ACCS)opts+=`<option value="${a.key}">${esc(a.nickname||a.key)}</option>`;
  opts+='<option value="__none__">（未知账号）</option>';
  $('sessSel').innerHTML=opts;
  renderSess();renderTok();renderCred();
  loadBals();
  const last=(ck.logs||[])[0];
  $('ckLast').textContent=last?('上次签到动作: '+last.name+' · '+({ok:'成功',already:'已签过',error:'失败',skipped:'跳过'}[last.result]||last.result)+' · '+fmtTime(last.ts)):'';
}
/* ---- 切换弹窗（官方结构 1:1：关联会话 / 复制会话） ---- */
let SW=null;
function pill(txt,cls){return '<span class="pill '+(cls||'')+'">'+txt+'</span>'}
function mSummary(){
  const ns=SW?SW.syncSel.size:0, nc=SW?SW.copySel.size:0;
  $('mSummary').textContent='将同步 '+ns+' 个关联会话'+(nc?' · 已选复制 '+nc+' 个会话':'')+(nc?'':' · 未选择复制会话');
}
async function doSwitch(id,name){
  SW={id,name,tab:'sync',syncSel:new Set(),copySel:new Set(),sessions:null};
  $('mTitle').textContent='切换到「'+name+'」';
  $('mSyncN').textContent='…';$('mSyncList').innerHTML='<div class="mut" style="padding:14px">加载会话中…</div>';
  $('mCopyList').innerHTML='';$('mSelAllRow').style.display='none';
  $('modalBg').style.display='flex';setTab('sync');
  const d=await api('/api/relay_preview?target='+encodeURIComponent(id));
  if(d.error){$('mSyncList').innerHTML='<div class="mut" style="padding:14px">❌ '+esc(d.error)+'</div>';$('mSyncN').textContent='0';return;}
  SW.sessions=d.sessions||[];
  $('mSyncN').textContent=SW.sessions.filter(s=>s.linked).length;
  $('mSyncClientWarn').style.display=d.clientRunning?'':'none';
  renderSyncTab();renderCopyTab();mSummary();
}
function setTab(t){
  SW.tab=t;
  $('mtabSync').classList.toggle('on',t==='sync');$('mtabCopy').classList.toggle('on',t==='copy');
  $('mSyncPane').style.display=t==='sync'?'':'none';$('mCopyPane').style.display=t==='copy'?'':'none';
}
function renderSyncTab(){
  const linked=SW.sessions.filter(s=>s.linked);
  $('mSelAllRow').style.display=linked.length?'':'none';
  $('mSyncCount').textContent='已选 '+SW.syncSel.size+' / '+linked.filter(s=>s.syncable).length+' 个可同步';
  $('mSyncList').innerHTML=linked.length?('<div class="mlist">'+linked.map(s=>
    '<div class="li"><label><input type="checkbox" '+(s.syncable?'':'disabled ')+' '+(SW.syncSel.has(s.id)?'checked':'')
    +' onchange="tgSync(\''+s.id+'\')">​</label>'
    +'<div style="flex:1;min-width:0"><div class="t">'+esc(s.title)+'</div>'
    +'<div class="sub">将当前账号的新内容同步到「'+esc(SW.name)+'」'+(s.newRecords?'（+'+s.newRecords+' 条）':'')+'</div></div>'
    +(s.syncable?pill('可同步','amber'):pill('内容一致','grn'))
    +'</div>').join('')+'</div>')
    :'<div class="mut" style="padding:14px">当前账号还没有与「'+esc(SW.name)+'」关联的会话。去「复制会话」标签勾选要带过去的会话，复制后自动建立关联。</div>';
  $('mSelAllSync').checked=linked.length&&SW.syncSel.size===linked.filter(s=>s.syncable).length;
}
function renderCopyTab(){
  const copyable=SW.sessions.filter(s=>!s.linked&&s.hasHistory);
  const tasks=copyable.filter(s=>!s.isPlayground), spaces=copyable.filter(s=>s.isPlayground);
  const grp=(label,disp,arr)=>{
    if(!arr.length)return '';
    return '<div class="mlist"><div class="ghead" onclick="tgGrp(\''+label+'\')"><label style="display:flex;gap:8px;align-items:center;flex:1">'
      +'<input type="checkbox" id="grp_'+label+'" onchange="tgGrpAll(\''+label+'\')"> '+disp+'（'+arr.length+'）</label><span>▸</span></div>'
      +arr.map(s=>'<div class="li"><label><input type="checkbox" '+(SW.copySel.has(s.id)?'checked':'')
        +' onchange="tgCopy(\''+s.id+'\')">​</label><div style="flex:1;min-width:0"><div class="t">'+esc(s.title)+'</div>'
        +'<div class="sub">最近活动 '+fmtT(s.updatedAt)+'</div></div></div>').join('')+'</div>';
  };
  $('mCopyList').innerHTML=copyable.length?(grp('task','任务',tasks)+grp('space','空间',spaces))
    :'<div class="mut" style="padding:14px">当前账号没有可复制的会话（没有正文内容的会话不在列表里）。</div>';
  syncGrpBox('task',tasks);syncGrpBox('space',spaces);
}
function syncGrpBox(label,arr){
  const b=$('grp_'+label);if(!b||!arr.length)return;
  b.checked=arr.every(s=>SW.copySel.has(s.id));
}
function tgSync(id){SW.syncSel.has(id)?SW.syncSel.delete(id):SW.syncSel.add(id);renderSyncTab();mSummary();}
function tgCopy(id){SW.copySel.has(id)?SW.copySel.delete(id):SW.copySel.add(id);renderCopyTab();mSummary();}
function tgGrp(label){
  const arr=SW.sessions.filter(s=>!s.linked&&s.hasHistory&&(label==='task'?!s.isPlayground:s.isPlayground));
  const on=!arr.every(s=>SW.copySel.has(s.id));
  arr.forEach(s=>on?SW.copySel.add(s.id):SW.copySel.delete(s.id));
  renderCopyTab();mSummary();
}
function tgGrpAll(label){tgGrp(label)}
$('mSelAllSync').onchange=e=>{
  const can=SW.sessions.filter(s=>s.linked&&s.syncable);
  can.forEach(s=>e.target.checked?SW.syncSel.add(s.id):SW.syncSel.delete(s.id));
  renderSyncTab();mSummary();
};
$('mtabSync').onclick=()=>setTab('sync');$('mtabCopy').onclick=()=>setTab('copy');
$('mClose').onclick=closeModal;
function closeModal(){$('modalBg').style.display='none';SW=null;}
$('mGo').onclick=async()=>{
  const body={id:SW.id,copy_ids:[...SW.copySel],sync_ids:[...SW.syncSel]};
  closeModal();
  toast('移植中…（全量备份 + 会话接力 + 验收链，请勿关闭）');
  const d=await api('/api/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(d.ok){
    let m='✅ 已移植到「'+d.nickname+'」\n'+(d.steps||[]).join('\n');
    const ch=d.chain||{};
    if(ch.memory)m+='\n✔ 引导层蓝本已落位';
    if(ch.basic_memory)m+='\n'+(ch.basic_memory.ok?(ch.basic_memory.state==='skip'?'◇ Basic Memory 未安装（可选），已跳过——顶栏「记忆体检」可一键安装':(ch.basic_memory.state==='ok'?'✔ Basic Memory 体检通过':'⚠ Basic Memory 安装未完成，顶栏「记忆体检」可重试')):'⚠ Basic Memory 体检未全过');
    if(ch.broadcast==='appended')m+='\n✔ 主脑广播已写';
    if(ch.verify_rc===0)m+='\n✔ 验收链全绿';
    m+='\n→ 重启 WorkBuddy 客户端即用';
    toast(m,true);
  }else toast('❌ '+d.error,!!d.ok);
  refresh();
};
async function doCheckin(id,name){
  toast('签到中…');
  const d=await api('/api/checkin_one',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  toast(d.ok?(d.already?('「'+name+'」今日已签到'):('✅「'+name+'」'+(d.message||'签到成功'))):'❌「'+name+'」'+(d.error||'失败'),!!d.ok);
  refresh();
}
async function doCheckinAll(){
  if(!confirm('对全部国内版账号执行签到？\n（已签的自动跳过，信封态/国际版自动略过）'))return;
  toast('批量签到中…');
  const d=await api('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  const rs=d.results||[];
  const ok=rs.filter(r=>r.ok).length, err=rs.filter(r=>!r.ok&&r.error&&!r.skipped).length, sk=rs.filter(r=>r.skipped).length;
  toast('批量签到完成：成功/已签 '+ok+' · 失败 '+err+' · 跳过 '+sk+'\n'+rs.map(r=>'· '+r.name+': '+(r.ok?(r.already?'已签':'成功'):(r.skipped?'跳过':'失败 '+(r.error||'')))).join('\n'),err===0);
  refresh();
}
async function doBackup(){
  toast('记忆备份中…');
  const d=await api('/api/backup',{method:'POST'});
  toast(d.ok?('✅ 备份完成 '+d.size_mb+'MB · projects '+d.project_files+' 文件\n'+d.dest):'❌ '+d.error,!!d.ok);
}
async function bmCheck(){
  toast('记忆体检中…');
  const h=await api('/api/bm/health');
  if(h.state==='ok') return toast('✔ Basic Memory 体检通过（'+[h.exe&&'程序',h.mcp&&'注册',h.brain&&'主脑'].filter(Boolean).join('/')+'）',true);
  if(h.state==='skip'){
    toast('Basic Memory 未安装（可选增强），正在一键安装…（需联网，约 1 分钟）');
    const r=await api('/api/bm/install',{method:'POST'});
    toast(r.ok?'✔ '+r.message:('✘ '+(r.message||r.install_error||'安装失败')),true);
    return;
  }
  toast('⚠ 体检未全过：'+[!h.exe&&'程序缺失',!h.mcp&&'未注册',!h.brain&&'主脑目录缺失'].filter(Boolean).join('/')+'（点「记忆体检」未装可自动装）',true);
}

/* ---- 关联会话 ---- */
let sessPage=1, sessLast=null;
async function renderSess(){
  const k=$('sessSel').value||'', size=$('sessSize').value||'50', q=$('sessQ').value||'';
  let d;
  try{d=await api('/api/sessions_page?account='+encodeURIComponent(k)+'&page='+sessPage+
    '&size='+size+'&q='+encodeURIComponent(q));}
  catch(e){$('sessRows').innerHTML='<tr><td colspan="4" class="mut">查询失败</td></tr>';return;}
  if(d.error){$('sessRows').innerHTML='<tr><td colspan="4" class="mut">'+esc(d.error)+'</td></tr>';return;}
  sessLast=d;
  $('sessCount').textContent='共 '+(d.total||0)+' 个会话';
  $('sessPageInfo').textContent='第 '+(d.page||1)+' / '+(d.pages||1)+' 页';
  $('sessRows').innerHTML=(d.rows||[]).map(s=>`<tr><td>${esc(s.title)}</td><td>${esc(s.model)}</td>
    <td>${esc(s.status)}</td><td>${fmtT(s.updated_at||s.created_at)}</td></tr>`).join('')
    ||'<tr><td colspan="4" class="mut">无会话</td></tr>';
}
function sessJump(delta){
  const cur=(sessLast&&sessLast.page)||sessPage;
  const max=(sessLast&&sessLast.pages)||1;
  const p=cur+delta;
  if(p<1||p>max)return;
  sessPage=p;renderSess();
}

/* ---- 关联分歧（P2）---- */
const DIVLBL={diverged:'⚠ 双方都有新增',a_ahead:'源侧有新内容',b_ahead:'副本侧有新内容',in_sync:'一致'};
async function loadDivergence(){
  $('divBox').innerHTML='<div class="mut">检测中…</div>';
  let d;try{d=await api('/api/divergence');}catch(e){$('divBox').innerHTML='<div class="mut">检测失败</div>';return;}
  if(d.error){$('divBox').innerHTML='<div class="mut">'+esc(d.error)+'</div>';return;}
  const pairs=d.pairs||[];
  const head=`<div class="setcard" style="max-width:none"><h3>${ic('diff')} 关联会话分歧（${pairs.length} 对 · 分歧 ${d.diverged||0} · 待同步 ${d.ahead||0}）</h3>`;
  if(!pairs.length){$('divBox').innerHTML=head+'<div class="mut">没有关联会话</div></div>';return;}
  const rows=pairs.map((p,i)=>{
    const cls=p.state==='diverged'?'warn':'';
    return `<tr>
      <td>${esc(p.aNick)}<div class="mut" style="font-size:11px">${p.aCount} 条 · 新增 ${p.aNew}</div></td>
      <td>${esc(p.bNick)}<div class="mut" style="font-size:11px">${p.bCount} 条 · 新增 ${p.bNew}</div></td>
      <td>${pill(DIVLBL[p.state]||p.state,cls)}<div class="mut" style="font-size:11px">基线 ${p.base}</div></td>
      <td style="white-space:nowrap">
        ${p.state==='diverged'?`
          <button class="btn sm" onclick="resolveDiv(${i},'keep_a')">以源为准</button>
          <button class="btn sm" onclick="resolveDiv(${i},'keep_b')">以副本为准</button>`:''}
        ${p.state==='a_ahead'?`<button class="btn grn sm" onclick="resolveDiv(${i},'keep_a')">基线推到源</button>`:''}
        ${p.state==='b_ahead'?`<button class="btn sm" onclick="resolveDiv(${i},'keep_b')">基线推到副本</button>`:''}
        ${p.state==='diverged'?`<button class="btn amber sm" onclick="resolveDiv(${i},'overwrite')">源覆盖副本</button>`:''}
      </td></tr>`;
  }).join('');
  window._divPairs=pairs;
  $('divBox').innerHTML=head+`<table><thead><tr><th>源会话</th><th>副本会话</th><th>状态</th><th>处理</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="mut">「以源/副本为准」只移动基线，不动任何正文；「源覆盖副本」会把副本正文替换为源内容（自动备份副本）。</p></div>`;
}
async function resolveDiv(i,action){
  const p=(window._divPairs||[])[i];if(!p)return;
  if(action==='overwrite'&&!confirm('确定用源会话正文覆盖「'+p.bNick+'」的副本？\n副本会先备份，但内容会被替换。'))return;
  const r=await api('/api/divergence/resolve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({groupId:p.groupId,aUid:p.aUid,aSession:p.aSession,bUid:p.bUid,
      bSession:p.bSession,action:action})});
  toast(r.ok?('已处理：'+action+'（基线 '+r.base+'）'):('失败：'+(r.error||'')),!!r.ok);
  if(r.ok)loadDivergence();
}

/* ---- 编辑器账号（P0）---- */
const IDE_TARGETS=['vscode','ide_ai','ide_cn'];
let IDEST=null;
async function loadIde(){
  try{IDEST=await api('/api/ide/status');}catch(e){$('ideGrid').innerHTML='<div class="mut">加载失败</div>';return;}
  const t=IDEST.targets||{};
  $('ideGrid').innerHTML=IDE_TARGETS.map(k=>{
    const s=t[k]||{};
    const state=s.installed?(s.running?'运行中':'已安装'):'未安装';
    const cls=s.running?'warn':(s.installed?'ok':'');
    return `<div class="card">
      <div class="chead"><div class="av" style="background:${acol(k)}">${esc((s.label||'').slice(0,2))}</div>
        <div class="nm">${esc(s.label||k)}</div><div class="sp"></div>${pill(state,cls)}</div>
      <div class="cbody">
        <div class="row"><span class="lb">登录态</span><span class="vl">${s.loggedIn?'已登录':'未登录 / 无 secret'}</span></div>
        <div class="row"><span class="lb">当前账号</span><span class="vl">${esc(s.activeAccountName||'未记录')}</span></div>
        <div class="row"><span class="lb">数据目录</span><span class="vl" style="font-size:11px" title="${esc(s.dataDir||'')}">${esc((s.dataDir||'').slice(-28)||'-')}</span></div>
        <div class="row"><span class="lb">程序</span><span class="vl" style="font-size:11px">${esc(s.appPath? '已定位':'未找到')}</span></div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn grn sm" onclick="openIdeSwitch('${k}')">${ic('switch')} 切换账号</button>
          <button class="btn sm" onclick="ideDetect('${k}')">检测当前</button>
        </div>
      </div></div>`;
  }).join('');
}
async function ideDetect(k){
  const r=await api('/api/ide/detect',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target:k})});
  toast(r.matched?('已识别：'+(r.nickname||r.accountId)):((r.message||r.error||'未识别')),!!r.matched);
  loadIde();
}
async function openIdeSwitch(k){
  const accs=ACCS||[];
  if(!accs.length){toast('账号库为空',false);return;}
  const s=(IDEST&&IDEST.targets&&IDEST.targets[k])||{};
  let opts=accs.map(a=>`<option value="${esc(a.id)}">${esc(a.nickname||a.key)} [${esc(a.variant||'cn')}]</option>`).join('');
  const bg=document.createElement('div');bg.id='ideBg';bg.style.cssText=
    'position:fixed;inset:0;background:rgba(5,3,18,.82);z-index:96;display:flex;align-items:center;justify-content:center';
  bg.innerHTML='<div style="width:640px;max-width:94vw;background:#0f1219;border:3px solid #000;box-shadow:6px 6px 0 #000,0 0 0 2px #2a3142">'+
    '<div class="mhead"><b>切换到「'+esc(s.label||k)+'」</b><span id="ideClose"><svg class=\"ic sm\"><use href=\"#i-close\"/></svg></span></div>'+
    '<div class="mpane">'+
    '<div class="minfo">写入的是该客户端自己的登录态；WorkBuddy 客户端的账号不受影响。'+
    (s.running?'<br><b style="color:#f5b544">客户端正在运行：切换会先自动关闭再重开。</b>':'')+'</div>'+
    '<div class="setrow">目标账号 <select id="ideAcc" style="width:300px">'+opts+'</select></div>'+
    '<div class="setrow"><label><input type="checkbox" id="ideRestart" checked> 切换后重启客户端</label></div>'+
    '<div class="setrow"><label><input type="checkbox" id="ideCopy"> 先复制当前会话（需要客户端已关闭，会列出可勾选项）</label></div>'+
    '<div id="ideCopyBox" style="display:none"></div>'+
    '<div class="setrow"><button class="btn grn sm" id="ideGo">执行切换</button>'+
    '<span class="mut" id="ideMsg"></span></div>'+
    '</div></div>';
  document.body.appendChild(bg);
  $('ideClose').onclick=()=>bg.remove();
  $('ideCopy').onchange=async()=>{
    const box=$('ideCopyBox');
    if(!$('ideCopy').checked){box.style.display='none';return;}
    box.style.display='';box.innerHTML='<div class="mut">加载会话…</div>';
    const d=await api('/api/ide/sessions?target='+encodeURIComponent(k));
    if(d.error){box.innerHTML='<div class="mut">'+esc(d.error)+'</div>';return;}
    const list=d.sessions||[];
    if(!list.length){box.innerHTML='<div class="mut">当前账号没有可复制的会话</div>';return;}
    box.innerHTML='<div class="mlist" style="max-height:220px;overflow:auto">'+list.map((x,i)=>
      `<div class="li"><label><input type="checkbox" class="ideCk" data-ws="${esc(x.workspaceHash)}" data-id="${esc(x.id)}">
        <span class="t">${esc(x.title)}</span></label><span class="sp" style="flex:1"></span>
        <span class="mut" style="font-size:11px">${fmtT(x.updatedAt)}</span></div>`).join('')+'</div>';
  };
  $('ideGo').onclick=async()=>{
    const id=$('ideAcc').value;
    const items=[...document.querySelectorAll('.ideCk')].filter(c=>c.checked)
      .map(c=>({workspaceHash:c.dataset.ws,conversationId:c.dataset.id}));
    $('ideMsg').textContent='执行中…（关闭 / 写入 / 重开，约 10-40 秒）';
    $('ideGo').disabled=true;
    const r=await api('/api/ide/switch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:k,id:id,restart:$('ideRestart').checked,copy_items:items})});
    $('ideGo').disabled=false;
    $('ideMsg').textContent=r.ok?('✅ '+r.message):('❌ '+(r.error||'切换失败'));
    toast(r.ok?r.message:('切换失败：'+(r.error||'')),!!r.ok);
    if(r.steps&&r.steps.length)console.log(r.steps.join('\n'));
    if(r.ok)setTimeout(()=>{bg.remove();loadIde();},1400);
  };
}

/* ---- 限额台账（P1）---- */
async function loadLimits(force){
  $('limSummary').textContent=force?'扫描日志中…':'加载中…';
  let d;try{d=await api('/api/limits'+(force?'?scan=1&days=30':''));}
  catch(e){$('limSummary').textContent='加载失败';return;}
  if(d.error){$('limSummary').textContent=d.error;return;}
  $('limSummary').textContent=(d.summary||'')+(d.hookInstalled?' · hook 事件流已接入':' · hook 事件流未接入（仅日志）');
  const card=(e,active)=>{
    const left=active?('剩 '+Math.max(1,Math.round(e.remainingSec/60))+' 分钟'):'已恢复';
    return `<div class="card ${active?'cur':''}">
      <div class="chead"><div class="av" style="background:${acol(e.accountId||e.uid||'x')}">${esc((e.nickname||'?').slice(0,2))}</div>
      <div class="nm">${esc(e.nickname||'未知账号')}</div><div class="sp"></div>${pill(left,active?'warn':'ok')}</div>
      <div class="cbody">
        <div class="bignum" style="font-size:16px">${esc(e.model||'未知模型')}</div>
        <div class="row"><span class="lb">恢复时刻</span><span class="vl">${esc(e.resetAtText||'-')}</span></div>
        <div class="row"><span class="lb">命中次数</span><span class="vl">${e.hitCount||1}</span></div>
        <div class="row"><span class="lb">来源</span><span class="vl" style="font-size:11px">${e.source==='hook'?'hook 事件':'客户端日志'}</span></div>
      </div></div>`;
  };
  const act=d.active||[], exp=d.expired||[];
  $('limActive').innerHTML=act.length?'<div class="grid">'+act.map(e=>card(e,true)).join('')+'</div>'
    :'<div class="minfo" style="max-width:none">当前没有生效中的限额。被限流时这里会自动出现「账号 · 模型 · 恢复时刻」。</div>';
  $('limExpired').innerHTML=exp.length?('<h3 style="font-size:13px;color:var(--text-2);margin-bottom:8px">已恢复（最近 '+exp.length+' 条）</h3>'+
    '<div class="grid">'+exp.map(e=>card(e,false)).join('')+'</div>'):'';
}

/* ---- Token 深度统计（P2）---- */
let DEEP=null;
async function loadDeep(){
  try{DEEP=await api('/api/stats_deep');}catch(e){return;}
  if(DEEP.error)return;
  const daily=DEEP.daily||[];
  const max=Math.max(1,...daily.map(x=>x.used));
  $('tokTrend').innerHTML=daily.length?('<div style="display:flex;align-items:flex-end;gap:4px;height:110px;padding:6px 0">'+
    daily.map(x=>{const h=Math.max(3,Math.round(x.used/max*100));
      return `<div style="flex:1;min-width:8px;background:linear-gradient(180deg,#5b8cff,#3a5fd9);height:${h}%;
        border-radius:3px 3px 0 0" title="${x.date} · ${fmtN(x.used)} token · ${x.sessions} 个会话"></div>`}).join('')+
    '</div><div class="mut" style="font-size:11px">'+esc(daily[0].date)+' → '+esc(daily[daily.length-1].date)+
    ' · 合计 '+fmtN((DEEP.totals||{}).used)+' token</div>'):'<div class="mut">暂无用量数据</div>';
  const models=DEEP.top_models||[];
  const mmax=Math.max(1,...models.map(x=>x.used));
  $('tokModels').innerHTML=models.length?models.map((m,i)=>`<div class="row">
    <span class="lb" style="width:28px">${i+1}</span>
    <span class="vl" style="flex:0 0 150px;text-align:left">${esc(m.model)}</span>
    <div class="bar" style="flex:1;margin:0 10px"><i style="width:${Math.round(m.used/mmax*100)}%"></i></div>
    <span class="dt">${fmtN(m.used)} · ${m.sessions} 个会话</span></div>`).join(''):'<div class="mut">暂无数据</div>';
  const req=DEEP.requests||[];
  $('tokDetail').innerHTML=req.map(r=>`<tr><td>${esc(r.title)}</td><td>${esc(r.model)}</td>
    <td>${fmtN(r.used)}</td><td>${fmtN(r.size)}</td><td>${fmtT(r.updated_at)}</td></tr>`).join('')
    ||'<tr><td colspan="5" class="mut">暂无数据</td></tr>';
}

/* ---- 设置：扩展配置（P3）---- */
let EXT=null;
const FEATLBL={ide:'CodeBuddy IDE / VS Code 切换',limits:'限额台账',cli:'CodeBuddy CLI 切换',jetbrains:'JetBrains 插件切换',checkin:'每日签到',companion:'桌面伙伴（宠物栏）'};
/* 主题切换只改 CSS 变量，绝不 reload —— reload 会导致页面自我重载死循环（疯狂闪烁） */
const THEME_VARS=['--bg','--surface','--surface-2','--surface-3','--line','--line-soft','--text','--text-2','--text-3'];
const THEME_MAP={
  light:{'--bg':'#f4f6fb','--surface':'#ffffff','--surface-2':'#eef2f8','--surface-3':'#e3e9f3',
         '--line':'#cdd7e6','--line-soft':'#e2e8f2','--text':'#172031','--text-2':'#4a5670','--text-3':'#7b879c',page:'#f4f6fb'},
  dark:{'--bg':'#0b0e14','--surface':'#141924','--surface-2':'#1c2230','--surface-3':'#252d3d',
        '--line':'#2c3444','--line-soft':'#232a38','--text':'#e8edf6','--text-2':'#a7b2c6','--text-3':'#6d7890',page:''}
};
function applyTheme(t){
  const root=document.documentElement, name=(t==='light'||t==='dark')?t:'pixel';
  root.dataset.theme=name;
  THEME_VARS.forEach(k=>root.style.removeProperty(k));
  document.body.style.background='';
  const m=THEME_MAP[name];
  if(m){THEME_VARS.forEach(k=>{if(m[k])root.style.setProperty(k,m[k]);});document.body.style.background=m.page||'';}
  /* pixel：不设内联变量，沿用 CSS :root 默认配色 */
}
async function loadExtras(){
  try{EXT=await api('/api/extras');}catch(e){return;}
  if(EXT.error)return;
  $('cfgTheme').value=EXT.theme||'pixel';
  $('cfgProxy').value=EXT.proxy||'';
  $('cfgAutoClose').checked=EXT.auto_close_client!==false;
  const feats=EXT.features||{};
  $('featBox').innerHTML=Object.keys(FEATLBL).map(k=>
    `<label style="margin-right:16px"><input type="checkbox" class="featCk" data-k="${k}" ${feats[k]!==false?'checked':''}> ${FEATLBL[k]}</label>`).join('');
  applyTheme(EXT.theme||'pixel');
  if($('ver'))$('ver').textContent=EXT.appVersion||'APP_VER_PLACEHOLDER';
}
async function saveExtras(){
  const features={};
  document.querySelectorAll('.featCk').forEach(c=>features[c.dataset.k]=c.checked);
  const r=await api('/api/extras/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({theme:$('cfgTheme').value,proxy:$('cfgProxy').value.trim(),
      auto_close_client:$('cfgAutoClose').checked,features:features})});
  if(r.error){toast('保存失败：'+r.error,false);return;}
  EXT=r;applyTheme(r.theme);toast('设置已保存');
}
/* ---- 桌面伙伴（宠物栏）---- */
async function petState(){
  try{const s=await api('/api/companion/state');
    $('petState').textContent=(s.open?'运行中':'未打开')+(s.enabled===false?'（开关已关）':'');
  }catch(e){}
}
async function petOpen(){
  const r=await api('/api/companion/open',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.ok?'🐾 伙伴已出现（右下角，可拖动）':'打开失败：'+(r.error||''),!!r.ok);
  petState();
}
async function petClose(){
  await api('/api/companion/close',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast('已收起伙伴');petState();
}
async function checkUpdate(){
  $('updInfo').textContent='检查中…';
  const d=await api('/api/update_check?upstream=1');
  if(d.error){$('updInfo').textContent='检查失败：'+d.error;return;}
  let msg=d.message||'';
  if(d.upstream&&d.upstream.ok)msg+=' ｜ 官方最新 '+(d.upstream.tag||'');
  else if(d.upstream&&d.upstream.error)msg+=' ｜ 官方检查失败';
  $('updInfo').textContent=msg;
  if(d.found&&d.found.length){
    $('notifBox').innerHTML='<div class="mlist">'+d.found.map(f=>
      `<div class="li"><div style="flex:1"><div class="t">${esc(f.name)}</div>
        <div class="sub">${esc(f.path)}</div></div>
        <span class="mut" style="font-size:11px">${fmtT(f.mtime)}</span></div>`).join('')+'</div>';
  }
}
async function loadNotif(){
  const d=await api('/api/notifications?limit=30');
  const items=d.items||[];
  $('notifBox').innerHTML=items.length?'<div class="mlist">'+items.map(n=>
    `<div class="li"><div style="flex:1"><div class="t">${esc(n.title)}</div>
      <div class="sub">${esc(n.description)}</div></div>
      <span class="mut" style="font-size:11px">${esc(n.atText)}</span></div>`).join('')+'</div>'
    :'<div class="mut">暂无通知</div>';
}

/* ---- Token 统计 ---- */
function renderTok(){
  if(!STATS)return;
  $('tokGrid').innerHTML=ACCS.map(a=>{
    const u=STATS.usage[a.key];
    const used=u?u.used:0,size=u?u.size:0;
    const pct=size?Math.min(100,used/size*100):0;
    const cls=pct>85?'warn':pct>60?'warn':'';
    const cur=a.key===CUR;
    return `<div class="card ${cur?'cur':''}">
     <div class="chead"><div class="av" style="background:${acol(a.key)}">${esc((a.nickname||'?').slice(0,2))}</div>
     <div class="nm">${esc(a.nickname||'-')}</div><div class="sp"></div>${cur?'<span class="pill ok">当前</span>':''}</div>
     <div class="cbody">
     <div class="bignum">${fmtN(used)}<small>/ ${fmtN(size)} token</small></div>
     <div class="bar ${cls}"><i style="width:${pct}%"></i></div>
     <div class="row"><span class="lb">会话</span><span class="vl">${u?u.n:0} 个</span><span class="dt">${fmtT(u&&u.last)}</span></div>
     </div></div>`}).join('')||'<div class="mut">暂无数据</div>';
}

/* ---- 积分总体汇总（对齐官方账号消耗构成） ---- */
function setOvPeriod(p){ovPeriod=p;renderOverview();}
function renderOverview(){
  const slots=document.querySelectorAll('.credOverviewSlot');if(!slots.length||!OV)return;
  const d=OV;
  const tabs=[['today','今天'],['week7','近7天'],['month30','近30天'],['month','本月']];
  const tabName=(tabs.find(t=>t[0]===ovPeriod)||[])[1]||'近7天';
  const rows=(d.accounts||[]).slice().sort((a,b)=>(b[ovPeriod]||0)-(a[ovPeriod]||0));
  const total=rows.reduce((s,r)=>s+(r[ovPeriod]||0),0);
  const pct=r=>total>0?Math.round((r[ovPeriod]||0)/total*1000)/10:0;
  const metrics=[
    {lb:'今日消耗',num:fmtC(d.today),ic:'chart',sub:'较昨日 · 24h'},
    {lb:'近 7 天消耗',num:fmtC(d.week7),ic:'refresh',sub:'滚动 7 日'},
    {lb:'本月累计消耗',num:fmtC(d.month),ic:'checkin',sub:'自然月'},
  ];
  const html=`<div class="ov-wrap">
    <div class="ov-bento">
      <div class="ov-hero">
        <div class="eyebrow"><svg class="ic"><use href="#i-coin"/></svg>当前可用积分池</div>
        <div class="hero-num">${d.unlimited?'∞':fmtC(d.remaining)}<small>积分</small></div>
        <div class="hero-foot"><span class="dot"></span>${d.unlimited?'企业不限量':'全部账号余额合计'} · ${d.updated?'更新于 '+esc(d.updated):'本机实时'}</div>
      </div>
      <div class="ov-metrics">${metrics.map(m=>`<div class="ov-metric">
        <div class="mi"><svg class="ic"><use href="#i-${m.ic}"/></svg></div>
        <div class="mt"><div class="lb">${m.lb}</div><div class="num">${m.num}<small>积分</small></div></div>
        <div class="trend">${m.sub}</div></div>`).join('')}</div>
    </div>
    <div class="ov-rank">
      <div class="rhead"><h3>账号消耗排行</h3>
        <span class="src">${tabName} · ${esc(d.source||'本机用量')}</span>
        <span class="sp"></span>
        <div class="ov-tabs">${tabs.map(([v,t])=>`<a class="${ovPeriod===v?'on':''}" onclick="setOvPeriod('${v}')">${t}</a>`).join('')}</div>
      </div>
      <div class="rsum">${rows.length} 个账号 · 本期合计 <b>${fmtC(total)}</b> 积分</div>
      ${rows.length?rows.map((r,i)=>`<div class="ov-row">
        <div class="idx">${i+1}</div>
        <div class="body"><div class="line"><span class="nm" title="${esc(r.name)}">${esc(r.name)}</span>
          <span class="pct">${pct(r)}%</span></div>
          <div class="track"><i style="width:${pct(r)}%"></i></div></div>
        <div class="val">${fmtC(r[ovPeriod])}<small>积分</small></div></div>`).join('')
        :'<div class="mut" style="padding:12px 0">暂无本机积分用量记录</div>'}
    </div></div>`;
  slots.forEach(el=>{el.innerHTML=html;});
}

/* ---- 积分统计 ---- */
function renderCred(){
  if(!STATS)return;
  const cr=STATS.credits||{};
  $('credGrid').innerHTML=ACCS.map(a=>{
    const m=cr[a.key]||{};
    const rows=Object.entries(m).sort((x,y)=>y[1]-x[1]).slice(0,6);
    const tot=Object.values(m).reduce((s,v)=>s+v,0);
    const cur=a.key===CUR;
    const b=BALS?BALS[a.id]:null;
    let bal='';
    if(b&&b.ok)bal=`<div class="row"><span class="lb">当前余额</span><span class="vl" style="color:#7ba2ff">${b.unlimited?'∞ 不限量':fmtC(b.remaining)+(b.total?' / '+fmtC(b.total):'')}</span>
      <span class="dt">${(b.packages||[]).length} 个积分包</span></div>`;
    else if(b&&!b.ok)bal=`<div class="row"><span class="lb">当前余额</span><span class="vl">查询失败</span></div>`;
    return `<div class="card ${cur?'cur':''}">
     <div class="chead"><div class="av" style="background:${acol(a.key)}">${esc((a.nickname||'?').slice(0,2))}</div>
     <div class="nm">${esc(a.nickname||'-')}</div><div class="sp"></div>${cur?'<span class="pill ok">当前</span>':''}</div>
     <div class="cbody">
     <div class="bignum">${tot.toFixed(2)}<small>积分消耗</small></div>
     ${bal}
     ${rows.length?rows.map(([k,v])=>`<div class="row"><span class="lb" title="${esc(k)}">${esc(k.slice(0,10))}…</span><span class="vl">${v.toFixed(2)}</span></div>`).join(''):'<div class="mut">暂无消耗记录</div>'}
     </div></div>`}).join('')||'<div class="mut">暂无数据</div>';
}

/* ---- 设置 ---- */
async function loadCfg(){
  const c=await api('/api/config');CFG=c;
  $('cfgBackup').checked=!!c.backup;$('cfgChain').checked=!!c.chain;
  $('cfgCheckin').checked=!!c.checkin_auto;
  $('cfgCkStart').value=c.checkin_start||'';$('cfgCkEnd').value=c.checkin_end||'';
}
async function saveCfg(){
  CFG.backup=$('cfgBackup').checked;CFG.chain=$('cfgChain').checked;
  CFG.checkin_auto=$('cfgCheckin').checked;
  CFG.checkin_start=$('cfgCkStart').value;CFG.checkin_end=$('cfgCkEnd').value;
  const d=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(CFG)});
  // 自动关闭客户端属于扩展配置，一起落盘（两个卡片共用一个「保存设置」）
  await api('/api/extras/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({auto_close_client:$('cfgAutoClose').checked})});
  toast(d.ok?'✅ 设置已保存':'❌ 保存失败',!!d.ok);
}

const CFG_PATHS=PATHS_JSON_PLACEHOLDER;
$('ver').textContent='APP_VER_PLACEHOLDER';
$('setPort').textContent='http://127.0.0.1:PORT_PLACEHOLDER';
$('setBackupRoot').textContent=CFG_PATHS.backupRoot;
$('setLogFile').textContent=CFG_PATHS.logFile;
refresh();setInterval(refresh,30000);loadCfg();loadExtras();loadDeep();petState();
(function(){const h=(location.hash||'').slice(1);if(h&&(PAGES.indexOf(h)>=0||ALIAS[h]))go(h);})();
</script></body></html>"""


CFG_FILE = os.path.join(HOME, ".wb-switch", "memshift_config.json")


def run_all_selftests():
    """一键体检：跑扩展包全部自检（不写业务数据、不改任何账号/会话）。"""
    out = {}
    mods = (("aesgcm", aesgcm), ("secret_storage", secret_storage),
            ("proc_ctl", proc_ctl), ("ext_sessions", ext_sessions),
            ("ide_switch", ide_switch), ("limits_board", limits_board),
            ("extras", ext_extras), ("pet_rail", pet_rail),
            ("wb_adapter", wb_adapter), ("hook_install", hook_install))
    for name, mod in mods:
        try:
            ok, rows = mod.selftest()
        except Exception as e:
            ok, rows = False, [("异常", False, repr(e))]
        out[name] = {"ok": bool(ok),
                     "items": [{"name": n, "pass": bool(p), "info": str(i)} for n, p, i in rows]}
    items = [it for v in out.values() for it in v["items"]]
    out["summary"] = {
        "modules": len(out),
        "checks": len(items),
        "passed": len([it for it in items if it["pass"]]),
        "allOk": all(v["ok"] for v in out.values()),
        "appVersion": APP_VER,
    }
    return out


def load_cfg():
    try:
        return json.load(open(CFG_FILE, encoding="utf-8"))
    except Exception:
        return {"backup": True, "chain": True,
                "checkin_auto": False, "checkin_start": "", "checkin_end": ""}


def save_cfg(c):
    os.makedirs(os.path.dirname(CFG_FILE), exist_ok=True)
    _atomic_write(CFG_FILE, json.dumps({
        "backup": bool(c.get("backup")), "chain": bool(c.get("chain")),
        "checkin_auto": bool(c.get("checkin_auto")),
        "checkin_start": str(c.get("checkin_start") or ""),
        "checkin_end": str(c.get("checkin_end") or "")}))
    return load_cfg()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 浏览器提前断开（切页 / 关闭面板）属正常，不打堆栈
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("APP_VER_PLACEHOLDER", APP_VER).replace(
                "PORT_PLACEHOLDER", str(PORT)).replace(
                "PATHS_JSON_PLACEHOLDER",
                json.dumps({"backupRoot": BACKUP_ROOT + os.sep,
                            "logFile": LOGF}, ensure_ascii=False))
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/api/accounts":
            self._send(200, json.dumps({
                "accounts": [account_brief(a) for a in load_accounts()],
                "current_uid": current_uid(),
            }, ensure_ascii=False))
        elif self.path == "/api/stats":
            self._send(200, json.dumps(collect_stats(), ensure_ascii=False))
        elif self.path == "/api/credits_overview":
            self._send(200, json.dumps(credits_overview(), ensure_ascii=False))
        elif self.path == "/api/config":
            self._send(200, json.dumps(load_cfg(), ensure_ascii=False))
        elif self.path.startswith("/api/credits_balances"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("force") and q.get("id"):
                acc = next((a for a in load_accounts() if a.get("id") == q["id"][0]), None)
                if acc:
                    CREDITS_CACHE.pop(acc.get("id"), None)
                    self._send(200, json.dumps({acc.get("id"): fetch_account_credits(acc)},
                                               ensure_ascii=False))
                else:
                    self._send(200, json.dumps({"error": "账号不存在"}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(fetch_all_credits(), ensure_ascii=False))
        elif self.path == "/api/checkin":
            accs = load_accounts()
            states = {}
            for a in accs:
                r0, e0 = checkin_today_state(a.get("id"))
                env = _is_envelope(a)
                states[a.get("id")] = {
                    "today": r0 or "none",
                    "msg": (e0 or {}).get("error") or (e0 or {}).get("message"),
                    "envelope": env,
                    "supported": a.get("variant", "cn") == "cn",
                }
            self._send(200, json.dumps({"states": states,
                                        "logs": _checkin_log()[-30:][::-1]}, ensure_ascii=False))
        elif self.path.startswith("/api/relay_preview"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src_uid, tgt_uid = current_uid(), None
            tgt = next((a for a in load_accounts() if a.get("id") == (q.get("target") or [""])[0]), None)
            tgt_uid = (tgt or {}).get("uid")
            if not src_uid or not tgt_uid:
                self._send(200, json.dumps({"error": "未读取到本机登录态或目标账号缺 uid"},
                                           ensure_ascii=False))
            elif tgt.get("uid") == src_uid:
                self._send(200, json.dumps({"error": "目标即当前账号"}, ensure_ascii=False))
            else:
                try:
                    self._send(200, json.dumps({
                        "sessions": relay_preview(src_uid, tgt_uid),
                        "clientRunning": _wb_client_running(),
                    }, ensure_ascii=False))
                except Exception as e:
                    self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/oauth/status"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lid = (q.get("id") or [""])[0]
            self._send(200, json.dumps(oauth_poll(lid), ensure_ascii=False))
        elif self.path == "/api/ide/status":
            try:
                self._send(200, json.dumps({
                    "targets": ide_switch.status_all(load_accounts()),
                    "extRoot": ext_sessions.data_root(),
                }, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/ide/sessions"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            target = (q.get("target") or ["vscode"])[0]
            if target not in ide_switch.TARGETS:
                self._send(200, json.dumps({"error": "未知目标"}, ensure_ascii=False))
            else:
                try:
                    self._send(200, json.dumps(
                        ide_switch.list_sessions(target, load_accounts()), ensure_ascii=False))
                except Exception as e:
                    self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/limits"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = bool(q.get("scan"))
            try:
                r = limits_board.collect(load_accounts(), days=int((q.get("days") or ["7"])[0]),
                                         use_hook=True, use_logs=force or not limits_board.load_state().get("events"))
                r["summary"] = limits_board.summary_line(r)
                self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path == "/api/stats_deep":
            try:
                self._send(200, json.dumps(collect_stats_deep(), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/sessions_page"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._send(200, json.dumps(sessions_page(
                    account_key=(q.get("account") or [""])[0],
                    page=int((q.get("page") or ["1"])[0]),
                    size=int((q.get("size") or ["50"])[0]),
                    query=(q.get("q") or [""])[0]), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e), "rows": []}, ensure_ascii=False))
        elif self.path == "/api/divergence":
            try:
                self._send(200, json.dumps(detect_divergence(), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e), "pairs": []}, ensure_ascii=False))
        elif self.path.startswith("/api/notifications"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._send(200, json.dumps(
                    ext_extras.notifications(limit=int((q.get("limit") or ["30"])[0])),
                    ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e), "items": []}, ensure_ascii=False))
        elif self.path == "/api/extras":
            try:
                cfg = ext_extras.load()
                cfg["appVersion"] = APP_VER
                cfg["running"] = proc_ctl.is_any_running()
                self._send(200, json.dumps(cfg, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/update_check"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                r = ext_extras.check_update(APP_VER, sys.executable)
                if q.get("upstream"):
                    r["upstream"] = ext_extras.check_upstream(
                        proxy=ext_extras.load().get("proxy") or None)
                self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))

        elif self.path == "/companion/rail":
            self._send(200, pet_rail.html(), "text/html; charset=utf-8")
        elif self.path == "/companion/verify":
            # 验收路由：绝对 URL 版宿主桥（headless 浏览器可直达，协议同款）
            self._send(200, pet_rail.wrapper_html("http://127.0.0.1:%d" % PORT),
                       "text/html; charset=utf-8")
        elif self.path.startswith("/companion/embed/"):
            data, ctype = pet_rail.embed_file(self.path[len("/companion/embed/"):])
            if data is None:
                self._send(404, json.dumps({"error": "not found"}))
            else:
                self._send(200, data, ctype)
        elif self.path.startswith("/api/companion/snapshot"):
            try:
                self._send(200, json.dumps(pet_rail.build_snapshot(), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"version": 1, "ready": False, "ts": 0,
                                            "sources": {}, "sessions": [], "events": [],
                                            "error": repr(e)}, ensure_ascii=False))
        elif self.path == "/api/companion/settings":
            self._send(200, json.dumps(pet_rail.get_settings(), ensure_ascii=False))
        elif self.path == "/api/companion/rail_prefs":
            self._send(200, json.dumps(pet_rail.get_rail_prefs(), ensure_ascii=False))
        elif self.path.startswith("/api/companion/sessions"):
            try:
                self._send(200, json.dumps(pet_rail.collect(), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": repr(e), "sessions": []},
                                           ensure_ascii=False))
        elif self.path == "/api/companion/state":
            self._send(200, json.dumps({"ok": True, "open": pet_rail.is_open(),
                                        "enabled": ext_extras.load().get("features", {}).get("companion", False)},
                                       ensure_ascii=False))
        elif self.path == "/api/bm/health":
            self._send(200, json.dumps(bm_health(), ensure_ascii=False))
        elif self.path == "/font/zh" and "zh" in FONTS:
            self._send(200, FONTS["zh"], "font/woff2")
        elif self.path == "/font/la" and "la" in FONTS:
            self._send(200, FONTS["la"], "font/woff2")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n))
            except Exception:
                body = {}
        if self.path == "/api/switch":
            r = switch_account(body.get("id"), do_backup=body.get("backup", True),
                               do_chain=body.get("chain", True),
                               copy_ids=body.get("copy_ids") or [],
                               sync_ids=body.get("sync_ids") or [])
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/backup":
            self._send(200, json.dumps(backup_sessions(), ensure_ascii=False))
        elif self.path == "/api/bm/install":
            self._send(200, json.dumps(bm_install(), ensure_ascii=False))
        elif self.path == "/api/config":
            self._send(200, json.dumps({"ok": True, **save_cfg(body)}, ensure_ascii=False))
        elif self.path == "/api/checkin":
            self._send(200, json.dumps({"ok": True, "results": checkin_all(
                force=bool(body.get("force")))}, ensure_ascii=False))
        elif self.path == "/api/checkin_one":
            r = checkin_account(body.get("id"), force=bool(body.get("force")))
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/oauth/start":
            r = oauth_start(body.get("variant", "cn"))
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/account_delete":
            self._send(200, json.dumps(delete_account(body.get("id")), ensure_ascii=False))
        elif self.path == "/api/import_local":
            self._send(200, json.dumps(import_local_account(body.get("variant", "cn")), ensure_ascii=False))
        elif self.path == "/api/export":
            self._send(200, json.dumps(export_accounts_file(body.get("ids") or []), ensure_ascii=False))
        elif self.path == "/api/import_accounts":
            self._send(200, json.dumps(import_accounts_text(body.get("text")), ensure_ascii=False))
        elif self.path == "/api/switch_cli":
            self._send(200, json.dumps(switch_cli_account(body.get("id")), ensure_ascii=False))
        elif self.path == "/api/rotate_cli":
            self._send(200, json.dumps(rotate_cli_account(), ensure_ascii=False))
        elif self.path == "/api/switch_jb":
            self._send(200, json.dumps(switch_jetbrains(body.get("id")), ensure_ascii=False))
        elif self.path == "/api/ide/switch":
            target = body.get("target")
            if target not in ide_switch.TARGETS:
                self._send(200, json.dumps({"ok": False, "error": "未知目标"}, ensure_ascii=False))
            else:
                r = ide_switch.switch(
                    target, body.get("id"), load_accounts(),
                    restart=body.get("restart", True),
                    copy_items=body.get("copy_items") or None,
                    backup_root=os.path.join(BACKUP_ROOT, "ext_state"))
                self._send(200, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/ide/detect":
            target = body.get("target")
            if target not in ide_switch.TARGETS:
                self._send(200, json.dumps({"ok": False, "error": "未知目标"}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(ide_switch.detect(target, load_accounts()),
                                           ensure_ascii=False))
        elif self.path == "/api/limits/scan":
            try:
                r = limits_board.collect(load_accounts(), days=int(body.get("days") or 30),
                                         use_hook=True, use_logs=True)
                r["summary"] = limits_board.summary_line(r)
                self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": repr(e)}, ensure_ascii=False))
        elif self.path == "/api/divergence/resolve":
            r = resolve_divergence(
                body.get("groupId"), body.get("aUid"), body.get("aSession"),
                body.get("bUid"), body.get("bSession"), body.get("action"))
            self._send(200, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/extras/save":
            self._send(200, json.dumps({"ok": True, **ext_extras.save(body)},
                                       ensure_ascii=False))
        elif self.path == "/api/companion/open":
            try:
                self._send(200, json.dumps(pet_rail.launch("http://127.0.0.1:%d" % PORT),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        elif self.path == "/api/companion/close":
            try:
                self._send(200, json.dumps(pet_rail.close(), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        elif self.path == "/api/companion/settings_save":
            pet_rail._save_cfg({"settings": body or {}})
            self._send(200, json.dumps(pet_rail.get_settings(), ensure_ascii=False))
        elif self.path == "/api/companion/rail_prefs_save":
            p = body or {}
            pet_rail._save_cfg({"railPrefs": {k: v for k, v in p.items()
                                              if k in ("avatarStyle", "visibleCount",
                                                       "animation", "size")}})
            self._send(200, json.dumps(pet_rail.get_rail_prefs(), ensure_ascii=False))
        elif self.path == "/api/companion/open_url":
            u = (body or {}).get("url") or ""
            if u.startswith(("http://", "https://")):
                try:
                    import webbrowser
                    webbrowser.open(u)
                    self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
                except Exception as e:
                    self._send(200, json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": False, "error": "非法链接"}, ensure_ascii=False))
        elif self.path == "/api/companion/reset_pos":
            self._send(200, json.dumps(pet_rail.reset_position(), ensure_ascii=False))
        elif self.path == "/api/workbuddy-hook":
            # 官方 collector /api/workbuddy-hook 等价端点：WorkBuddy hook
            # 转发脚本 POST 到这里，喂给 hub（workbuddy.rs:ingest_workbuddy_hook）。
            try:
                r = wb_adapter.ingest_hook(body or {})
                self._send(200, json.dumps({"ok": bool(r)}, ensure_ascii=False))
            except Exception:
                self._send(200, "{}", "application/json")
        elif self.path == "/api/workbuddy-close":
            r = wb_adapter.session_monitor_close(body or {})
            self._send(200, json.dumps(r, ensure_ascii=False))

        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def serve(open_browser=True, native=False):
    """native=True：原生桌面窗口模式（WebView2 内嵌，不弹浏览器）——双击 exe 即此模式。"""
    ensure_checkin_thread()
    try:
        # 官方语义（companion.rs + workbuddy.rs merge_workbuddy_hooks）：
        # 采集端在位时确保 WorkBuddy hook 链路挂好；失败不影响主程序。
        r = hook_install.install()
        log("companion hook 安装: %s" % json.dumps(r, ensure_ascii=False))
    except Exception as e:
        log("companion hook 安装失败: %r" % e)
    url = "http://127.0.0.1:%d" % PORT
    log("webui 启动 %s (native=%s)" % (url, native))
    print("%s %s webui: %s  (Ctrl+C 退出)" % (APP_NAME, APP_VER, url))
    if native:
        def _make_server():
            return ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        try:
            srv = _make_server()
        except OSError:
            # 端口被占 = 已有实例在跑：直接开窗口连现有实例（单实例多窗口）
            log("端口 %d 被占，连接已有实例" % PORT)
            try:
                import webview
                webview.create_window("%s %s · 记忆移植工作台" % (APP_NAME, APP_VER), url,
                                      width=1280, height=860, min_size=(980, 640))
                webview.start()
            except Exception as e:
                log("连接已有实例失败: %r" % e)
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            import webview
            webview.create_window("%s %s · 记忆移植工作台" % (APP_NAME, APP_VER), url,
                                  width=1280, height=860, min_size=(980, 640))
            if ext_extras.load().get("features", {}).get("companion", False):
                try:
                    pet_rail.launch(url)
                except Exception as e:
                    log("桌面伙伴启动失败: %r" % e)
            webview.start()  # 阻塞至窗口关闭 → 关窗即退出软件
            log("桌面窗口已关闭，退出")
        except Exception as e:
            log("原生窗口不可用(%r)，回退浏览器模式" % e)
            print("原生窗口不可用(%r)，回退浏览器模式" % e)
            threading.Timer(1.2, lambda: webbrowser.open(url)).start() if False else None
            import webbrowser
            webbrowser.open(url)
            srv.serve_forever()
        return
    if open_browser:
        try:
            import webbrowser
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


# ---------------- CLI ----------------

def cmd_status():
    accs = load_accounts()
    cu = current_uid()
    print("账号库: %s (%d 个)" % (ACCOUNTS_JSON, len(accs)))
    cur = next((a for a in accs if a.get("uid") == cu), None)
    print("当前账号: %s" % (("%s (%s)" % (cur.get("nickname"), mask(cu))) if cur else (mask(cu) if cu else "未登录/未知")))
    for a in accs:
        flag = " ←当前" if a.get("uid") == cu else ""
        print("  - %s [%s] %s%s" % (a.get("nickname"), a.get("variant"), mask(a.get("uid", "")), flag))


def cmd_list():
    print(json.dumps([account_brief(a) for a in load_accounts()], ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(prog="MemShift", description="忆迁 MemShift · 双击即用：不带参数 = 桌面窗口模式")
    ap.add_argument("cmd", nargs="?", default=None,
                    choices=["status", "list", "backup", "switch", "chain", "serve", "verify"],
                    help="省略 = 桌面窗口模式；serve = 浏览器模式；verify = 扩展功能自检")
    ap.add_argument("--id", help="账号 id（switch 用）")
    ap.add_argument("--yes", action="store_true", help="switch 危险操作确认")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-chain", action="store_true")
    a = ap.parse_args()
    init_runtime_io()
    if a.cmd is None:
        serve(open_browser=False, native=True)  # 双击 = 原生桌面窗口
        return
    if a.cmd == "status":
        cmd_status()
    elif a.cmd == "list":
        cmd_list()
    elif a.cmd == "backup":
        print(json.dumps(backup_sessions(), ensure_ascii=False, indent=2))
    elif a.cmd == "chain":
        print(json.dumps(run_chain(), ensure_ascii=False, indent=2))
    elif a.cmd == "switch":
        if not a.id:
            print("需要 --id <账号id>（先用 list 查看）")
            sys.exit(1)
        if not a.yes:
            print("⚠️ 切换是真实写操作（会改 WorkBuddy 登录态）。确认请加 --yes")
            sys.exit(1)
        print(json.dumps(switch_account(a.id, do_backup=not a.no_backup,
                                        do_chain=not a.no_chain), ensure_ascii=False, indent=2))
    elif a.cmd == "serve":
        serve(open_browser=True)
    elif a.cmd == "verify":
        r = run_all_selftests()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        print("\n结果: %d/%d 项通过 · %s" % (
            r["summary"]["passed"], r["summary"]["checks"],
            "全部模块正常" if r["summary"]["allOk"] else "有模块异常，见上"))
        sys.exit(0 if r["summary"]["allOk"] else 1)


if __name__ == "__main__":
    main()
