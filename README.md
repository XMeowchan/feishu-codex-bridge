# Feishu Codex Bridge

`feishu_codex_bridge` 是一个本地常驻守护工具，用来把飞书 P2P 私聊消息桥接到同一台机器上的长驻 `Codex CLI` 会话。

从这版开始，`Codex` 不再跑在隐藏 PTY 里，而是固定运行在一个可 attach 的 `tmux` session 中。你可以随时进入这个 session 看真实界面，也可以 detach 后让它继续后台工作。

它只做三件事：

1. 用 `lark-cli event +subscribe --event-types im.message.receive_v1 --compact --quiet --as bot` 订阅飞书消息
2. 只接收指定 `allowed_sender_open_id` 的 `p2p` 消息
3. 把消息送进本地 `Codex CLI` 会话，并要求 `Codex` 自己通过 `lark-im` 回消息

桥接层不会把 `stdout/stderr` 自动发回飞书；终端输出只用于本地日志和诊断。

## 目录内容

- `bridge.py`: 守护进程主脚本
- `bridge.example.toml`: 配置模板
- `start_bridge.py`: 跨平台公共启动器
- `start_bridge.command`: macOS 双击启动脚本
- `start_bridge.cmd`: Windows 双击启动脚本
- `feishu-codex-bridge.plist.example`: macOS `launchd` 示例

## 前置条件

本机需要满足：

- `python3` 可用
- `tmux` 可用
- `lark-cli` 已安装并完成 `lark-cli config init`
- 飞书开放平台已开启长连接事件订阅
- 已添加 `im.message.receive_v1`
- 已开通 `im:message:receive_as_bot`
- `Codex CLI` 可用

如果你打算让 `Codex` 在处理过程中读取 `lark-im` 与 `lark-shared` 技能文件，推荐把 `command` 配成：

```bash
codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox
```

如果你不用完全放开模式，至少要确保 `Codex` 能读取技能目录和 `cwd` 外的必要路径。

## 配置

先复制模板：

```bash
cp Tools/feishu_codex_bridge/bridge.example.toml Tools/feishu_codex_bridge/bridge.toml
```

然后填写真实值：

```toml
cwd = "/ABSOLUTE/PATH/TO/YOUR/WORKSPACE"
command = "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox"
allowed_sender_open_id = "ou_xxxxxxxxxxxxxxxxx"
message_scope = "p2p"
skill_path = "/Users/zhangyoufei/.agents/skills/lark-im/SKILL.md"
ack_text = "已收到，正在转给本地 Codex 处理，后续回复会直接回到这条消息下。"
session_idle_minutes = 30
log_path = "/ABSOLUTE/PATH/TO/feishu-codex-bridge.log"
tmux_session_name = "feishu-codex-bridge"
```

字段说明：

- `cwd`: `Codex` 工作目录
- `command`: 原样执行的 `Codex` 启动命令
- `allowed_sender_open_id`: 只允许这个飞书用户触发
- `message_scope`: v1 固定为 `p2p`
- `skill_path`: 注入给 `Codex` 的 `lark-im` 技能文件路径
- `ack_text`: 服务收到有效消息后立即回的确认文案
- `session_idle_minutes`: 会话空闲多久后自动重建
- `log_path`: 本地日志文件
- `tmux_session_name`: 长驻 `tmux` 会话名，后续可用它 attach 进去

补充说明：

- `cwd`、`skill_path`、`log_path` 都支持相对路径
- 相对路径会以 `bridge.toml` 所在目录作为基准来解析
- 如果你把整个 `feishu_codex_bridge` 文件夹打包发给别人，别人通常至少需要改这三项：`cwd`、`allowed_sender_open_id`、`skill_path`

## 运行

先做环境检查：

```bash
python3 Tools/feishu_codex_bridge/bridge.py --config Tools/feishu_codex_bridge/bridge.toml --check
```

再以前台方式启动：

```bash
python3 Tools/feishu_codex_bridge/bridge.py --config Tools/feishu_codex_bridge/bridge.toml
```

启动后通常会先看到：

```text
Starting Feishu event subscription
```

然后在订阅进程稳定约 2 秒后，你会看到这条 ready 日志：

```text
Feishu event subscription ready (connection stable, you can now send messages from Feishu)
```

看到这条后，就表示当前已经过了启动窗口期，可以去飞书发送第一条消息了。

如果后面又看到：

```text
Feishu event subscription is receiving events (first event observed)
```

说明这条订阅连接已经实际收到了至少一条飞书事件。

如果你希望直接双击启动：

- macOS：双击 [start_bridge.command](/Volumes/M.2/WorkSpace/OwMini/Tools/feishu_codex_bridge/start_bridge.command)
- Windows：双击 [start_bridge.cmd](/Volumes/M.2/WorkSpace/OwMini/Tools/feishu_codex_bridge/start_bridge.cmd)

这两个文件都会用相对位置自动找到同目录下的 [bridge.py](/Volumes/M.2/WorkSpace/OwMini/Tools/feishu_codex_bridge/bridge.py) 和 [bridge.toml](/Volumes/M.2/WorkSpace/OwMini/Tools/feishu_codex_bridge/bridge.toml)。

启动后可以随时查看真实 `Codex` 终端：

```bash
tmux attach -t feishu-codex-bridge
```

如果你改了 `tmux_session_name`，把上面的名字换掉即可。

在 `tmux` 里按 `Ctrl+b` 然后按 `d` 可以 detach，不会停止桥接服务。

