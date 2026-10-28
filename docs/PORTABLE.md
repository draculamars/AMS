# Atlas 随行资产库

这是基于 0.5.1 的独立便携开发分支 `codex/portable-vault`。它不改变 Mac、服务器部署的目录、功能规划、同步目标或 Git 分支；发布前仍需通过目标盘写入验证和运行验收。

## 目录

在已有磁盘上使用一个新建、独立的 `AtlasPortable` 目录。无需重新分区、格式化或移动其他目录。可更换磁盘名称和挂载位置。

```text
AtlasPortable/
  Start Atlas.command       Mac 启动入口
  Stop Atlas.command        Mac 停止入口
  start.sh / stop.sh         Ubuntu、Mac 终端入口
  launcher.py                平台无关启动和关闭管理
  portable.json             通过校验后才生成的当前数据集指针
  app/                      应用 ZIP、SHA-256、上一版应用包
  runtimes/                 四种平台的 Python、依赖和 Git 离线包
  datasets/<id>/catalog/    项目目录、关系、开发记录、知识目录、Git 历史
  archive/<sha-prefix>/     内容寻址的资产文件，保留完整逻辑路径映射
  backups/                  正常停止时的目录库一致性副本
  reports/                  导出、完整校验和关闭记录
  build/                    本次构建材料、写入验证临时文件
```

不在盘根目录建立 `.git`，不收录磁盘上的其他资料。原项目的未提交成果与已有 Git 历史分别保存；不代替工程自身的 commit/push，也不迁移 Codex 的项目配置。

## 启动与停止

- Mac：双击 `Start Atlas.command`。也可以在终端执行 `sh "/实际挂载目录/AtlasPortable/start.sh"`。
- Ubuntu：执行 `sh "/media/你的账号/磁盘名/AtlasPortable/start.sh"`。无图形桌面时，终端仍会输出访问地址。
- 仅绑定 `127.0.0.1`，从 `8770` 起选择空闲端口，不占用现有 8765/8767 部署。浏览器会自动打开；`ATLAS_NO_BROWSER=1` 可禁止自动打开。
- 停止：网页顶部“安全停止”，或双击 `Stop Atlas.command`／执行 `sh stop.sh`。等待终端显示目录库保存完成，然后通过操作系统弹出磁盘。不能直接拔盘代替关闭。
- 完整复核：`sh start.sh verify`。逐个校验所有已收录资产的 SHA-256、SQLite 完整性、索引清单与 Git 对象；数据量大时耗时较长。

磁盘接入不会自动执行程序。已经准备好对应平台运行包后，启动不需要联网、不需要安装 Python/Git/Node/Docker，也不需要管理员权限。

平台目标为 macOS 14 或更新版本的 Apple Silicon/Intel、Ubuntu 22.04 或更新版本的 x86_64/aarch64。平台包的存在不代表已在该硬件实测；部署验收应分别记录“已打包”“自动测试通过”“真实主机/磁盘验证通过”。专有 CAD 软件与 Blender 不在基础运行包中；STL、STEP、GLB 等查看器随应用提供。新增 `.blend` 渲染仍需当前主机的 Blender。

Mac 默认将运行包展开到专用目录中的 `runtimes/expanded`，避免占用内置盘；包内符号链接已物化为普通文件，兼容 ExFAT。Ubuntu 默认展开到 `${XDG_CACHE_HOME:-~/.cache}/atlas-portable/runtimes`，支持外置盘以 `noexec` 挂载的情况；需要约 500 MB 可用空间。`ATLAS_RUNTIME_CACHE` 可指定其他允许执行程序的缓存目录。缓存只包含应用和运行时，不复制资产与私人目录库。

## 可管理的内容与边界

可以查看和下载实际导出的项目文件、目录结构、文档、依赖、模型、资产版本、已有 Git 提交及文件历史、已索引的 Codex 公开开发记录和知识入口。原路径作为来源信息显示，读取不回退到接入电脑的同名原路径。

