# Atlas · 工程与知识管家（AMS）

AMS 是可部署在 macOS、Ubuntu 的私人资产管理系统。它把项目文件、文档、模型、已有 Git 历史、资产快照和 Codex 开发记录放入同一个工作空间。浏览器可从桌面、平板和手机访问。

**当前版本：0.5.1。** 每个服务实例展示自身节点的工作目录与已经完整接收的归档映射。机器名称、SSH 目标、存储位置均属于部署配置。

## 可以做什么

- **工作台**：常用工程、跨文件和开发记录搜索、最近修改、需要查看的事项、节点状态。
- **工程文件**：保留真实目录层级；区分当前映射、全部已保存归档、历史索引采样。
- **开发过程**：按轮次查看请求、交付说明、过程消息、命令结果、文件差异；图形显示开发轮次与产物之间的变更证据。
- **内容关系**：解析文档引用、代码导入、模型引用和工程清单；也可人工确认依赖、派生、替代、验证关系。
- **模型工程**：保留工程入口、装配、材质、贴图、几何与工程快照。支持 STL、OBJ、STEP/IGES/BREP、3MF、glTF/GLB、URDF；Blend 通过本节点 Blender 转换。
- **历史**：浏览已有 Git 分支、标签、提交、文件历史、差异和历史模型。读取历史不 checkout、commit 或 push 原仓库。
- **管理与修改**：上传、下载、备注、标签、工程包更新；在线文本编辑先展示差异，确认后写入本节点原文件并保存前后快照。
- **同步**：SSH 增量传输工程内容、索引、引用、开发记录和已有 Git 对象；校验内容后发布目标节点映射。

- **备份与恢复**：加密增量保存 Codex 与工程原件，数据库一致性副本、覆盖清单、全量校验、隔离恢复与样本演练。
- **技能与工具**：技能、规则、记忆、插件、工具配置和版本预览；脱敏清单可同步到归档节点。

完整范围见 [备份与恢复](docs/BACKUP.md) 和 [功能规划](docs/ROADMAP.md)。

## 快速运行

需要 Python 3.12、Git。预编译的网页查看器已随源码提供，普通部署不需要 Node.js。重新构建查看器时需要 Node.js 与 `npm ci`。

```bash
./scripts/install.sh
./scripts/run.sh
```

打开 <http://127.0.0.1:8765>。默认数据目录为 `~/AssetVault/catalog`，归档内容为 `~/AssetVault/archive`。在网页“接入工程”中选择当前服务主机上的项目路径，或导入项目 JSON：

```json
{"projects":[{"projectId":"example-project","label":"示例工程","path":"/absolute/path/to/project"}]}
```

所有文件操作都发生在**服务所在节点**。浏览器所在电脑的本地路径不会自动变成服务器路径。

```bash
export ASSET_DATA_DIR="$HOME/AssetVault/catalog"
export ASSET_BLOB_DIR="$HOME/AssetVault/archive"
.venv/bin/python -m asset_manager import projects.json
.venv/bin/python -m asset_manager index
```

## 节点、现状与历史

每个目录数据库生成独立节点 ID；显示名称可在工作台设置。项目的本节点位置分为：

| 类型 | 当前文件来自哪里 | 如何更新 |
| --- | --- | --- |
| 本机工作目录 | 当前节点绑定的真实目录 | 后台轮询核对，变化后重新索引；也可手动刷新 |
| 同步归档映射 | 本节点已接收的不可变内容对象与当前清单 | 来源节点成功同步后更新，网页轮询版本号自动刷新 |
| 历史内容 | 实际资产快照或 Git 对象 | 仅已保存内容可恢复；目录索引采样本身不是内容备份 |

归档映射保留源路径作为来源信息，**不是该路径在目标主机上的工作副本**。归档内容按 SHA-256 去重存储，工程下载会重建相对路径。若要在目标主机继续开发，应绑定真实工作目录；同步接收器拒绝覆盖已绑定的工作目录。原项目迁移仍需逐项目确认。

文件树默认排除已移除的历史路径；“全部可用归档”可以找回它们。上传到资产库的更新与原工程文件是两种操作：上传保留库内版本；“编辑原文件”明确写入本节点工作目录。Git commit/push 仍由各工程管理。

## 开发过程与关系证据

AMS 只读访问配置目录下的 Codex `state_5.sqlite` 与 `thread_history_1.sqlite`；若会话尚无 SQLite 投影，则从该目录内对应 JSONL 的公开事件补全。自动发现已配置且本机存在的 Codex 项目目录（只建立索引，不创建 Git 仓库），并关联工程路径。默认来源是 `~/.codex`，可用 `ASSET_CODEX_HOME` 或工作台设置替换。

开发过程索引只收录用户消息、公开助手消息、文件变更事件与命令执行状态，不读取认证文件或推理条目，不执行记录中的命令。明显凭据会遮盖；开发记录仍属于私人数据，应保留在自己的部署与备份中。独立的原件备份按明确范围加密保存文件，不将认证内容暴露到索引预览。

