# Telegram ↔ Codex bridge

用一个 Telegram Bot 控制本机已有的 Codex 会话，保留原会话的历史、模型和权限设置。这是通过 Codex app-server 工作的 Python 桥接程序。

## 让其他 agent 配置

可以把仓库地址和下列文字发给目标机器上的 agent。私有仓库需要先为该机器或 agent 使用的 GitHub 账号配置读取权限；Bot Token、Telegram 用户 ID 和实际会话绑定不在仓库里，需要在目标机器上配置。环境要求见下方“环境”一节。

```text
请部署 https://github.com/HViktorTsoi/codex-telegram-bridge，使用 v0.2.1 或更新的稳定版本。

先阅读 README.md、DEPLOYMENT.md 和 TOPICS.md，检查目标机器的 Python、tmux 和 Codex 服务。
复用本机已有 Codex app-server，列出并核对我要绑定的原 session；绑定时保持原 thread ID，不新建或 fork。
使用独立 Bot 和私有配置目录，避免与已有实例争用同一个 Token；将访问策略设为仅允许本人 Telegram 用户 ID。
按需引导我开启 BotFather Threaded Mode；多会话模式应创建真实 Telegram 话题并保存返回的 topic ID，替换示例里的占位值。
安装依赖并运行测试，再启动对应实例；manage.py 只管理默认实例，独立状态目录需使用 bridge.py 或 topics_bridge.py 的 run --state-dir 入口，不要误操作已有实例。
验证 /status 中的原 thread ID、Markdown 和图片输入；多会话模式还需验证 /sessions 和各话题的消息归属。
缺少必要配置时再向我索取；Bot Token 只保存在目标机器私有凭据文件中，不提交到 GitHub。
```

目前支持的能力见下方“功能”。生成图片、文件自动回传尚未实现；跨会话任务盘点也尚未做成 `/tasks` 命令。

## 功能

- 文字、图片、文件、相册、位置、联系人和投票输入。
- 一个私聊内用不同话题连接不同 Codex 会话。
- `/sessions` 按钮流程：查看本机会话 → 选择 → 核对完整 ID → 确认绑定到新话题。
- 已绑定会话可通过按钮重新创建话题，恢复删除后的入口。
- 可选话题内工作状态消息：原地更新用时、待处理、完成、停止和断连状态，覆盖从电脑端发起的任务。
- Codex 回复默认使用 Telegram Rich Markdown 排版，支持标题、表格、代码块和公式；可按会话切换纯文本。
- `/model`、`/effort` 查询和修改对应会话的参数；`/status` 核对绑定；`/stop` 停止对应会话的任务。
- 持久化收发队列、重复更新去重、按原目标恢复任务，未确认的输入不盲目重复执行。

每个 Bot 只接受配置的白名单用户的私聊。同一个 Token 只能运行一个更新接收器。

## 环境

- Linux、Python 3.10 或更新版本、tmux。
- 可访问的 Codex app-server Unix socket，且该服务能读取目标已有会话。
- Telegram Bot Token、本人 Telegram 用户 ID。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

自动测试使用模拟 Telegram 和本机临时 socket，不需要真实 Token 或 Codex 登录。

## 配置

代码和每台机器的配置分开保存：

| 内容 | 默认位置 |
|---|---|
| 桥接配置、SQLite、日志、健康状态 | `~/.local/state/codex-telegram/` |
| Bot 凭据、访问白名单 | `~/.claude/channels/telegram/` |
| Codex socket 示例 | `~/.codex/app-server-control/app-server-control.sock` |

socket 路径应指向实际使用的已有服务；使用另一个新服务不等于接入桌面上正在使用的会话。配置中的路径需要填写为本机绝对路径。

参考 [config.single.example.json](config.single.example.json)、[.env.example](.env.example) 和 [access.example.json](access.example.json)，在上述私有目录中创建实际配置。Token、owner ID、socket 路径和已有会话 ID 都需要替换；核对完成后将 `setup_required` 改为 `false`。目录权限使用 `700`，凭据和状态文件使用 `600`。

绑定已有会话时应先用 `thread/read` 核对标题、ID 和历史，再用 `thread/resume` 恢复同一个 ID。程序不以 `thread/start` 或 `thread/fork` 作为绑定失败后的替代方案。

### 多会话话题模式

1. 在 BotFather 为该 Bot 开启 Threaded Mode / Topics。
2. 将原会话绑定到一个 Telegram 私聊话题，参考 [config.topics.example.json](config.topics.example.json)。
3. 启动后发送 `/sessions`，通过按钮添加其他已有会话；新增绑定无需重启。
4. 会话若尚未出现在列表中，先在电脑端创建并发送首条消息，再点击“重新加载”。

原私聊入口继续使用 `root_target` 指定的原会话。话题模式、恢复策略和完整配置说明见 [TOPICS.md](TOPICS.md)。

## 运行与命令

在代码目录中运行：

