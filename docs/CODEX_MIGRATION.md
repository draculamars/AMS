# Codex 跨机迁移

## 0.7.0-migration.5 的完成边界

完成验收针对明确捕获的工程快照和已结束轮次。源机继续运行时，未来回复、执行进度和新文件不会因为之前的 SHA-256 通过而自动成为已迁入内容。完成操作现在重新比较两端的已结束轮次；发现新增结束内容会阻止完成，并要求补同步。捕获时仍在进行的轮次单独列出，不能把它们伪装成已完成。

接收 Python 需要安装 `requirements.lock`（包含 `zstandard`）。已有 Atlas 的目标可在方案中用 `remote_python` 指向其虚拟环境解释器；该路径必须是绝对路径。SSH 地址变动可设置 `host_key_alias` 保留已验证的主机身份，不会关闭主机密钥检查。

### 已迁入环境的一致性修复

网页依次提供“准备历史与资源链接修复”“捕获已结束会话的补同步内容”“合入隔离副本并检查冲突”“全量验证隔离副本”“保留当前副本，重启目标并接入修复”。CLI 共用这些实现：

```bash
python -m asset_manager.codex_migration prepare-history TASK_ID
python -m asset_manager.codex_migration capture-finished TASK_ID
python -m asset_manager.codex_migration apply-finished TASK_ID
python -m asset_manager.codex_migration verify-history TASK_ID
python -m asset_manager.codex_migration install-history TASK_ID
python -m asset_manager.codex_migration verify-native TASK_ID
python -m asset_manager.codex_migration verify-workspaces TASK_ID
python -m asset_manager.codex_migration complete TASK_ID
```

- 原始 `incoming` 不变；新副本和切换前数据保存在 `history-maintenance/<运行 ID>/`。
- `.jsonl` 与 `.jsonl.zst` 都参与分支发现。压缩原件保存在独立目录，工作副本展开后才修改。
- 对每个实际记录重新计算位置，同时检查轮次起止、投影游标以及 `history_base.end_byte_offset`。父历史边界用记录序号绑定，不能沿用改写前的字节地址。
- 分支沿 `history_base` 递归展开，并遵循 `end_ordinal_exclusive`。附属聊天的继承上下文不重复计入其可见聊天；来源保留的空中断占位记录有独立说明，不删除。
- 原生验收逐页核对所有可见轮次 ID、结束状态、结束时间、内容 ID 及每一个保存字段。不同版本新增的字段可以保留，来源字段不允许静默改变。
- 已结束会话补同步在同一只读数据库事务中选取轮次，以该轮次结束位置截断日志。目标新增设置保留；目标独立对话内容或已结束内容与来源不同则拒绝覆盖。
- 准备阶段逐字段核对目标原有历史。切换前正常退出目标应用，重新检查数据库与日志是否变化，存在新内容就拒绝覆盖。任一切换失败恢复旧目录和 WAL 文件。
- 补同步的是聊天历史。工程文件仍按工程快照及单独的变更交付验收；聊天中出现新文件路径不等于对应工程内容已经更新。每个实际任务需单列其工程补交付范围。

`native-acceptance.json` 中 `full_history`、`offsets`、`local_links` 必须通过，且 `local_links.native_checked` 为真。旧版报告不足以完成迁移。压缩与普通历史中的来源缺口、保留的上下文占位和未结束轮次分别报告。数据库保留、原生可见内容、工程可运行性是不同验收范围。

目标继续工作后，Git 引用可能与旧快照出现差异。复验仍严格报告差异，既不回退目标新工作，也不将新的失败隐藏在过去的完成状态下。原始快照保留与当前工作目录一致是两个不同结论。

### 聊天正文中的图片与文件链接

迁移会解析助手 Markdown 正文的内联图片、普通文件链接、引用式链接定义及 `file://` 链接，仅重绑定实际链接地址中的来源用户根目录或明确的附件别名。中文、空格、括号、锚点和文件行号保留其 URL 含义；标签、标题、普通叙述、代码块、行内代码及历史工具命令保持原文。网页 URL、相对路径和其他用户目录不做猜测性映射。

派生 SQLite、JSONL 中的助手事件和响应副本使用同一个转换器，之后重算日志位置；`incoming` 原始快照不变。首次迁移也会发现工程外的图片、音视频文件并捕获仍可读取的附件。普通文档链接不会自动扩大工程归档范围。

