# -*- coding: utf-8 -*-
"""CodeBuddy IDE / VS Code 插件账号切换（P0，对齐 codebuddy_ide.rs 与 vscode_ext.rs）。

时序（严格照官方，不做简化）：
  1. 校验目标：账号存在 / 档位匹配 / access_token 非空 / 用户数据目录存在
     —— 注定失败的先挡掉，**不关用户的编辑器**
  2. restart 时关闭客户端（优雅 → 超时强杀 → 确认消失）
  3. 会话复制（可选，必须客户端未运行；逐条隔离，失败不阻断切换）
  4. 加密注入 secret → 写后校验
  5. 记录 activeAccountId（~/.wb-switch/<name>.json，与官方共用）
  6. restart 时重新拉起

与官方一致的两个硬约束：
  - IDE 运行时会缓存整张 ItemTable，运行中写入读不到也留不住 → 必须先退出
  - 写后必须回读校验（行存在 + Buffer 结构），不靠「命令没报错」当成功
"""

import json
import os
import time

from . import ext_sessions, proc_ctl, secret_storage

STORE_DIR = os.path.join(os.path.expanduser("~"), ".wb-switch")

TARGETS = {
    "vscode": {
        "label": "VS Code 插件",
        "store": "vscode",
        "secret_target": "vscode",
        "state_file": "vscode_ext.json",
        "process": "vscode",
        "requires_variant": None,      # 两档账号都能写（载荷按 variant 决定 domain）
        "app_hint": "VS Code",
    },
    "ide_ai": {
        "label": "CodeBuddy 国际版 IDE",
        "store": "ide",
        "secret_target": "ide_ai",
        "state_file": "codebuddy_ide.json",
        "process": "ide_ai",
        "requires_variant": "ai",
        "app_hint": "CodeBuddy",
    },
    "ide_cn": {
        "label": "CodeBuddy 国内版 IDE",
        "store": "ide",
        "secret_target": "ide_cn",
        "state_file": "codebuddy_ide_cn.json",
        "process": "ide_cn",
        "requires_variant": "cn",
        "app_hint": "CodeBuddy CN",
    },
}


# ---------------------------------------------------------------- 状态文件


def _state_path(target):
    return os.path.join(STORE_DIR, TARGETS[target]["state_file"])


