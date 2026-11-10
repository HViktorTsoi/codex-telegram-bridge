# 源码与部署管理

源码仓库维护程序、测试、示例配置和文档；每台机器的凭据、实际会话绑定、数据库和日志留在私有状态目录。不同机器使用同一份源码，分别使用自己的 Bot 和状态目录。

## 开发与发布

1. 在工作分支修改源码，运行 `python3 -m unittest discover -v`。
2. 通过 Pull Request 检查修改和 GitHub Actions 测试。
3. 合并后记录变更，为 `VERSION` 中的稳定版本创建对应 `v` 前缀 tag。
4. 在一台机器验证后，再将相同版本部署到其他机器。

工作流运行 Python 3.10 和 3.12 自动测试，不连接真实 Telegram 或 Codex 账号。尚未配置自动部署。

## 选择实例与稳定版本

先确定这次更新的 Bot、私有状态目录和实际 Python 解释器。`manage.py` 只管理默认 `~/.local/state/codex-telegram/` 实例；其他实例使用自己的 `bridge.py` 或 `topics_bridge.py run --state-dir ...` 启动入口，不要停止同机其他 Bot。下文的 `python3` 均应替换为该实例已安装依赖的解释器，例如 `.venv/bin/python`。

在已连接远程仓库的代码目录依次执行，任何一步失败时先处理原因：

```sh
git status --short
git fetch origin main --tags
python3 manage.py status
bridge_version="v$(git show origin/main:VERSION)"
python3 -c 'import re,sys; assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+",sys.argv[1]), "请选择稳定版本"' "$bridge_version"
git rev-parse --verify "${bridge_version}^{commit}"
test "$(git show "${bridge_version}:VERSION")" = "${bridge_version#v}"
```

`bridge_version` 只定位候选稳定版本。核对该 tag 对应提交的 GitHub Actions 已成功后再继续；不能将未经验证的 `main`、测试版或任意“最新 tag”自动当作稳定版。若 `origin/main` 中的版本还没有对应 tag，停在版本选择阶段，改用已发布且检查通过的稳定 tag；也可直接把变量指定为这样的 tag。推荐呈现的默认值从 v0.11.24 开始，首次部署或统一迁移应使用该版本或更新的稳定版本。

先保存本机未提交的改动，并备份本次实例的配置、凭据和 SQLite。运行中的数据库使用 SQLite backup API 获取一致快照。保留待处理请求，不用过期备份覆盖当前数据库。

## 呈现配置与旧实例迁移

话题模式参考 [config.topics.example.json](config.topics.example.json)。新话题实例默认采用累计正文与持久按钮状态栏；部署或统一迁移时，仍建议将以下三项明确合并到原有私有 `config.json` 顶层：

```json
{
  "notification_mode": "completion",
  "preview_style": "status-bar",
  "status_card_pin": false
}
```

这段 JSON 只是要合并的字段，不能替换完整配置。保留 Bot 凭据、owner、socket、会话 ID、`root_target`、`targets` 和各话题 ID。单个 `targets` 项的同名配置优先于顶层；选择统一呈现后，检查每个目标，将其中旧的三项覆盖删除或对齐。否则某些会话仍可能逐条发送或显示独立状态消息。

显式选择的 `notification_mode: "all"`、`preview_style: "plain"`、`"block"`、`"status-card"` 或 `"status-draft"` 继续受支持，代码升级不会强行覆盖。显式旧配置不会因切换源码而删除，升级前已准备或尝试发送的旧计划也会保留固定内容，所以仅更新 Git 代码或只改示例文件不等于统一迁移所有目标。统一启用应明确写入三项并重启本次实例，随后核对健康状态。

中途正文默认 `preview_body_style: "normal"`。小号淡色段落另设 `"footer"`，引用样式另设 `"quote"`；这些是可选外观，不是累计预览和按钮状态栏生效的前提。单会话兼容入口使用 [config.single.example.json](config.single.example.json)，保持原行为；不能只向单会话配置加入三个字段就视为已部署话题功能，切换话题模式需先按 [TOPICS.md](TOPICS.md) 配置真实绑定。

## 更新与启动

停止本次实例的接收器，切换到已验证版本，使用实际运行解释器更新依赖、运行测试并重新启动。默认实例示例：

```sh
python3 manage.py stop
git switch --detach "$bridge_version"
python3 -m pip install -r requirements.txt
python3 -m unittest discover -v
python3 manage.py start
python3 manage.py status
```

Footer 渲染需要 `markdown-it-py`，升级时应在服务启动前安装当前 `requirements.txt`；只更新源码不能视为完成依赖升级。代码切换不改写私有配置；显式选择保留，未配置字段按当前版本默认值解析。

## 核验实际效果

核对该实例的 `health.json`，不要只看顶层 `connected`：

- 进程已重新启动，代码版本、Bot 身份和私有状态目录与本次实例一致。
- 每个目标 `connected` 为真且无错误，原生会话 ID 与话题 ID 保留。
- 统一推荐呈现时，每个 `targets[key]` 的实际 `notification_mode` 是 `completion`、`preview_style` 是 `status-bar`、`status_card_pin` 是 `false`；`preview_body_style` 与所选 `normal`、`footer` 或 `quote` 一致。

在 Telegram 验证 `/sessions`、`/status`、`/skills`、Markdown、图片及回复归属。用一轮新任务核对：接收后出现普通预览消息，中途正文和按钮状态栏在同一消息更新；成功后过程合并静音发送、最终回复单独通知，终态按钮保留，不追加成功完成文案。状态栏随消息滚动，不是屏幕固定悬浮条。旧历史不会重排，升级前已经准备或尝试发送的旧计划保留固定内容；使用新任务验证新设置。

同机有其他 Bot、提醒任务或其他服务时，确认它们未被重启、改绑或修改配置。

## 回退

记录当前版本和配置备份后，停止对应接收器，切换到已经验证的旧 tag，再按启动与核验步骤操作。新版本若改变数据库结构，应先检查旧版兼容性；不能直接用过期备份覆盖仍有未处理请求的数据库。回退源码不会自动回退呈现配置。

## 初始快照

`v0.1.0` 是开始统一版本管理时的源码快照。此前运行版本的配置和数据库备份仍保留在各自状态目录。GitHub 仓库创建、首次推送和两台机器的 remote 设置属于后续接入步骤。
