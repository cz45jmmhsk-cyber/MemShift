# -*- coding: utf-8 -*-
"""模型限额台账（P1，对齐 limits.rs + rate_limit_events.rs）。

数据来源（两条，互补）：
  A. hook 事件流 `~/.wb-switch/hook-events.jsonl`（官方 hook 实时追加，秒级可见）
  B. 客户端日志 `~/.workbuddy/logs/<日期>/*.log`（含 .old.log；可以补到 hook 装之前的历史）

识别口径（与官方常量逐字一致）：
  限额文案  「超出频率限制」 / 「usage exceeds frequency limit」
  恢复句式  「将在 <YYYY-MM-DD HH:MM:SS> UTC+8 重置」 / 「will reset at <...> UTC±H」
  —— 取不到官方恢复时刻就不入账（绝不猜时间）。

账号归因：文案尾部的 `(requestId/sessionId)` → `sessions` 表 → `user_id` → 账号库 uid。
拿不到就丢弃（宁可少显示，不可显示错账号）。
"""

import datetime
import glob
import json
import os
import re
import sqlite3
import time

QUOTA_MARKERS = ("超出频率限制", "usage exceeds frequency limit")
RESET_MARKERS = ("将在 ", "will reset at ")
MODEL_SENTINELS = {"auto", "undefined", "null", ""}

WB_DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
WB_LOGS = os.path.join(os.path.expanduser("~"), ".workbuddy", "logs")
HOOK_EVENTS = os.path.join(os.path.expanduser("~"), ".wb-switch", "hook-events.jsonl")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".wb-switch", "memshift_rate_limits.json")

