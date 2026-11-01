# 安装、生命周期与部署

运行数据只放在运营者自己的数据目录，不进入源码、软件包、镜像或静态站点。Python 项目通过 `pyproject.toml` 安装，并使用已安装的 `nannanews` 命令。

## 安装与配置

需要 Python 3.11 或更新版本，以及系统 Cairo 共享库。Debian/Ubuntu 安装 Cairo 后，再安装 Python 软件包：

```sh
sudo apt-get update && sudo apt-get install -y libcairo2
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
nannanews --help
```

生成 PDF 另需 Typst 和中文字库；Docker 镜像已内置这些运行依赖。

`NANNA_DATA_DIR` 指定私有数据根目录。未设置、为空或只有空白时，默认为 `$XDG_DATA_HOME/nannanews`，通常是 `~/.local/share/nannanews`。数据库、上传文件、采集依据和会话密钥都保存在这里；限制该目录的访问权限。将 `.env` 权限设为 `0600`，不要提交 `.env`、密钥、数据库、上传文件或备份。

`NANNA_SECRET_KEY` 留空时，显式准备运行环境会在数据目录中生成私有 `session.key`。配置加载、CLI 帮助和来源目录导入不会写文件。`nannanews import-source-catalog INPUT OUTPUT` 将审核过的工作簿 JSON 导入公开来源目录，不访问数据库或网络。

## 本地服务与目录布局

本机将维护中的源码留在 `~/project/NannaNews/`，实例集中在 `~/service/nannanews/`。不要将仓库复制进实例目录，也不要在项目父目录另存 Compose 文件或 PDF。

| 实例目录 | 用途 |
| --- | --- |
| `config/` | 私有 `console.env`、独立的 `reader.env`，权限 `0600` |
| `data/runtime/` | 控制台数据库、上传、账号及会话密钥 |
| `data/history/` | 单一 `foundation.sqlite3`；`blobs/<SHA-256>` 统一保存正文、PDF、图片、请求与回执原始字节 |
| `data/state/` | `XDG_STATE_HOME`，含微信桌面的宿主锁和私有导出 |
| `data/legacy-sources/` | 冻结的历史来源库和付费防重原始台账，不作为另一套活动运行库 |
| `publication/` | 审核后的 `source/` 与读者端 `site/`，保持在同一文件系统以支持硬链接 |
| `secrets/`、`tools/`、`backups/` | 独立保管的凭据、归档工具及本地备份 |
| `ops/legacy/` | 既有整理记录、恢复现场和旧部署文件；仅保留溯源，不作为活动配置 |

实例根目录限制为 `0700`。原件文件名、来源路径和日期仍保存在目录元数据中；当前文件由附件摘要定位，历史来源路径不是搬迁后的文件地址。删除散落 PDF 前，须核对附件池中的大小和 SHA-256；不能删除仅有的一份原件。迁移冻结来源库使用同文件系统改名，保持原库字节、inode 和修改时间，不重新初始化付费台账。

两个本地服务由 systemd 用户管理器运行，源码模板在 `deploy/systemd/`。先停止旧实例的写入者，准备已迁移到当前结构的运行库、历史底座和已审核站点；已有账号或发布目录不要再次初始化。将 `config/console.env` 中的 `NANNA_CODE_ROOT` 指向源码绝对路径，`NANNA_DATA_DIR` 指向 `data/runtime`，`NANNA_READER_EVIDENCE_DIR` 指向 `data/history`，`NANNA_READER_DATA_DIR` 和 `NANNA_READER_SITE_DATA_DIR` 分别指向 `publication/source`、`publication/site/data`，`XDG_STATE_HOME` 指向 `data/state`；保留原有会话密钥。设置 `NANNA_PORT=53684`、`NANNA_PUBLIC_URL=http://127.0.0.1:53684`、`NANNA_READER_URL=http://127.0.0.1:53685/`，搜索构建器指向源码中的 `reader/scripts/build-search.mjs`。采集保持关闭，不继承模型凭据。

`config/reader.env` 只需 `NANNA_CODE_ROOT`、`NANNA_READER_PORT=53685` 和指向 `publication/site` 的 `NANNA_READER_SITE_ROOT`；不要让静态读者服务加载控制台私有配置。EnvironmentFile 不是 shell 脚本：路径使用实际绝对值，不写 `$HOME` 或变量引用。在源码根目录注册、启动并查看服务：

```sh
systemctl --user enable --now "$PWD/deploy/systemd/nannanews-console.service" \
  "$PWD/deploy/systemd/nannanews-reader.service"
systemctl --user status nannanews-console.service nannanews-reader.service
journalctl --user -u nannanews-console.service -u nannanews-reader.service
```

控制台只监听 `127.0.0.1:53684`，读者端只监听 `127.0.0.1:53685`，其 HTTP 根目录只能是 `publication/site`。停止使用 `systemctl --user stop nannanews-console.service nannanews-reader.service`。用户管理器在登录时启动；无登录常驻需另行配置用户 lingering，不把本地测试当作公网部署。

`~/service/nju-social-acquisition/` 是独立的第三方采集工作区，不是本应用的源码或运行依赖，不合并其 vendor、账号或私有状态。微信客户端会话仍由 Docker 私有卷 `nannanews_wechat_desktop_private` 管理，不手动搬动 Docker 内部数据；宿主操作前设置 `XDG_STATE_HOME="$HOME/service/nannanews/data/state"`，使 `runtime.sh` 与控制台使用同一宿主状态目录。

## 数据库生命周期与备份

`nannanews migrate` 会显式准备运行环境并执行有版本记录、可重复的结构迁移；它不调用模型、不采集来源，也不执行历史回填。首次创建管理员时，`nannanews init-admin owner` 会先迁移，再创建账号；没有默认密码，也可使用 `--password-stdin` 从标准输入提供密码。

当前服务结构版本为 3：版本 2 保存人工修订，版本 3 新增稳定的新活动登记和按数据集区分的私有证据关联。从旧版本显式迁移不重写原始依据，也不回填历史。私有底座自己的结构版本与服务版本分别管理。

`nannanews serve --host 127.0.0.1 --port 8080`、`nannanews collect-worker` 和读取命令都要求数据库结构为当前版本；启动或读取不会静默迁移。结构缺失、过旧或过新时，命令会拒绝继续并要求显式迁移。采集 worker 仍须由运营者设置 `NANNA_COLLECTION_ENABLED=true` 并单独启动。

备份或迁移前，先停止 Web、采集、历史复核及其他所有写入者。备份放在运行数据目录之外，并限制访问：

```sh
nannanews backup "$BACKUP_FILE"
nannanews migrate
nannanews restore "$BACKUP_FILE" --target "$FRESH_DATA_DIR"
```

`BACKUP_FILE` 和 `FRESH_DATA_DIR` 必须由运营者设置为实际路径。恢复只允许写入当前数据根目录之外的全新目标目录；备份包含数据库、文件及付费尝试防重历史。恢复时，`NANNA_DATA_DIR` 仍须指向已停止写入的当前运行数据：命令会保留其中较新的历史台账、原始回执，以及原生来源提取和每日采集的付费尝试防重事实，包括备份之后才发生的尝试。不得删除、重置或替换这些记录以重试付费请求，也不得原地降级数据库。备份不包含运行凭据或会话密钥；恢复后要配置自己的凭据，现有登录会话也会失效。

原生防重按规范化来源 URL 和内容摘要匹配，即使备份中没有该来源也保留尝试记录和回执；这不恢复备份之后新增的草稿或采集结果。已完成、结果不确定及已失败的尝试都不会因恢复而重新进入自动采集；既有的显式编辑重试规则不变。

## 离线活动数据底座与历史清理

`nannanews data-foundation` 管理独立的私有 `foundation.sqlite3`，不是服务数据库迁移或站点发布命令。它保留既有活动 ID、来源关联、日期事实与历史修订，不做语义归并，不调用模型或重新抓取网页。控制台直接只读查询同一历史数据库，不生成逐行 JSON 或另一份查询数据库。

准备私有目录并指定已审核的公开包。`--canonical-data` 接收原运行数据目录，仅读取其中的 `database.db` 和原件；`--originals-plan` 接收已核对的原件清单。不要将底座放入源码、公开目录或原运行数据目录：

```sh
export FOUNDATION_ROOT="$HOME/service/nannanews/data/history"
nannanews data-foundation build --root "$FOUNDATION_ROOT" \
  --public-data "$REVIEWED_PUBLIC_DATA" \
  --canonical-data "$CANONICAL_DATA_DIR" \
  --originals-plan "$ORIGINALS_PLAN"
nannanews data-foundation verify --root "$FOUNDATION_ROOT"
```

`build` 和 `extract` 是显式准备操作，可升级底座自己的结构；不会升级原服务数据库。底座结构版本 2 将误存为日期原文的 JSON、HTML 和超过 512 字符的片段改存摘要与省略原因，保留日期、依据和修订 ID。`compact` 仅对已准备且结构为当前版本的底座执行 VACUUM，不接受原服务数据库。

先停止本底座的其他写入者，再按有限批次提取历史文件。`inputs.json` 是选定路径的 JSON 数组；单个 JSON 或 JSONL 记录最多 128 MiB。未知格式、损坏、超限、输入变化和依赖外部原件的记录保留为 `held`，不能据此删除原文件。

```sh
nannanews data-foundation extract --root "$FOUNDATION_ROOT" \
  --inputs-from "$INPUT_MANIFEST"
```

`extract` 只写本地正文、图片和核查结果，按既有来源中精确声明的快照网址或缓存原始请求网址关联活动，不自动备份或删除输入。提交后控制台直接读取新记录，无须刷新查询目录。跳转地址用于快照身份，不改写活动身份；共享快照不合并活动。登录页、验证码和 HTML 错误响应只保留为失败依据，不当作活动正文或图片。

`receipt.json` 提取会核对同目录实际 `request.json`、`response.bin` 的字节数与摘要，分别保存原始请求、响应和回执，包含准备后的图片数据及失败／截断响应。历史请求缺失时明确记录，不从来源网页重建；原始文件始终保留。已有 SQLite 来源捕获可用 `data-foundation preserve-captures --root "$FOUNDATION_ROOT" DB...` 离线保全原始 JSON、HTTP 和媒体字节；输入只读，记录始终 `held`。摘要不符的内容与声明分开记录，不能宣称已验证或删除源库。

捕获导入读取原生 `source_documents` 的 `source_url`、`digest` 和 `document_json`，保留原记录的 `id` 与 `rowid`。不符合该结构的 SQLite 库保留为 `held`，不会改写源表来适配导入。