def load_state(target):
    try:
        with open(_state_path(target), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(target, data):
    os.makedirs(STORE_DIR, exist_ok=True)
    path = _state_path(target)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _display_name(acc):
    return acc.get("nickname") or acc.get("email") or (acc.get("uid") or "")[:8] or "未知账号"


def _clean_token(acc, key="access_token"):
    value = acc.get(key)
    if isinstance(value, dict):
        return ""
    return (value or "").strip()


# ---------------------------------------------------------------- 状态


def status(target, accounts=None):
    """返回该目标的可观测状态（只读、不解密、不建目录）。"""
    spec = TARGETS[target]
    secret_target = spec["secret_target"]
    dir_ = secret_storage.TARGETS[secret_target]["dir"]
    installed = os.path.isdir(dir_)
    db_exists = secret_storage.find_existing_db(dir_) is not None
    logged_in = secret_storage.has_secret_row(secret_target) if db_exists else False
    exe = proc_ctl.exe_path(spec["process"])
    state = load_state(target)
    active_id = state.get("activeAccountId")
    active_name = None
    if active_id and accounts:
        hit = next((a for a in accounts if a.get("id") == active_id), None)
        if hit:
            active_name = _display_name(hit)
        else:
            active_id = None
    return {
        "target": target,
        "label": spec["label"],
        "installed": installed,
        "appPath": exe,
        "running": proc_ctl.is_running(spec["process"]),
        "loggedIn": logged_in,
        "dataDir": dir_,
        "dbPath": secret_storage.find_existing_db(dir_),
        "dbExists": db_exists,
        "activeAccountId": active_id,
        "activeAccountName": active_name,
        "statePath": _state_path(target),
    }


def status_all(accounts=None):
    return {t: status(t, accounts) for t in TARGETS}


# ---------------------------------------------------------------- 校验


def _validate(target, acc):
    spec = TARGETS[target]
    if not acc:
        raise ValueError("账号不存在")
    want = spec["requires_variant"]
    variant = acc.get("variant", "cn")
    if want and variant != want:
        raise ValueError("%s 只能切换%s账号（该账号档位为 %s）" % (
            spec["label"], "国际版" if want == "ai" else "国内版", variant))
    if isinstance(acc.get("access_token"), dict):
        raise ValueError("该账号 token 是加密信封，无法写入%s（请用 OAuth 扫码取明文）" % spec["label"])
    if not _clean_token(acc):
        raise ValueError("账号缺少明文 access_token，无法写入%s" % spec["label"])
    root = secret_storage.TARGETS[spec["secret_target"]]["dir"]
    if not os.path.isdir(root):
        raise ValueError("未找到 %s 用户数据目录（%s）。请先手动打开并登录一次" % (spec["label"], root))
    return root


def _source_uid(target):
    """当前目标登录账号的 uid：secret 里的 uid → 状态文件记录 → 账号库。"""
    spec = TARGETS[target]
    try:
        secret = secret_storage.read_secret(spec["secret_target"])
    except Exception:
        secret = None
    if secret:
        uid, _ = secret_storage.parse_identity(secret)
        if uid and ext_sessions.is_safe_uid(uid):
            return uid
    state = load_state(target)
    active = state.get("activeAccountId")
    if active:
        return None  # 需要账号库反查，交给调用方
    return None


# ---------------------------------------------------------------- 检测


def detect(target, accounts):
    """读当前登录 secret，匹配账号库（uid 优先，token 兜底）。"""
    spec = TARGETS[target]
    try:
        secret = secret_storage.read_secret(spec["secret_target"])
    except Exception as e:
        return {"ok": False, "found": False, "error": "读取登录态失败：%s" % e}
    if not secret:
        return {"ok": True, "found": False, "message": "未找到登录 secret"}
    uid, token = secret_storage.parse_identity(secret)
    hit = None
    if uid:
        hit = next((a for a in accounts if a.get("uid") == uid), None)
    if hit is None and token:
        hit = next((a for a in accounts if _clean_token(a) == token), None)
    if hit:
        save_state(target, {
            "activeAccountId": hit.get("id"),
            "updatedAt": int(time.time() * 1000),
        })
        return {"ok": True, "found": True, "matched": True,
                "accountId": hit.get("id"), "nickname": _display_name(hit), "uid": uid}
    return {"ok": True, "found": True, "matched": False,
            "message": "本机登录的账号不在账号库中", "uid": uid}


# ---------------------------------------------------------------- 切换


def switch(target, account_id, accounts, restart=True, copy_items=None, backup_root=None):
    """执行切换。返回 {ok, message, steps, copy, ...}；失败也带 steps 便于定位。"""
    spec = TARGETS[target]
    steps = []
    acc = next((a for a in accounts if a.get("id") == account_id), None)

    # 1) 校验（不关编辑器）
    try:
        root = _validate(target, acc)
        steps.append("校验通过：%s（%s）" % (_display_name(acc), acc.get("variant", "cn")))
    except ValueError as e:
        return {"ok": False, "error": str(e), "steps": steps}

    secret_target = spec["secret_target"]
    proc_kind = spec["process"]

    # 2) 关闭客户端
    was_running = proc_ctl.is_running(proc_kind)
    if restart:
        if was_running:
            ok, msg = proc_ctl.close(proc_kind, 20)
            steps.append(msg)
            if not ok:
                return {"ok": False, "error": msg, "steps": steps}
        else:
            steps.append("%s 未在运行，跳过关闭" % spec["label"])

    # 3) 会话复制（必须客户端未运行）
    copy_report = None
    if copy_items:
        if proc_ctl.is_running(proc_kind):
            copy_report = {"error": "%s 仍在运行，已跳过会话复制" % spec["label"]}
            steps.append(copy_report["error"])
        else:
            try:
                source_uid = None
                try:
                    secret = secret_storage.read_secret(secret_target)
                except Exception:
                    secret = None
                if secret:
                    source_uid, _ = secret_storage.parse_identity(secret)
                if not source_uid:
                    state = load_state(target)
                    hit = next((a for a in accounts
                                if a.get("id") == state.get("activeAccountId")), None)
                    source_uid = (hit or {}).get("uid")
                target_uid = acc.get("uid")
                data_root = ext_sessions.data_root()
                if not data_root:
                    raise ValueError("未找到扩展数据目录（CodeBuddyExtension），无法复制会话")
                if not source_uid or not ext_sessions.is_safe_uid(source_uid):
                    raise ValueError("未检测到当前登录账号，无法定位源会话")
                backup_dir = os.path.join(
                    backup_root or os.path.join(os.path.expanduser("~"), ".wb-switch", "backups"),
                    ext_sessions.STORES[spec["store"]]["backup_kind"],
                    time.strftime("%Y%m%d_%H%M%S"))
                copy_report = ext_sessions.copy_sessions(
                    spec["store"], data_root, backup_dir, source_uid, target_uid, copy_items)
                steps.append("会话复制：成功 %d，失败 %d" % (
                    len(copy_report.get("copied") or []), len(copy_report.get("errors") or [])))
            except Exception as e:
                copy_report = {"error": str(e)}
                steps.append("会话复制失败：%s" % e)

    # 4) 注入 secret
    try:
        if secret_target == "vscode":
            try:
                existing = secret_storage.read_secret(secret_target)
            except Exception:
                existing = None
            payload = secret_storage.build_vscode_session_json(acc, existing)
        else:
            payload = secret_storage.build_ide_session_json(acc, secret_target)
        db_path = secret_storage.write_secret(
            secret_target, payload, backup_dir=os.path.join(
                backup_root or os.path.join(os.path.expanduser("~"), ".wb-switch", "backups"),
                "state_vscdb"))
        steps.append("已写入登录态 → %s" % db_path)
    except Exception as e:
        # 注入失败：把关掉的客户端开回来，再如实报错（避免双输）
        if restart and was_running:
            proc_ctl.launch(proc_kind)
            steps.append("注入失败，已尝试重新打开 %s" % spec["label"])
        return {"ok": False, "error": "注入登录态失败：%s" % e, "steps": steps}

    # 5) 记录当前账号
    save_state(target, {
        "activeAccountId": account_id,
        "updatedAt": int(time.time() * 1000),
        "dbPath": db_path,
    })
    steps.append("已记录当前账号")

    # 6) 重开
    restarted = False
    if restart:
        ok, msg = proc_ctl.launch(proc_kind)
        steps.append(msg)
        restarted = ok
        if not ok:
            steps.append("⚠ 凭证已生效，但客户端未能自动打开，请手动启动")

    return {
        "ok": True,
        "target": target,
        "label": spec["label"],
        "account": _display_name(acc),
        "accountId": account_id,
        "restarted": restarted,
        "dbPath": db_path,
        "copy": copy_report,
        "steps": steps,
        "message": "已切换 %s 到「%s」%s" % (
            spec["label"], _display_name(acc), "并重启" if restarted else "（请手动重启生效）"),
    }


# ---------------------------------------------------------------- 会话列表


def list_sessions(target, accounts=None):
    """列出当前登录账号在该客户端可复制的会话。"""
    spec = TARGETS[target]
    root = ext_sessions.data_root()
    if not root:
        return {"sessions": [], "skipped": 0, "dataRoot": None,
                "error": "未找到 CodeBuddyExtension 数据目录"}
    uid = _source_uid(target)
    if not uid and accounts:
        state = load_state(target)
        hit = next((a for a in accounts if a.get("id") == state.get("activeAccountId")), None)
        uid = (hit or {}).get("uid")
    if not uid:
        return {"sessions": [], "skipped": 0, "dataRoot": root,
                "error": "未检测到当前登录账号（请先在 %s 中登录）" % spec["label"]}
    data = ext_sessions.list_sessions(spec["store"], root, uid)
    data["uid"] = uid
    return data


def selftest():
    rows = []
    for target, spec in TARGETS.items():
        st = status(target)
        rows.append(("%s 状态可读" % spec["label"], True,
                     "installed=%s running=%s loggedIn=%s"
                     % (st["installed"], st["running"], st["loggedIn"])))
        try:
            detect(target, [])
            rows.append(("%s 检测不崩" % spec["label"], True, "ok"))
        except Exception as e:
            rows.append(("%s 检测不崩" % spec["label"], False, str(e)))
    # 未安装时切换必须给出可读错误、不得抛异常
    try:
        r = switch("ide_ai", "nope", [])
        rows.append(("账号不存在时安全拒绝", r["ok"] is False, r.get("error", "")))
    except Exception as e:
        rows.append(("账号不存在时安全拒绝", False, "抛异常：%s" % e))
    return all(r[1] for r in rows), rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