_RE_LOG_TS = re.compile(r"\[(\d{2})/(\d{2})/(\d{4}), (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\]")
_RE_PAIR = re.compile(r"\(([0-9a-fA-F]{32})/([0-9a-fA-F\-]{36})\)")
_RE_RESOLVED_MODEL = re.compile(r"resolved model=([A-Za-z0-9._\-]+)")
_RE_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_RE_UTC = re.compile(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?")


# ---------------------------------------------------------------- 解析


def parse_reset_at(text):
    """从文案里取官方重置时刻 → epoch 毫秒；取不到返回 None。"""
    for marker in RESET_MARKERS:
        index = text.find(marker)
        if index < 0:
            continue
        rest = text[index + len(marker):]
        m_date = _RE_DATE.match(rest)
        if not m_date:
            continue
        m_utc = _RE_UTC.search(rest)
        if not m_utc:
            continue
        year, month, day, hour, minute, second = (int(g) for g in m_date.groups())
        sign = 1 if m_utc.group(1) == "+" else -1
        offset_h = int(m_utc.group(2))
        offset_m = int(m_utc.group(3) or 0)
        try:
            naive = datetime.datetime(year, month, day, hour, minute, second)
            utc = naive - datetime.timedelta(minutes=sign * (offset_h * 60 + offset_m))
            return int(utc.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def parse_line_timestamp(line):
    """日志行首的 `[DD/MM/YYYY, HH:MM:SS.mmm]` → epoch 毫秒（本地时区）。"""
    m = _RE_LOG_TS.search(line)
    if not m:
        return 0
    day, month, year, hour, minute, second, ms = m.groups()
    try:
        dt = datetime.datetime(int(year), int(month), int(day),
                               int(hour), int(minute), int(second))
        return int(dt.timestamp() * 1000) + (int(ms.ljust(3, "0")) if ms else 0)
    except ValueError:
        return 0


def parse_pair(line):
    """`(requestId/sessionId)` → (request_id, session_id)。"""
    m = _RE_PAIR.search(line)
    return (m.group(1), m.group(2)) if m else (None, None)


def is_quota_text(text):
    return any(marker in text for marker in QUOTA_MARKERS)


def normalize_model(model):
    if not model:
        return None
    model = str(model).strip()
    if model.lower() in MODEL_SENTINELS:
        return None
    return model


# ---------------------------------------------------------------- 归因


class Attributor:
    """sessionId → 账号；一次扫描内缓存，避免反复查库。"""

    def __init__(self, accounts, db_path=WB_DB):
        self.accounts = accounts or []
        self.by_uid = {}
        for acc in self.accounts:
            uid = (acc.get("uid") or "").strip()
            if uid:
                self.by_uid[uid] = acc
        self.db_path = db_path
        self._cache = {}

    def account_of_session(self, session_id):
        if not session_id:
            return None, None
        if session_id in self._cache:
            return self._cache[session_id]
        user_id = None
        if os.path.isfile(self.db_path):
            try:
                con = sqlite3.connect("file:%s?mode=ro" % self.db_path.replace("\\", "/"), uri=True)
                try:
                    row = con.execute("SELECT user_id, model FROM sessions WHERE id = ?",
                                      (session_id,)).fetchone()
                finally:
                    con.close()
                if row:
                    user_id, model = row[0], row[1]
                    acc = self.by_uid.get((user_id or "").strip())
                    hit = (acc, model)
                    self._cache[session_id] = hit
                    return hit
            except sqlite3.Error:
                pass
        # 兜底：session id 可能就是账号 id 无关，无法归因 → 丢弃
        self._cache[session_id] = (None, None)
        return None, None


# ---------------------------------------------------------------- 采集


def scan_logs(attributor, days=7, max_bytes=40 * 1024 * 1024):
    """扫客户端日志，返回限额事件列表。"""
    if not os.path.isdir(WB_LOGS):
        return [], ["未找到日志目录：%s" % WB_LOGS]
    today = datetime.date.today()
    allowed = set()
    for i in range(max(1, days)):
        allowed.add((today - datetime.timedelta(days=i)).isoformat())

    events = []
    notes = []
    scanned = 0
    for date_dir in sorted(os.listdir(WB_LOGS)):
        dir_path = os.path.join(WB_LOGS, date_dir)
        if not os.path.isdir(dir_path) or date_dir not in allowed:
            continue
        for path in sorted(glob.glob(os.path.join(dir_path, "*.log"))):
            try:
                if os.path.getsize(path) > max_bytes:
                    notes.append("跳过超大日志：%s" % os.path.basename(path))
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    last_model = None
                    for line in f:
                        scanned += 1
                        m = _RE_RESOLVED_MODEL.search(line)
                        if m:
                            last_model = normalize_model(m.group(1))
                        if not is_quota_text(line):
                            continue
                        reset_at = parse_reset_at(line)
                        if not reset_at:
                            continue
                        _, session_id = parse_pair(line)
                        acc, session_model = attributor.account_of_session(session_id)
                        events.append({
                            "resetAt": reset_at,
                            "firstSeenAt": parse_line_timestamp(line) or int(time.time() * 1000),
                            "sessionId": session_id,
                            "model": normalize_model(session_model) or last_model,
                            "accountId": (acc or {}).get("id"),
                            "uid": (acc or {}).get("uid"),
                            "nickname": (acc or {}).get("nickname"),
                            "source": "log",
                        })
            except OSError as e:
                notes.append("读取失败 %s：%s" % (os.path.basename(path), e))
    return events, notes


def scan_hook_events(attributor):
    """消费 hook 事件流（只看新追加的行，offset 记在 state 里）。"""
    if not os.path.isfile(HOOK_EVENTS):
        return [], ["未找到 hook 事件流（%s）" % HOOK_EVENTS]
    state = load_state()
    offset = int(state.get("hookOffset") or 0)
    size = os.path.getsize(HOOK_EVENTS)
    if size < offset:  # 文件被轮转 / 截断
        offset = 0
    events = []
    consumed = offset
    try:
        with open(HOOK_EVENTS, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # 半行留到下一轮
                consumed += len(raw)
                text = raw.decode("utf-8", "replace")
                if not is_quota_text(text):
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                message = payload.get("last_assistant_message") or ""
                if not is_quota_text(message):
                    continue
                reset_at = parse_reset_at(message)
                if not reset_at:
                    continue
                _, session_id = parse_pair(message)
                session_id = session_id or payload.get("session_id")
                acc, session_model = attributor.account_of_session(session_id)
                events.append({
                    "resetAt": reset_at,
                    "firstSeenAt": int(time.time() * 1000),
                    "sessionId": session_id,
                    "model": normalize_model(payload.get("model")) or normalize_model(session_model),
                    "accountId": (acc or {}).get("id"),
                    "uid": (acc or {}).get("uid"),
                    "nickname": (acc or {}).get("nickname"),
                    "source": "hook",
                })
    except OSError as e:
        return [], ["读取 hook 事件失败：%s" % e]
    state["hookOffset"] = consumed
    save_state(state)
    return events, []


# ---------------------------------------------------------------- 状态


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def merge_events(old, new):
    """按 (账号, 模型, 恢复时刻) 合并；命中次数累加，保留最早发生时刻。"""
    index = {}
    for ev in (old or []):
        key = (ev.get("accountId"), ev.get("model"), ev.get("resetAt"))
        index[key] = dict(ev)
    for ev in (new or []):
        key = (ev.get("accountId"), ev.get("model"), ev.get("resetAt"))
        if key in index:
            index[key]["hitCount"] = int(index[key].get("hitCount") or 1) + 1
            index[key]["firstSeenAt"] = min(
                index[key].get("firstSeenAt") or ev["firstSeenAt"], ev["firstSeenAt"])
        else:
            item = dict(ev)
            item["hitCount"] = 1
            index[key] = item
    return list(index.values())


def collect(accounts, days=7, use_hook=True, use_logs=True, keep_days=3):
    """采集 + 合并 + 落盘，返回展示用报告。"""
    attributor = Attributor(accounts)
    state = load_state()
    stored = state.get("events") or []

    fresh, notes = [], []
    if use_hook:
        events, more = scan_hook_events(attributor)
        fresh.extend(events)
        notes.extend(more)
    if use_logs:
        events, more = scan_logs(attributor, days=days)
        fresh.extend(events)
        notes.extend(more)

    merged = merge_events(stored, fresh)
    now = int(time.time() * 1000)
    # 过期超过 keep_days 的条目清掉，避免无限增长
    cutoff = now - keep_days * 86400000
    merged = [e for e in merged if (e.get("resetAt") or 0) >= cutoff]
    merged.sort(key=lambda e: (e.get("resetAt") or 0))

    state["events"] = merged
    state["updatedAt"] = now
    save_state(state)

    active = [e for e in merged if (e.get("resetAt") or 0) > now]
    expired = [e for e in merged if (e.get("resetAt") or 0) <= now]
    for item in merged:
        item["active"] = (item.get("resetAt") or 0) > now
        item["remainingSec"] = max(0, int(((item.get("resetAt") or 0) - now) / 1000))
        item["resetAtText"] = time.strftime(
            "%m-%d %H:%M", time.localtime((item.get("resetAt") or 0) / 1000))
        if not item.get("nickname"):
            item["nickname"] = "未知账号"

    return {
        "now": now,
        "active": active,
        "expired": expired[:20],
        "total": len(merged),
        "newEvents": len(fresh),
        "notes": notes,
        "stateFile": STATE_FILE,
        "hookInstalled": os.path.isfile(HOOK_EVENTS),
        "logsDir": WB_LOGS,
    }


def summary_line(report):
    if not report.get("active"):
        return "当前无生效中的模型限额"
    parts = []
    for item in report["active"][:3]:
        parts.append("%s · %s → %s 恢复" % (
            item.get("nickname") or "未知账号",
            item.get("model") or "未知模型",
            item.get("resetAtText")))
    more = "" if len(report["active"]) <= 3 else "（另 %d 条）" % (len(report["active"]) - 3)
    return "；".join(parts) + more


# ---------------------------------------------------------------- 自检


def selftest():
    rows = []
    # 官方示例文案（本机 2026-11-19 20:00:43 实测行）
    line = ("[19/11/2026, 20:00:43.668] [Info] [pid=332] [Interruption] Catch block entered, "
            "error: 429 您的使用量已超出频率限制，将在 2026-11-19 22:16:24 UTC+8 重置，"
            "您也可以切换其他模型继续使用。 "
            "(01a1f3ae37e77e37a1df6b9a198a2b95/14d902b6-c071-4041-a39d-acc5fd1cb152)")
    reset = parse_reset_at(line)
    want = int(datetime.datetime(2026, 11, 19, 14, 16, 24,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)
    rows.append(("解析恢复时刻", reset == want, "%s vs %s" % (reset, want)))
    ts = parse_line_timestamp(line)
    rows.append(("解析行时间", ts > 0 and datetime.datetime.fromtimestamp(ts / 1000).year == 2026,
                 str(datetime.datetime.fromtimestamp(ts / 1000))))
    _, sid = parse_pair(line)
    rows.append(("解析会话 id", sid == "14d902b6-c071-4041-a39d-acc5fd1cb152", str(sid)))
    rows.append(("限额文案识别", is_quota_text(line), "ok"))
    rows.append(("英文文案识别", is_quota_text("usage exceeds frequency limit"), "ok"))
    rows.append(("无恢复时刻不入账",
                 parse_reset_at("超出频率限制，请稍后再试") is None, "ok"))

    # 真实环境：跑一次采集，确认不崩且有结构
    try:
        accounts = []
        try:
            with open(os.path.join(os.path.expanduser("~"), ".wb-switch", "accounts.json"),
                      encoding="utf-8") as f:
                data = json.load(f)
            accounts = data if isinstance(data, list) else data.get("accounts", [])
        except Exception:
            pass
        report = collect(accounts, days=3, use_hook=False)
        rows.append(("实机采集", isinstance(report.get("active"), list),
                     "有效 %d / 累计 %d" % (len(report["active"]), report["total"])))
    except Exception as e:
        rows.append(("实机采集", False, str(e)))
    return all(r[1] for r in rows), rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
