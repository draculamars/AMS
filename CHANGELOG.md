# Changelog

## Portable branch (based on 0.5.1)

- Independent removable-disk deployment, offline platform runtimes, content export and full verification tools.
- Portable scope guards, archived Git preference, mount relocation, single-instance launch and checked shutdown backups.
- Storage persistence preflight rejects disappearing writes; deployment success is recorded separately from code and runtime tests.
- Main deployment defaults and the core roadmap remain unchanged.

## 0.5.1 — 2026-10-17

- 技能符号链接入口在已授权来源内建立索引，并显示实际路径。
- 恢复样本使用实际来源路径；禁止把隔离目录外的原文件误当作已恢复证据。
- 选择穿过外部符号链接的文件恢复时明确失败，保留直接恢复链接本身的能力。

## 0.5.0 — 2026-10-17

- Atlas 备份与恢复中心：Restic 加密增量恢复点、范围预览、进度、失败与部分成功记录。
- Codex 与 Atlas SQLite 一致性副本，工作树共享 Git 目录覆盖，原件恢复映射。
- 仓库全量/抽样校验、实际恢复样本演练、确认后写入全新隔离目录。
- 技能与工具目录：明确依赖、资源、脱敏配置、入口版本和预览差异；纳入统一搜索。
- 来源节点到归档节点同步恢复点清单和知识目录，恢复密钥保持在来源节点。
- 固定版本且校验 SHA-256 的跨平台 Restic 安装器，真实引擎集成测试与恢复文档。

## 0.4.1

修正 A → B → A 内容回退后的重复同步：目标节点提供当前清单的哈希，避免复用插入顺序更晚但已不再生效的历史版本。增加同步清单回归校验。补全尚未进入 SQLite 投影的 Codex JSONL 公开事件，并同步只有元信息的会话。结构索引保留持久化待重试标记，服务中断后可继续补全引用与文档。

## 0.4.0

- 面向项目操作的新工作台：常用工程、知识搜索、资源入口、开发迭代与节点状态。
- Codex 本地公开会话记录适配，开发轮次与文件变更的可视化证据、修改说明及执行结果。
- 独立节点身份与当前归档映射；将已移除文件保留在历史范围，修正当前内容恢复为旧版本的选择问题。
- 本机逐工程自动核对，SSH 同步发布后自动刷新网页，编辑期间延后刷新。
- 带差异确认、冲突检查、备份与审计的在线文本原文件编辑。
- 通用 macOS/Linux 服务安装器、SSH 隧道工具、校验发布包与回退源码包；整理部署、架构和验证文档。

## 0.3.0

工程结构、文档正文索引、内容引用、已有 Git 历史和同提交模型依赖；结构化同步与历史备份。

## 0.2.0

模型工程、装配依赖、三维查看、固定工程版本与工程包更新。

## 0.1.0

项目资产索引、文件阅览、上传下载、版本快照、首次 Git 纳管审核、认证与部署基础。
