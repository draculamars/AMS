# 部署、发布与恢复

## 目录约定

根目录由部署者指定，例如用户主目录下的 `AssetVault`，或管理员授权的服务目录：

```text
AssetVault/
  app/       # AMS 程序与 .venv
  projects/  # 经确认放置的工程工作目录
  archive/   # 不可变内容对象
  catalog/   # SQLite、工程清单、Git 历史、预览缓存、服务日志
  backups/   # 数据备份与源码发布包
```

项目目录可位于任何获授权的位置；索引不自动迁移原目录。运行数据库复制到新节点时，应删除 settings 中的 `node_id` 后让新实例生成身份，并复核项目位置绑定；灾难恢复到原节点则保留身份。不要复制正在写入的 SQLite 文件，使用 backup 命令或 SQLite backup API。

## 原生部署

1. 将源码放入目标根目录的 `app`，执行 `scripts/install.sh`。
2. 配置数据和归档环境变量，导入本节点工程或配置接收同步。
3. 先手动启动，验证文件预览与目录读取权限，再安装后台服务。

```bash
.venv/bin/python scripts/install-service.py --root /absolute/path/AssetVault --port 8765
```

Linux 安装用户级 systemd 服务，macOS 安装 LaunchAgent；`--write-only` 仅生成配置。`--name` 可命名独立实例。Linux 无登录启动还需 `loginctl enable-linger "$USER"`，是否允许取决于主机策略。

macOS 建议程序位于稳定的非 Documents 目录；受隐私保护的工程仍需要授权。可用 `scripts/background.py start|status|stop` 在已授权会话中运行。`ASSET_PORT` 和 `ASSET_HOST` 控制运行脚本。

## 公网 HTTPS

先在服务主机设置管理员密码：

```bash
ASSET_DATA_DIR=/absolute/path/AssetVault/catalog .venv/bin/python -m asset_manager set-password
```

将 `deploy/env.example` 复制为部署专用 `.env`，填写域名、ACME 邮箱、宿主 UID/GID 和目录；配置 DNS 后执行：

```bash
docker compose up -d --build
```

Caddy 终结 HTTPS，后端只暴露容器内部端口。公网模式禁止 local bootstrap，启用 Secure cookie 与 Host 白名单。使用 Docker 部署时，项目路径需映射为容器可访问的同一路径；Codex 来源可通过只读挂载和 `ASSET_CODEX_HOME` 配置。容器默认未打包 Blender；需要该格式时安装运行时或使用原生部署。

## 发布与升级

发布包包含源码、网页查看器、测试、部署和说明；附 `RELEASE-MANIFEST.json` 对所有文件校验。每次发布：

1. 跑测试与浏览器验收，更新版本号、CHANGELOG 和审查说明。
2. `scripts/package-release.py` 生成 ZIP，记录其 SHA-256。
3. 对数据库作一致性备份；大文件归档对象另行备份。停止后台服务。
4. 将 ZIP 送至目标主机，运行已审查的安装器：

```bash
python3 scripts/install-release.py ams-release.zip --root /absolute/path/AssetVault
cd /absolute/path/AssetVault/app
.venv/bin/python -m pip install -r requirements.lock
```

安装器先验证发布包，保存旧程序源码 ZIP，再写入目标 `app`；保留 `.venv`、数据和节点配置。现有部署首次采用安装器时，没有旧清单的遗留文件不会自动清理。

5. 重启服务，检查 `/api/node` 的版本和节点身份、`/api/workbench` 的项目与开发记录、一个文档、一个模型和一个历史版本。
6. 来源和目标版本一致后执行工程同步；查看任务状态与目标版本号自动刷新。

回退：停止服务，用安装器安装 `backups/ams-before-*.zip`，按需恢复对应版本的一致性数据库备份，再启动。0.4 的 schema 为新增表，未删除旧业务表；仍建议保留匹配的数据库备份。不要在回退时删除内容对象。

## 备份恢复

`python -m asset_manager backup DEST` 保存一致性数据库、归档对象、项目索引清单、Git 历史目录及校验清单，清除备份中的登录会话。该命令需要容纳完整归档的空间。

恢复到停止中的服务：把 `catalog.sqlite3` 放入 catalog，将备份 `blobs` 映射为归档目录，复制 `project-index` 和 `git-history` 到 catalog，然后按清单验证。来源节点的工作文件不属于这份备份，除非已经归档；恢复后需核对或重映射真实工程路径。

## 运维检查

- systemd：`systemctl --user status ams-assets`，日志 `journalctl --user -u ams-assets`。
- LaunchAgent：检查 `catalog/server.log`、`catalog/server-error.log` 与节点工作台错误。
- 自动核对等待长任务结束；重启会把未完成的任务标记为 interrupted，可重试。
- 索引采样与 Git 历史持续保留。当前没有自动删除历史的策略；容量趋势需要通过归档存储与备份位置管理。
- SSH 接收端仅在对象与元数据完整时提交当前映射。不要用直接覆盖运行中数据库的方式同步节点。