资源验收记录所有解析到的本地路径、旧链接残留、缺失文件及现存媒体。现存媒体计算 SHA-256；不超过 16 MiB 的文件通过目标 Codex `fs/readFile` 再读并比较字节，较大媒体检查原生 `fs/getMetadata` 并明确记为元数据检查。缺失资源单列，`all_resources_available=false`，不宣称已恢复。原生 API 读取通过也不等于桌面聊天界面已渲染，`visual_rendering_verified` 保持 false；浏览器解码或桌面人工复核须另记证据。

仅修复既有目标聊天链接时，可以跳过捕获与合入补同步步骤；新目标聊天仍参与基线保护和全量验证。此时修复验收不代表源机新产生的聊天、文件已同步，最终迁移完成门禁仍要求检查已结束轮次差异。

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
| 用户工具运行环境 | 发现 NVM、用户 bin、HF CLI 及启动器指向的 `.local/share` 软件目录；补全 Codex 工具 PATH，保留目标已有设置，不导入第三方登录目录 |
| RAM、swap、进程、socket、锁 | 不读取内存转储，不复制系统交换文件；新机重建 |
| 服务发现缓存、模型目录缓存、Atlas 服务日志 | 可重建，不让持续写入的缓存干扰内容校验 |
| 来源认证、浏览器 Cookie、系统钥匙串 | 不导入来源登录；保留目标登录，必要时重新授权 |
| 来源自动任务、队列、旧进程状态 | 原件归档，不在两台机器上重复启用来源任务 |
| Atlas 目录库、归档对象、历史备份和恢复资料 | 可一起迁移，目标使用独立节点身份、目录绑定和服务 |

数据库是一份份生成的一致副本，不是整台机器同一瞬间的快照。来源仍在工作时会继续产生内容；已结束的快照不代表后续写入。任务保存捕获记录。来源会话不会被中断，目标副本中的未完成轮次标记为 interrupted，避免显示无法恢复的运行进程。

## 使用流程

1. 配置目标 SSH 公钥登录，通过可信方式确认主机密钥。Atlas 使用 BatchMode、StrictHostKeyChecking，不收集密码、不自动信任未知主机。
2. 输入目标、端口和专用暂存目录。目录来自会话索引、项目配置、工作区记录，以及历史 commandExecution 的实际工作目录；已有模型权重缓存一起列入方案，可手工补充资料目录。历史诊断命令的用户主目录和系统应用配置目录不会被当成工程整体导入。
   暂存目录不得位于 `.codex` 或其他接续目录内部，也不能包含这些目录，防止递归复制和切换时移动自身。
3. 核对目录方案、来源缺口及两端内存、swap、磁盘。启动后可关闭网页，独立进程继续迁移。
4. 网络中断有限重试，可暂停、续传；保留已传文件。后续阶段重试仍完整核对所有目录。
5. 生成 SHA-256 清单，核对复制期间差异，再校验目标文件、目录、软链接、权限、扩展属性和目录内硬链接。
6. 清单外的中断残留移到任务证据区保留，不带入激活快照。内容校验失败时禁止接续。
7. 先检查历史图片：恢复副本调整结构化附件路径与助手 Markdown 链接地址；普通叙述、代码和工具参数原文保留，嵌入图像不保证原图分辨率。随后在副本中合并会话、项目和记忆。UUID 内容冲突、数据库谱系不兼容、同名目标工程会阻止切换，原件保留。
8. 接续时正常退出目标 Codex，检查活动会话和准备后变化，切换已准备目录，再重启。源机应用不停止；切换失败时恢复已移动目录。
9. 用目标原生 thread/list、thread/read、thread/turns/list、thread/items/list 验证全部可见轮次与内容字段，并选取一个迁入会话执行不含新轮次的 thread/resume 和 skills/list。子代理与附属会话按各自父会话查询并验证，不要求出现在顶层列表。历史分支通过普通或压缩日志的 history_base 关联到实际聊天，不因分支 ID 不在 threads 表中就判定为未注册；无法关联的记录才单列缺口。验收不发送提示、不启动模型轮次。
   数据库有记录而原生接口返回空历史、接续返回错误工作目录或技能发现缺少该目录，均视为未通过。
