# -*- coding: utf-8 -*-
"""CodeBuddy 扩展会话树：列举 + 跨账号复制（对齐 vscode_session.rs）。

目录布局（两个数据仓共用）：
    <LOCALAPPDATA>/CodeBuddyExtension/Data/<uid>/<客户端>/<uid>/history/<工作区hash>/<会话id>/
        index.json          会话索引（messages[] / requests[]）
        .index_bak.json     扩展自用备份（可选）
        messages/<msgid>.json
        assets/...          附件

客户端段：
    VSCode        VS Code 里的 CodeBuddy 插件
    CodeBuddyIDE  CodeBuddy 桌面 IDE

两套复制策略（与官方一致，不是可调参数）：
    VSCODE_COPY  副本恒取新 id + 重映射消息/请求 id + 同步写工作区 .index_bak.json
    IDE_COPY     沿用源 id，仅冲突时重随机 + 不改目标 .index_bak.json
"""

import json
import os
import shutil
import time
import uuid

DATA_ROOT_NAME = "CodeBuddyExtension"

STORES = {
    "vscode": {
        "label": "VS Code 插件",
        "client_dir": "VSCode",
        "backup_kind": "vscode-sessions",
        "id_policy": "always_new",
        "write_ws_backup": True,
    },
    "ide": {
        "label": "CodeBuddy IDE",
        "client_dir": "CodeBuddyIDE",
        "backup_kind": "codebuddy-ide-sessions",
        "id_policy": "keep_unless_conflict",
        "write_ws_backup": False,
    },
}


# ---------------------------------------------------------------- 基础工具


def data_root_candidates():
    """扩展数据根候选：%LOCALAPPDATA%/<app>/Data 与 %LOCALAPPDATA%/<app>/Data（cache 兜底）。"""
    bases = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.path.expanduser("~/.cache"),
    ]
    out = []
    for base in bases:
        if not base:
            continue
        cand = os.path.join(base, DATA_ROOT_NAME, "Data")
        if cand not in out:
            out.append(cand)
    return out


def data_root():
    for cand in data_root_candidates():
        if os.path.isdir(cand):
            return cand
    return None


def uid_data_dir(root, client_dir, uid):
    return os.path.join(root, uid, client_dir, uid)


def history_root(root, client_dir, uid):
    return os.path.join(uid_data_dir(root, client_dir, uid), "history")


def is_safe_uid(uid):
    """非空、不含路径分隔符 / ..、不是 default / Public（杜绝路径穿越）。"""
    uid = (uid or "").strip()
    if not uid or uid in (".", ".."):
        return False
    if "/" in uid or "\\" in uid:
        return False
    if uid.lower() in ("default", "public"):
        return False
    return True


def is_hex32(text):
    text = text or ""
    return len(text) == 32 and all(c in "0123456789abcdef" for c in text)


def gen_hex32():
    return uuid.uuid4().hex


