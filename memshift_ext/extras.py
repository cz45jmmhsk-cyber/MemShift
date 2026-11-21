# -*- coding: utf-8 -*-
"""P3 辅助能力：自动更新检查、通知中心、主题、代理、支持工具开关。

这些都不碰账号与会话数据，只读写 `~/.wb-switch/memshift_extras.json` 与官方
`~/.wb-switch/notifications.json`（只读展示，避免改坏官方结构）。

自动更新的现实口径：MemShift 是本地打包分发的，没有云端 release 通道，所以「更新」=
在指定目录里发现比自己更新的 MemShift 构建（按版本号 → 修改时间 → 大小比较），
提示路径并支持一键替换（替换后需重启自己）。
同时提供「官方 workbuddy-switch 是否有新版」的旁路检查（它更新通常意味着有功能可跟）。
"""

import json
import os
import re
import time
import urllib.request

STORE_DIR = os.path.join(os.path.expanduser("~"), ".wb-switch")
EXTRAS_FILE = os.path.join(STORE_DIR, "memshift_extras.json")
NOTIFICATIONS_FILE = os.path.join(STORE_DIR, "notifications.json")

DEFAULT_UPDATE_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads"),
    STORE_DIR,
]

DEFAULTS = {
    "theme": "pixel",              # pixel / dark / light
    "proxy": "",                   # 形如 http://127.0.0.1:7890，空 = 直连
    "auto_close_client": True,     # 切换 WorkBuddy 时自动关 / 重开客户端
    "features": {                  # 侧栏「支持工具」开关
        "ide": True,
        "limits": True,
        "cli": True,
        "jetbrains": True,
        "checkin": True,
        "companion": False,        # 桌面伙伴（宠物栏）——2026-11-20 默认关闭：用户不要开机自动弹右上角落，设置里可随时开启
    },
    "update_dirs": DEFAULT_UPDATE_DIRS,
    "last_update_check": 0,
}


# ---------------------------------------------------------------- 配置


def load():
    data = {}
    try:
        with open(EXTRAS_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
    out = json.loads(json.dumps(DEFAULTS))
    for key, value in data.items():
        if key == "features" and isinstance(value, dict):
            out["features"].update({k: bool(v) for k, v in value.items()})
        elif key in out:
            out[key] = value
    return out


def save(patch):
    current = load()
    for key, value in (patch or {}).items():
        if key == "features" and isinstance(value, dict):
            current["features"].update({k: bool(v) for k, v in value.items()})
        elif key in DEFAULTS:
            current[key] = value
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = EXTRAS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EXTRAS_FILE)
    return current


# ---------------------------------------------------------------- 通知中心


def notifications(limit=30, level=None):
    """读官方通知流（只读），返回按时间倒序的最近条目。"""
    try:
        with open(NOTIFICATIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"items": [], "total": 0, "unreadHint": 0}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if level and item.get("level") != level:
            continue
        rows.append({
            "level": item.get("level") or "info",
            "title": item.get("title") or "",
            "description": item.get("description") or "",
            "at": item.get("at"),
            "atText": time.strftime("%m-%d %H:%M", time.localtime((item.get("at") or 0) / 1000))
            if item.get("at") else "-",
        })
    rows.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return {"items": rows[:limit], "total": len(rows)}


# ---------------------------------------------------------------- 版本比较


def parse_version(text):
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def compare_versions(a, b):
    """a > b → 1，a == b → 0，a < b → -1（不足位补 0）。"""
    pa, pb = parse_version(a), parse_version(b)
    length = max(len(pa), len(pb))
    pa = pa + (0,) * (length - len(pa))
    pb = pb + (0,) * (length - len(pb))
    return (pa > pb) - (pa < pb)


