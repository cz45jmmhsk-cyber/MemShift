# -*- coding: utf-8 -*-
"""客户端进程控制：检测 / 优雅关闭 / 等待退出 / 重新拉起（对齐 process.rs）。

为什么不用 tasklist 输出：中文 Windows 下 tasklist 走 GBK，进程名与路径解析容易踩编码坑；
这里直接调 Win32（EnumProcesses + QueryFullProcessImageNameW），拿到的都是 Unicode，
且能按**完整路径**区分「CodeBuddy」与「CodeBuddy CN」这类同名进程。

关闭口径（与官方一致）：
  1. `taskkill /PID <pid> /T`（不带 /F，等价 WM_CLOSE 优雅退出）
  2. 等 8 秒（且不超过总超时）
  3. 仍存活 → `taskkill /PID <pid> /T /F`
  4. 再等剩余时间；还有残留就报错，**不静默成功**
"""

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

KINDS = {
    "workbuddy": {"label": "WorkBuddy 客户端", "exe": "workbuddy.exe", "path_hint": None},
    "vscode": {"label": "VS Code", "exe": "code.exe", "path_hint": None},
    "ide_ai": {"label": "CodeBuddy 国际版 IDE", "exe": "codebuddy.exe", "path_hint": "codebuddy"},
    "ide_cn": {"label": "CodeBuddy 国内版 IDE", "exe": "codebuddy.exe", "path_hint": "codebuddy cn"},
}


# ---------------------------------------------------------------- 枚举


def _psapi():
    return ctypes.WinDLL("psapi.dll")


def _kernel32():
    return ctypes.WinDLL("kernel32.dll", use_last_error=True)


def list_processes():
    """枚举进程 → [{pid, name, path}]。非 Windows 返回空。"""
    if os.name != "nt":
        return []
    psapi, kernel32 = _psapi(), _kernel32()
    count = 1024
    while True:
        arr = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        used = needed.value // ctypes.sizeof(wintypes.DWORD)
        if used < count:
            break
        count *= 2
        if count > 65536:
            break
    out = []
    size = wintypes.DWORD(32768)
    for i in range(used):
        pid = arr[i]
        if pid <= 4:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            n = wintypes.DWORD(32768)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(n)):
                path = buf.value
                out.append({"pid": pid, "name": os.path.basename(path).lower(), "path": path})
        finally:
            kernel32.CloseHandle(handle)
    return out


def _matches(kind, proc):
    spec = KINDS[kind]
    if proc["name"] != spec["exe"]:
        return False
    hint = spec.get("path_hint")
    if hint and hint not in proc["path"].lower():
        return False
    return True


def find(kind):
    """返回该客户端当前运行的进程列表。"""
    return [p for p in list_processes() if _matches(kind, p)]


def is_running(kind):
    return bool(find(kind))


def is_any_running():
    return {k: is_running(k) for k in KINDS}


# ---------------------------------------------------------------- 关闭


