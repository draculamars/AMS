# Codex 跨机迁移

Atlas 的“Codex 迁移”入口位于 `#codex-migration`。它将工作目录、Git、会话数据库、持久化记忆、技能和插件资源一起迁移，分别记录复制、校验、接续与原生读取验收。来源文件始终保留。

本适配器检查实际 SQLite schema，并使用目标安装的 Codex App Server 验收。它是 Atlas 的适配器，不是 OpenAI 官方导入接口。当前原生桌面切换适配 macOS；SSH 文件接收器支持 macOS/Linux。Windows Atlas 原有功能不变，当前迁移执行器仍需要本机 SSH、rsync 与受支持的 Python 运行时。

## 范围与资源

| 内容 | 处理方式 |
| --- | --- |
| 工程、未提交/未跟踪文件、`.git`、worktrees | 保留层级、内容、权限和软链接；Git 工作树指针在目标副本中修复 |
| Codex 会话和状态数据库 | SQLite online backup，完整性检查后传输，原始快照保留 |
| JSONL 会话、全局目录状态、仍有写入的工程日志 | macOS 使用 APFS 文件副本冻结，再发送；活动进程锁在目标重建，源服务不停止 |
| 历史图片的临时路径 | 仍存在的外部图片单独校验复制；已丢失原图尝试从原始对话中明确配对的嵌入图像恢复，记录来源和差异，不猜测有冲突的图片 |
| 记忆、技能、规则、附件、插件 | 归档实际文件；插件目录中的 cache 不按普通临时缓存排除 |
| 模型权重、数据集缓存 | 将已存在的 Hugging Face、Torch、Whisper、Ollama 等模型目录列入方案；不把模型权重等同于临时下载缓存 |
| Codex Python/Node 与用户安装的 Python 运行时 | 保留可复用缓存和 uv 管理的 Python；修复虚拟环境配置、脚本首行与路径文件，目标已有 Codex 运行时优先保留 |
| RAM、swap、进程、socket、锁 | 不读取内存转储，不复制系统交换文件；新机重建 |
| 服务发现缓存、模型目录缓存、Atlas 服务日志 | 可重建，不让持续写入的缓存干扰内容校验 |
| 来源认证、浏览器 Cookie、系统钥匙串 | 不导入来源登录；保留目标登录，必要时重新授权 |
| 来源自动任务、队列、旧进程状态 | 原件归档，不在两台机器上重复启用来源任务 |
| Atlas 目录库、归档对象、历史备份和恢复资料 | 可一起迁移，目标使用独立节点身份、目录绑定和服务 |

数据库是一份份生成的一致副本，不是整台机器同一瞬间的快照。来源仍在工作时会继续产生内容；已结束的快照不代表后续写入。任务保存捕获记录。来源会话不会被中断，目标副本中的未完成轮次标记为 interrupted，避免显示无法恢复的运行进程。

## 使用流程

1. 配置目标 SSH 公钥登录，通过可信方式确认主机密钥。Atlas 使用 BatchMode、StrictHostKeyChecking，不收集密码、不自动信任未知主机。
2. 输入目标、端口和专用暂存目录。目录来自会话索引、项目配置、工作区记录，以及历史 commandExecution 的实际工作目录；已有模型权重缓存一起列入方案，可手工补充资料目录。历史诊断命令的用户主目录和系统应用配置目录不会被当成工程整体导入。
3. 核对目录方案、来源缺口及两端内存、swap、磁盘。启动后可关闭网页，独立进程继续迁移。
4. 网络中断有限重试，可暂停、续传；保留已传文件。后续阶段重试仍完整核对所有目录。
5. 生成 SHA-256 清单，核对复制期间差异，再校验目标文件、目录、软链接、权限、扩展属性和目录内硬链接。
6. 清单外的中断残留移到任务证据区保留，不带入激活快照。内容校验失败时禁止接续。
7. 先检查历史图片：恢复副本只调整结构化的附件路径；历史文本和工具参数原文保留，嵌入图像不保证原图分辨率。随后在副本中合并会话、项目和记忆。UUID 内容冲突、数据库谱系不兼容、同名目标工程会阻止切换，原件保留。
8. 接续时正常退出目标 Codex，检查活动会话和准备后变化，切换已准备目录，再重启。源机应用不停止；切换失败时恢复已移动目录。
9. 用目标原生 thread/list、thread/read、thread/turns/list 逐会话验证，并选取一个迁入会话执行不含新轮次的 thread/resume 和 skills/list。附属会话按父子层级检查，不要求出现在顶层列表。验收不发送提示、不启动模型轮次。
10. 校验工程目录与 Git HEAD、refs、对象完整性。源机已经缺失的历史工作树可按会话记录的提交，在目标重建 detached 工作树；报告明确其不能恢复源机已经丢失的未提交内容。
11. 可安装目标 Atlas，核对节点、文件、文档和模型。服务绑定目标 localhost；跨机访问使用 SSH 隧道或已配置认证的网络部署。