`data-foundation preserve-legacy-attempts --root "$FOUNDATION_ROOT" LEDGER...` 按原台账行中明确的请求键保全 `request_json`、`raw_output_json` 和 `decision_json`，将结果关联到该次历史请求，不改写防重状态或已有结果引用。控制台分别标为历史请求元数据、历史解析结果和历史判定记录；它们不冒充实际 HTTP 请求或提供方原始响应。没有请求外键的历史 outcome 五元组只保留原记录，不猜测归属。大台账按行读取并核对来源文件身份及每项内容摘要，不为验证而重新全量散列 40 GB 原库；原库始终保留。

通用 `data-foundation extract` 只处理有界 JSON／JSONL、回执及捕获包，遇到 SQLite 文件一律保留，不打开事务边车，也不全量散列大型台账。SQLite 捕获库使用 `preserve-captures`，付费历史库使用 `preserve-legacy-attempts`；有活动事务边车的源库先由运营者一致停写，不能绕过保护导入。

`verify` 默认执行 SQLite `quick_check`、外键和业务引用检查，并检查本地文件位置、大小及登记的原件身份；不会访问云端，也不宣称验证了全部内容摘要。合并、迁移或显式全量巡检时使用 `verify --full`，增加 SQLite 完整索引检查与本地文件 SHA-256 核对。本地内容缺失明确报错，不通过云端证明替代，也不自动恢复。

原图归档保留 PNG、JPEG、GIF、WebP、BMP、TIFF 与 SVG 的原始字节及摘要，不重编码或渲染图片。SVG 仅接受有界、禁止 DTD／实体／外部实体的 XML 根节点；这不是面向浏览器的安全预览，后续展示或模型接入仍须执行现有媒体预检。

### 配置原生 restic

归档使用原生 restic 仓库，由 restic 负责加密、分块、去重和快照历史，不再另建分卷或附件索引格式。日常历史统一存入一份 SQLite 数据库；来源台账和整理批次作为溯源元数据保留，不再作为日常查询的数据集选项。

先安装 restic 并准备源码、原运行数据、底座和公开目录之外的私有仓库与密钥路径：

```sh
export RESTIC_BIN="$HOME/service/nannanews/tools/restic/0.19.1/restic"
export FOUNDATION_REPOSITORY="$HOME/service/nannanews/backups/archives/restic"
export FOUNDATION_PASSWORD_FILE="$HOME/service/nannanews/secrets/restic-password"
nannanews data-foundation archive init --root "$FOUNDATION_ROOT" \
  --repository "$FOUNDATION_REPOSITORY" \
  --password-file "$FOUNDATION_PASSWORD_FILE" --restic-binary "$RESTIC_BIN"
```

`init` 显式初始化或打开仓库；新密码文件以 `0600` 创建，不打印密码，不复用试验密钥。已有密钥必须归当前用户所有、权限私有且不是符号链接或硬链接。密钥不包含在归档中，须另行安全保全；丢失后无法解密。已绑定的底座拒绝静默更换仓库或密钥。


内部数据目录为 `nju:NannaNews`，仅供南哪消息内部工作人员使用，不作为公开静态站目录。原生归档目标为 `rclone:nju:NannaNews/activity-snapshots/restic-production-20261027`。仓库搬迁由运营者使用原生工具完成，保留源仓库和全部历史；不要把明文密码或临时锁混入复制。目标复制完成后可显式重新绑定：

```sh
nannanews data-foundation archive relocate --root "$FOUNDATION_ROOT" \
  --repository "$COPIED_REPOSITORY"
```

`relocate` 不上传、不初始化、不换密钥或删除源。它检查同一仓库 ID、完整快照及标签，并对目标执行原生数据检查后才更新绑定；失败保留旧绑定。源位置不可访问时不要伪造来源或手改绑定；保留原配置，先使用原生工具处理搬迁。没有旧云端目录的自动回退或专用旧前缀迁移接口。

密码文件独立保管，不进入 Git、公开目录或 restic 仓库。只有运营者明确授权且确认内部目录访问权限后，才可另行备份到 `nju:NannaNews/archive-keys/`；同一账号同时保存密文和明文密钥不能抵御该账号泄露。上传后须读回核对并实际解密打开仓库，不能仅凭文件存在删除旧备份。

### 备份、恢复与附件读取

```sh
nannanews data-foundation archive backup --root "$FOUNDATION_ROOT"
nannanews data-foundation archive verify --root "$FOUNDATION_ROOT"
nannanews data-foundation archive restore --root "$FOUNDATION_ROOT" \
  --snapshot-id "$SNAPSHOT_ID" --target "$FRESH_FOUNDATION_ROOT"
nannanews data-foundation archive get --root "$FOUNDATION_ROOT" \
  --sha256 "$BLOB_SHA256" --output "$FRESH_BLOB_FILE"
```

`backup` 先通过 SQLite Backup API 创建一致性目录副本，再将该副本与登记的本地文件直接交给 restic；不先复制一整套附件或生成逐行 JSON。引用的本地文件缺失时停止，恢复是另一个显式操作。任何非零备份退出码，包括部分成功，均不登记成功快照。原生备份成功只表示该次备份完成，不冒充全仓数据巡检或恢复演练；普通备份不自动执行它们。

普通备份记录 `content_hashes_verified=false`，表示未重新核对每份本地文件与目录中的摘要，不把原生命令成功标作全内容验证。若怀疑本地文件已经损坏，先显式执行本地 `verify --full`；仓库检查验证的是备份存储本身，不能替代这项应用目录与文件的对应检查。

`archive verify` 默认执行原生仓库结构检查；`archive verify --full` 才读取全部备份数据，两者都不自动恢复整库，也不因本地后来有新数据就把旧备份判作损坏。恢复是独立的 `archive restore` 操作，可选择已登记的本数据集快照。目标必须不存在、位于当前底座和原件之外，且父目录已存在；恢复后附件位置重绑为目标内的 `blobs/<SHA-256>`。`get` 只导出指定附件，核对摘要和大小后发布新文件，不覆盖已有文件或跟随符号链接。

新的快照使用 restic 原生文件树保存目录副本和登记文件，SQLite 中的附件位置用于定位该快照的内容。历史 `/payload/foundation.sqlite3` 与 `/payload/blobs/<SHA-256>` 快照仍可恢复，但旧的派生查询文件不再成为运行或备份前提。

```sh
"$RESTIC_BIN" --repo "$FOUNDATION_REPOSITORY" \
  --password-file "$FOUNDATION_PASSWORD_FILE" snapshots
"$RESTIC_BIN" --repo "$FOUNDATION_REPOSITORY" \
  --password-file "$FOUNDATION_PASSWORD_FILE" \
  restore "$SNAPSHOT_ID" --target "$FRESH_NATIVE_RESTORE" --verify
```

### 连接单一历史数据库

将控制台的私有证据设置直接指向已准备的底座根目录：

```sh
export NANNA_READER_EVIDENCE_DIR="$FOUNDATION_ROOT"
```

该目录必须包含已准备的 `foundation.sqlite3`，并与源码、公开站点和控制台服务数据库相互独立。浏览只读打开 SQLite，不迁移、不生成查询缓存、不创建备份，也不调用来源或模型。原始请求、响应、回执、图片及 PDF 只在选中后读取登记的本地文件并检查大小和摘要；文件缺失就明确报错，不自动从云端下载或重建。

历史合并保留稳定活动和核查身份，重复附件按摘要复用，同一付费请求的不同结果保留为历史。来源和整理批次保留为元数据；没有多目录清单或资料目录选择器。既有人工证据关联须在停写切换时显式迁移到单库身份，不使用旧目录别名自动授权其他库。

编辑或管理员登录后可打开“历史刊物附件”（`/console/documents`），按文件名、刊期日期、期号或完整 SHA-256 检索。列表只读取元数据，原刊可独立于活动保留，不因日期或期号相同就自动建立关联。

“下载原刊 PDF”通过 `/console/documents/<SHA-256>/pdf` 读取已登记的本地原件；不能用任意模型附件摘要替代文档身份。下载要求编辑或管理员身份并禁止公共缓存。后台保存和公开发布仍是不同操作。


清理同一 PDF 的本地副本时，先核对 SHA-256 和大小；需要保留的发布与归档路径可用硬链接共用一个文件实体，不重复存储内容。不同字节的版本分别保留，不因同日或同期号合并。PDF 页眉可能沿用旧日期和刊号；经人工核对更正内容归属日期时，保留原始提取及确认依据，不改写 PDF。刊号默认保留未知；经用户明确授权，可依据前后已知刊期的连续编号推算，并另存授权、锚点和推算值，不能把推算值标作原刊记载。刊期不一定逐日连续，也不能仅凭日期或推算刊号绑定公开期次。


业务表中的旧绝对路径是来源信息，不代表新机器的文件位置。跨机器恢复应使用显式 `archive restore` 完成本地位置重绑定，不借用旧机器路径或手改仓库身份。

### 数据保留与维护

原件、付费防重台账、原服务大库、原始回执和运行凭据不是可自动删除的缓存。输入变化、未知格式、网络或空间失败时保留输入；重跑相同清单复用已完成记录，变化的输入转为待处理，不覆盖旧证据。程序不提供“备份后删本地缓存／源文件”的自动或批量入口。

经用户明确授权删除无法归属的业务记录时，在同一 SQLite 事务中清理指定记录及其专属关联，核对其余记录不变；这不删除原刊、原始付费台账或改写旧快照。提交后查询直接反映新状态，云端备份单独安排。只在确有空间需求时显式执行 `data-foundation compact` 回收 SQLite 空闲页，不将压缩或整库检查附带到每次写入中。

```sh
nannanews data-foundation compact --root "$FOUNDATION_ROOT"
nannanews data-foundation export --root "$FOUNDATION_ROOT" --output "$NEW_PUBLIC_JSON"
```

`export` 只向不存在的新文件写入活动要点、状态、来源链接和日期，不含私有正文、图片、核查答复、云端位置或凭据。它不会替换现有站点；原站点仍按独立发布流程维护。现有服务仍依赖的付费尝试库和原始回执继续保留，不能因为底座已提取部分事实就手工删除。

## 公开来源与采集版本

首次初始化会将包内 `data/automation_sources.json` 复制到 `$NANNA_DATA_DIR/automation_sources.json`。此后以运营者的私有文件为准；升级不会用新种子覆盖它。已有部署新增来源时，审核包内对应定义并合并到私有文件，不要删除文件来重置来源、队列或成功检查点。

