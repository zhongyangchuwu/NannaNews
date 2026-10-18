# NannaNews

NannaNews 是独立运行的高校信息服务，用于收录公开消息、维护私有信息库、审核刊发内容并保存历史刊物。采集、编辑审核、刊发和静态读者端导出是彼此独立的操作。

## 产品边界

- 公开来源收录及可选的模型提取只会形成私有草稿；提取不代表审核或刊发。
- 人工编辑审核与单独的刊发决定共同确定哪些内容公开。
- 历史刊物使用独立归档；静态读者端只从已审核的公开白名单导出构建。
- 采集默认关闭。CI、Web 启动、迁移和静态读者端浏览不会启动采集或调用模型；采集 worker 须另行显式启用。
- 本实例不发送邮件、不做外部讲座同步，也不复用上游运营方的服务、账号、数据或凭据。

产品与编辑流程见 [docs/product.md](docs/product.md)，安装、迁移和部署见 [docs/operations.md](docs/operations.md)，日常操作见 [WORKFLOW.md](WORKFLOW.md)。

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