```sh
.venv/bin/python manage.py start
.venv/bin/python manage.py status
.venv/bin/python manage.py logs
.venv/bin/python manage.py stop
```

`manage.py` 根据 `config.json` 的 `mode` 选择单会话或话题模式。单会话首次启动可使用 `start --notify`；话题模式使用 `/sessions`。电脑、网络和 Codex 服务需要保持运行；当前没有配置开机自启。

| Telegram 命令 | 操作 |
|---|---|
| `/sessions` | 话题模式下打开会话列表、添加和重建入口 |
| `/model`、`/model <ID>` | 查询或切换当前目标会话的模型 |
| `/effort`、`/effort <级别>` | 查询或修改当前目标会话的推理强度 |
| `/format`、`/format rich`、`/format plain` | 查询回复格式，或切换排版与纯文本 |
| `/status` | 显示主机、会话名称、完整 ID 和参数 |
| `/stop` | 停止当前目标会话的任务 |
| `/help` | 查看说明 |

参数命令调用 `thread/settings/update` 并回读确认，不启动模型生成。普通消息沿用原会话设置。原 Codex 客户端继续处理审批和其他交互请求。

### 回复排版

默认将新的 Codex 回复通过 `sendRichMessage` 发送，使用 Telegram 的 Rich Markdown。菜单、状态和其他控制回复仍使用普通文字。客户端需要支持 Telegram Rich Messages。

在对应会话发送 `/format rich` 开启排版，`/format plain` 改为纯文本，`/format` 查看当前选择。选择按目标会话持久化，对之后生成的回复生效，不更改模型参数。

长回复按段发送；代码围栏会在分段处补齐，原始文字另行保留。服务端明确拒绝格式或不支持富文本接口时，发送同一段原文。话题不存在、权限、限流和送达结果不明的网络错误不会被当作格式错误降级。旧发信队列保持原来的内容和分段，更新不会将已经投递的历史重新发送。

回复中的本机 Markdown 文件、图片链接会显示为名称和路径，避免 Telegram 尝试读取本机路径而使整条回复退回纯文本。本机图片会明确标注“尚未上传”；公网链接和代码示例保留原样。这一步不读取或上传文件。

### 话题内工作状态

话题模式可在 `config.json` 的单个 `targets` 项中设置 `"activity_indicator": true`，重启对应实例后启用。也可在配置顶层开启，单个目标的设置优先；默认关闭，便于先在一个话题试用。

每次观察到对应原 Codex 会话正在执行任务时，在绑定话题发送一条静音状态消息，随后每约 15 秒编辑同一条消息显示用时。等待输入／审批、完成、失败、停止和连接中断会触发状态变化。电脑端发起的任务同样可见；状态只通过原会话的元数据和任务记录读取，不发起模型生成。

状态消息包含最后更新时间；中间回复不会被当作任务完成。重启后复用已保存的消息 ID，完成后的旧任务不补发状态。删除或重建话题后，不会把旧状态消息改投到其他话题。该功能是周期采样，短于采样间隔的任务可能没有工作中提示；启动时已完成的任务不会创建新状态消息。发信结果不明时不盲目重发，避免重复；网络恢复后有已知消息 ID 的状态可继续更新。

## 附件与可靠性

Telegram 托管 Bot API 的附件下载上限为 20MB。图片作为 `localImage` 发送；其他文件下载到本机私有目录后提供文件元数据与本机路径。相册聚合后作为一次输入。

语音和视频目前按文件接收，没有自动转写或抽帧。生成文件暂不会自动上传回 Telegram。转接层不自动执行或解压附件，也不向模型暴露带 Token 的下载链接。

消息接收与 offset 原子提交。每条输入和回复保留自己的会话与话题，切换和重建入口不会把旧任务结果转发到另一个会话。Telegram 发信没有幂等键，因此发信成功但本地尚未提交时崩溃，仍可能重复投递一次回复。

删除话题后可使用 `/sessions` → 选择原会话 → **重新创建话题** → 确认，保留原 Codex 历史。旧话题的未送达回复会保留，不自动重发或重复执行任务。

## 版本维护

两台机器使用同一个源码仓库，各自保留 Bot Token、白名单和运行数据库。修改通过测试后提交，再发布版本 tag，并分别更新机器；更新与回退流程见 [DEPLOYMENT.md](DEPLOYMENT.md)，功能记录见 [CHANGELOG.md](CHANGELOG.md)。

`.github/workflows/tests.yml` 为推送和 Pull Request 配置 Python 3.10 / 3.12 自动测试。GitHub 仓库建立并推送后才会实际运行该工作流。

## 可选提醒示例

`bus_reminder.py` 和 `manage_bus_reminder.py` 是独立的九巴末班车提醒示例。它们不接收 Telegram 更新，可与聊天桥接共用 Bot；配置与状态默认位于 `~/.local/state/kmb72-reminder/`，需要另行配置，启动桥接不会自动启用提醒。

## 接口文档

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Telegram Bot API](https://core.telegram.org/bots/api)
