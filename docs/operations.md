# 安装、生命周期与部署

运行数据只放在运营者自己的数据目录，不进入源码、软件包、镜像或静态站点。Python 项目通过 `pyproject.toml` 安装，并使用已安装的 `nannanews` 命令。

## 安装与配置

需要 Python 3.11 或更新版本：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
nannanews --help
```

`NANNA_DATA_DIR` 指定私有数据根目录。未设置、为空或只有空白时，默认为 `$XDG_DATA_HOME/nannanews`，通常是 `~/.local/share/nannanews`。数据库、上传文件、采集依据和会话密钥都保存在这里；限制该目录的访问权限。将 `.env` 权限设为 `0600`，不要提交 `.env`、密钥、数据库、上传文件或备份。

`NANNA_SECRET_KEY` 留空时，显式准备运行环境会在数据目录中生成私有 `session.key`。配置加载、CLI 帮助和来源目录导入不会写文件。`nannanews import-source-catalog INPUT OUTPUT` 将审核过的工作簿 JSON 导入公开来源目录，不访问数据库或网络。

## 数据库生命周期与备份

`nannanews migrate` 会显式准备运行环境并执行有版本记录、可重复的结构迁移；它不调用模型、不采集来源，也不执行历史回填。首次创建管理员时，`nannanews init-admin owner` 会先迁移，再创建账号；没有默认密码，也可使用 `--password-stdin` 从标准输入提供密码。

`nannanews serve --host 127.0.0.1 --port 8080`、`nannanews collect-worker` 和读取命令都要求数据库结构为当前版本；启动或读取不会静默迁移。结构缺失、过旧或过新时，命令会拒绝继续并要求显式迁移。采集 worker 仍须由运营者设置 `NANNA_COLLECTION_ENABLED=true` 并单独启动。

备份或迁移前，先停止 Web、采集、历史复核及其他所有写入者。备份放在运行数据目录之外，并限制访问：

```sh
nannanews backup "$BACKUP_FILE"
nannanews migrate
nannanews restore "$BACKUP_FILE" --target "$FRESH_DATA_DIR"
```

`BACKUP_FILE` 和 `FRESH_DATA_DIR` 必须由运营者设置为实际路径。恢复只允许写入当前数据根目录之外的全新目标目录；备份包含数据库、文件及付费尝试防重历史。恢复时，`NANNA_DATA_DIR` 仍须指向已停止写入的当前运行数据：命令会保留其中较新的历史台账、原始回执，以及原生来源提取和每日采集的付费尝试防重事实，包括备份之后才发生的尝试。不得删除、重置或替换这些记录以重试付费请求，也不得原地降级数据库。备份不包含运行凭据或会话密钥；恢复后要配置自己的凭据，现有登录会话也会失效。

原生防重按规范化来源 URL 和内容摘要匹配，即使备份中没有该来源也保留尝试记录和回执；这不恢复备份之后新增的草稿或采集结果。已完成、结果不确定及已失败的尝试都不会因恢复而重新进入自动采集；既有的显式编辑重试规则不变。

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

Git tag 是镜像版本。注册表已启用时，`release-image` 会要求 CI 作业凭据并推送 `CI_REGISTRY_IMAGE:CI_COMMIT_TAG`，不会创建 `latest` 别名；注册表停用时跳过推送，CI 只生成短期 OCI 构建产物。注册表配置由项目管理员负责。

Pages 默认不发布。只有默认分支的 `PUBLISH_STATIC_READER=true` 且 `STATIC_READER_DATA_SOURCE` 指向已公开并审核过的白名单导出时，才会构建静态站点。URL 不得带凭据或查询参数，也不能跨站重定向；输入不能指向私有数据库、原始归档、备份或应用数据目录。暂存步骤只复制清单引用的资源并拒绝额外文件。Pages 只托管静态页面，不是 Python 后端。Pages 公开权限、Runner、注册表和真实目标主机权限都须由运营者提供；CI 成功不代表后端已部署或远端发布已完成。

