# NannaNews

NannaNews 是独立运行的高校信息服务，用于收录公开消息、维护私有信息库、审核刊发内容并保存历史刊物。采集、编辑审核、刊发和静态读者端导出是彼此独立的操作。

## 产品边界

- 公开来源收录及可选的模型提取只会形成私有草稿；提取不代表审核或刊发。
- 人工编辑审核与单独的刊发决定共同确定哪些内容公开。
- 网页刊物首页按期阅读，支持范围搜索、详情原位返回及分享；卡片风险收束为“阅读提醒”，详情保留完整原因。私有[人工核查控制台](docs/operations.md#人工核查网页活动)支持对照原刊修改活动、保存修订并由管理员单独发布；静态读者端只使用公开白名单数据，PDF 为可选附件。
- 采集默认关闭。CI、Web 启动、迁移和静态读者端浏览不会启动采集或调用模型；采集 worker 须另行显式启用。
- 本实例不发送邮件、不做外部讲座同步，也不复用上游运营方的服务、账号、数据或凭据。

产品与编辑流程见 [docs/product.md](docs/product.md)，安装、迁移和部署见 [docs/operations.md](docs/operations.md)，日常操作见 [WORKFLOW.md](WORKFLOW.md)。

## 源码、静态站点与私有归档

| 内容 | 存放与上传方式 |
| --- | --- |
| 应用与前端源码、测试、配置模板、依赖锁文件、部署脚本、文档 | 提交 GitLab 仓库；不附带运行凭据 |
| 经审核的活动基本事实 `reader/public/activity-facts.json`（稳定 ID、标题、摘要、明确日期、地点／参与要点、来源及公开核查状态） | 用白名单导出更新并提交 Git；可独立构建基础阅读与检索，不含完整正文、大型原件或私有证据 |
| 编译后的网页、公开正文 JSON、搜索索引、配图、可选 PDF、分享落地页 | 由审核后的公开导出生成，作为 Pages 或其他静态托管发布产物；不提交 Git |
| 历史 HTML／PDF 原件、原始抓取响应、模型请求与回执、数据库、核查档案、备份 | 仓库外的本地私有归档或私人云盘；不进入镜像、Pages 或 Git |
| 密码、模型 key、会话密钥 | 独立受限配置，不混入可分享的归档；依赖与构建缓存可重建，不归档 |

静态读者端的正文与检索依赖公开 JSON，不能删除这些运行文件来代替剔除原始响应。HTML 是正常原件格式，不要求每期都有 PDF；文件名与首页日期冲突的 PDF 不能自动配期。

控制台与静态读者端可用独立的[本地 Docker 部署](docs/operations.md#本地人工核查与读者端-docker)一起启动，私有数据与公开站点分别挂载，不开启采集或继承模型凭据。归档和上传边界见[运维说明](docs/operations.md#源码静态发布与离线归档)。
离线整理现有活动、原件和核查结果使用 [`nannanews data-foundation`](docs/operations.md#离线活动数据底座与历史清理)。私有控制台直接只读查询单一 SQLite 历史库，仅在选中证据时读取登记的本地文件，不生成逐行 JSON 查询目录。原生 restic 保存加密增量快照；本地保存、云端备份和公开发布分别执行。新活动、人工修订与明确授权的取证审查使用同一私有工作流，详见[人工核查网页活动](docs/operations.md#人工核查网页活动)。
本机源码保留在 `~/project/NannaNews/`；配置、运行库、统一附件池、公开站点和历史整理记录集中在 `~/service/nannanews/`，由 systemd 用户服务运行。PDF 与其他原件一起按摘要保存，不散放在项目父目录。完整路径和启停命令见[本地服务与目录布局](docs/operations.md#本地服务与目录布局)。


## 安装与启动

需要 Python 3.11 或更新版本和系统 Cairo 库（安装方法见[运维文档](docs/operations.md#安装与配置)）。按 `pyproject.toml` 将项目作为软件包安装：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
nannanews --help
nannanews init-admin owner
nannanews serve
```

`init-admin` 会显式准备运行环境、迁移数据库并创建首位管理员。后续结构变更前须停止所有写入进程，再运行 `nannanews migrate`；服务启动和读取操作不会静默迁移。Docker、备份恢复、回滚、CI、配置和 Pages 说明见 [运维文档](docs/operations.md)。

## 上游与许可

本项目是基于 [nik-nul/WebNews](https://github.com/nik-nul/WebNews) 修改并独立运行的产品。保留上游署名和许可声明；本次独立重构修改日期为 2026-10-18。项目按 GNU GPL 第 3 版授权，详见 [LICENSE](LICENSE)。不包含上游生产数据库、私有材料、凭据、账号或分发服务。
