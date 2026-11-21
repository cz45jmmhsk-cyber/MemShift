# -*- mode: python ; coding: utf-8 -*-
# 构建方式：在 _build 目录内执行 `pyinstaller MemShift.spec`（或 `python -m PyInstaller MemShift.spec`）
# 产物：_build/dist/MemShift.exe（onefile，发布到 GitHub Releases）

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

a = Analysis(
    ['../wb_switch_pro.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, '_fonts'), '_fonts'),
           (os.path.join(ROOT, 'companion_embed'), 'companion_embed')],
    hiddenimports=[
        'memshift_ext',
        'memshift_ext.aesgcm',
        'memshift_ext.secret_storage',
        'memshift_ext.proc_ctl',
        'memshift_ext.ext_sessions',
        'memshift_ext.ide_switch',
        'memshift_ext.limits_board',
        'memshift_ext.extras',
        'memshift_ext.pet_rail',
        'memshift_ext.hub',
        'memshift_ext.wb_adapter',
        'memshift_ext.hook_install',
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'clr',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MemShift',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
