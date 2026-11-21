# -*- coding: utf-8 -*-
"""MemShift 扩展能力包（对齐 workbuddy-switch / wb-switch-core 的基础功能）。

模块划分：
  aesgcm          纯 Python AES-256-GCM（Windows Chromium Safe Storage 用）
  secret_storage  DPAPI + Local State + state.vscdb 的 secret 读写
  proc_ctl        客户端进程检测 / 优雅关闭 / 等待退出 / 重新拉起
  ext_sessions    CodeBuddy 扩展会话树（VS Code 插件 / CodeBuddy IDE）列表与复制
  ide_switch      CodeBuddy IDE、VS Code 插件账号切换（含会话复制）
  limits_board    模型限额台账（hook 事件 → 账号/模型/恢复时刻）
  extras          自动更新、通知中心、主题、代理、工具开关
"""

__all__ = [
    "aesgcm",
    "secret_storage",
    "proc_ctl",
    "ext_sessions",
    "ide_switch",
    "limits_board",
    "extras",
]
