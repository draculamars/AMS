# Atlas 备份与恢复（0.5）

Atlas 的工程索引、资产快照、Git 历史与原件恢复点是四种不同记录。索引描述一次观察；资产快照保存已归档内容；Git 历史只包含实际提交；加密恢复点保存指定目录中的文件，包括未提交、未跟踪和被 Git 忽略的文件。网页不会把“已索引”显示成“已备份”。

## 建立恢复点

需要 Python 3.12、Git、Restic；SFTP 目标需要现有 SSH 认证、已核验的主机密钥和可写目录。目标不需要运行 Restic 服务。安装器固定 Restic 0.19.1，校验官方发行文件 SHA-256，支持 macOS 与 Linux 的 amd64/arm64：

```bash
python3 scripts/install-restic.py --destination /absolute/path/AssetVault/tools/restic
export ASSET_DATA_DIR=/absolute/path/AssetVault/catalog
export ASSET_BLOB_DIR=/absolute/path/AssetVault/archive
export ATLAS_RESTIC=/absolute/path/AssetVault/tools/restic
.venv/bin/python -m asset_manager vault configure \
  --name '开发资料' \
  --repository sftp:user@backup-host:/absolute/path/backups/development \
  --codex-root /absolute/path/.codex \
  --root /absolute/path/projects
.venv/bin/python -m asset_manager vault init PROFILE_ID
.venv/bin/python -m asset_manager vault plan PROFILE_ID
.venv/bin/python -m asset_manager vault backup PROFILE_ID
```

配置默认包含本节点登记的工作目录、Atlas catalog 和 archive。`--no-projects`、`--no-catalog` 可关闭对应范围；`--root` 可重复。路径、主机与节点名称只在部署配置中保存。仓库不能位于备份来源内。源目录不存在时失败，避免悄悄减少覆盖。

配置会生成权限 0600 的 `catalog/private/vault-<ID>.key`，终端仅输出路径。**必须另存受保护的离线密钥副本**；不要提交 Git，不要和唯一仓库只放在同一台主机。密钥丢失将无法解密。已有密钥可通过 `--password-file` 引用。网页与清单同步不传输密钥。

网页“备份与恢复”提供范围预览、启动增量备份、恢复点、错误、校验、样本演练和隔离恢复。失败和部分成功分别保留；重试复用上传成功的加密对象。0.5 不自动删除历史、不自动 prune、不安排周期任务。需要周期备份时，应在下一阶段配置明确的频率、保留策略和通知。

## 覆盖与一致性

- Codex 根目录中的会话 JSONL、工作树、文档、配置、技能、插件资源与认证原件按文件加密保存。网站只展示允许公开的会话事件及脱敏知识预览；原始认证文件不会成为网站预览。
- 发现的工作树共享 Git 目录也纳入范围，并记录路径、分支和 HEAD。备份保留文件中的绝对路径；迁移恢复需要修复 worktree、环境和工具路径。
- 已识别的 Codex SQLite 数据库和 Atlas 目录数据库使用 SQLite online backup API。副本转换成独立数据库并执行 `quick_check`，原数据库及 WAL/SHM 不作为普通文件复制。恢复点保存原路径到副本路径的映射及哈希。其他项目自己的数据库没有通用一致性保证，需应用专用适配器或停写后备份。
- 每个数据库副本一致；不同数据库、文件和目录之间**不是同一时刻的原子快照**。文件写入期间可能变化，异常与部分成功会显示。需要严格一致的工程交付时，应停止相关写入后再建立恢复点。
- 不跟随符号链接；链接目标需另行纳入来源。运行时 IPC、临时目录、Atlas 密钥、恢复输出、缓存和其他备份暂存目录明确排除。完整排除项随每次恢复点保存。
- 在线能恢复的是磁盘已保存状态，不包含未落盘上下文、隐藏推理状态或远程服务端数据。认证文件恢复不保证登录仍有效；插件和工具还可能依赖系统安装、远程权限和版本。
- 该方案不是可引导系统镜像。跨操作系统恢复需核对权限、扩展属性、大小写、符号链接和工具兼容性；不能据此承诺 macOS 与 Ubuntu 应用状态完全等价。

## 校验与恢复

```bash
.venv/bin/python -m asset_manager vault verify RECEIPT_ID --full
.venv/bin/python -m asset_manager vault drill RECEIPT_ID
.venv/bin/python -m asset_manager vault status
```

完整校验读取仓库所有内容对象（包含其他历史恢复点），不是仅检查本次新上传文件。省略 `--full` 时为结构加 5% 内容抽检，清单会记录范围。恢复演练把样本实际恢复到新目录，比对备份前哈希，并检查 SQLite 样本。样本演练不能代替整机恢复演练。

网页选择原始绝对路径，先展示方案，再确认恢复到 `catalog/vault-restores/<ID>`。恢复使用 Restic `--verify`；不会覆盖工作目录。目录恢复保留内部结构，恢复前应核对目标空间。数据库原件需要从恢复点显示的 `snapshot_path` 取出，使用映射恢复至停止中的应用。恢复后再运行数据库与应用检查。

Atlas 自身不可用时，可直接使用 Restic 和离线密钥：

```bash
export RESTIC_REPOSITORY=sftp:user@backup-host:/absolute/path/backups/development
export RESTIC_PASSWORD_FILE=/secure/offline-key
restic snapshots
restic restore SNAPSHOT_ID --target /absolute/path/new-recovery --verify
```

先用 `restic ls SNAPSHOT_ID` 找到 `vault-stage/<PROFILE_ID>/current/manifest.json`，其数据库映射描述具体恢复路径。恢复应用之前核对节点身份与工作目录绑定。CLI 恢复的输出目录仍应是全新空目录。

## 知识目录与节点同步

“技能与工具”索引技能入口、明确引用、资源路径、规则、记忆、工程指令、自动化与配置中的 MCP/插件名称。配置值、命令参数和环境变量不进入预览；文本预览会遮盖明显凭据，但仍应按私人数据管理。版本是入口文件内容哈希对应的采集版本，不代表包内所有资源已保存历史，也不证明该工具实际执行过。

```bash
.venv/bin/python -m asset_manager vault scan-knowledge
.venv/bin/python -m asset_manager vault sync
```

同步沿用已配置的 SSH 目标，只传输备份清单和脱敏知识目录；目标显示“来源节点管理”。来源节点需要持有密钥才能恢复。工程同步和加密备份是独立操作，原件恢复点完成不会自动发布工程当前映射。

## Atlas 服务备份

`python -m asset_manager backup DEST` 仍可生成独立的 Atlas 一致性数据库、归档对象、索引、Git 历史与校验清单。它不包含未归档的工程目录、原始 Codex 状态或恢复密钥，且本身不加密，应放在受保护存储。服务器本机备份能帮助回退和恢复误操作；与原件在同一物理磁盘时不提供磁盘故障冗余。

参考：[Restic 仓库准备](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)、[备份](https://restic.readthedocs.io/en/stable/040_backup.html)、[恢复](https://restic.readthedocs.io/en/stable/050_restore.html)。Restic 独立安装，遵循其 [BSD 2-Clause 许可](https://github.com/restic/restic/blob/master/LICENSE)。