“原生会话验收通过”表示原生接口能够读取内容，不代表所有工程的编译、设备驱动、模型推理和第三方账户都已验证。项目虚拟环境、工具绝对路径、外部设备和模型缓存需要按依赖报告核对。来源已经不存在的文件不能由索引恢复；从 Git 重建时应注明提交及无法恢复的未提交内容。

## 目录和证据

来源任务位于 `ASSET_DATA_DIR/codex-migrations/<任务 ID>/`，包含方案、传输日志、来源清单和回执；不递归归档自己。

```text
目标专用暂存目录/
  ATLAS-MIGRATION.json            任务身份
  incoming/                      接收到的原始快照，保留来源目录关系
  evidence/                      文件、接续、原生读取、部署报告与额外文件隔离区
  supplements/attachments/       工程范围外仍可读取的原始图片
  derived-attachments/            从原始上下文恢复的图像及来源记录
  prepared/                      待切换副本及保持未启用的来源自动任务
  destination-before-activation/  目标数据库准备基线
  destination-original-codex/     切换时保存的目标完整原配置
  preparation-history/           重试时保留的旧准备副本
```

SSH 加密传输；目标是可直接访问的普通文件，不额外加密归档。控制文件使用 0600。来源登录文件不迁移，但所选工程内的 `.env`、私有文档、工具配置仍可能包含敏感内容，不应公开发布迁移目录。

macOS openrsync 的 `-E` 对只读 Git 对象和重命名单文件存在兼容问题，所以文件传输与扩展属性恢复分开进行，不修改来源 Git 对象权限。原始快照和接续副本分别保存；历史对话原文不做全局字符串替换。

## CLI 与运维

CLI 和网页共用任务引擎。使用实际部署的 `ASSET_DATA_DIR`、`ASSET_BLOB_DIR`：

```bash
python -m asset_manager.codex_migration plan migration-plan.json
python -m asset_manager.codex_migration start TASK_ID
python -m asset_manager.codex_migration status TASK_ID
python -m asset_manager.codex_migration prepare-profile TASK_ID
python -m asset_manager.codex_migration activate TASK_ID
python -m asset_manager.codex_migration verify-native TASK_ID
python -m asset_manager.codex_migration verify-workspaces TASK_ID
python -m asset_manager.codex_migration deploy-atlas TASK_ID
python -m asset_manager.codex_migration complete TASK_ID
```

配置示例：

```json
{"host":"user@target-mac.local","port":22,"include_atlas":true,"extra_roots":["/Users/source/Documents/AdditionalProjects"]}
```

不要直接覆盖目标活动 `.codex`。需要人工恢复时，先正常退出目标应用；保留当前新配置，再恢复 `destination-original-codex`。接收工程和原始快照无需删除。正常开发仍在各工程 commit/push，迁移不替用户创建提交。

功能在独立分支开发。维护时运行复制、SQLite WAL、路径边界、标识冲突、目标保留、扩展属性及切换失败回退测试，保存实际双机报告。复制进度、文件校验和原生接续是不同阶段，不能相互替代。

完成验收要求文件校验、原生会话与工程 Git 校验全部通过；选择迁移 Atlas 时还要求目标服务已部署。接收节点可直接查看本地回执，无需 SSH 连接自身。目标应用的首次启动、后续工具授权和逐工程构建应与文件验收结果分别记录。

图片恢复逐条读取目标上的已校验 JSONL，默认单条记录上限 128 MiB；超限记录、无法配对的图片和同一路径的不同内容均在报告中列出。原始 JSONL 始终保留，恢复出的模型输入图像不等同于已丢失的完整原图。
