# 网页与应用归档

网页目录属于 Atlas 的资产管理能力。Token 看板、SpaceLens、Atlas 和各工程应用继续独立运行；登记页面不自动启动后端，不改变其服务、端口、数据库或源码仓库。

## 数据与来源

- `web_entries`：项目＋相对路径形成稳定身份；记录标题、类型、直接依赖及明确的会话文件事件。
- `web_bundles`：按内容哈希标识的不可变清单，保存相对路径到 SHA-256 对象的映射；普通发布复用本节点完整接收的工程映射。
- `web_publications`：网页入口与清单的版本关系；保留历史，浏览验证与归档状态分开。
- `catalog/web-bundles/<sha>.json.gz`：清单文件。服务备份必须包含此目录、数据库及引用的归档对象。
- `web_objects`：已发布网页引用的去重对象索引，纳入侧栏归档容量；容量不含尚未发布的传输暂存或独立 Restic 仓库。早期开发版升级后重新导入已有清单以补全此索引，不生成重复发布。

登记只解析 HTML，不执行脚本。来源会话依赖已索引的明确文件事件，未匹配时显示缺口。直接依赖分析不覆盖全部动态请求、CSS、JavaScript 或外部后端，不把没有直接报错标为完整可运行。大于 8 MiB 的 HTML 不自动解析；最多记录 500 项直接引用并标记截断。

已归档目录不等于已部署工作副本；设备控制、采集和系统操作网页不会在备份节点自动启动。外部参考网页保留来源属性。

## 操作

配置 `ASSET_DATA_DIR`、`ASSET_BLOB_DIR` 后执行：

```sh
.venv/bin/python scripts/web-assets.py discover
# 普通发布仅使用本节点已接收的 mirror_files，不读取原始主机路径。
.venv/bin/python scripts/web-assets.py publish
.venv/bin/python scripts/web-assets.py verify --bundle BUNDLE_SHA256
```

内容校验不是功能验收。网页目录提供固定版本预览和历史切换；根 URL、外部 API、动态导入可能仍需适配。浏览器验收应检查控制台、请求、模型渲染、下载及预期交互，单独保存结果。

验收完成后可用 `web-assets.py record-verification --publication ID --manifest evidence.json` 保存范围明确的验收证据；文件要求 `status: passed`、`checks` 数组和 `tested_url`，签名访问令牌不会写入目录。验证某一发布不会把其他页面自动标为通过。

## 模型库静态适配

`freeze-model-library.py` 适配 `model_library/catalog.json` 格式，不依赖机器人名称或主机路径。

```sh
.venv/bin/python scripts/freeze-model-library.py \
  --root /absolute/project --project PROJECT_ID \
  --output /absolute/staging --session SOURCE_THREAD_ID --send
```

先刷新原模型库目录。适配器保存原前端代码和清单、生成冻结的交互前端、纳入目录所列文件及 Three.js 依赖和许可证。内容哈希去重，越界链接及读取期间变化会拒绝；不会覆盖原有 CAD、URDF 或制造状态。

目标节点已有项目后，传输 `publication.json`，执行 `web-assets.py import --manifest /path/publication.json`。`--send` 只发送缺少的对象，不自动发布或运行程序。发布前检查所有对象存在。已有交付 ZIP 保持原字节；冻结页不提供原服务的即时 ZIP 构建，不伪造缺失历史包。

## 预览边界

管理员生成一小时有效、限定发布清单的签名入口。预览仅可 GET 清单中的文件，无命令执行、写入、原路径回退或管理权限。CSP sandbox 允许脚本和下载但不给同源身份，不发送 Referrer；模块加载使用无凭据 CORS。管理 API 保持认证和跨站写入检查。签名 URL 是临时访问凭证，不公开分享。

首次打开不会自动标为已验证。外部资源、绝对路径及后端依赖显示为待处理。模型设计、描述修订、展示修订保持独立；撤回与制造状态以项目记录为准。

## 验收

合成测试覆盖历史冻结、缺失归档不退回原目录、路径越界、错误签名、管理接口隔离及缺失依赖报告。真实验收在目标节点或其隧道进行；原工程不可访问仍可浏览才计为迁移验证。网页可打开不等于模型通过制造验收。

模型库浏览验收脚本为 `scripts/web-model-browser-qa.cjs`。设置 `PLAYWRIGHT_MODULE`、`ATLAS_QA_URL`、`ATLAS_QA_OUTPUT` 和 `ATLAS_WEB_CASES`；使用已安装 Chrome 时设置 `ATLAS_BROWSER_CHANNEL=chrome`。用例 JSON 包含 `entry_path`、`expected_cards` 和 `models` 数组，每个模型声明 `title`、预期 `status` 文字及可选 `download_sha256`。脚本保存截图、所测模型及限制，访问凭证不会写入报告。

## 节点部署与备份

源节点用 `configure-sync --host USER@HOST --root /absolute/AssetVault` 配置目标，实例地址不写入应用代码。传输源包后，目标依次安装锁定依赖、运行 `scripts/install-release.py` 和 `scripts/install-service.py`。使用回环监听配合 `scripts/open-tunnel.sh` 访问；公网部署另行配置身份验证和 HTTPS。

目标执行网页发现、发布和模型库清单导入。源节点未完成接收的工程不得标记发布成功。静态发布需要 `catalog.sqlite3`、`web-bundles` 和所有引用的内容对象；`python -m asset_manager backup` 同时包含普通资产与网页独有对象，并校验 SHA-256。加密原件备份另用 `vault` 配置，显式包含生成发布目录；保留源工程 Git 与软链接语义，恢复密钥另存。

同步是文件级捕获，不是跨工程原子快照。最终回执应分别记录同步范围、跳过项、网页发布数、浏览器验收范围、加密快照及恢复演练结果。Linux 用户服务若未启用 linger，不能承诺无人登录时自动启动。