def _taskkill(pid, force=False):
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(args, capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def wait_pids_gone(pids, timeout):
    """轮询等待指定 PID 全部消失，返回仍存活的 PID 列表。"""
    deadline = time.time() + max(1, timeout)
    alive = set(int(p) for p in pids)
    while time.time() < deadline:
        current = {p["pid"] for p in list_processes()}
        alive &= current
        if not alive:
            return []
        time.sleep(0.5)
    current = {p["pid"] for p in list_processes()}
    return sorted(alive & current)


def wait_gone(kind, timeout):
    if not is_running(kind):
        return True
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if not is_running(kind):
            return True
        time.sleep(0.5)
    return not is_running(kind)


def close(kind, timeout=20):
    """优雅关闭 → 超时强杀 → 确认消失。返回 (ok, message)。"""
    procs = find(kind)
    if not procs:
        return True, "%s 未在运行" % KINDS[kind]["label"]
    pids = [p["pid"] for p in procs]
    started = time.time()
    for pid in pids:
        _taskkill(pid, force=False)
    remaining = wait_pids_gone(pids, min(8, max(1, timeout)))
    if not remaining:
        return True, "%s 已关闭（%.1fs）" % (KINDS[kind]["label"], time.time() - started)
    for pid in remaining:
        _taskkill(pid, force=True)
    rest = max(1, timeout - (time.time() - started))
    leftover = wait_pids_gone(remaining, rest)
    if not leftover:
        return True, "%s 已强制关闭（%.1fs）" % (KINDS[kind]["label"], time.time() - started)
    return False, "%s 无法关闭（残留 PID: %s），请手动结束" % (
        KINDS[kind]["label"], ", ".join(str(p) for p in leftover))


# ---------------------------------------------------------------- 启动


def _exe_candidates(kind):
    local = os.environ.get("LOCALAPPDATA", "")
    prog = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    prog86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    if kind == "workbuddy":
        cands = []
        try:
            with open(os.path.join(os.path.expanduser("~"), ".wb-switch", "workbuddy_exe.json"),
                      "r", encoding="utf-8") as f:
                data = json.load(f)
            for value in data.values():
                if isinstance(value, str):
                    cands.append(value)
        except Exception:
            pass
        cands += [
            os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"),
            r"D:\WorkBuddy\WorkBuddy.exe",
            r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
        ]
        return cands
    if kind == "vscode":
        return [
            os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"),
            os.path.join(prog, "Microsoft VS Code", "Code.exe"),
            os.path.join(prog86, "Microsoft VS Code", "Code.exe"),
        ]
    if kind == "ide_ai":
        return [
            os.path.join(local, "Programs", "CodeBuddy", "CodeBuddy.exe"),
            os.path.join(prog, "CodeBuddy", "CodeBuddy.exe"),
        ]
    if kind == "ide_cn":
        return [
            os.path.join(local, "Programs", "CodeBuddy CN", "CodeBuddy.exe"),
            os.path.join(prog, "CodeBuddy CN", "CodeBuddy.exe"),
        ]
    return []


def exe_path(kind):
    """解析可执行文件路径：优先「正在运行的进程路径」，再按候选目录找。"""
    procs = find(kind)
    for p in procs:
        if os.path.isfile(p["path"]):
            return p["path"]
    for cand in _exe_candidates(kind):
        if cand and os.path.isfile(cand):
            return cand
    return None


def installed(kind):
    return exe_path(kind) is not None


def launch(kind):
    """启动客户端；失败返回可读错误。"""
    exe = exe_path(kind)
    if not exe:
        return False, "未找到 %s 程序，请先手动打开一次" % KINDS[kind]["label"]
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008)
    except Exception as e:
        return False, "启动 %s 失败：%s（路径 %s）" % (KINDS[kind]["label"], e, exe)
    return True, "已启动 %s" % KINDS[kind]["label"]


def close_then(kind, restart, timeout=20):
    """关闭（如需要）→ 执行回调 → 按需重开。回调抛错时不吞，返回 (ok, msg, extra)。"""
    was_running = is_running(kind)
    if was_running:
        ok, msg = close(kind, timeout)
        if not ok:
            return False, msg, {"closed": False, "restarted": False}
    if not restart:
        return True, "已关闭 %s（未要求重开）" % KINDS[kind]["label"], \
            {"closed": was_running, "restarted": False}
    ok, msg = launch(kind)
    return ok, msg, {"closed": was_running, "restarted": ok}


def selftest():
    """自检：进程枚举可用 + 各客户端安装状态（不改动任何进程）。"""
    rows = []
    procs = list_processes()
    rows.append(("进程枚举", len(procs) > 0, "共 %d 个进程" % len(procs)))
    for kind, spec in KINDS.items():
        path = exe_path(kind)
        rows.append(("%s 安装状态" % spec["label"], True,
                     path or "未安装（切换时给出提示，不影响其它功能）"))
    return all(r[1] for r in rows), rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
