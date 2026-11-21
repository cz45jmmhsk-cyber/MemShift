# -*- coding: utf-8 -*-
"""Windows Chromium Safe Storage：DPAPI 取密钥 + AES-256-GCM v10 加解密 + state.vscdb 读写。

对齐 workbuddy-switch 的 `vscode_cn_inject.rs`：
  密钥 = base64(Local State → os_crypt.encrypted_key)，去掉 `DPAPI` 5 字节前缀，
         再走 CryptUnprotectData 解出 32 字节 AES-256 key（当前用户绑定）。
  密文 = b"v10" + nonce(12) + AES-256-GCM(明文) + tag(16)
  落库 = sqlite 表 ItemTable(key TEXT PRIMARY KEY, value TEXT)
         value = {"type":"Buffer","data":[<密文字节>...]}

三个目标（扩展 ID 相同、key 与数据目录不同）：
  VS Code 插件      %APPDATA%/Code        planning-genie.new.accessToken   载荷 id Tencent-Cloud.coding-copilot
  CodeBuddy IDE 国际 %APPDATA%/CodeBuddy    planning-genie.new.accessToken   载荷 id Tencent-Cloud.genie-ide
  CodeBuddy IDE 国内 %APPDATA%/CodeBuddy CN planning-genie.new.accessTokencn 载荷 id Tencent-Cloud.genie-ide
"""

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import time
from ctypes import wintypes

from . import aesgcm

V10 = b"v10"
EXTENSION_ID = "tencent-cloud.coding-copilot"

# 目标描述符（与 Rust 侧 VscodeSafeStorageTarget 一一对应）
TARGETS = {
    "vscode": {
        "label": "VS Code 插件",
        "dir": os.path.join(os.environ.get("APPDATA", ""), "Code"),
        "ext_id": EXTENSION_ID,
        "secret_key": "Tencent-Cloud.coding-copilot.new.accessToken",
        "payload_id": "Tencent-Cloud.coding-copilot",
    },
    "ide_ai": {
        "label": "CodeBuddy 国际版 IDE",
        "dir": os.path.join(os.environ.get("APPDATA", ""), "CodeBuddy"),
        "ext_id": EXTENSION_ID,
        "secret_key": "planning-genie.new.accessToken",
        "payload_id": "Tencent-Cloud.genie-ide",
    },
    "ide_cn": {
        "label": "CodeBuddy 国内版 IDE",
        "dir": os.path.join(os.environ.get("APPDATA", ""), "CodeBuddy CN"),
        "ext_id": EXTENSION_ID,
        "secret_key": "planning-genie.new.accessTokencn",
        "payload_id": "Tencent-Cloud.genie-ide",
    },
}