def _candidates(directories):
    out = []
    for directory in directories or []:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".exe"):
                continue
            if "memshift" not in name.lower():
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            version = None
            m = re.search(r"v?(\d+\.\d+\.\d+)", name)
            if m:
                version = m.group(1)
            out.append({
                "path": path,
                "name": name,
                "version": version,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            })
        for sub in names:
            sub_path = os.path.join(directory, sub)
            if os.path.isdir(sub_path) and "memshift" in sub.lower():
                exe = os.path.join(sub_path, "MemShift.exe")
                if os.path.isfile(exe):
                    stat = os.stat(exe)
                    out.append({"path": exe, "name": os.path.join(sub, "MemShift.exe"),
                                "version": None, "size": stat.st_size,
                                "mtime": int(stat.st_mtime * 1000)})
    return out


def check_update(current_version, current_exe=None, dirs=None):
    """在配置目录里找比当前更新的 MemShift 构建。"""
    config = load()
    directories = dirs if dirs is not None else config.get("update_dirs") or DEFAULT_UPDATE_DIRS
    current_exe = os.path.abspath(current_exe) if current_exe else None
    self_mtime = 0
    if current_exe and os.path.isfile(current_exe):
        self_mtime = int(os.stat(current_exe).st_mtime * 1000)

    found = []
    for item in _candidates(directories):
        if current_exe and os.path.abspath(item["path"]) == current_exe:
            continue
        newer = False
        if item["version"]:
            newer = compare_versions(item["version"], current_version) > 0
        if not newer and self_mtime:
            # 没版本号的文件名：只有「更新时间明显更新」才算候选（给 60 秒容差，避免同批打包误报）
            newer = item["mtime"] > self_mtime + 60_000
        if newer:
            found.append(item)
    found.sort(key=lambda x: (parse_version(x["version"] or "0"), x["mtime"]), reverse=True)

    config["last_update_check"] = int(time.time() * 1000)
    save({"last_update_check": config["last_update_check"]})
    return {
        "currentVersion": current_version,
        "currentExe": current_exe,
        "found": found,
        "count": len(found),
        "dirs": directories,
        "message": ("发现 %d 个更新的构建，最新：%s" % (len(found), found[0]["path"]))
        if found else "没有发现更新的构建（当前 %s）" % current_version,
    }


def check_upstream(timeout=6, proxy=None):
    """旁路：查官方 workbuddy-switch 最新 release（失败不影响本软件使用）。"""
    url = "https://api.github.com/repos/changexbc/workbuddy-switch/releases/latest"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "MemShift-UpdateCheck",
            "Accept": "application/vnd.github+json",
        })
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "tag": data.get("tag_name"),
            "name": data.get("name"),
            "publishedAt": data.get("published_at"),
            "url": data.get("html_url"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 自检


def selftest():
    rows = []
    cfg = load()
    rows.append(("配置可读", isinstance(cfg, dict) and "theme" in cfg, json.dumps(
        {"theme": cfg["theme"], "proxy": cfg["proxy"]}, ensure_ascii=False)))
    rows.append(("版本比较 1.2.0 > 1.1.9", compare_versions("1.2.0", "1.1.9") == 1, "ok"))
    rows.append(("版本比较 2.3.0 == 2.3", compare_versions("2.3.0", "2.3") == 0, "ok"))
    rows.append(("版本比较 v1.0 < 1.0.1", compare_versions("v1.0", "1.0.1") == -1, "ok"))
    try:
        result = check_update("0.0.1", None)
        rows.append(("更新检查可跑", isinstance(result.get("found"), list),
                     "候选 %d 个" % result.get("count", 0)))
    except Exception as e:
        rows.append(("更新检查可跑", False, str(e)))
    try:
        notes = notifications(limit=3)
        rows.append(("通知中心可读", isinstance(notes.get("items"), list),
                     "共 %d 条（显示 %d）" % (notes.get("total", 0), len(notes.get("items") or []))))
    except Exception as e:
        rows.append(("通知中心可读", False, str(e)))
    return all(r[1] for r in rows), rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
