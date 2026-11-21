# -*- coding: utf-8 -*-
"""纯 Python AES-256-GCM（仅标准库）。

用途：Windows 上 VS Code / CodeBuddy 的 `state.vscdb` 里，secret 行的值是 Chromium
Safe Storage 的 `v10` 密文 —— `b"v10" + nonce(12) + AES-256-GCM(明文) + tag(16)`，
密钥来自 `<数据目录>/Local State` 的 `os_crypt.encrypted_key`（去掉 `DPAPI` 前缀后
用 DPAPI 解出 32 字节）。本机没有 cryptography / pycryptodome，又不想给单文件 exe
增加二进制依赖，因此这里自带一份实现（数据量只有几 KB，速度足够）。

实现对照 FIPS-197（AES-256 单块）与 NIST GCM 测试向量自检，见 `selftest()`。
"""

# ---------------------------------------------------------------- AES 基础表

_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a & 0xFF


def _mul(a, b):
    """GF(2^8) 乘法。"""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


# 预计算 2/3 倍表，MixColumns 用
_MUL2 = [_mul(i, 2) for i in range(256)]
_MUL3 = [_mul(i, 3) for i in range(256)]


def expand_key(key):
    """AES-256 密钥扩展 → 15 个 16 字节轮密钥（列主序）。"""
    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    nk, nr = 8, 14
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(nr + 1)]


def encrypt_block(block, round_keys):
    """AES-256 单块加密（16 字节）。"""
    if len(block) != 16:
        raise ValueError("块长度必须为 16")
    s = [block[i] ^ round_keys[0][i] for i in range(16)]
    for rnd in range(1, 15):
        # SubBytes + ShiftRows
        t = [0] * 16
        for c in range(4):
            for r in range(4):
                t[4 * c + r] = _SBOX[s[4 * ((c + r) & 3) + r]]
        if rnd != 14:
            # MixColumns
            s = t
            t = [0] * 16
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c], s[4 * c + 1], s[4 * c + 2], s[4 * c + 3]
                t[4 * c] = _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3
                t[4 * c + 1] = a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3
                t[4 * c + 2] = a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3]
                t[4 * c + 3] = _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3]
        s = [t[i] ^ round_keys[rnd][i] for i in range(16)]
    return bytes(s)


# ---------------------------------------------------------------- GCM 部分

_R = 0xE1 << 120


def _gf_mul(x, y):
    """GF(2^128) 乘法（GCM 规定的位序）。x / y 为 128 位整数。"""
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ _R
        else:
            v >>= 1
    return z


def _ghash(h, data):
    """GHASH(h, data)，data 为字节串（自动补 16 字节块）。"""
    y = 0
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        if len(chunk) < 16:
            chunk = chunk + b"\x00" * (16 - len(chunk))
        y = _gf_mul(y ^ int.from_bytes(chunk, "big"), h)
    return y


def _ctr_crypt(key, round_keys, counter_block, data):
    """GCM 的 GCTR：从 counter_block 起逐块 +1 加密。"""
    out = bytearray()
    counter = int.from_bytes(counter_block, "big")
    for i in range(0, len(data), 16):
        block = counter.to_bytes(16, "big")
        stream = encrypt_block(block, round_keys)
        chunk = data[i:i + 16]
        out.extend(bytes(a ^ b for a, b in zip(chunk, stream)))
        counter = ((counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF))
    return bytes(out)


def _inc32(block):
    """J0 的低 32 位 +1。"""
    n = int.from_bytes(block, "big")
    n = (n & ~0xFFFFFFFF) | ((n + 1) & 0xFFFFFFFF)
    return n.to_bytes(16, "big")