- 文件变更边有事件 ID、成功/失败状态、保存的差异。公开消息中的明确文件链接也会关联到产物，并单独标记为会话引用；引用不证明文件由该轮生成。
- 多轮修改同一路径表示迭代顺序，不能直接证明一个设计派生于另一个设计。
- 内容依赖来自实际解析引用或人工确认，不依据“同一会话提到两个文件”自动认定。
- 脚本生成文件、外部工具修改、未保存的会话可能缺少文件级事件；可结合索引采样与 Git 历史核对。
- 会话基线 commit 是会话元数据，不代表每轮修改都已提交。
- 本地 SQLite 与 JSONL 格式不是稳定公共 API；适配器检查 schema，格式不支持时显示错误。JSONL 仅接收允许的公开完成事件，超过 4 MiB 的单行会跳过并记录数量。官方公共接口见 [Codex App Server](https://developers.openai.com/codex/app-server)。

开发记录随项目同步到归档节点。没有 Codex 安装的服务器仍可浏览同步的记录。

## 自动更新

网页每 5 秒查询节点版本号，接收后台扫描和 SSH 同步发布的变化。正在编辑时延后刷新，已打开的预览保留并提示新状态。

后台默认每 10 秒核对一个本机工程，逐工程轮询；每分钟尝试更新会话记录。大工程核对时间叠加在间隔上，**不是实时文件系统监听**。正在归档或同步时让出资源，失败会显示节点状态并在后续周期重试。状态和时效可在工作台查看。

扫描比较路径、大小、mtime 和已有 Git 引用；保留相同大小与时间戳的特殊修改需要手动重新索引/同步核对。服务停止时不监控。部署多个进程共享同一目录数据库不受支持。

## 配置

| 变量 | 默认值 / 用途 |
| --- | --- |
| `ASSET_DATA_DIR` | 直接执行 Python 时为应用 `data`；运行脚本为 `~/AssetVault/catalog` |
| `ASSET_BLOB_DIR` | 默认数据目录内 `blobs`；运行脚本为 `~/AssetVault/archive` |
| `ASSET_HOST`, `ASSET_PORT` | 运行脚本的绑定地址与端口，默认 `127.0.0.1:8765` |
| `ASSET_NODE_NAME` | 可覆盖数据库保存的节点名称 |
| `ASSET_CODEX_HOME` | Codex 会话来源目录，默认 `~/.codex` |
| `ASSET_WATCH_ENABLED` | `1` 开启自动核对；`0` 关闭 |
| `ASSET_WATCH_INTERVAL` | 每个工程核对的等待秒数，最少 5，默认 10 |
| `ASSET_ALLOWED_HOSTS` | 允许的 HTTP Host，默认 `localhost,127.0.0.1` |
| `ASSET_LOCAL_BOOTSTRAP` | 本机初始管理模式；公网部署设置为 `0` |
| `ASSET_SECURE_COOKIE` | HTTPS 部署设置为 `1` |
| `ASSET_COOKIE_NAME` | 同一浏览器访问多个节点时可配置不同会话 cookie 名称 |
| `ASSET_BLENDER` | 可选 Blender 可执行文件；也可通过 PATH 发现 |
| `ATLAS_RESTIC` | 可选 Restic 路径；默认 PATH 或数据根目录相邻 `tools/restic` |

## SSH 同步

在来源节点配置，目标节点需先部署相同版本，具有 `app/.venv/bin/python`、`catalog`、`archive` 目录：

```bash
.venv/bin/python -m asset_manager configure-sync --host user@archive-host --root /absolute/path/AssetVault
.venv/bin/python -m asset_manager sync
# 可选：只同步一个项目
.venv/bin/python -m asset_manager sync --project example-project
# 工程已同步后，只更新开发记录，无需重新传输或扫描大工程
.venv/bin/python -m asset_manager sync-development
```

可从网页触发同步。目标完成内容验证和目录事务后才发布新映射。失败任务可重试，已接收的内容对象复用；不会自动清理历史副本。网络断开不影响浏览已有归档。同步没有隐含的定时上传任务。

浏览远端只绑定回环地址的服务：

```bash
AMS_SSH_HOST=user@archive-host AMS_REMOTE_PORT=8765 ./scripts/open-tunnel.sh
```

## 部署、升级和备份

详见 [部署与发布](docs/DEPLOYMENT.md)、[架构与审查](docs/REVIEW.md)、[版本说明](CHANGELOG.md)。

```bash
# 一致性数据库、内容、工程清单、Git 历史备份（需要足够空间）
.venv/bin/python -m asset_manager backup /path/to/new-backup-directory
# 带每文件 SHA-256 清单的源码发布包
.venv/bin/python scripts/package-release.py /path/to/ams-release.zip
```

GitHub 仓库只存系统源码、通用说明与合成测试；运行数据库、会话记录、项目产物、凭据与实例验收资料不属于源码发布包。

## 开发与验证

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
# 安装备份引擎后设置其绝对路径；否则真实加密集成测试会跳过
ASSET_WATCH_ENABLED=0 ATLAS_RESTIC=/absolute/path/restic .venv/bin/python -m pytest -q
npm ci
npm run build:viewer
# Playwright 由开发环境安装或通过 PLAYWRIGHT_MODULE 指向已有安装
AMS_QA_URL=http://127.0.0.1:8765 node scripts/steward-browser-qa.cjs
AMS_QA_URL=http://127.0.0.1:8765 node scripts/vault-browser-qa.cjs
```

浏览器验收覆盖工作台、开发记录、目录范围和手机布局。Git、文件写入冲突、归档校验、同步发布、模型依赖等通过合成工程测试验证。

## 已知边界

- macOS 隐私控制可能阻止 LaunchAgent 读取 Documents 等目录。需要给服务运行时目录访问权限，或从已授权的会话运行 `scripts/background.py start`；后台启动成功不代表所有目录均可读取。
- 工程解析对数量、文本大小有边界，页面显示覆盖说明。专有 CAD 参数、外部缺失依赖及未保存的历史无法推断补全。
- 浅克隆和部分克隆保留其真实历史缺口；不会擅自从上游补抓对象。Git LFS 指针不等同于大文件内容。
- 在线原文件编辑限 UTF-8 文本且不超过 512 KiB；复杂模型和二进制资产使用原生工具。差异超过显示上限时网页要求缩小修改后复核。
- 公开 HTTPS 需要实际域名、DNS 和目标主机网络配置。源码提供部署方式，不会自动申请用户尚未提供的域名。
