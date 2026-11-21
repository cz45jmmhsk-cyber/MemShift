# 开源前检查清单（OPEN SOURCE CHECKLIST）

> 按顺序执行，全部勾完再推 GitHub。依据 2026-11-21 自检结果。

## 一、合规（决定能不能开、怎么开）

- [ ] **确认衍生关系**：workbuddy-switch 是 MIT 许可（Copyright (c) 2026 wb-switch），允许任意二次开发、再分发、改名；唯一硬性要求 = 保留其版权声明与许可文本
- [ ] **你的 LICENSE**：MIT，`Copyright (c) 2026 <你的 GitHub 署名>`（衍生项目自己的修改部分归你）
- [ ] **THIRD_PARTY_NOTICES.md**：原项目 MIT 声明全文已备好（opensource_prep/），复制进仓库根目录
- [ ] **README 致谢区块**：明确写"基于 workbuddy-switch 改造 + 链接原仓库"，不冒充原创、不暗示官方
- [ ] **商标避让**：README/仓库描述用 "for WorkBuddy / WorkBuddy 社区工具" 表述；不要用官方 logo / 图标做项目头像；`companion_embed` 里的 icon.png 属于原项目资产，已在第三方声明中覆盖
- [ ] **免责声明**：README 已含「非官方 + 自动化操作账号风险自负 + 遵守官方服务条款」三段

## 二、脱敏（个人数据一个不进仓库）

- [ ] 新建**干净的仓库目录**（不要直接把当前工作目录推上去！）
- [ ] 只拷贝以下内容进新目录：
  - `wb_switch_pro.py`、`memshift_ext/`（12 个 .py，不含 `__pycache__`）
  - `companion_embed/`、`_fonts/`
  - `_build/MemShift.spec`（构建配置，对用户有用）
  - `opensource_prep/` 里的 README.md、KNOWN_ISSUES.md、THIRD_PARTY_NOTICES.md（复制到仓库根目录）
- [ ] **绝不拷贝**：换号蓝本文档（00/01/02/04/06/07/99 系列）、个人探针脚本（03/05/07/08 验收系列）、所有 `.bak`、所有 `.exe`、`S1_S1_00001_.png`、`_test_serve.log`、`_newstyle.css`、`_build/`（dist 产物）、`__pycache__`
- [ ] **处理 A1 硬编码路径**（见 KNOWN_ISSUES A1 表）：7 处改完再开源
- [ ] 全库再扫一遍：`grep -rn "G:/WorkBuddy\|C:/Users/Administrator\| Administrator\|张连生\|祝姐\|快乐的小画家\|memory-brain" 新目录` —— 应无命中（memory-brain 生成逻辑可保留，个人内容不可有）

## 三、.gitignore（新建仓库时放根目录）

```gitignore
__pycache__/
*.pyc
_build/
build/
dist/
*.exe
*.bak
*.log
_test_*
*.db
*.db-*
*_backup*/
wb_accounts_export/
memory-brain/
换号*/
```
> 注意：`memory-brain/` 是运行时自动生成的个人记忆库，必须 ignore。

## 四、发布形态

- [ ] exe 只放 **GitHub Releases**（tag 对应版本，如 v2.9.0），不进 git
- [ ] Release 说明写：更新点 + SmartScreen 放行提示 + 已知问题链接
- [ ] README 里的截图换成**新 UI**（顶部导航版），并确认截图里**没有你的账号昵称/积分数字**（用测试账号或打码！当前截图全是真实账号数据，**必须重新造演示数据截图**）
- [ ] 建议加在线演示说明或空库截图

## 五、发布前最后验证

- [ ] **干净克隆测试**：换一台目录（或删掉 G 盘路径后），从零 `pip install` → `python wb_switch_pro.py` → 全页面能开
- [ ] 新机器语义检查：首次运行自动建蓝本/主脑骨架是否正常（可分发改造已做，验证一次）
- [ ] `KNOWN_ISSUES.md` 保留在仓库并从 README 链接（如实列出问题反而是加分项）
- [ ] 版本号统一：`APP_VER` 与 git tag、Release 一致

## 六、开源后

- [ ] 开启 Issues 模板（bug 报告：版本 / Windows 版本 / 官方客户端版本 / 日志）
- [ ] README 加"官方更新导致失效怎么办"的说明（这是此类工具最常见的 issue）