新增定义的 `enabled: true` 表示运营者允许采集；注册来源在当轮身份／发现核验前仍可保持 `pending_validation`。隔离验收通过不跳过生产校验，也不等于已立即运行采集或付费解析。

数学学院的 `math-recent-reports` 使用 `nju_math_reports` 适配器，读取官方 `reports.do` 分页接口，不依赖动态列表中的空链接。列表按报告时间排序；适配器在分页预算内持续扫描，保留尚未处理的分页，不会因遇到已知链接或较早发布日期而提前结束。报告时间用于活动优先级，不替代来源发布日期。

就业资讯仍使用官网前端调用的详情 API。官方返回 401 的文章保留受限响应依据，不改用未经验证的接口、不自动登录。图片限制根据隔离实测制定；仍超出正式预算或无法完整解码的原件保留阻断原因，不把部分图片当作完整正文。

当前采集协议 `public-source-v9-article-media-20261023` 保留 v8 的媒体预算、v7 的有限像素预算、v6 的图像准备流程和 v5 的精确正文边界：在已有精确选择器之后识别 `.article.w`，避免回退读取整页而把导航和页脚图片混入正文；不扩大到所有 `.article` 元素。WebPlus 仅在同一 `.article` 具有直属标题、唯一正文及正文外成对的详情标签时保留同文的时间、地点和海报；普通发布者／浏览计数外壳仍排除。仍在 VSB 正文内优先采用 `src="/__local/..."` 的官网图片，避免残留微信 `data-src` 指向旧资源；微信文章仍优先使用自己的懒加载地址。原始 HTTP 响应不改写。正文图片地址如果实际返回首页 HTML，仍不具备完整图片证据，不能靠删除这些图片标记采集完成。

v7 将三项像素预算在 v6 基础上翻倍，给已观察到的最大样本留出明显余量；Pillow 仍保留有限硬错误阈值，并与单帧上限同步。像素保护主要约束解码内存，不由磁盘空闲容量决定；每图最多 64 帧／页、原始单图 32 MiB／每文 128 MiB 等预算不变。

| 像素预算 | v6 | v7 |
| --- | ---: | ---: |
| 单帧 | 200,000,000 | 400,000,000 |
| 单图累计帧 | 256,000,000 | 512,000,000 |
| 整文累计 | 512,000,000 | 1,024,000,000 |

历史依据仍是 1,795 份留存公开文档、4,020 份去重媒体的隔离普查：单帧 p99 为 32,741,376 像素，最大 193,174,220；五张超过最初上限的原件均已完整解码并查看内容，最大文档累计 488,309,116 像素。元数据普查不等于所有媒体已通过解码，损坏 MPO、返回 HTML 的图片响应和不完整采集仍拒绝进入模型。

最初的大图试验在串行子进程中放宽到 10 亿像素，并设置 4 GiB 地址空间、60 秒 CPU／90 秒墙钟上限。v7 另用两色合成输入验证新增区间，子进程限制为 6 GiB 地址空间、90 秒 CPU／120 秒墙钟，模型凭据为空且网络命名空间隔离；两例均完整解码、预览色块正确，原始压缩字节及摘要不变。这些是合成测试，不是新增公开样本，也不代表所有实图都有固定内存占用。

| 合成输入 | 像素 | 图片准备时间（秒） | 进程峰值 RSS（MiB，含造图） |
| --- | ---: | ---: | ---: |
| PNG | 399,980,000 | 6.278 | 2,083.7 |
| JPEG | 225,000,000 | 1.351 | 914.7 |

单帧 JPEG 继续采用解码器缩放，完整读取后先原地缩小，再做 EXIF 方向拷贝；多帧仍完整解码并生成联系表，不跳过损坏后续帧。正式整文预检已覆盖真实大图来源和损坏／不完整来源；预算外输入仍保留原件并拒绝付费调用，不取消有限像素保护。

v8 区分每来源与多来源模型合计：单来源、上传及 PDF 仍最多 64 张／页，模型合计最多 128 张；不拆分、截断图片或跳过动画后续帧，既有字节、像素和请求预算不变。静态 SVG 可引用规范的 `data:image/png;base64,...` 内嵌单帧 PNG：验证完整 PNG 结构、校验和、像素及解码，每张最多 16,777,216 像素，重复引用也计入每 SVG 的 32,000,000 内嵌像素预算；SVG 原件仍最多 2 MiB。空本地裁剪片段 `url(#)` 按渲染器的无裁剪语义处理，不改写原件。APNG、外部资源、递归 SVG、脚本、事件、DTD／实体和其他既有禁用结构仍拒绝；自定义加载器仅返回预先验证的精确 PNG 字节，不回退到普通资源加载器。15 条真实留存准备失败已完整通过新预检，732 个原始图片位置均保留；这不是模型审查成功的计数。

v9 按正文内的 DOM 顺序保留内联 `background-image: url(...)` 声明的图片，包括以 SVG 背景承载的海报；不加载外部样式表或执行 SVG／脚本，本地绘制片段不当作远程图片。已选定正文内的 `header` 可能承载通知或海报，因此保留其内容；回退读取整页时仍排除站点头部。数学学院的 `.article_content` 和 `.edui-pdf[data-pdf]` 海报声明沿用既有 PDF 完整采集流程，缺地址不算完整来源。微信静态图文除 `content_noencode` 外，还读取字面量 `picture_page_info_list` 中每页直接声明的 `cdn_url`，不使用嵌套的封面、水印或原图元数据，不执行动态表达式，也不接受展开、计算属性或歧义图片清单。发现新图片或附件只表示应补齐素材；所有既有地址、媒体排除、完整性和模型预检仍适用，不等于核查成功。

公开来源的 OS DNS 若全部落在标准 Fake-IP 池 `198.18.0.0/15` 或 `2001:2::/48`，仅对非字面量公开域名使用固定 Cloudflare DoH：连接公共 `1.1.1.1`，以 `cloudflare-dns.com` 校验 TLS／SNI，并保留原来源与解析器完全相同的继承代理／直连策略；任一配置不一致即拒绝，不切换线路。A、AAAA、CNAME 和附加地址仍须通过原公共地址检查；混合私网答案、真实私网、TLS 失败、重定向解析器或无效 DNS 响应不获放行。该路径不修改 OS DNS、代理或路由，也不影响模型解析；公开来源重定向仍逐跳校验，验证码、登录及付费限制不绕过。

正文边界和媒体选择变化会影响来源摘要，因此不能将新版用于接管旧协议冻结的历史复查：继续使用冻结时版本，不改 `scope.json`、缓存或付费台账来强行恢复。新版本会在请求之前拒绝配置不匹配的冻结任务；已有不可变缓存不会因此重新解析。

## 历史日报链接提取

HTML 导入保留活动表格行、详情正文和详情标题中实际出现的 HTTP／HTTPS 网址，包括没有包在锚点里的明文网址。已有锚点以 `href` 为准，不把缩短的显示文字当成另一个地址；脚本、样式和模板文字不参与明文网址提取。外围标点不属于网址，路径中成对的括号和查询参数继续保留。链接只关联到其所在活动，不从相邻活动复制。

提取出网址不等于确认主来源；报名表、群邀请、读者归档页和图片材料仍须分别判断用途与可读性，不能把二手日报链接直接升级为一手依据。升级解析代码不会改写旧原件、既有入库记录或冻结复查范围；相同原件摘要的重复导入仍复用原记录。历史重新提取应先保留原件摘要和定位依据，生成隔离候选并核对整类结果，再单独决定是否应用，不能为了补链接重置模型尝试或旧回执。

## 历史复查的网络冷却与重启

历史全文复查将临时传输故障与模型内容质量分开处理：

| 故障 | 处理 |
| --- | --- |
| 连接／超时、明确的响应截断、HTTP 408／429／5xx | 首次冷却 10 分钟；尚未恢复有效响应时再次失败，冷却 30 分钟。合法 `Retry-After` 更长时遵守服务端等待时间。 |
| 无法通过严格校验的模型内容／JSON，或不可确认属于临时网络问题的失败 | 保留独立的连续三次质量熔断，须运营者检查并确认。401／403 等永久 HTTP 错误不当成可恢复限流。 |
| 停止／人工暂停、每日采集优先、磁盘不足、冻结配置或范围变化、请求预算耗尽 | 继续保留原有阻断，不因网络冷却结束而绕过。 |

`transport_cooldown` 是有心跳的有意等待，状态显示 `transport_failures`、`cooldown_until` 和 `cooldown_remaining_seconds`；不把正常冷却误报为进度卡死。停止请求会及时结束等待。只有新的有效模型响应才证明服务恢复并重置网络退避；免费复用既有回执不会重置退避。

**自动恢复是继续未处理队列，不是重新发送旧的付费逻辑请求。** 已完成、已失败、已保留但结果未知的尝试仍永久受原台账保护；超时或断线可能已经被计费，不能删除记录、清零预算或重置请求身份来重试。冷却与质量计数通过操作日志重建，重启不清除历史失败或付费防重事实，也不重写原回执。

重启冻结批次前，使用与原捕获／媒体／请求／规则／审计协议及提示词完全一致的运行版本；操作性冷却修复可以应用到该版本的独立副本，但必须先核对其冻结配置，不能只改版本字符串冒充兼容。保留原数据根目录、输出目录、请求预算和台账，先确认旧工作器已退出，再使用 `nannanews review-bulletins-full --output "$FULL_REVIEW_DIR"` 恢复。同批次监控使用 `nannanews watch-bulletin-full-review --output "$FULL_REVIEW_DIR" --interval 600`。启动日志或进程存活不足以证明恢复，应检查新增进度／真实冷却状态与心跳。

大批冻结回执的重建可能先经历较长的无输出启动阶段；不能因就绪等待超时就再启动一个工作器。先核对进程身份、重建状态和原计数。质量熔断会在重启后保留，检查触发失败的回执并获准恢复后，才在输出目录创建一次普通文件 `resume-request`。工作器仅在 `quality_circuit` 状态消费该文件、追加 `quality_acknowledged` 事件并清除连续质量失败计数；累计失败、请求数与旧终态不清零。不得用循环写入该文件的方式绕过再次熔断。