10. 校验工程目录与 Git HEAD、refs、对象完整性。部署后有明确升级交付时，`evidence/git-upgrades.json` 可记录逐引用的快照值与交付值；验收要求引用集合精确匹配、已存在引用只能快进、HEAD 变化有对应记录，并继续运行完整 fsck。未记录修改、回退或删除引用仍然失败，原始快照不修改。源机已经缺失的历史工作树可按会话记录的提交，在目标重建 detached 工作树；报告明确其不能恢复源机已经丢失的未提交内容。
   源机已不存在、未注册为工程且位于临时目录的历史测试 cwd 单独列为来源缺口，不建立空目录冒充工程；其他缺失工程仍阻止完成验收。
11. 可安装目标 Atlas，核对节点、文件、文档和模型。服务绑定目标 localhost；跨机访问使用 SSH 隧道或已配置认证的网络部署。
   发布包从当前 Atlas 代码生成，单独校验后安装；工程迁移原始快照保持原来的版本，不用改写历史快照来升级服务。

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

macOS openrsync 的 `-E` 对只读 Git 对象和重命名单文件存在兼容问题，所以文件传输与扩展属性恢复分开进行，不修改来源 Git 对象权限。原始快照和接续副本分别保存；历史对话正文不做全局字符串替换。接续副本重绑定 JSONL 元数据、事件中的结构化路径与数据库路径，同时重建 SQLite 和 history_base 的字节位置，避免应用重建索引时恢复旧用户名路径。原始 JSONL 保留在 incoming。

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

如果执行中发现漏列的用户运行环境，可以先用相同文件清单和接收校验流程生成关联补充任务，再在主任务已接续后执行 `install-tools TASK_ID --supplement SUPPLEMENT_ID`。该维护命令拒绝覆盖现有工具目录，保留配置原件，检查选定工具的版本命令，把回执归入主任务。它不适用于复制第三方登录、启动外部设备或运行工程测试。常规迁移应在首次方案中包含这些目录。

配置示例：

```json
{"host":"user@target-mac.local","port":22,"include_atlas":true,"extra_roots":["/Users/source/Documents/AdditionalProjects"]}
```

不要直接覆盖目标活动 `.codex`。需要人工恢复时，先正常退出目标应用；保留当前新配置，再恢复 `destination-original-codex`。接收工程和原始快照无需删除。正常开发仍在各工程 commit/push，迁移不替用户创建提交。

功能在独立分支开发。维护时运行复制、SQLite WAL、路径边界、标识冲突、目标保留、扩展属性及切换失败回退测试，保存实际双机报告。复制进度、文件校验和原生接续是不同阶段，不能相互替代。

完成验收要求文件校验、原生会话与工程 Git 校验全部通过；选择迁移 Atlas 时还要求目标服务已部署。接收节点可直接查看本地回执，无需 SSH 连接自身。目标应用的首次启动、后续工具授权和逐工程构建应与文件验收结果分别记录。

图片恢复逐条读取目标上的已校验 JSONL，默认单条记录上限 128 MiB；超限记录、无法配对的图片和同一路径的不同内容均在报告中列出。原始 JSONL 始终保留，恢复出的模型输入图像不等同于已丢失的完整原图。

## Python 与工具兼容验收

迁移会修复副本中的只读 Git alternates、虚拟环境配置与启动脚本，随后恢复原权限，不改源文件。缺失的 Python 基础解释器优先匹配已迁入或目标已有的相同完整版本、架构与 ABI。若已迁入 uv 且没有相同版本，可在该虚拟环境内部安装所需的准确版本，记录安装日志；不会静默升级 Python 或整体复制 Homebrew/Miniforge。来源和目标原始配置继续保留。

原生验收同时检查受修复环境的启动、版本、环境前缀、SSL 与 SQLite；这不代替逐工程依赖、编译和硬件验收。`python-runtime-rebinding.json` 与 `python-runtime-acceptance.json` 分别记录修改与运行结果。已准备但未激活时可运行 `repair-runtimes TASK_ID`。已激活时，该入口只恢复有本任务记录且再次失效的解释器软链接，检查清单哈希、版本与 ABI，保存原配置并验证启动，失败则回退。工作中的解释器、独立修改的软链接和版本不被覆盖；目标有活动轮次时不执行。网页仅在原生验收发现运行环境失败时提供该修复按钮，修复后须重新验收。NVM 使用已记录的默认版本，避免后台服务 PATH 选择来源其他 Node 版本。

已激活的旧任务须使用下述隔离准备、全量验收、切换流程。旧版 `repair-paths` 原位修改入口已停用，避免未经验证直接改变活动数据库和日志。