# ---------------------------------------------------------------- DPAPI


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_decrypt(encrypted):
    """CryptUnprotectData：解当前用户绑定的 DPAPI 密文。"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅 Windows 可用")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob = _blob(encrypted)
    out_blob = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        raise RuntimeError("DPAPI CryptUnprotectData 失败（错误码 %s）" % (err or kernel32.GetLastError()))
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ---------------------------------------------------------------- 密钥


def local_state_path(data_root):
    return os.path.join(data_root, "Local State")


def get_windows_encryption_key(data_root):
    """从 Local State 取 AES-256 key（DPAPI 解 encrypted_key）。"""
    path = local_state_path(data_root)
    if not os.path.isfile(path):
        raise FileNotFoundError("未找到 Local State: %s" % path)
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    b64 = (state.get("os_crypt") or {}).get("encrypted_key")
    if not b64:
        raise ValueError("Local State 缺少 os_crypt.encrypted_key")
    raw = base64.b64decode(b64)
    if len(raw) < 6:
        raise ValueError("encrypted_key 数据过短")
    if raw[:5] != b"DPAPI":
        raise ValueError("encrypted_key 前缀不是 DPAPI，实际为 %r" % raw[:5])
    key = dpapi_decrypt(raw[5:])
    if len(key) != 32:
        raise ValueError("解出的密钥长度异常：%d（期望 32）" % len(key))
    return key


# ---------------------------------------------------------------- v10 加解密


def encrypt_v10(key, plaintext):
    nonce = os.urandom(12)
    return V10 + nonce + aesgcm.gcm_encrypt(key, nonce, bytes(plaintext))


def decrypt_v10(key, blob):
    if len(blob) < 3 + 12 + 16:
        raise ValueError("密文过短")
    if blob[:3] != V10:
        if blob[:3] == b"v11":
            raise ValueError("检测到 v11 前缀（Linux keyring 口径），当前平台不支持")
        if blob[:3] == b"v20":
            raise ValueError(
                "检测到 v20 前缀：VS Code/Chromium 127+ 启用了 app-bound 加密，"
                "官方工具同样只支持 v10，暂无法写入"
            )
        raise ValueError("未知密文前缀：%r" % blob[:3])
    return aesgcm.gcm_decrypt(key, blob[3:15], blob[15:])


# ---------------------------------------------------------------- 路径


def secret_item_key(target):
    spec = TARGETS[target] if isinstance(target, str) else target
    return 'secret://{"extensionId":"%s","key":"%s"}' % (spec["ext_id"], spec["secret_key"])


def _db_candidates(data_root):
    return [
        os.path.join(data_root, "User", "globalStorage", "state.vscdb"),
        os.path.join(data_root, "globalStorage", "state.vscdb"),
        os.path.join(data_root, "state.vscdb"),
    ]


def find_existing_db(data_root):
    """只挑已存在的 state.vscdb；不创建任何目录（只读探测用）。"""
    for p in _db_candidates(data_root):
        if os.path.exists(p):
            return p
    return None


def resolve_db(data_root, create=True):
    """优先已存在；否则返回首选路径（create=True 时建父目录）。"""
    hit = find_existing_db(data_root)
    if hit:
        return hit
    preferred = _db_candidates(data_root)[0]
    if create:
        os.makedirs(os.path.dirname(preferred), exist_ok=True)
    return preferred


def data_root_from_db(db_path):
    return os.path.dirname(os.path.dirname(os.path.dirname(db_path)))


# ---------------------------------------------------------------- 读写


def _decode_value(raw, data_root):
    """value 可能是显式明文串，也可能是 {"type":"Buffer","data":[...]} 密文。"""
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    if isinstance(parsed, dict) and "data" in parsed:
        data = parsed.get("data")
        if not isinstance(data, list):
            raise ValueError("secret Buffer 格式错误：data 不是数组")
        blob = bytes(int(x) & 0xFF for x in data)
        return decrypt_v10(get_windows_encryption_key(data_root), blob).decode("utf-8")
    if isinstance(parsed, str):
        return parsed
    return raw


def read_secret(target, data_root=None):
    """读并解密 secret → 明文 JSON 字符串；无该行返回 None。只读，不建目录。"""
    spec = TARGETS[target]
    root = data_root or spec["dir"]
    db = find_existing_db(root)
    if not db:
        return None
    if not os.path.isfile(db):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (secret_item_key(spec),)
            ).fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return None
            raise
        if not row:
            return None
        raw = row[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return _decode_value(raw, data_root_from_db(db))
    finally:
        conn.close()


def has_secret_row(target, data_root=None):
    spec = TARGETS[target]
    root = data_root or spec["dir"]
    db = find_existing_db(root)
    if not db or not os.path.isfile(db):
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    except sqlite3.Error:
        return False
    try:
        conn.execute(
            "SELECT 1 FROM ItemTable WHERE key = ? LIMIT 1", (secret_item_key(spec),)
        ).fetchone()
        return True
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return False
        raise
    finally:
        conn.close()
    return False


def write_secret(target, plaintext, data_root=None, backup_dir=None):
    """加密并写入 secret；返回 db 路径。写入前需确保目标客户端已退出。"""
    spec = TARGETS[target]
    root = data_root or spec["dir"]
    db = resolve_db(root, create=True)
    os.makedirs(os.path.dirname(db), exist_ok=True)

    # 写前备份（官方无此步，属我们的保险层；失败不阻塞）
    if backup_dir and os.path.isfile(db):
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            tag = os.path.basename(root.rstrip("\\/")) or "db"
            shutil.copy2(db, os.path.join(backup_dir, "state_%s_%s.vscdb" % (tag, stamp)))
        except Exception:
            pass

    key = get_windows_encryption_key(root)
    blob = encrypt_v10(key, plaintext.encode("utf-8"))
    value = json.dumps({"type": "Buffer", "data": list(blob)})

    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (secret_item_key(spec), value),
        )
        conn.commit()
        # 写后校验：行存在且是 Buffer
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (secret_item_key(spec),)
        ).fetchone()
        if not row:
            raise RuntimeError("写后校验失败：secret 行不存在")
        parsed = json.loads(row[0])
        if parsed.get("type") != "Buffer":
            raise RuntimeError("写后校验失败：value 不是 Buffer")
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------- 载荷构造


def build_vscode_session_json(acc, existing=None):
    """VS Code 插件载荷（merge 既有私有键；accounts[] 按 uid upsert）。"""
    uid = acc.get("uid") or ""
    tok = _token(acc)
    rt = _token(acc, "refresh_token")
    domain = "www.codebuddy.cn" if acc.get("variant", "cn") != "ai" else "www.workbuddy.ai"
    now_ms = int(time.time() * 1000)
    expires_at = acc.get("expiresAt") or now_ms + 30 * 86400000

    root = {}
    if existing:
        try:
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                root = loaded
        except Exception:
            root = {}

    root["id"] = TARGETS["vscode"]["payload_id"]
    root["token"] = tok
    if rt:
        root["refreshToken"] = rt
    root["expiresAt"] = expires_at
    root["domain"] = domain
    root["accessToken"] = "%s+%s" % (uid, tok) if uid else tok

    acct = {
        "id": uid or acc.get("id"),
        "uid": uid,
        "label": acc.get("nickname"),
        "nickname": acc.get("nickname"),
        "enterpriseId": acc.get("enterpriseId"),
        "enterpriseName": acc.get("enterpriseName"),
        "pluginEnabled": True,
        "lastLogin": True,
    }
    old_acct = root.get("account") if isinstance(root.get("account"), dict) else {}
    for k, v in old_acct.items():
        if k not in acct:
            acct[k] = v
    root["account"] = acct

    accs = [a for a in (root.get("accounts") or []) if isinstance(a, dict)]
    hit = next((a for a in accs if a.get("uid") == uid), None)
    if hit is not None:
        accs[accs.index(hit)] = acct
    else:
        accs.append(acct)
    for a in accs:
        if a.get("uid") != uid:
            a["lastLogin"] = False
    root["accounts"] = accs

    auth = root.get("auth") if isinstance(root.get("auth"), dict) else {}
    auth["accessToken"] = tok
    auth["refreshToken"] = rt or auth.get("refreshToken", "")
    auth["tokenType"] = acc.get("token_type") or "Bearer"
    auth["domain"] = domain
    auth["expiresAt"] = expires_at
    auth["expiresIn"] = expires_at
    auth["refreshExpiresIn"] = 0
    if acc.get("refreshExpiresAt") is not None:
        auth["refreshExpiresAt"] = acc["refreshExpiresAt"]
    elif "refreshExpiresAt" not in auth:
        auth["refreshExpiresAt"] = 0
    auth["lastRefreshTime"] = now_ms
    root["auth"] = auth
    return json.dumps(root, ensure_ascii=False)


def build_ide_session_json(acc, flavor="ide_ai"):
    """CodeBuddy IDE 载荷（与官方 build_session_json 同结构，整体覆盖）。"""
    uid = acc.get("uid") or ""
    tok = _token(acc)
    rt = _token(acc, "refresh_token")
    domain = "www.codebuddy.cn" if acc.get("variant", "cn") != "ai" else "www.workbuddy.ai"
    now_ms = int(time.time() * 1000)
    expires_at = acc.get("expiresAt") or 0
    return json.dumps({
        "id": TARGETS[flavor]["payload_id"],
        "token": tok,
        "refreshToken": rt,
        "expiresAt": expires_at,
        "domain": domain,
        "accessToken": "%s+%s" % (uid, tok) if uid else tok,
        "converted": True,
        "account": {
            "id": uid,
            "uid": uid,
            "label": acc.get("nickname") or "",
            "nickname": acc.get("nickname") or "",
            "enterpriseId": acc.get("enterpriseId") or "",
            "enterpriseName": acc.get("enterpriseName") or "",
            "pluginEnabled": True,
            "lastLogin": True,
        },
        "auth": {
            "accessToken": tok,
            "refreshToken": rt,
            "tokenType": acc.get("token_type") or "Bearer",
            "domain": domain,
            "expiresAt": expires_at,
            "expiresIn": expires_at,
            "refreshExpiresIn": 0,
            "refreshExpiresAt": 0,
            "lastRefreshTime": now_ms,
        },
    }, ensure_ascii=False)


def _token(acc, key="access_token"):
    value = acc.get(key)
    if isinstance(value, dict):
        return ""
    text = (value or "").strip()
    if text.startswith("Bearer "):
        text = text[7:].strip()
    return text


def parse_identity(secret_text):
    """从 secret 明文里取 (uid, token)，用于「检测当前账号」。"""
    try:
        data = json.loads(secret_text)
    except Exception:
        data = None
    if isinstance(data, dict):
        token = (
            data.get("token")
            or data.get("access_token")
            or data.get("accessToken")
            or (data.get("auth") or {}).get("accessToken")
            or ""
        )
        uid = data.get("uid") or (data.get("account") or {}).get("uid") or (data.get("account") or {}).get("id")
        token = (token or "").strip()
        if "+" in token:
            prefix, _, suffix = token.partition("+")
            if suffix.strip():
                return (uid or prefix.strip() or None), suffix.strip()
        return (uid or None), token
    text = secret_text.strip()
    if "+" in text:
        prefix, _, suffix = text.partition("+")
        if suffix.strip():
            return (prefix.strip() or None), suffix.strip()
    return None, text


def selftest():
    """本机可跑的自检：DPAPI 取密钥 + 加解密回环（不读不写任何真实 secret）。"""
    rows = []
    ok = True
    root = None
    for target in TARGETS.values():
        if os.path.isfile(local_state_path(target["dir"])):
            root = target["dir"]
            rows.append(("发现目标数据目录", True, target["dir"]))
            break
    # 没有目标时用 Edge 的 Local State 验证 DPAPI + AES 兼容性（只读密钥，不碰用户数据）
    if root is None:
        edge = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
        if os.path.isfile(local_state_path(edge)):
            root = edge
            rows.append(("回退 Edge 密钥验证", True, edge))
    if root is None:
        rows.append(("取密钥", False, "本机无 VS Code / CodeBuddy / Edge 的 Local State，无法验证"))
        return False, rows
    try:
        key = get_windows_encryption_key(root)
        rows.append(("DPAPI 解出密钥", len(key) == 32, "len=%d" % len(key)))
        sample = json.dumps({"uid": "u_test", "token": "tok_测试", "n": 1}, ensure_ascii=False)
        blob = encrypt_v10(key, sample.encode("utf-8"))
        rows.append(("v10 加密", blob[:3] == V10, "len=%d" % len(blob)))
        back = decrypt_v10(key, blob).decode("utf-8")
        rows.append(("v10 解密回环", back == sample, "len=%d" % len(back)))
        ok = all(r[1] for r in rows)
    except Exception as e:
        rows.append(("加解密链路", False, str(e)))
        ok = False
    return ok, rows


if __name__ == "__main__":
    good, items = selftest()
    for name, passed, info in items:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