def unique_hex32(used):
    while True:
        cand = gen_hex32()
        if cand not in used:
            used.add(cand)
            return cand


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def dump(value):
    """等价 serde_json::Value::to_string()：紧凑、非 ASCII 原样。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def time_to_ms(value):
    """索引里的时间字段 → epoch 毫秒（RFC3339 字符串或数字秒/毫秒）。"""
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = time.strptime(text.replace("+00:00", "Z") if not text.endswith("Z") else text, fmt)
                return int(time.mktime(dt) * 1000)
            except Exception:
                continue
        try:  # 兜底：ISO 简化处理
            import datetime
            normalized = text.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(normalized)
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw if raw > 1_000_000_000_000 else raw * 1000
    return 0


def copy_dir_recursive(src, dst):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            copy_dir_recursive(s, d)
        elif os.path.isfile(s):
            shutil.copy2(s, d)


def remove_dir_if_exists(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def json_files(directory):
    out = []
    if not os.path.isdir(directory):
        return out
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            out.extend(json_files(path))
        elif name.lower().endswith(".json"):
            out.append(path)
    return out


# ---------------------------------------------------------------- id 重映射


def replace_exact_ids(value, mapping):
    """递归：恰好等于映射旧 id 的字符串 → 新 id。"""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [replace_exact_ids(item, mapping) for item in value]
    if isinstance(value, dict):
        return {k: replace_exact_ids(v, mapping) for k, v in value.items()}
    return value


def contains_exact_id(value, target):
    if isinstance(value, str):
        if value == target:
            return True
        try:
            inner = json.loads(value)
            return isinstance(inner, dict) and contains_exact_id(inner, target)
        except Exception:
            return False
    if isinstance(value, list):
        return any(contains_exact_id(item, target) for item in value)
    if isinstance(value, dict):
        return any(contains_exact_id(item, target) for item in value.values())
    return False


def replace_ids_in_extra(extra, mapping):
    """消息 extra：兼容字符串化 JSON 与对象两种形态。"""
    stringified = False
    if isinstance(extra, str):
        try:
            parsed = json.loads(extra)
        except Exception:
            return extra
        if not isinstance(parsed, dict):
            return extra
        obj = parsed
        stringified = True
    elif isinstance(extra, dict):
        obj = dict(extra)
    else:
        return extra
    obj = {k: replace_exact_ids(v, mapping) for k, v in obj.items()}
    return dump(obj) if stringified else obj


class RemapPlan:
    def __init__(self, message_ids, request_ids, message_total):
        self.message_ids = message_ids
        self.request_ids = request_ids
        self.message_total = message_total
        combined = dict(request_ids)
        combined.update(message_ids)  # messages 优先
        self.combined_ids = combined

    @classmethod
    def build(cls, source_index, source_dir):
        message_ids = {}
        request_ids = {}
        used = set()

        def add_message_id(old):
            old = (old or "").strip()
            if not old or old in message_ids:
                return
            message_ids[old] = unique_hex32(used)

        for msg in (source_index.get("messages") or []):
            if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                add_message_id(msg["id"])
        messages_dir = os.path.join(source_dir, "messages")
        if os.path.isdir(messages_dir):
            for name in os.listdir(messages_dir):
                if name.endswith(".json"):
                    stem = name[:-5]
                    if is_hex32(stem):
                        add_message_id(stem)
        for req in (source_index.get("requests") or []):
            if isinstance(req, dict) and isinstance(req.get("id"), str):
                rid = req["id"].strip()
                if rid and rid not in request_ids:
                    request_ids[rid] = unique_hex32(used)
        return cls(message_ids, request_ids, len(message_ids))


def remap_session_index(source_index, plan):
    out = json.loads(json.dumps(source_index))  # 深拷贝
    for msg in (out.get("messages") or []):
        if isinstance(msg, dict) and isinstance(msg.get("id"), str):
            new_id = plan.message_ids.get(msg["id"])
            if new_id:
                msg["id"] = new_id
    for req in (out.get("requests") or []):
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        if isinstance(rid, str) and rid in plan.request_ids:
            req["id"] = plan.request_ids[rid]
        msgs = req.get("messages")
        if isinstance(msgs, list):
            req["messages"] = [
                plan.message_ids.get(m, m) if isinstance(m, str) else m for m in msgs
            ]
    return out


def remap_message_file(text, old_stem, plan):
    try:
        value = json.loads(text)
    except Exception:
        return text
    if not isinstance(value, dict):
        return text
    new_id = plan.message_ids.get(old_stem)
    if new_id:
        value["id"] = new_id
    if "extra" in value:
        value["extra"] = replace_ids_in_extra(value["extra"], plan.combined_ids)
    return dump(value)


def remap_json_references(value, mapping):
    out = json.loads(json.dumps(value))
    if isinstance(out, dict):
        for key in list(out.keys()):
            out[key] = replace_exact_ids(out[key], mapping)
        if "extra" in out:
            out["extra"] = replace_ids_in_extra(out["extra"], mapping)
        return out
    return replace_exact_ids(out, mapping)


def rewrite_conversation_id_references(dest_dir, old_id, new_id):
    """改写副本内「恰好等于旧会话 id」的 JSON 引用；无引用的文件保持字节不变。"""
    mapping = {old_id: new_id}
    for path in json_files(dest_dir):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            value = json.loads(text)
        except Exception:
            continue
        if not contains_exact_id(value, old_id):
            continue
        try:
            atomic_write(path, dump(remap_json_references(value, mapping)))
        except Exception:
            pass


# ---------------------------------------------------------------- 会话写入


def write_conversation(source_dir, dest_dir, source_index, plan):
    """重映射写入：index.json + messages/*.json + 其余原样复制。"""
    os.makedirs(dest_dir, exist_ok=True)
    atomic_write(os.path.join(dest_dir, "index.json"), dump(remap_session_index(source_index, plan)))

    src_messages = os.path.join(source_dir, "messages")
    if os.path.isdir(src_messages):
        dst_messages = os.path.join(dest_dir, "messages")
        os.makedirs(dst_messages, exist_ok=True)
        for name in os.listdir(src_messages):
            path = os.path.join(src_messages, name)
            if not os.path.isfile(path):
                continue
            stem = name[:-5] if name.endswith(".json") else name
            if not is_hex32(stem):
                shutil.copy2(path, os.path.join(dst_messages, name))
                continue
            new_name = "%s.json" % plan.message_ids.get(stem, stem)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                atomic_write(os.path.join(dst_messages, new_name), remap_message_file(text, stem, plan))
            except Exception:
                shutil.copy2(path, os.path.join(dst_messages, new_name))

    src_bak = os.path.join(source_dir, ".index_bak.json")
    if os.path.isfile(src_bak):
        bak = read_json(src_bak)
        if bak is not None:
            atomic_write(os.path.join(dest_dir, ".index_bak.json"), dump(remap_session_index(bak, plan)))
        else:
            shutil.copy2(src_bak, os.path.join(dest_dir, ".index_bak.json"))

    for name in os.listdir(source_dir):
        if name in ("index.json", ".index_bak.json", "messages"):
            continue
        src = os.path.join(source_dir, name)
        dst = os.path.join(dest_dir, name)
        if os.path.isdir(src):
            copy_dir_recursive(src, dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)


def copy_conversation_keep(source_dir, dest_dir, old_id, new_id):
    """沿用 id 路径：整目录字节复制，仅 id 变更时改写副本内引用。"""
    copy_dir_recursive(source_dir, dest_dir)
    if old_id != new_id:
        rewrite_conversation_id_references(dest_dir, old_id, new_id)


# ---------------------------------------------------------------- 工作区索引


def find_conversation(index, conversation_id):
    for entry in (index.get("conversations") or []):
        if isinstance(entry, dict) and entry.get("id") == conversation_id:
            return entry
    return None


def conversation_entry(source_entry, new_id):
    if isinstance(source_entry, dict):
        entry = dict(source_entry)
        entry["id"] = new_id
        return entry
    return {"id": new_id, "type": "craft", "name": "",
            "createdAt": None, "lastMessageAt": None}


def merge_workspace_index(index, entry, new_id):
    """并入会话条目；顺带保证 current 指向索引内真实存在的会话。"""
    if not isinstance(index, dict):
        index = {}
        return index
    conversations = index.get("conversations")
    if not isinstance(conversations, list):
        conversations = []
        index["conversations"] = conversations
    conversations.append(entry)
    current = index.get("current")
    current_ok = isinstance(current, str) and any(
        isinstance(item, dict) and item.get("id") == current for item in conversations
    )
    if not current_ok:
        index["current"] = new_id
    return index


def conversation_exists(target_ws_dir, index, conversation_id):
    if os.path.isdir(os.path.join(target_ws_dir, conversation_id)):
        return True
    return find_conversation(index, conversation_id) is not None


def used_conversation_ids(target_ws_dir, index):
    used = set()
    for entry in (index.get("conversations") or []):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            used.add(entry["id"].strip())
    if os.path.isdir(target_ws_dir):
        used.update(os.listdir(target_ws_dir))
    return used


class WorkspaceState:
    """目标工作区：磁盘备份 + 内存工作副本，支持逐条回退。"""

    def __init__(self, directory, original, current, backup_dir):
        self.dir = directory
        self.original = original
        self.current = current
        self.backup_dir = backup_dir

    @classmethod
    def load(cls, directory, backup_root, workspace_hash):
        index_path = os.path.join(directory, "index.json")
        original = None
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                original = f.read()
        bak_path = os.path.join(directory, ".index_bak.json")
        original_bak = None
        if os.path.isfile(bak_path):
            with open(bak_path, "rb") as f:
                original_bak = f.read()

        workspace_backup = os.path.join(backup_root, workspace_hash)
        if original is not None or original_bak is not None:
            os.makedirs(workspace_backup, exist_ok=True)
            if original is not None:
                with open(os.path.join(workspace_backup, "index.json"), "wb") as f:
                    f.write(original)
            if original_bak is not None:
                with open(os.path.join(workspace_backup, ".index_bak.json"), "wb") as f:
                    f.write(original_bak)

        current = {}
        if original is not None:
            try:
                current = json.loads(original.decode("utf-8"))
            except Exception:
                current = {}
        return cls(directory, original, current, workspace_backup)

    def persist(self, write_backup):
        os.makedirs(self.dir, exist_ok=True)
        text = dump(self.current)
        atomic_write(os.path.join(self.dir, "index.json"), text)
        if write_backup:
            atomic_write(os.path.join(self.dir, ".index_bak.json"), text)

    def restore(self):
        idx = os.path.join(self.dir, "index.json")
        bak = os.path.join(self.dir, ".index_bak.json")
        if self.original is not None:
            with open(idx, "wb") as f:
                f.write(self.original)
            backup_bak = os.path.join(self.backup_dir, ".index_bak.json")
            if os.path.isfile(backup_bak):
                shutil.copy2(backup_bak, bak)
        else:
            for path in (idx, bak):
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


# ---------------------------------------------------------------- 列举


def list_sessions(store, root, uid):
    spec = STORES[store]
    sessions = []
    skipped = 0
    if not is_safe_uid(uid):
        return {"sourceUid": uid, "sessions": [], "skipped": 0, "dataRoot": root}
    history = history_root(root, spec["client_dir"], uid)
    if os.path.isdir(history):
        for workspace_hash in os.listdir(history):
            ws_dir = os.path.join(history, workspace_hash)
            if not os.path.isdir(ws_dir):
                continue
            index = read_json(os.path.join(ws_dir, "index.json"))
            if not isinstance(index, dict) or not isinstance(index.get("conversations"), list):
                skipped += 1
                continue
            for conv in index["conversations"]:
                if not isinstance(conv, dict):
                    continue
                cid = (conv.get("id") or "").strip()
                if not cid:
                    continue
                title = (conv.get("name") or "").strip() or "(无标题)"
                last = time_to_ms(conv.get("lastMessageAt"))
                updated = last if last > 0 else time_to_ms(conv.get("createdAt"))
                sessions.append({
                    "id": cid,
                    "workspaceHash": workspace_hash,
                    "title": title,
                    "updatedAt": updated,
                    "type": conv.get("type") or "",
                    "hasHistory": conversation_has_history(ws_dir, cid),
                })
    sessions.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    return {"sourceUid": uid, "sessions": sessions, "skipped": skipped, "dataRoot": root}


def conversation_has_history(ws_dir, conversation_id):
    conv_dir = os.path.join(ws_dir, conversation_id)
    index = read_json(os.path.join(conv_dir, "index.json"))
    if isinstance(index, dict):
        messages = index.get("messages")
        if isinstance(messages, list) and messages:
            return True
    messages_dir = os.path.join(conv_dir, "messages")
    if os.path.isdir(messages_dir):
        return any(os.path.isfile(os.path.join(messages_dir, n)) for n in os.listdir(messages_dir))
    return False


# ---------------------------------------------------------------- 复制


def copy_sessions(store, root, backup_root, source_uid, target_uid, items):
    """把 items（[{workspaceHash, conversationId}]）复制到目标账号；逐条隔离。"""
    spec = STORES[store]
    if not is_safe_uid(source_uid):
        raise ValueError("源账号 uid 非法")
    if not is_safe_uid(target_uid):
        raise ValueError("目标账号 uid 非法")
    if source_uid == target_uid:
        raise ValueError("源账号与目标账号相同，无需复制会话")

    source_history = history_root(root, spec["client_dir"], source_uid)
    target_history = history_root(root, spec["client_dir"], target_uid)

    # 目标工作区去重（只留合法 hex，避免建出无关目录）
    workspaces = {i.get("workspaceHash", "").strip() for i in items}
    states = {}
    for ws in sorted(w for w in workspaces if is_hex32(w)):
        states[ws] = WorkspaceState.load(
            os.path.join(target_history, ws), backup_root, ws
        )

    copied, errors = [], []
    for item in items:
        ws = (item.get("workspaceHash") or "").strip()
        cid = (item.get("conversationId") or "").strip()
        if not is_hex32(ws) or not is_hex32(cid):
            errors.append({"workspaceHash": ws, "conversationId": cid, "error": "工作区或会话 id 非法"})
            continue
        source_dir = os.path.join(source_history, ws, cid)
        if not os.path.isdir(source_dir):
            errors.append({"workspaceHash": ws, "conversationId": cid, "error": "源会话目录不存在"})
            continue
        state = states.get(ws)
        if state is None:
            errors.append({"workspaceHash": ws, "conversationId": cid, "error": "工作区状态缺失"})
            continue
        try:
            copied.append(_copy_one(spec, source_dir, os.path.join(target_history, ws),
                                    ws, cid, state))
        except Exception as e:
            errors.append({"workspaceHash": ws, "conversationId": cid, "error": str(e)})

    report = {
        "sourceUid": source_uid,
        "targetUid": target_uid,
        "copied": copied,
        "backup": backup_root,
    }
    if errors:
        report["errors"] = errors
    return report


def _copy_one(spec, source_dir, target_ws_dir, workspace_hash, conversation_id, state):
    source_index = read_json(os.path.join(source_dir, "index.json"))
    if not isinstance(source_index, dict):
        raise ValueError("源会话 index.json 缺失或损坏")

    always_new = spec["id_policy"] == "always_new"
    plan = RemapPlan.build(source_index, source_dir) if always_new else None

    if always_new:
        new_cid = gen_hex32()
    elif conversation_exists(target_ws_dir, state.current, conversation_id):
        new_cid = unique_hex32(used_conversation_ids(target_ws_dir, state.current))
    else:
        new_cid = conversation_id

    tmp_dir = os.path.join(target_ws_dir, ".tmp-%s" % new_cid)
    remove_dir_if_exists(tmp_dir)
    try:
        if plan is not None:
            write_conversation(source_dir, tmp_dir, source_index, plan)
        else:
            copy_conversation_keep(source_dir, tmp_dir, conversation_id, new_cid)
    except Exception as e:
        remove_dir_if_exists(tmp_dir)
        raise ValueError("写入临时会话目录失败：%s" % e)

    final_dir = os.path.join(target_ws_dir, new_cid)
    remove_dir_if_exists(final_dir)
    try:
        os.makedirs(target_ws_dir, exist_ok=True)
        os.rename(tmp_dir, final_dir)
    except Exception as e:
        remove_dir_if_exists(tmp_dir)
        raise ValueError("提交会话目录失败：%s" % e)

    source_ws_index = read_json(os.path.join(os.path.dirname(source_dir), "index.json"))
    source_entry = find_conversation(source_ws_index, conversation_id) if isinstance(source_ws_index, dict) else None
    entry = conversation_entry(source_entry, new_cid)

    before = json.loads(json.dumps(state.current))
    state.current = merge_workspace_index(state.current, entry, new_cid)
    try:
        state.persist(spec["write_ws_backup"])
    except Exception as e:
        state.current = before
        try:
            state.persist(spec["write_ws_backup"])
        except Exception:
            state.restore()
        remove_dir_if_exists(final_dir)
        raise ValueError("合并工作区索引失败：%s" % e)

    messages = plan.message_total if plan is not None else len(source_index.get("messages") or [])
    return {
        "workspaceHash": workspace_hash,
        "oldId": conversation_id,
        "newId": new_cid,
        "messages": messages,
    }


# ---------------------------------------------------------------- 自检


def selftest(base_dir=None):
    """fixture 自检：复制策略、id 重映射、索引合并、冲突重随机。"""
    import tempfile

    rows = []
    ok = True

    def check(name, passed, info=""):
        nonlocal ok
        rows.append((name, passed, info))
        ok = ok and passed

    if base_dir is None:
        # 临时目录：优先跟随备份根所在盘，失败退回系统临时目录
        tmp_base = None
        try:
            import wb_switch_pro as _w
            tmp_base = os.path.join(os.path.dirname(_w.BACKUP_ROOT), "_tmp")
            os.makedirs(tmp_base, exist_ok=True)
        except Exception:
            tmp_base = None
        base = tempfile.mkdtemp(prefix="memshift_ext_test_", dir=tmp_base)
    else:
        base = base_dir
    root = os.path.join(base, "Data")
    ws = "0123456789abcdef0123456789abcdef"
    conv_old = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    conv_existing = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    msg1 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    req1 = "dddddddddddddddddddddddddddddddd"
    src_uid, dst_uid = "uid-src-0001", "uid-dst-0002"

    def make_source():
        h = history_root(root, "VSCode", src_uid)
        d = os.path.join(h, ws, conv_old)
        os.makedirs(os.path.join(d, "messages"), exist_ok=True)
        os.makedirs(os.path.join(d, "assets"), exist_ok=True)
        with open(os.path.join(d, "assets", "图片.1.png"), "wb") as f:
            f.write(b"\x89PNG\r\n")
        conv_index = {
            "id": conv_old,
            "messages": [{"id": msg1, "role": "user"}],
            "requests": [{"id": req1, "messages": [msg1]}],
        }
        with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as f:
            json.dump(conv_index, f, ensure_ascii=False)
        msg_payload = {"id": msg1, "text": "你好", "extra": {"requestId": req1, "modelId": "hy3"}}
        with open(os.path.join(d, "messages", msg1 + ".json"), "w", encoding="utf-8") as f:
            json.dump(msg_payload, f, ensure_ascii=False)
        ws_index = {"current": conv_old, "conversations": [{"id": conv_old, "name": "会话A", "type": "craft"}]}
        with open(os.path.join(h, ws, "index.json"), "w", encoding="utf-8") as f:
            json.dump(ws_index, f, ensure_ascii=False)

    # --- 场景 1：VS Code 策略（恒新 id + 重映射 + 写 target .index_bak）
    make_source()
    report = copy_sessions("vscode", root, os.path.join(base, "backup"), src_uid, dst_uid,
                           [{"workspaceHash": ws, "conversationId": conv_old}])
    check("复制成功（无 errors）", not report.get("errors"), str(report.get("errors")))
    new_id = report["copied"][0]["newId"] if report.get("copied") else ""
    check("副本取新 id", new_id and new_id != conv_old, new_id)
    dst_conv = os.path.join(history_root(root, "VSCode", dst_uid), ws, new_id)
    check("副本目录落盘", os.path.isdir(dst_conv))
    dst_index = read_json(os.path.join(dst_conv, "index.json")) or {}
    new_msg = dst_index.get("messages", [{}])[0].get("id", "")
    check("消息 id 已重映射", new_msg and new_msg != msg1, new_msg)
    check("消息文件已改名", os.path.isfile(os.path.join(dst_conv, "messages", new_msg + ".json")))
    msg_body = read_json(os.path.join(dst_conv, "messages", new_msg + ".json")) or {}
    check("extra.requestId 已重映射",
          msg_body.get("extra", {}).get("requestId") not in (req1, None),
          str(msg_body.get("extra", {}).get("requestId")))
    check("extra.modelId 未被误改", msg_body.get("extra", {}).get("modelId") == "hy3")
    check("附件中文名保留", os.path.isfile(os.path.join(dst_conv, "assets", "图片.1.png")))
    dst_ws_index = read_json(os.path.join(history_root(root, "VSCode", dst_uid), ws, "index.json")) or {}
    check("目标工作区索引已并入",
          any(c.get("id") == new_id for c in dst_ws_index.get("conversations", [])))
    check("current 有效", dst_ws_index.get("current") == new_id)
    check("VS Code 侧写 .index_bak",
          os.path.isfile(os.path.join(history_root(root, "VSCode", dst_uid), ws, ".index_bak.json")))

    # --- 场景 2：同源同目标再次复制 → 再取另一个新 id（不覆盖）
    report2 = copy_sessions("vscode", root, os.path.join(base, "backup"), src_uid, dst_uid,
                            [{"workspaceHash": ws, "conversationId": conv_old}])
    new_id2 = report2["copied"][0]["newId"] if report2.get("copied") else ""
    check("重复复制取不同 id", new_id2 and new_id2 != new_id, new_id2)
    check("首次副本仍在", os.path.isdir(dst_conv))

    # --- 场景 3：IDE 策略（沿用 id；冲突才重随机；不写 .index_bak）
    root2 = os.path.join(base, "Data2")
    h2 = history_root(root2, "CodeBuddyIDE", src_uid)
    d2 = os.path.join(h2, ws, conv_old)
    os.makedirs(os.path.join(d2, "messages"), exist_ok=True)
    with open(os.path.join(d2, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"id": conv_old, "messages": [{"id": msg1}]}, f, ensure_ascii=False)
    with open(os.path.join(d2, "messages", msg1 + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": msg1, "text": "x"}, f, ensure_ascii=False)
    with open(os.path.join(h2, ws, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"current": conv_old, "conversations": [{"id": conv_old, "name": "A"}]},
                  f, ensure_ascii=False)
    r3 = copy_sessions("ide", root2, os.path.join(base, "backup2"), src_uid, dst_uid,
                       [{"workspaceHash": ws, "conversationId": conv_old}])
    keep_id = r3["copied"][0]["newId"] if r3.get("copied") else ""
    check("IDE 沿用源 id", keep_id == conv_old, keep_id)
    check("IDE 字节复制消息文件",
          os.path.isfile(os.path.join(history_root(root2, "CodeBuddyIDE", dst_uid), ws, conv_old,
                                      "messages", msg1 + ".json")))
    check("IDE 不写 .index_bak",
          not os.path.isfile(os.path.join(history_root(root2, "CodeBuddyIDE", dst_uid), ws,
                                          ".index_bak.json")))
    r4 = copy_sessions("ide", root2, os.path.join(base, "backup2"), src_uid, dst_uid,
                       [{"workspaceHash": ws, "conversationId": conv_old}])
    conflict_id = r4["copied"][0]["newId"] if r4.get("copied") else ""
    check("IDE 冲突时重随机", conflict_id and conflict_id != conv_old, conflict_id)

    # --- 场景 4：非法 uid / 非法 id 拒绝
    try:
        copy_sessions("vscode", root, base, "../evil", dst_uid, [])
        check("非法 uid 被拒", False, "未抛异常")
    except ValueError:
        check("非法 uid 被拒", True)
    bad = copy_sessions("vscode", root, base, src_uid, dst_uid,
                        [{"workspaceHash": "zz", "conversationId": msg1}])
    check("非法 id 记入 errors", bool(bad.get("errors")), str(bad.get("errors")))

    if base_dir is None:
        shutil.rmtree(base, ignore_errors=True)
    return ok, rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
