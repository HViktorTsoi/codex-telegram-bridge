# 源码与部署管理

源码仓库维护程序、测试、示例配置和文档；每台机器的凭据、实际会话绑定、数据库和日志留在私有状态目录。不同机器使用同一份源码，分别使用自己的 Bot 和状态目录。

## 开发与发布

1. 在工作分支修改源码，运行 `python3 -m unittest discover -v`。
2. 通过 Pull Request 检查修改和 GitHub Actions 测试。
3. 合并后记录变更，并为稳定提交创建版本 tag，例如 `v0.1.0`。
4. 在一台机器验证后，再将相同版本部署到另一台机器。

工作流运行 Python 3.10 和 3.12 的自动测试，不连接真实 Telegram 或 Codex 账号。尚未配置自动部署。

## 更新机器

在已经连接远程仓库的代码目录中：

```sh
git status --short
git fetch origin --tags
python3 manage.py status
```

先保存本机未提交的改动，并备份私有状态目录中的配置和 SQLite。数据库运行时应使用 SQLite backup API 获取一致快照。

然后停止该 Bot 的接收器，切换到已经验证的版本并重新启动：

```sh
python3 manage.py stop
git switch --detach v0.1.0
python3 -m pip install -r requirements.txt
python3 -m unittest discover -v
python3 manage.py start
python3 manage.py status
```

命令中的 `python3` 应使用已安装依赖的解释器，也可以换成 `.venv/bin/python`。代码切换不修改私有配置。确认健康状态的 `connected` 为真、机器人身份和已有会话 ID 正确，并在 Telegram 中验证 `/sessions`、`/status` 和回复归属。

如果一台机器运行多个 Bot 实例，只操作本次更新实例的进程与状态目录。`manage.py` 当前管理默认 `codex-telegram` 实例；其他实例使用自己的启动入口。

## 回退

记录当前版本和配置备份后，停止对应接收器，切换到已经验证的旧 tag，再按启动与验证步骤操作。新版本若改变数据库结构，应先检查旧版兼容性；不能直接用过期备份覆盖仍有未处理请求的数据库。

## 初始快照

`v0.1.0` 是开始统一版本管理时的源码快照。此前运行版本的配置和数据库备份仍保留在各自状态目录。GitHub 仓库创建、首次推送和两台机器的 remote 设置属于后续接入步骤。