上传、资产版本、备注、标签和人工关系保存在此便携副本内。扫描接入电脑、重新绑定原项目、Git 首次提交、原件恢复和远端同步被便携模式明确禁用。更新原工程请回到原部署；便携副本的修改不会自动回写 Mac 或服务器。

导出范围沿用 AMS 的资产规则：依赖缓存、凭据与不安全符号链接排除；未保存/未取得的历史不会被补造。Codex 对话与知识内容是已有索引的可浏览副本，不是原件加密备份。原 Restic 恢复点的历史元数据可保留，但远端加密仓库和恢复密钥不会随便携导出被隐式复制。

关系解析的覆盖范围与既有缺陷仍会保留，例如原包中的失效相对路径不会因复制而变成有效引用。内容完整性、依赖完整性、渲染与工程可复现是不同的验收事项。

导出是逐文件验证的捕获区间，不是所有项目在同一时刻的文件系统快照。导出期间请尽量暂停工程的大批量写入。每个读取中的文件如发生大小/时间变化，会中止发布。此盘保存的数据明文可读；网页登录密码只保护网页访问。

## 构建、导出与升级

构建机需要 Python 3.12、uv、curl、Git；Mac 运行包还需要一份只依赖系统库的 macOS universal Git。官方 Python 构建包按发布方 SHA256SUMS 验证，Ubuntu Git 包按官方软件包索引 SHA-256 验证，来源与许可证随包保存。

```bash
python scripts/export-portable.py /新磁盘/AtlasPortable \
  --source-catalog /原AssetVault/catalog --source-archive /原AssetVault/archive
python scripts/build-portable-runtimes.py /新磁盘/AtlasPortable \
  --mac-git /Library/Developer/CommandLineTools/usr/bin/git
python scripts/package-portable.py /新磁盘/AtlasPortable
```

导出先以 SQLite online backup 复制原目录库，在副本中重新索引工程；原目录库和原 Git 仓库不写入。保存当前内容、历史资产、历史目录清单和可达 Git 对象后，对所有内容做 SHA-256 复核，并执行 SQLite integrity/foreign-key checks 与 Git fsck。只有全部成功才生成 `portable.json`。索引数字不是导出完成凭证。

失败时保留未发布目录；`--resume` 只支持仍然有效的未完成导出。已发布资产库不能被导出工具覆盖，以免丢掉便携版中新增的上传或备注。需要更新来源数据时，先导出到新目录并验收，再决定保留/迁移哪些便携修改。自动双向合并不属于当前便携分支。

应用升级只需在停止服务后再次执行 `package-portable.py`；不修改资产数据，保存上一版应用 ZIP。运行环境需改变时重新构建对应平台，校验和决定是否重用缓存。

写入持久性探针会在复制前检查测试文件关闭后是否仍存在且内容一致。若安全软件拦截、文件消失、空间不足或数据库检查失败，必须停止部署并处理原因，不将失败目录标为已发布。不得为部署停用或绕过企业安全控制。

## 验收

1. 导出报告中所有资产 SHA-256、目录库与 Git 校验通过；所需运行包完整且来源可追踪。
2. Mac 和 Ubuntu 从实际介质启动，网页工程树、历史、开发记录、下载、上传、模型查看通过。
3. 更换包含中文和空格的挂载路径，原工程不可访问时仍能浏览；同名原工程存在时也必须读盘内副本。
4. 重复启动复用现有服务；占用端口不误杀其他服务；停止后校验目录库并创建恢复副本。
5. 非法路径、缺失/篡改内容、半成品导出、写入失效和源文件变更都应阻止发布或给出明确错误。
6. 主分支和现有 Mac/服务器实例不受影响。只有通过实际验收的项才能写入部署完成记录。

运行时来源：[Python Build Standalone](https://docs.astral.sh/python-build-standalone/)、[Ubuntu 官方软件包](https://packages.ubuntu.com/)、[Apple Git 源码](https://github.com/apple-oss-distributions/Git)。