历史审查说明位于 `src/nannanews/content_rules.py`，与普通草稿提取说明分开维护。说明要求先选择证据，再核对学院原名与关系是否同时出现在同一段引文或同一张原图中；不能通过补“南京大学”、删除校区限定或借用讲者单位来推断学院关系。日期结论必须在构造 claims 后决定：所有日期均为 `null` 时不得返回 `resolved`，但也不能为了满足校验而补年份或丢弃未知日期。输入图片可直接阅读，不授权生成、重绘图片或调用工具。Responses 与 Chat Completions 的 JSON 请求在创建私有请求审计前显式发送 `tools: []`、`tool_choice: "none"`，而非仅依赖提示词或未声明工具时的服务默认值。上游若仍返回工具调用，整份响应继续拒绝，不能跳过工具片段接受其后的 JSON，也不提高响应上限来容纳生成图片。

历史生成 schema `historical-evidence-json-schema-v2` 将单条日期的 `resolution` 与 `normalized_date` 绑定：`explicit`／`contextual` 只能携带日期字符串，`year_unknown`／`ambiguous`／`invalid` 只能携带 `null`，不再允许两者独立组合。schema 使用提供方支持的嵌套 `anyOf`，保留未知日期和图片证据分支；学院项先生成 `evidence`，再生成原名 `name` 和关系 `relation`，总 `status` 则放在 `claims` 之后。提供方按 schema 的字段顺序生成结果，参见 [Structured Outputs 约束](https://developers.openai.com/api/docs/guides/structured-outputs)。这些是生成约束，不会修正旧响应；真实日历日期、逐字引文、同一来源／图片和学院关系仍须通过原有 V5 校验。

修改历史审查说明会改变冻结配置中的 `instructions_sha256`；修改生成 schema 须升级单独冻结的 `response_schema_version`，不需要改变未变更的证据规则或付费请求版本。冻结批次继续使用原说明与原 schema，仓库修改不会自动切换它。新版须完成对应配置的验收并使用新的输出范围；不能将 v2 模块复制进 v1 冻结运行树、覆盖旧 `scope.json`、修改版本号冒充兼容或重置台账来应用新版。既有完成、失败和结果未知的请求仍受付费防重保护。


## 对照日报来源覆盖

在已迁移的数据目录上运行只读命令；未指定范围时选取已保留的最大数字刊号：

```sh
nannanews source-coverage
nannanews source-coverage --issue-from 450 --issue-to 451
```

数字范围包含两端，不是日期或论坛帖号。JSON 报告以历史刊物的有效原文链接为分母，分别统计进入发现队列、当前原文已留存、当前版本提取完成和已产生编辑条目；一篇原文涉及多个活动时，原文只计一次，活动与刊物出现次数另列。报告只查元数据，不读取模型回执或原始正文，不迁移数据库，也不调用模型。

只有精确匹配首次发现时间、采集运行及来源运行关联且操作者为 `system:daily` 时，才确认自主日采集发现。PDF 补抓、其他操作者和证据不足分别列出；仅有队列记录或 `captured_by=system:daily` 不代表独立发现。对照时仅合并 `es.nju.edu.cn` 同一文章路径的 `page.htm` / `pagem.htm`，不改存储 URL、采集摘要或付费请求身份。

这些比例表示限定日报原文集合的覆盖，不是活动召回率，也不表示已覆盖全校全部消息；日报本身和采集启动时间也可能不同。

## 专用微信桌面与手动登录

`deploy/wechat-desktop/` 提供独立的官方 Linux 微信客户端，不挂载宿主桌面、个人目录或已有微信会话。它以非 root 用户运行，根文件系统只读，限制为 4 GiB 内存和两个 CPU，容器不使用交换空间；官方安装包的版本、架构及 SHA-256 均在构建时核对。厂商替换下载内容时构建会拒绝继续，不应跳过校验。

需要本机 Docker 和 Compose。从仓库根目录运行：

```sh
bash deploy/wechat-desktop/runtime.sh build
bash deploy/wechat-desktop/runtime.sh start
bash deploy/wechat-desktop/runtime.sh status
```

`start` 等待容器健康的上限为 120 秒；健康检查包括实际微信进程及其可见窗口，不仅检查端口。随后同步设置现有 x11vnc 的轮询与发送等待为 20 ms，不改变屏幕尺寸或 XDamage 策略，也无须为此重建镜像。浏览器打开 `http://127.0.0.1:33440/vnc.html?autoconnect=1&resize=scale&quality=6&compression=0`，使用本机 `${XDG_STATE_HOME:-$HOME/.local/state}/nannanews/wechat-desktop/vnc-password` 私有文件中的密码连接。文件权限为 `0600`，不要把密码或登录链接发送给模型、提交到仓库或加到 URL。端口只绑定本机回环地址，不应直接暴露到公网。

连接参数保留画质等级 6，以压缩等级 0 减少本机回环连接的编码开销；画面仍由浏览器缩放，不修改采集器使用的桌面分辨率。参数在打开该链接并连接时生效，不会自动改写已经连接的浏览器；经低带宽隧道使用时可在 noVNC 设置中提高压缩等级，但会增加服务端 CPU 开销。

已有登录会话只需在线调整资源时，不调用 `start`：Compose 配置变更可能触发容器重建。以下两步分别调整内存与 CPU，不停止现有容器；内存和内存加交换空间上限保持相等，继续禁用容器交换空间：

```sh
docker update --memory 4g --memory-swap 4g nannanews-wechat-desktop-desktop-1
docker update --cpus 2 nannanews-wechat-desktop-desktop-1
```

VNC 显示卡顿与微信页面联网失败应分别诊断。Windows 系统代理、WSL 自动代理和容器内客户端的代理设置不是同一个配置；容器里的 `127.0.0.1` 也不是 Windows。没有 Linux 微信及其文章浏览器的实际支持证据时，不把环境变量当作已生效的在线代理修复，不开放宿主网络或公共代理中继。调整整个 WSL 前需安排所有后台任务的停机窗口，不能为单个桌面问题直接执行 `wsl --shutdown`。

专用桌面默认 `restart: "no"`，停止后不会自行恢复。GUI 发现适配器在容器不是 `running` 时以 `source_runtime_unavailable` 拒绝界面操作，不执行账号或剪贴板命令；这与登录失效、公开网页访问验证是不同故障。恢复时先启动现有专用容器，保留外部会话卷，不为此重建镜像或删除账号数据。

首次扫码、手机确认和后续安全验证均由运营者完成。客户端就绪只表示桌面可用，不表示已登录或本轮发现成功；不能拿登录页或模拟数据当作采集成功。重启或重建容器后即使账号目录保留，客户端也可能要求再次点击 `Log In` 或手机确认，不应因此删除会话卷或自动处理登录。桌面启动不调用付费模型，也不自动发送消息、订阅公众号或读取私聊。

独立会话持久化在带用途标签的外部卷 `nannanews_wechat_desktop_private`，容器内目录为 `/home/wechat`；不会接管同名但没有标签的现有卷。`bash deploy/wechat-desktop/runtime.sh stop` 保留此卷及会话。`screenshot` 子命令导出私有截图，`input` 子命令只在该隔离桌面执行本地键鼠操作；截图可能包含登录或账号信息，不应作为公开站点资源，也不要在含私人聊天的界面向模型发送截图。

### 公众号官网链接发现（匿名 HTTP）

`wechat_official_links` 不使用微信桌面，只从明确配置并核验的官网 HTML 页面读取真实的微信文章锚点，不把官网自己的新闻页面冒充公众号文章。`url` 是该官网页面，`listing_identity` 是页面应显示的官方发布者名称；`identity_url` 是独立核验的微信身份参考原文，`expected_identity` 与 `wechat_biz` 分别记录准确的公众号名称和公开 BIZ。官网列表和微信参考原文可以跨主机，但官网重定向不得改变发布者主机。参考原文或官网列表核验失败时，不继续旧队列。

每轮只读配置的一个官网页面，去重后最多核验 `min(4 × NANNA_COLLECTION_PAGE_LIMIT, 24)` 个真实微信链接。官网可能同时引用多个公众号，候选原文先通过匿名 HTTP 核对 BIZ；其他发布者正常排除，不能按官网名称推断公众号身份。脚本和隐藏内容不参与发现，嵌套隐藏容器移除后不再读取其已销毁的子节点。候选仅缺少可核验发布者元数据时，跳过该链接、报告覆盖缺口，继续核验其他独立公开链接；不把无法归属的页面写入账号原文。遇到验证／登录拦截、网络故障或其他受控错误仍停止后续探测，保留此前已核验链接并报告错误码；未知异常不伪装为正常发现。单次 HTTP 保留自己的 30 秒预算，同时服从历史任务的总截止时刻，不把单次请求超时误用为整轮多文章核验时限。

本方式始终是 `partial`，不代表公众号全量历史、不推进完整成功检查点，也不是 GUI 登录或验证码的替代通道。实际原文仍独立复核发布者、解析真实发布日期并留存完整媒体；官网文章的日期、链接首次发现时间和抓取时间均不能替代微信原文发布日期。现有 HTTP 同主机限流、图片预检和付费防重全部保留。

2026-10-20 包内 `automation_sources.json` 已列入 41 个 `wechat_official_links` 定义，其中 39 个启用。此前从历史消息差集和当前官网引用核实 26 个候选，24 个通过隔离真实采集验收后启用；南京大学新生学院、南大会计的验收遇到验证拦截，保持禁用，未重试或更换入口绕过。该轮新启用来源中 13 个补自原先未接入的 81 个历史账号，其余 11 个是另行核实的校内公众号；剩余 68 个历史账号不自动启用，官网链接发现也不代表完整账号历史。

此前扩展验收保留 62 篇原文、876 份原始媒体，发布者、真实发布日期与原始响应／媒体摘要均已核对；61 篇完整原文通过离线图片预检，另 1 篇触及每文 64 份媒体的采集上限，保留不完整证据且不入模。两个付费台账均为零，未推进完整成功检查点。已有私有配置不会自动合并新版种子，部署时应逐项审核并显式合并需要的定义；新增来源必须先完成上述无模型凭据的真实验收，再启用每日工作器。

随后全目录核验又启用长江产发智库、微英语：官网实际引用、准确名称／BIZ、3 篇完整原文及 32 份媒体均通过无模型验收；微英语遇到后续候选验证拦截时停止，未宣称完整覆盖。数学学院近期报告也经真实公开接口和原文验收后显式合入私有配置；每日配置现有 70 个非微信 HTTP 来源与 39 个启用的公众号来源。

### GUI 公众号独立发现（显式启用）

`wechat_gui` 来源使用专用客户端“搜一搜”的公开账号主页发现文章，不依赖日报 PDF。种子中的南京大学、南大青年、南京大学信息管理学院、南京大学大气科学学院和南大就业 GUI 定义均默认禁用；运营者核验会话和账号后，在现有私有 `automation_sources.json` 中显式启用对应定义。`expected_identity` 是界面中应精确匹配的公众号名称，`wechat_biz` 是经公开原文核验的发布者标识；`url` 和 `identity_url` 指向同一篇身份参考原文，不是伪装成 HTTP 接口的账号文章列表。新增账号必须先核验，历史目录中的名称不会自动变成白名单，官网链接接入通过也不等于 GUI 已就绪。

采集工作器需要本机 Docker CLI 权限，并使用上述专用桌面镜像中的 `xclip` 和中文 Tesseract。OCR 只处理已核验进程拥有的公开窗口；识别助手无网络、无账号卷，不使用付费视觉模型。当前核验界面为官方 Linux 微信 `4.1.13.26` 的英文控件与中文正文；公开页面按控件实际出现等待，不因固定 OCR 帧数用尽而把正常慢加载判为失败。控件、焦点、窗口位置变化或人工验证提示会停止操作，不继续使用旧坐标。该公开浏览窗口由采集器独占，结束时向已核验的浏览窗发送正常关闭请求，不强行销毁 X 窗口或重启已登录客户端；采集时不要同时操作该专用桌面，也不要在其中打开个人服务或私人链接。

账号搜索先进入 `Accounts`，再明确选择 `OfficialAccounts` 子筛选，避免把同名视频号当作公众号。账号名称与认证标识可能被 OCR 合并为一行；采集器只用精确名称对应的连续真实词块边框定位，不猜测整行中心、不把认证标识的残缺文本当作名称别名。候选必须唯一；同名公众号无法消歧时等待后停止，不自动挑选第一项。完整 OCR 行仍保留用于人工验证提示检测。

每个公众号每轮最多扫描 `min(3 × NANNA_COLLECTION_PAGE_LIMIT, 18)` 个视口、取得 `min(4 × NANNA_COLLECTION_PAGE_LIMIT, 24)` 篇文章，另有各自的 180 秒界面操作上限。有限界面扫描保持 `partial`，不宣称账号全量历史覆盖、不推进完整成功检查点；已核对链接仍进入原有发现队列。原文仍由现有匿名 HTTP 采集器获取、核对发布者并留存全部媒体，不把剪贴板里的纯文字冒充完整图文，也不把 GUI 截图伪造成 HTTP 响应。验证页、不可公开内容和不完整媒体不会进入模型。

GUI 发现阶段只取得官方客户端实际复制的文章 URL，并先写入发现队列；HTTP 连接失败不会抹掉这次发现。公开菜单可以是没有 `WM_CLASS` 的独立窗口，只有其进程与已核验的公开浏览窗一致、类型明确为菜单且完整位于浏览窗范围内时才允许读取；不扩大为桌面或私人聊天截图。链接缺少列表发布日期时，两种微信来源均可在有界预算内先留存原文，再由原文中明确的发布时间决定是否具备首次提取资格，绝不把“刚发现”当作“刚发布”。

GUI 暂时不可用不再阻塞已发现链接的处理：只有本轮匿名 HTTP 身份参考校验通过，且 GUI 返回受控的 `source_runtime_unavailable` 或 `source_blocked`、没有人工验证标记时，才继续当前来源定义下的已知 URL 队列。每篇原文仍在保存前核对发布者，保留既有抓取预算、发布日期和付费防重限制；本轮标记 `partial` 并报告发现覆盖缺口，不推进成功检查点，也不改写链接的首次发现时间。参考文章不可验证、发布者不匹配、验证码、人工验证提示或未知异常仍阻止队列继续，不能借此绕过访问限制。

若已取得部分链接后出现人工验证，链接仍入队，但本轮不继续该号的原文抓取或模型提取，也不再切换后续公众号。此前已核验账号的 HTTP 队列和独立官网来源仍可继续；不能通过换号绕过同一桌面的人工验证。

清空标记不等于剪贴板可信：复制后必须出现新的选择区拥有者，并核验其进程为当前公开浏览器；读取时以 X11 服务端临界区绑定转换请求，返回前再次检查拥有者。无法核验拥有者或检测到其他程序接管时拒绝读取，清理也不会覆盖其他程序的选择区。验证码检查在每次菜单输入前执行，而不是仅在出现“复制链接”后执行。采集时限结束后，另有短暂且有界的清理预算；只有重新核验焦点、进程和位置后才正常关闭浏览窗，人工验证界面不自动关闭。

选择区的进程身份通过 X-Resource `QueryClientIds` 向服务器查询，不信任窗口自行填写的 PID，也不猜测 XID 位段。微信的隐藏剪贴板窗口可以不带 `_NET_WM_PID`；只要服务器确认它属于已核验的公开浏览进程，就能继续安全复制。查询与选择区转换共用同一条 Xlib／XCB 连接，保持服务端临界区；扩展不可用、回复不完整或归属不匹配时仍拒绝读取。所需 `libX11-xcb` 和 `libxcb` 已包含在专用桌面镜像中，无须为此重建客户端或再次登录。

两种微信渠道复用原有历史保留与提取流程。无法确认发布日期的文章不进入自动付费队列；启动前历史不自动回填。同一微信文章 URL 的任何版本只要已有完成、失败、提取中状态或模型尝试，自动采集就只保留新原文，不再次付费；发送前在同一事务内再次检查，避免并发人工提取造成重复请求。既有历史复查的冻结范围、缓存和独立付费台账不修改、不重置。

默认选择链接发现，而不是让 GUI 承担整篇图文留存：

| 方式 | 用途与边界 |
| --- | --- |
| 官网真实微信文章锚点，再走现有 HTTP 核查 | 不依赖桌面的有限公开发现；官网身份、逐篇 BIZ、原始响应和完整媒体仍须核验，不扩展为账号历史接口。 |
| GUI 复制文章链接，再走现有 HTTP 核查 | 常规采集。复用正文、文章声明的图片响应字节、URL、摘要、媒体完整性校验与模型预检；不要求 GUI 先渲染完整正文和图片。 |
| GUI 保存单图或复制图片地址 | 人工补充线索。当前客户端公开图片菜单提供这些操作，但尚未验证保存文件的字节一致性、原图质量或整篇媒体覆盖；不能当作完整 HTTP 采集写入原文缓存。 |
| GUI 截图 | 仅作明确标注的人工截图证据；可能裁切、缩放或仍含加载占位，不等于原始图片或完整原文。 |

在上述客户端版本中，已实测文章正文和图片尚未加载时也能复制文章链接；列表卡片直接复制链接尚未验证为稳定入口，现有自动化仍打开文章后使用其复制链接菜单。图片文件如需人工录入，应使用现有公开材料上传路径并明确关联文章来源，不能把导航 OCR 或截图冒充匿名 HTTP 响应。

### 多公众号与全部来源批量采集

批量采集使用一个协调器：GUI 发现走单独的串行工作槽，最多提交一个公众号任务；官网发现、已取得链接的原文和图片留存走 HTTP 工作池。等待下一公众号界面时，前一公众号的原文和其他官网来源可以继续，不应启动多个采集器争用同一桌面。GUI 参考文章的 HTTP 请求仍与工作池共用限流器；`NANNA_COLLECTION_FETCH_WORKERS` 支持 1–6，实际公共请求保持同主机单路，模型工作槽仍最多两个。并发增加不会取消来源轮转、发布日期、媒体完整性或付费防重限制。

先区分来源目录与可采集定义。`source_catalog.json` 的历史名称、微信号或官网替代入口不等于公众号已核验；只有有公开依据的账号名称、参考文章和匹配 `wechat_biz` 才能加入 `automation_sources.json`。已有官网或公开接口可直接覆盖的来源优先使用 HTTP，不必逐号打开 GUI；官网覆盖也不能标成微信全量历史覆盖。新增账号可参照包内 `nju-wechat-official`、`nju-medicine-wechat-official`，需要 GUI 发现时再参照 `nju-wechat-public`、`nju-youth-wechat-public`，逐项审核后合并，不要把历史目录整批改成启用。

先在已迁移、已审核来源配置的隔离目录中验证。`ISOLATED_DATA_DIR` 必须指向该隔离目录，不能指向生产库或冻结历史复查的数据目录：

```sh
NANNA_DATA_DIR="$ISOLATED_DATA_DIR" \
NANNA_COLLECTION_FETCH_WORKERS=3 \
NANNA_COLLECTION_PAGE_LIMIT=1 \
nannanews collect --mode history
```

`history` 只留存有界的未处理历史原文，不自动付费回填；`PAGE_LIMIT` 是每个来源的预算，不是整批上限。小预算可先验证广度、磁盘和网络承载，再按实际容量增加 HTTP 并发或每源预算。后续轮次从持久队列继续，不删除队列、检查点或模型台账来重跑；明确失败或媒体不完整的原文仍需检查，不能靠提高并发当作采集完整。

全量验收应同时清点私有配置、包内新增定义、实际使用的私有目录和已注册来源，并分别记录身份／列表、完整原文／媒体、离线图片预检结果。`history` 只抓取历史候选；列表候选均为当前内容时，可能发现了链接却没有抓取原文，不能把 `no_change` 当作完整验收。此时在隔离库使用单篇公开抓取与原文留存函数完成无模型验收，不靠切换到 `new` 模式触发解析。只有名称、缺少可验证 URL／BIZ，或 GUI 未通过就绪检查的账号应标为未能实测；官网替代入口或 PDF 可达，也不代表对应公众号已核验。

核对命令 JSON 中各来源的 `discovered`、`fetched`、`pending`、状态和覆盖缺口，并在 `/library/sources` 区分已启用官网、公众号与历史目录。`partial` 或命令正常退出不代表全源、全文章完成；历史任务临近每日采集时段会停止提交并排空在途请求。公众号主页的“全部／贴图／文章”可能被 OCR 合并成一行，采集器在已核验主页内按“文章”词块的真实边框定位，不猜测合并行的点击中心。

验证新增配置应先使用隔离数据目录，禁止用生产失败记录测试重试。升级采集工作器不能顺便重启旧协议冻结的历史复查；桌面镜像更新也不等于现有每日服务已加载新代码。

`collect-worker` 在启动时加载代码、环境和 HTTP 并发配置，不会热加载仓库修改。调整前先核对各来源结果和当前模式队列，区分本轮新增错误与历次保留的失败：`model_failures` 是队列积压数，不等于本轮模型请求失败数。确认没有运行中的采集轮次后，只重载每日工作器，并保留数据目录、公开 HTTP 代理、模型配置与每日时刻；不要顺带重启冻结历史工作器。

需要日间补采时，在同一套已审核环境下运行 `nannanews collect --mode new`。它使用现有队列与防重台账，继续发现当前内容；不是重新付费提取旧失败原文的命令。核对这次运行的 `discovered`、`extracted` 和逐来源缺口，而不是只看总状态：未接入的历史参考入口及受保护的旧失败仍可能使整轮保持 `partial`。


## Docker Compose

运营需要 Docker CLI、可访问的 Docker context，以及支持多次 `--env-file`、`config --environment`、`up --wait --wait-timeout` 的 Docker Compose v2。本地生命周期脚本使用 POSIX shell 和 `mktemp`、`mv`、`rm` 等常规 Unix 工具；不要求主机安装 Python 或 jq。Python 运行时随应用镜像提供。只有构建和推送镜像需要 Buildx 及自有仓库权限。

`deploy/Dockerfile` 安装项目软件包并以 UID/GID `10001` 运行。容器根文件系统只读，`/data` 挂载已有的外部命名卷；它只是持久化父目录，活动根目录初始为 `/data/live`，回滚使用 `/data` 下另一个全新同级目录。不要把 `/data` 本身设为活动根目录。Compose 将 `NANNA_BACKUP_DIR` 绑定到 `/backups`；它必须是 Docker 主机上已存在的绝对私有目录，并允许 UID/GID `10001` 写入。使用远程 Docker context 时，目录和权限必须设置在远程主机，不能使用本机同名目录代替。

从仓库根目录操作。所有命令显式选择同一个 `nannanews` 项目和同一个 Docker context：

```sh
export NANNA_DEPLOY_TARGET="$DOCKER_CONTEXT"
cp .env.example .env
chmod 600 .env
umask 077
printf 'NANNA_ACTIVE_IMAGE=%s\nNANNA_ACTIVE_DATA_DIR=/data/live\n' "$IMAGE_REF" > .env.active
```

`DOCKER_CONTEXT` 和 `IMAGE_REF` 须由运营者设置为真实 context 和可访问的非 `latest` 版本镜像。配置分为两个文件：

- `.env` 保存站点配置、密钥、可选模型端点、`NANNA_BACKUP_DIR` 和 `NANNA_DATA_VOLUME`。默认卷名为 `nannanews_nanna-data`。原生运行的 `NANNA_DATA_DIR` 和旧配置中的 `NANNA_IMAGE` 不决定 Compose 的活动根目录或镜像。
- `.env.active` 只保存 `NANNA_ACTIVE_IMAGE` 和 `NANNA_ACTIVE_DATA_DIR`；它是活动版本和数据根目录的权威记录，权限为 `0600`。每次 Compose 操作都在 `.env` 后加载它。不要在 shell 中导出这两个托管变量，也不要在升级前手动改写它们。升级和回滚脚本只在 Web 健康后原子替换此文件，完全不改写 `.env` 或其中的秘密。

首次部署前，在 Docker 主机上创建备份目录并将所有者设为 UID/GID `10001`。新部署还须显式创建配置中的数据卷；使用默认卷名时：

```sh
docker --context "$NANNA_DEPLOY_TARGET" volume create nannanews_nanna-data
```

Compose 不自动创建数据卷；卷不存在时会失败，以免拼错名称后悄悄使用空库。接管已有部署时，不要创建新卷代替旧数据。先按旧部署配置停止所有写入者并备份，确认旧项目与实际卷名，再把 `.env` 的 `NANNA_DATA_VOLUME` 设为该卷名。原先直接存放在卷根目录的数据须离线移到 `live` 子目录，保留数据库、上传文件、来源依据、密钥、全部防重记录及原始回执；已有同级目录布局则将实际活动路径写入 `.env.active`。旧项目必须保持停止，不能与新项目共享写入同一卷。不要删除旧卷或使用 `down --volumes`。

已登录并有权推送自有镜像仓库的 Buildx 环境可发布显式版本标签：

```sh
NANNA_IMAGE="$IMAGE_REF" ./deploy/release.sh
```

脚本构建 `deploy/Dockerfile` 并推送该标签；没有 Buildx 或仓库权限时会失败。

首次启动时，先初始化管理员，再启动 Web 服务。日后重启 Web 也使用同一条 `up` 命令，不临时指定镜像或数据根目录：

```sh
docker --context "$NANNA_DEPLOY_TARGET" compose --project-name nannanews --project-directory . --env-file .env --env-file .env.active --file deploy/compose.yaml run --rm web init-admin owner
docker --context "$NANNA_DEPLOY_TARGET" compose --project-name nannanews --project-directory . --env-file .env --env-file .env.active --file deploy/compose.yaml up --detach --wait --wait-timeout 120 web
```

默认只向宿主 `127.0.0.1:8080` 监听。公网服务须先配置自有 HTTPS 反向代理，并设置真实的 `NANNA_PUBLIC_URL` 和 `NANNA_SESSION_COOKIE_SECURE=true`。

采集服务位于单独的 `collection` profile，默认不启动。配置自有公开来源和模型端点，并在 `.env` 中明确设置 `NANNA_COLLECTION_ENABLED=true` 后才启动：

```sh
docker --context "$NANNA_DEPLOY_TARGET" compose --project-name nannanews --project-directory . --env-file .env --env-file .env.active --file deploy/compose.yaml --profile collection up --detach collection
```

这条命令在回滚后仍从 `.env.active` 读取恢复后的根目录和活动镜像，不会切回旧数据。启用采集不会批准、刊发内容或发送通知。升级、回滚后 collection 均保持停止，须另行明确启动。

## 版本部署与回滚

脚本从本仓库运行，以 `NANNA_DEPLOY_TARGET` 明确选择目标；会检查 context 和 Docker 访问权限，不替运营者猜主机、创建权限或伪称部署成功。脚本只管理 `nannanews` 项目。先在 Compose 之外停止历史复核及其他所有写入进程，再设置 `NANNA_EXTERNAL_WRITERS_STOPPED=YES` 确认；Compose Web 和采集服务由脚本停止。不要并行执行生命周期操作，也不要在维护期间手动启动服务或编辑配置。

```sh
NANNA_DEPLOY_TARGET="$DOCKER_CONTEXT" \
NANNA_IMAGE="$RELEASE_IMAGE" \
NANNA_BACKUP_NAME="$UNUSED_BACKUP_NAME.zip" \
NANNA_EXTERNAL_WRITERS_STOPPED=YES \
./deploy/deploy.sh
```

当前镜像和根目录由 `.env.active` 读取；`NANNA_IMAGE` 只指定此次候选发布，不覆盖活动记录。脚本在维护窗口前拉取当前及候选版本，解析并固定当前真实数据根目录，检查目标机备份目录能否由非 root 用户写入，并拒绝覆盖同名备份。之后停止 Web/采集，用当前版本备份活动根目录并检查备份文件，再用候选版本对同一个根目录显式运行 `nannanews migrate`。最后只启动 Web，在 120 秒内等待健康检查，成功后才持久化候选镜像及该根目录。回滚后的下一次升级也会备份、迁移恢复后的根目录，不会改用已弃用的数据。

迁移或恢复失败时服务保持停止，活动记录不变；健康等待失败时脚本停止候选服务，也不更新活动记录。迁移可能已经前向改变数据库结构，因此失败后不要直接用活动记录中的旧镜像重启；先检查错误并选择兼容版本或执行下述恢复。脚本不自动降级或覆盖数据。

回滚须提供兼容恢复后结构及防重状态的目标镜像、迁移前备份，以及全新的恢复子目录名。当前版本仍由 `.env.active` 决定。先停止外部写入者并确认，再运行：

```sh
NANNA_DEPLOY_TARGET="$DOCKER_CONTEXT" \
NANNA_PREVIOUS_IMAGE="$ROLLBACK_IMAGE" \
NANNA_BACKUP_NAME="$PRE_MIGRATION_BACKUP.zip" \
NANNA_RESTORE_DIR="$NEW_RESTORE_DIRECTORY" \
NANNA_EXTERNAL_WRITERS_STOPPED=YES \
./deploy/rollback.sh
```

脚本先拉取两个版本并检查备份可读、恢复目标不存在且父目录可写，再停止 Compose 写入者。当前版本的 `nannanews restore` 以实际活动根目录为当前数据源，将备份恢复到 `/data/$NANNA_RESTORE_DIR`；该目录必须与当前根目录同级，不能是现有目录或其子目录。恢复会带入当前根目录中备份之后的历史、来源提取及每日采集付费防重记录和原始回执。目标版本通过健康检查后，脚本才把目标镜像和恢复根目录一起写入 `.env.active`，供后续重启、采集和升级使用；不会原地降级结构，也不会删除原根目录。

只回滚到能读取恢复后结构并保留防重状态的版本。若兼容性未确认，保持服务停止并先解决兼容问题；不要删除或回退付费尝试记录。恢复失败或目标版本不健康后，已创建的恢复目录不得复用；排除错误后选择另一个全新目录。

## CI 与静态 Pages

GitLab CI 安装 `.[test]`，运行 Python 测试、读者端测试与类型检查，并构建镜像，但不启动应用。CI 禁用采集、不提供模型 key、不访问生产数据库，也不控制服务。依赖和镜像构建可能通过网络下载软件包；这不等于运行来源采集或付费模型调用。

在 `git.nju.edu.cn` 的校内 Runner 上，可通过项目级 CI/CD 变量 `PIP_INDEX_URL=https://mirror.nju.edu.cn/pypi/web/simple` 使用学校 PyPI 镜像，避免直连 `files.pythonhosted.org` 下载超时；不将校内镜像写死为其他部署环境的默认源。切换前先确认镜像提供锁定版本及 Runner 平台对应的安装包，保留 HTTPS 校验和现有版本约束，再重跑失败作业。`pip install` 阶段失败表示依赖尚未安装完成，不能据此判断后续测试结果。

镜像构建通过 BuildKit secret `pip_index_url` 将同一 `PIP_INDEX_URL` 仅传入依赖安装步骤；项目级变量不会自动进入 Dockerfile。CI 未设置项目变量时默认使用 `https://pypi.org/simple`。该值不作为构建参数或持久化 `ENV` 写入镜像，普通本地构建不提供 secret 时仍使用 pip 的默认源。

镜像作业使用带 `dind` 标签的隔离 Runner、Docker 29 CLI／DinD 服务和 Buildx 的 `docker-container` 驱动，导出 `nannanews.oci.tar`。校内对应 `linux-docker-sysbox` Runner；普通 Docker Runner 不满足原 RootlessKit 的用户命名空间要求。BuildKit 显式加载 `deploy/buildkitd.nju.toml`，通过 `docker.nju.edu.cn` 和 `ghcr.nju.edu.cn` 拉取基础镜像，保留 TLS 证书校验；独立的 BuildKit 不会自动继承 Docker daemon 的镜像配置。其他 GitLab 实例需提供同标签、支持 DinD 的隔离 Runner，并按实际网络调整镜像入口。

Git tag 是镜像版本。注册表已启用时，`release-image` 会要求 CI 作业凭据并推送 `CI_REGISTRY_IMAGE:CI_COMMIT_TAG`，不会创建 `latest` 别名；注册表停用时跳过推送，CI 只生成短期 OCI 构建产物。注册表配置由项目管理员负责。

Pages 默认不发布；只有默认分支的 `PUBLISH_STATIC_READER=true` 才构建并上传静态站点。默认使用 Git 中审核过的 `activity-facts.json`，不访问私有云盘。可选 `STATIC_READER_DATA_SOURCE` 只接受已公开并审核过的原生 v2 白名单构建包，不接受缺少构建输入的 v3 网站清单；URL 不带凭据或查询参数，不能跨站重定向，输入不能指向私有数据库、归档、备份或运行目录。暂存只复制当前源清单引用的资源并校验路径与哈希，不带入额外文件和未引用版本。Pages 不运行 Python 后端；公开权限、Runner、注册表和真实主机权限仍由运营者提供，CI 成功不代表真实发布或社交平台预览已验收。

### 源码、静态发布与离线归档

GitLab 仓库保存代码、测试、锁文件、配置模板、文档和白名单 `reader/public/activity-facts.json`。运营者构建输入 `reader/public/data/`、归一化运行数据 `reader/.reader-site/`、`reader/dist/`、`.ci-data/`、`local-data/`、数据库、上传原件、模型与抓取审计、备份及凭据仍不提交；不要用 `git add -f` 绕过规则。审核原生 v2 公开导出后运行 `nannanews export-public-facts "$PUBLISHED_PUBLIC_PACK" reader/public/activity-facts.json`，仅更新稳定活动身份、公开标题／摘要与参与要点、明确日期、来源和公开核查状态，不导出完整正文、原件或私有证据。它不读取私有运行库或云端；不要将私有响应改名作为输入。

干净检出安装读者端依赖后，`npm run build` 可从该受版本管理的事实文件生成基础阅读、搜索和分享产物，不需要 1.5 GiB 完整资源包、私人云盘或密钥。完整正文、原刊配图／PDF 仍须使用审核过的完整资源包；仅凭基本事实构建不包含这些内容，也不伪造缺失原件。Pages 上传生成后的静态目录，不是上传私有底座。

原生 v2 白名单资源包是运营者输入，不是网页目录：它保留导出、人工核查发布和重建搜索所需的 `catalog.json`、详情及 `search-documents.json`。`npm run build:public` 将它转换为独立的 v3 网站数据，写入 `reader/.reader-site/data/`；Vite 的 public 资源目录改为 `.reader-site/`，不再把 `reader/public/` 下的输入复制为网页资源。正式发布整个 `dist/`；新生成的 v3 数据版本不包含构建输入 `activity-facts.json`、全量 catalog、搜索原始文档或旧式重复详情分片。人工发布仍保留旧版本供恢复和旧链接使用，不会为清理重复而删除既有版本。

网站 v3 清单使用 `generator: nannanews-reader-v3`，`data_version` 对应已审核的源版本；v3 JSON 统一放在 `versions/<data_version>/reader-v3/`，不与同一内容版本的旧 v2 文件争用路径，图片和 PDF 仍复用原有内容哈希路径。`files` 只引用期次元数据、消息身份与出现关系索引、事实分区及浏览器 MiniSearch 索引。每条刊载记录只存在于所属期次的分区，消息字段池只存在于其最新出现记录所在的分区；仅完全相同的值共享，缺失和 `null` 不混为一谈。详情只保存非空正文、证据、附件和真实的字段差异，读取时补齐安全的空字段；缺少 `original_dates` 的旧记录仍回退显示历史日期，不与明确的空数组混淆。基本事实版不生成整批空详情文件。无期次的独立收录使用独立分区，不伪造期号。进入历史、活动或收藏时由同一份分区数据恢复原有记录顺序，不另发布一份全量目录。

`npm run build` 依次暂存审核输入、生成真实搜索索引、归一化网站数据、检查并构建前端、生成固定 URL 的分享页；`npm run dev` 同样先准备归一化数据。搜索索引仍覆盖全部历史和输入包实际包含的公开正文，不以首页减载缩小范围。索引随版本路径切换，使用锁定 MiniSearch 的序列化 v2 格式；浏览器只读取索引，不依赖 `search-documents.json`。旧 v2 输入和旧部署仍可读取，但新部署只生成 v3；回滚时须恢复匹配的前端、清单和版本资源，不能只改清单版本号。发布体积须分别统计原始字节、压缩包与浏览器实际传输。

这些公开 JSON 区别于私有的 HTTP 原始响应、HTML 抓取包和模型请求／回执。原始 HTML 原件保存在私有离线归档；完整正文阅读使用已审核的结构化公开内容，不要求向匿名站点暴露整个原始归档。

公开包可能很大；本次现有活动版本约 1.5 GiB，不能假定目标 GitLab 的 Pages、作业产物、存储或传输限额足够。发布前确认实际配额和访问权限，不因本地构建成功宣称远端部署完成。

离线整理直接读取仓库外已有的历史原件、私有运行与核查资料、公开导出，不为集中展示而复制目录树或生成逐行查询快照。经运营者明确授权搬迁后，活动附件统一由 SQLite 登记的摘要路径定位，旧来源路径仅作溯源。历史原件同时记录文件名日期、PDF 首页／既有 HTML 元数据日期、期号、摘要和核对状态。日期冲突、未知或别名 PDF 标记待核对，不按文件名强行配期；首页日期提取也不等于全文人工核实。保留上游清单、原文件及既有日期审计，HTML 不改写，外链资源未必可以离线打开。

原始回执、数据库及付费防重台账保存在受限实例目录，不为归档再制作一份；不要为了减少 Git 内容而清除依据或重试历史请求。目录搬迁须先停写、验证原件保留并更新活动路径；冻结来源库和旧配置保留在私有历史目录，不作为活动运行入口。目录搬迁不授权云端上传、删除快照或重跑付费请求。凭据、密码、会话密钥、浏览器会话目录以及可重建缓存不纳入资料上传清单。数据库与核查资料只能放受限本地存储或私人云盘；排除凭据文件不代表其他私有内容可以公开。整理原件不等于导入、模型解析、审核或发布新活动。

### 本地人工核查与读者端 Docker

`deploy/compose.local.yaml` 是独立的 `nannanews-review` 项目，用于本地人工核查和公开静态阅读，不替代上文带版本回滚的 `deploy/compose.yaml`。镜像只从源码构建，内置 Node.js、搜索／分享构建器和前端，不携带宿主数据库、原件或已生成网页；默认屏蔽模型凭据并关闭采集。首次准备和结构升级均为显式操作，不随 `serve` 启动执行。

从仓库根目录设置两个已存在、由 UID/GID `10001` 管理的独立目录。以下目录名用于新的 Docker 实例，与上述 systemd 本机实例分开；不要将现有私有主库重新初始化或交给两个实例同时写入。`PUBLIC_INPUT` 必须是审定的公开导出，并可被 UID `10001` 只读访问；需要调整权限时先准备独立的公开输入，不放宽整个 home 或私有原始数据目录。

```sh
export NANNA_REVIEW_PRIVATE_DIR="$HOME/service/nannanews-review/data/runtime"
export NANNA_REVIEW_PUBLIC_ROOT="$HOME/service/nannanews-review/publication"
export PUBLIC_INPUT="/absolute/path/to/approved-public-export"
sudo install -d -m 700 -o 10001 -g 10001 "$NANNA_REVIEW_PRIVATE_DIR" "$NANNA_REVIEW_PUBLIC_ROOT"
docker compose -f deploy/compose.local.yaml build console
docker compose -f deploy/compose.local.yaml run --rm --no-deps \
  --volume "$PUBLIC_INPUT:/input:ro" --entrypoint sh console /app/deploy/stage-reader.sh /input
docker compose -f deploy/compose.local.yaml run --rm --no-deps console init-admin owner
docker compose -f deploy/compose.local.yaml up --detach --wait --wait-timeout 120 console reader
```

初始暂存只接受空目标，验证当前清单资源后生成搜索与分享页；已有 `source` 或 `site` 时拒绝替换，不删除它们重跑初始化。账号已存在时不再执行 `init-admin`；升级前停止控制台写入并备份，再显式运行 `docker compose -f deploy/compose.local.yaml run --rm --no-deps console migrate`。

默认地址为读者端 `http://127.0.0.1:4184/`、控制台 `http://127.0.0.1:4186/console/activities`，可分别通过 `NANNA_REVIEW_READER_PORT`、`NANNA_REVIEW_CONSOLE_PORT` 调整。两个服务均为非 root、只读根文件系统。控制台挂载私有目录到 `/data`，以及同一公开父目录到 `/publication`；`source` 和 `site/data` 必须保留在该单一文件系统内，支持人工发布所需硬链接。读者端仅以只读方式挂载 `site`，不能访问私有目录或公开父目录。

可选的 `NANNA_REVIEW_EVIDENCE_DIR` 使用容器内路径，如 `/data/evidence`，映射为控制台的 `NANNA_READER_EVIDENCE_DIR`；默认留空。须先显式准备私有绑定及其实际本地底座、工具、密钥路径，并确保 UID `10001` 可访问，不能直接使用宿主绝对路径。该手工核查 Compose 仍不继承模型凭据；需要付费新审查时使用另行显式配置的后端环境，不为展示历史而放开采集或模型。

控制台保存不更新站点；管理员单独发布后，本地容器读者端的正文、搜索与分享页更新。此过程不重建 Vite、不调用模型，也不自动部署远端 Pages。停止本地服务用 `docker compose -f deploy/compose.local.yaml down`，不要删除绑定目录；公开包、人工历史和账号均在镜像外持久保存。需公网访问时另行配置受保护的 HTTPS 入口，不直接暴露控制台。
重建镜像不会自动改写已绑定的前端站点。更换前端代码时，应先备份并停止控制台写入，再在新的公开父目录中用当前审定公开包重新暂存、切换绑定目录并启动；保留旧公开目录与私有备份，不删除原目录来绕过初始化保护。


### 更新网页刊物内容

首页按期展示消息，历史检索和当前活动分别浏览，阅读方式见[网页刊物与历史阅读](product.md#网页刊物与历史阅读)。AI 整理的活动日期须保有原文字面来源标记；仅更新前端代码不能补齐旧数据包的日期来源。通常应使用当前导出器重新生成公开包；人工核实的日期则通过下节控制台明确填写并发布。刊物总表的日期仍保留供检索和对照，但不自动成为已核实的报名截止日。

旧公开包若已保留当前规则下的 `date_review.claims[].literal_source`，可仅补齐日期来源元数据，无须重新调用模型或复制含完整模型证据的大数据库。在同一规范活动和来源文档内，按日期类别、原文字面、规范化日期、解析状态及依据五项严格匹配已有判断；缺少证明的日期不标为原文日期，证明冲突时停止升级。按正文分片处理并复用不可变的公开图片和 PDF；保持原刊正文、复核结论、待审保护及资源字节不变，生成新内容版本，核对全量记录后再原子切换清单，旧版本继续保留。

AI 整理的列表 `activity_facts` 只含已通过当前规则、来源与更正准入的活动时间、地点、对象和参与方式四项公开值。旧包已保存完整准入活动时，可按分片逐项原样补齐，不重新推断日期、不提升待审或其他非已核实记录；仍须生成原生内容摘要、复用不可变资源、全量比较其余字段并原子切换新版本。只改前端不会为旧包凭空产生这些字段。

离线复核结果不会自动进入网站。先在隔离数据副本中通过原生身份、输入摘要和关联上下文校验，再追加已验收的复核记录；不要直接复制统计索引、采用未经核对的模型肯定结果，或修改主库来绕过准入。原刊、旧复核和待审依据都应保留。

在 `NANNA_DATA_DIR` 指向已审核副本、且副本保有所需原刊文件的环境中，使用 `nannanews export-static "$PUBLIC_EXPORT_DIR"` 生成独立的公开白名单目录。输出不得位于私有数据或上传目录内。当前规则下完整、来源未失效的活动摘要用于列表预览；已核验的活动字段补入检索，原刊标题和正文仍保留在详情参考区。原文图片只导出本地安全预览，不导出原始采集响应或私有模型输入。

待审活动必须随导出显式传入。`--hold-occurrence-id "$PENDING_OCCURRENCE_ID"` 可重复使用；任一出现记录会保护其整个规范活动，而不只保护该期刊载。受保护记录显示“日期／来源待核对”，不提供已确认活动字段、原文海报或可筛选的确定日期，也不进入当前活动索引；原刊日期和旧模型记录仍在详情中作为参考。此选项不修改数据库、模型结论或原始日期，未知记录 ID 会使导出失败。

复核结果有变化时重新导出，而不是仅构建旧数据包。对已有合法清单的生成目录，可使用 `export-static --replace` 原子切换清单并保留历史版本；不要用该参数覆盖普通目录。审核公开导出后，在空的 `reader/public/data` 目标中执行 `npm run stage:data -- "$PUBLIC_EXPORT_DIR"`，再运行 `npm run build` 和本地预览。暂存步骤核对普通配图与原文安全预览的路径、资源数量及内容哈希；已有数据目标会拒绝覆盖，应使用空工作树构建，不删除旧目录来绕过保护。

`npm run build` 在 Vite 构建后为每条公开刊载记录和期次生成 `dist/share/` 静态落地页；必须连同整个 `dist` 部署，并支持目录 `index.html`。落地页只使用公开目录的标题、摘要与核实提示，含 HTML／Open Graph 预览元数据，并跳转到精确的期次或 `?occ=` 消息路由。路径使用相对回跳，支持子路径托管；不猜测正式站点域名。系统分享和剪贴板仍受浏览器权限限制，拒绝复制时保留手动复制入口。

上线前检查本期／当前活动／全部历史范围搜索、输入法回车、本期超过一页的匹配结果、栏目跳转、详情按钮与浏览器原位返回、年份档案返回、390／320 像素手机布局、原文海报和待审记录；核对活动日期与报名截止是否分开、部分已过截止是否仍醒目、刊物索引日期是否仍仅作参考。检查消息与期次分享落地页的 HTTP 内容、真实跳转、子路径、取消分享及复制被拒绝的反馈。正式域名公开部署后，另在目标社交平台验证实际预览；本地构建不证明微信等平台抓取成功。“已对照原文”表示 AI 来源对照，不等于人工审核。构建和本地预览不会替代正式站点的独立发布决定。

### 人工核查网页活动

控制台用于登记新活动、修订既有活动并按审查次查阅私有证据，不修改原刊或覆盖原始模型记录。使用单独私有目录保存账号、会话、登记与修订，另行绑定上文证据目录；不要放入静态站点。已有控制台先停止写入、备份并显式迁移到服务结构版本 3；不要迁移原始历史数据库来充当控制台。

先完成公开包暂存与读者端构建，并安装 `reader` 的 npm 依赖；运行环境需要 Node.js 22.12 或更新版本和 npm 10 或更新版本。人工发布使用真实搜索、归一化数据和分享页构建器，不自动安装依赖。`NANNA_READER_DATA_DIR` 是运营者的 v2 构建源，`NANNA_READER_SITE_DATA_DIR` 是匿名站点的 v3 数据，必须是互不嵌套的独立目录，不能再指向同一个根目录。旧的同目录配置须先停写、保留原包，再用独立目录重新暂存与构建；不要覆盖现有数据。公开源、站点和发布暂存目录仍须支持原生发布所需的同文件系统硬链接，原始库和私有证据不参与这些复制。

在项目根目录设置路径。以下 `init-admin` 只用于首次创建独立控制台账号；已有账号不必再次初始化：

```sh
export NANNA_DATA_DIR="$HOME/.local/share/nannanews-review-console"
export NANNA_READER_DATA_DIR="$PWD/reader/public/data"
export NANNA_READER_SITE_DATA_DIR="$PWD/reader/dist/data"
export NANNA_READER_SEARCH_BUILDER="$PWD/reader/scripts/build-search.mjs"
export NANNA_READER_URL="http://127.0.0.1:4174/"
export NANNA_READER_EVIDENCE_DIR="$HOME/.local/share/nannanews/evidence"
nannanews init-admin owner
nannanews serve --host 127.0.0.1 --port 8080
```

`NANNA_READER_URL` 应指向实际读者端地址，支持站点子路径；未设置时不提供猜测的阅读页链接。上例假定读者端已在 4174 端口预览，启动控制台本身不会启动读者端。控制台只绑定回环地址；远程使用须另行设置受保护的访问方式，不要将它当成匿名静态页面部署。

1. 登录后进入 `/console/activities` 的“人工核查”。按规范活动汇总问题，可搜索标题、筛选日期待核对、冲突、未发布或来源更新等状态；已过期本身不等于事实错误。
2. 打开活动，对照只读原刊、图片和 AI 记录，修改标题、摘要、正文、地点、对象、参与方式等普通字段。来源链接每行一条，日期逐行明确填写；未知日期应移除该行，不用刊载日期补齐。核查说明会向读者公开，操作者与逐字段差异只保留在私有历史，勿把秘密填入正文或公开说明。
3. 点击“保存修改”只追加私有修订，不更新网站。并发修改或来源变化时，页面保留输入并显示差异；明确勾选重新核对、更新基准后，还须再点击保存。编辑可保存，管理员才可发布。
4. 管理员单独确认“发布已保存修改”。正常发布同步所有活动的最新已保存修订及其全部刊载记录，生成新内容版本、搜索索引和受影响的分享页，再切换公开数据源与已构建站点，保留旧版本。内容发布无需重建 Vite 前端；已打开的读者页刷新后读取新版本。远端 Pages 等托管仍须另外部署更新后的整个 `reader/dist`，本地发布不等于远端已更新。

“登记新活动”先保存来源链接和稳定身份，尚未产生公开版本。打开活动的“审查记录”，可按数据集查看模型实际请求文本、准备后的图片、原始响应／回执、错误和独立来源快照。缺失记录明确标为缺失；不会从抓取网页推测当时的模型输入。历史活动身份为空时可显式关联并留审计，已知属于另一活动的记录不能改绑。

新活动的“取证并审查”须分别确认访问公开来源和付费模型请求；只处理已登记来源，采用既有防重台账，一次逻辑请求不自动重放。失败依据仍保存并可查看，不批准草稿、不上传云盘，也不公开。CLI 使用同一调用：

```sh
nannanews review-activity "$ITEM_ID" --actor owner \
  --foundation-root "$FOUNDATION_ROOT" --allow-fetch --allow-paid
```

人工和 agent 共用现有会话登录、角色与 CSRF：读取／登记 `GET/POST /console/api/activities`，读取／保存 `GET/PUT /console/api/activities/<id>`，查看／关联 `GET/POST .../<id>/reviews`；选定审查继续访问其 `artifacts/{request|response|receipt}`、`images/<index>` 或 `sources/<snapshot>`。历史元数据另用 `artifacts/{legacy-request|legacy-result|legacy-decision}`，不替代缺失的实际模型输入。写请求提供现有 `X-CSRF-Token`。`POST .../<id>/review` 仅接受所选 `dataset` 与两个为真的 `confirm_fetch`、`confirm_paid`，服务端从已配置的本地绑定决定路径，不接收任意文件路径。远端只读绑定须先准备本地写入底座，不能由浏览动作创建。

管理员公开发布使用现有确认页面，或 `POST /console/api/publish` 并提供 `confirm_publish:true`；仍需 CSRF。新活动按“独立收录”公开，无虚构期号／原刊，日期未知时不进入当前日程。云盘备份另行显式执行 `data-foundation archive backup`，发布不替代归档。


“已人工核实”采用人工填写的正文和明确日期；“仍待核对”可以公开修改内容和说明，但不作为已确认活动进入当前日程。原刊与 AI 记录始终作为参考保留。来源后来重新导出时，旧修订会标为需要重新核对，不会盲目套用。已移除或合并的活动须由管理员核对历史，填写私有原因并明确停用当前那一修订，才能解除发布阻断；停用不删除历史，也不发布内容。

发布失败页分别显示公开数据源、站点、分享页和私有记录的实际完成状态。准备失败不会切换清单；部分切换后不要当作全部成功。修复权限或配置，再明确重试：系统先完成上次已记录的发布快照，期间另存的新修订仍是未发布，须再单独发布。不会自动重试，也不会因公开数据已有人工标记就虚报私有发布完成。