def gcm_encrypt(key, nonce, plaintext, aad=b""):
    """AES-256-GCM 加密 → ciphertext || tag(16)。nonce 建议 12 字节。"""
    if len(nonce) != 12:
        raise ValueError("仅支持 12 字节 nonce（Chromium v10 口径）")
    round_keys = expand_key(key)
    h = int.from_bytes(encrypt_block(b"\x00" * 16, round_keys), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    ciphertext = _ctr_crypt(key, round_keys, _inc32(j0), plaintext)
    # GHASH 输入：AAD || pad || C || pad || len(AAD) || len(C)（均为 64 位比特数）
    padded = bytearray()
    padded.extend(aad + b"\x00" * ((16 - len(aad) % 16) % 16))
    padded.extend(ciphertext + b"\x00" * ((16 - len(ciphertext) % 16) % 16))
    padded.extend((len(aad) * 8).to_bytes(8, "big"))
    padded.extend((len(ciphertext) * 8).to_bytes(8, "big"))
    s = _ghash(h, bytes(padded))
    ek_j0 = int.from_bytes(encrypt_block(j0, round_keys), "big")
    tag = (s ^ ek_j0).to_bytes(16, "big")
    return ciphertext + tag


def gcm_decrypt(key, nonce, blob, aad=b""):
    """AES-256-GCM 解密（blob = ciphertext || tag）。tag 不符抛 ValueError。"""
    if len(blob) < 16:
        raise ValueError("密文过短（缺少 tag）")
    ciphertext, tag = blob[:-16], blob[-16:]
    round_keys = expand_key(key)
    h = int.from_bytes(encrypt_block(b"\x00" * 16, round_keys), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    padded = bytearray()
    padded.extend(aad + b"\x00" * ((16 - len(aad) % 16) % 16))
    padded.extend(ciphertext + b"\x00" * ((16 - len(ciphertext) % 16) % 16))
    padded.extend((len(aad) * 8).to_bytes(8, "big"))
    padded.extend((len(ciphertext) * 8).to_bytes(8, "big"))
    s = _ghash(h, bytes(padded))
    ek_j0 = int.from_bytes(encrypt_block(j0, round_keys), "big")
    want = (s ^ ek_j0).to_bytes(16, "big")
    if want != tag:
        raise ValueError("认证标签不匹配（密钥不对或密文被改）")
    return _ctr_crypt(key, round_keys, _inc32(j0), ciphertext)


# ---------------------------------------------------------------- 自检

def selftest():
    """FIPS-197 AES-256 单块 + NIST GCM 向量自检，返回 (ok, 明细列表)。"""
    details = []
    ok = True

    # FIPS-197 C.3
    key = bytes(range(32))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    want = "8ea2b7ca516745bfeafc49904b496089"
    got = encrypt_block(pt, expand_key(key)).hex()
    pass1 = got == want
    ok = ok and pass1
    details.append(("FIPS-197 AES-256 单块", pass1, "want %s / got %s" % (want, got)))

    # NIST GCM AES-256（全零 key / iv / pt）
    key0 = b"\x00" * 32
    iv0 = b"\x00" * 12
    pt0 = b"\x00" * 16
    blob = gcm_encrypt(key0, iv0, pt0)
    want_ct = "cea7403d4d606b6e074ec5d3baf39d18"
    want_tag = "d0d1c8a799996bf0265b98b5d48ab919"
    pass2 = blob[:16].hex() == want_ct and blob[16:].hex() == want_tag
    ok = ok and pass2
    details.append(("NIST GCM-AES256 向量", pass2,
                    "ct %s tag %s" % (blob[:16].hex(), blob[16:].hex())))
    back = gcm_decrypt(key0, iv0, blob)
    pass3 = back == pt0
    ok = ok and pass3
    details.append(("GCM 解密回环", pass3, "len %d" % len(back)))

    # 多块 + 中文
    text = ('{"token":"abc","nickname":"测试账号"}' * 5).encode("utf-8")
    blob = gcm_encrypt(key, b"123456789012", text)
    pass4 = gcm_decrypt(key, b"123456789012", blob) == text
    ok = ok and pass4
    details.append(("多块 UTF-8 回环", pass4, "len %d" % len(text)))

    return ok, details


if __name__ == "__main__":
    good, rows = selftest()
    for name, passed, info in rows:
        print(("PASS" if passed else "FAIL"), name, "|", info)
    raise SystemExit(0 if good else 1)