如果你只是想关闭当前 `tmux` 会话，但保留桥接服务继续运行：

```bash
tmux kill-session -t feishu-codex-bridge
```

下次再收到普通飞书消息时，桥接服务会自动重建一个新的 `tmux` 会话。

## 工作方式

收到有效飞书消息后，桥接层会先执行：

```bash
lark-cli im +messages-reply --message-id <incoming_message_id> --text "<ack_text>" --as bot
```

然后把消息包装成一段 envelope 送给当前 `Codex` 会话。启动时还会先注入一段 bootstrap prompt，明确告诉 `Codex`：

- 当前运行在飞书桥接模式
- 用户可见回复不要依赖终端输出
- 回复时优先使用 `lark-im`
- 先读取 `lark-shared`
- 对当前消息优先调用 `lark-cli im +messages-reply --message-id <id> --text ... --as bot`
- 不要等所有回答完成才一起发，收到消息后先发一条简短进度，再在处理中持续追加回复
- 如果 `Codex` 在 10 秒内还没有主动通过飞书回用户，桥接层会自动补一条“仍在处理中”的 watchdog 提示；之后每 30 秒补一条心跳，直到 `Codex` 真正开始通过飞书回复或任务结束
- 当前版本只处理订阅通道实时收到的消息；如果服务启动前或订阅建立窗口期有消息漏掉，桥接层不会补捞历史消息

桥接层会自动创建并复用配置里的 `tmux_session_name`；如果会话空闲超时，服务会销毁旧 session 并在下一条消息到来时重建。

## 行为边界

v1 固定行为：

- 只支持 `p2p`
- 只允许一个 `allowed_sender_open_id`
- 只接收 `text` / `post` 两类文本可读消息
- 图片、文件、卡片等消息会收到“当前仅支持文本消息”
- `Codex` 输出只记本地日志，不自动桥接回飞书
- 桥接日志会尽量压缩 TUI 噪音；完整画面请直接 `tmux attach`

## 控制命令

私聊机器人时，除了普通消息，还支持下面这些桥接层原生命令：

- `/status`：立即返回桥接状态，包括当前 `tmux` 会话是否在线、是否正在处理、排队消息数、工作目录以及 attach 命令
- `/reset`：按当前消息顺序重置会话。执行到这条命令时，会销毁旧的 `Codex` 会话并立刻创建一个新的 `tmux` 会话，然后回复你新的会话已就绪
- `/interrupt`：立即向当前 `tmux` 中的 `Codex` 会话发送中断信号，尝试停止当前这一轮处理，但不重建整个会话

推荐把它们理解成“桥接层保留命令”，而不是交给 `Codex` 解释的普通对话。

精确匹配规则：

- 只有消息内容在去掉首尾空白后，**完全等于** `/status`、`/reset`、`/interrupt` 之一时，才会触发桥接层命令
- 例如 `/status 现在怎么样`、`/reset please`、`/interrupt 帮我停一下` 都不会命中保留命令
- 其他以 `/` 开头但不精确匹配这三个命令的内容，会被当成普通用户消息，照常转发给 `Codex`

飞书端示例：

```text
/status
/reset
/interrupt
```

说明：

- `/reset` 不会粗暴打断前面已经在处理中的消息；它会进入同一个顺序队列，保证上下文切换时机可预期
- `/interrupt` 走立即执行路径，不排队；如果当前没有正在执行的任务，会直接回你“当前没有正在执行的任务”
- 如果 `/interrupt` 后当前这一轮仍未停止，可以再发送 `/reset`
- `/status` 现在会额外显示两段桥接状态：`订阅进程`、`事件通道`
- `事件通道：连接中，正在建立事件通道` 表示还没 ready，建议再等一下
- `事件通道：已就绪，当前可以在飞书发送消息` 表示现在可以发送第一条飞书消息
- `事件通道：已收到事件（最近 X 秒前）` 表示订阅已经实际收到过消息事件

## 推荐使用流程

第一次启动或重启桥接后，推荐按这个顺序操作：

1. 启动桥接服务
2. 等待本地终端出现 `Feishu event subscription ready (connection stable, you can now send messages from Feishu)`
3. 再去飞书发送第一条普通消息，或者先发 `/status`
4. 如果想看真实界面，执行 `tmux attach -t <tmux_session_name>`
5. 如果当前任务卡住，先发 `/interrupt`；还不行再发 `/reset`

## launchd

如果你希望它在 macOS 上常驻，复制并修改：

```bash
cp Tools/feishu_codex_bridge/feishu-codex-bridge.plist.example ~/Library/LaunchAgents/com.example.feishu-codex-bridge.plist
launchctl load ~/Library/LaunchAgents/com.example.feishu-codex-bridge.plist
```

记得把 `ProgramArguments`、`WorkingDirectory`、`StandardOutPath`、`StandardErrorPath` 改成你自己的绝对路径。

## 故障排查

- 启动后完全收不到消息：优先检查飞书开放平台是否开启长连接订阅，以及 `im.message.receive_v1`
- 能收消息但回不了消息：检查 bot scope 和群/会话可见范围；`messages-reply` 需要 bot 身份
- `Codex` 无法读取技能：检查 `command` 的权限模式，确保它能访问 `skill_path`
- 日志里看到 `Do you trust the contents of this directory?`：首次运行时脚本会自动确认
- 日志里看到 `Continue anyway? [y/N]`：脚本会自动接受兼容提示，并为子进程设置 `TERM=xterm-256color`
- 想看真正的交互界面：执行 `tmux attach -t <tmux_session_name>`
