# TG 群聊总结 Bot

从零部署一个只做两件事的 Telegram 群机器人：**群聊总结** 和 **群日报**。
不需要数据库服务、不需要 Web 面板、不依赖任何第三方 SaaS，一台 1 核 1G 的小机器就够。

- 群友发 `/sum 4h`，机器人把过去 4 小时的讨论压成几张原生折叠卡片，每条要点都带真实消息直链。
- 每天固定时间自动发一份「群日报」：5～8 个话题，人物归属和数字经过硬校验，宁可少写也不编造。
- 支持多 LLM 节点自动兜底：主节点挂了自动换下一个，连续失败自动熔断。

## 功能

| 功能 | 说明 |
|---|---|
| 手动总结 | `/sum`、`/sum 300`、`/sum 4h`、`/sum 今天`、`/sum 昨天` |
| 定时总结 | 每天固定时间点，或每 N 小时整点循环；槽位幂等，不会重复发 |
| 群日报 | 每天 1～N 个时间点，生成 5～8 条带来源的短话题 |
| 来源校验 | 只允许本次记录里真实存在的 `msg_id`；编造的链接会被删除 |
| 事实校验 | 日报逐条核对「主来源 → 作者」与正文数字，无据条目直接丢弃 |
| 思考过滤 | 自动剥掉 `think` 标签、伪装成卡片的「分析思路」和卡片外的草稿 |
| 多节点兜底 | 端点按顺序尝试，失败自动切换；单节点超时受总预算约束 |
| 权限分级 | Owner / 群管理员 / 普通群员三档取数上限 |
| 零公网端口 | 长轮询模式，不需要开防火墙端口，也没有 Web 面板可爆破 |

## 快速部署

### 1. 准备 Bot

1. 找 [@BotFather](https://t.me/BotFather) 发 `/newbot`，拿到 Token。
2. **关闭隐私模式**：`/setprivacy` → 选择你的 Bot → `Disable`。
   不关的话，Bot 只能收到 `/` 开头的指令，收不到普通聊天，就没有素材可总结。
3. 把自己的数字 ID 记下来（给 [@userinfobot](https://t.me/userinfobot) 发消息可查）。

### 2. 拉代码、装依赖

```bash
git clone <你的仓库地址> tg-summary-bot
cd tg-summary-bot
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

要求 Python 3.10+（推荐 3.11/3.12）。

### 3. 配置

```bash
cp .env.example .env
cp data/llm_endpoints.example.json data/llm_endpoints.json
```

编辑 `.env`，至少填三项：

```ini
TELEGRAM_BOT_TOKEN=你的Token
OWNER_ID=你的数字ID
LLM_API_KEY=你的模型密钥
```

编辑 `data/llm_endpoints.json`，把 `url` / `model` 换成你实际使用的 OpenAI 兼容端点：

```json
[
  {
    "id": "primary",
    "name": "主节点",
    "url": "https://api.example.com/v1/chat/completions",
    "key_env": "LLM_API_KEY",
    "model": "your-model-name",
    "timeout": 40
  }
]
```

要点：

- `url` 要写**完整路径**，到 `/chat/completions` 为止，不是 base_url。
- 密钥优先用 `key_env` 从环境变量读，不要把密钥写进 JSON。
- 想加备用节点就往后追加，数组顺序就是调用顺序。
- 改完 JSON 即时生效，不用重启。

### 4. 启动

```bash
.venv/bin/python bot.py
```

看到 `Bot 启动，开始轮询` 就成功了。

### 5. 授权群

1. 把 Bot 拉进群，随便发一条消息。
2. 在群里发 `/id` 拿到群 ID。
3. Owner 在群里发 `/auth`（或私聊 `/auth <群ID>`）。

之后 Bot 才开始积累该群消息。**授权之前的历史消息拿不到**，这是 Telegram Bot API 的限制。

### 6. 常驻（可选）

```bash
sudo cp deploy/tg-summary-bot.service /etc/systemd/system/
sudo sed -i 's/YOUR_USER/你的用户名/' /etc/systemd/system/tg-summary-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-summary-bot
sudo journalctl -u tg-summary-bot -f
```

`ProtectSystem=strict` + `ReadWritePaths` 只放行 `data/` 目录；如果你的代码目录不在 `/opt/tg-summary-bot`，记得同步改 unit 里的三个路径。

## 指令

### 群内所有人

| 指令 | 作用 |
|---|---|
| `/sum` | 最近 80 条 |
| `/sum 300` | 最近 300 条（普通成员上限 500） |
| `/sum 4h` / `/sum 90m` / `/sum 2d` | 按时间窗口 |
| `/sum 今天` / `/sum 昨天` | 北京时间自然日 |
| `/id` | 查看群 ID 与授权状态 |
| `/help` | 使用说明 |

### 群主 / 群管理员

| 指令 | 作用 |
|---|---|
| `/autosum 22:00 4h` | 每天 22:00 总结前 4 小时 |
| `/autosum 12:00,22:00 6h` | 每天两个时间点 |
| `/autosum 4h 4h` | 每 4 小时总结前 4 小时 |
| `/autosum test` | 立即触发一次 |
| `/autosum off` | 关闭 |
| `/autodaily 08:00,20:00 12h` | 每天 08:00 与 20:00 各出一份日报 |
| `/autodaily test` / `off` | 测试 / 关闭 |
| `/settings` | 图形面板：开关、预设、立即测试 |

### Owner

| 指令 | 作用 |
|---|---|
| `/auth` / `/auth <群ID>` | 授权群 |
| `/unauth <群ID>` | 撤销授权并清理该群配置 |
| `/status` | 查看模型链、密钥状态、熔断状态 |
| `/settings`（私聊） | 列出所有已授权群 |

Owner 私聊时，`/autosum` 与 `/autodaily` 支持第一个参数带群 ID，例如：

```
/autosum -1001234567890 22:00 4h
```

## 工作原理

```
群消息 → SQLite（文本 + msg_id + 回复链）
                │
      /sum 或 定时触发
                │
        按条数 / 时间窗取数
                │
   压缩成 msg_id|U1>回复id|内容
                │
      多节点 LLM 依次尝试
                │
  清洗思考痕迹 → 来源白名单校验
                │
  日报：作者归属 + 数字校验 → 不足补选一次
                │
   原生折叠卡片（失败降级 HTML）
```

几个刻意的设计：

- **发言人不直接进 prompt**。普通总结用 `U1/U2` 代号加映射表，日报用稳定 `user_id` 建代码，避免同名用户串线，也压缩 token。
- **来源必须可验证**。模型给出的每条链接都要能在本次记录里找到对应 `msg_id`，否则删掉。日报还要求每条只有一个来源。
- **日报数字必须有据**。标题或正文出现来源消息里没有的数字，标题无据就丢条，正文无据就回退成原文摘录。
- **投递不确定不重发**。网络超时可能已经发出去了，再发一次就是重复刷屏；这种情况只记日志、等人工确认。
- **定时槽位幂等**。同一时间槽成功后写 `last_run_slot`，重启、轮询抖动都不会重复发；失败最多重试 3 次，间隔 5 分钟。

## 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 必填 | Bot Token |
| `OWNER_ID` | 必填 | Owner 数字 ID |
| `AUTHORIZED_GROUPS` | 空 | 初始授权群，逗号分隔 |
| `DATA_DIR` | `./data` | 数据目录（SQLite + state.json） |
| `MESSAGE_RETENTION_DAYS` | `30` | 消息保留天数，0 = 永久 |
| `MEMBER_MAX_MESSAGES` | `500` | 普通成员条数上限 |
| `MEMBER_MAX_HOURS` | `24` | 普通成员时间上限 |
| `PRIVILEGED_MAX_MESSAGES` | `3000` | 管理员条数上限 |
| `PRIVILEGED_MAX_HOURS` | `168` | 管理员时间上限 |
| `MIN_MESSAGES` | `3` | 少于该条数不生成 |
| `DEFAULT_MESSAGE_LIMIT` | `80` | `/sum` 默认条数 |

端点字段：

| 字段 | 说明 |
|---|---|
| `id` | 节点唯一标识，日志与熔断用 |
| `name` | 显示名称 |
| `url` | 完整 chat completions 地址 |
| `model` | 模型 ID |
| `key_env` | 从环境变量读取密钥（推荐） |
| `key` | 直接写密钥（不推荐） |
| `timeout` | 单节点超时秒数，默认 40 |
| `extra_headers` | 额外请求头（可选） |

## 常见问题

**Bot 在群里完全不说话，也不入库？**
先确认 BotFather 的 privacy mode 已设为 `Disable`，再把 Bot 移出群重新拉一次。然后发 `/id` 看是否显示「已授权」。

**总结里出现「生成失败：全部模型节点均不可用」？**
跑 `/status` 看节点密钥是否齐全。再手动 curl 一次端点，确认 `url` 是完整路径、模型名存在。日志里会打印每个节点的 HTTP 状态码和前 400 字符响应体。

**卡片显示成一大段纯文本？**
你的 Bot API 环境可能不支持原生 rich message。程序会自动降级成 `<blockquote expandable>` HTML，视觉上仍是可折叠的引用块。如果连这个也没有，检查日志里的 `富文本被拒绝` 记录。

**日报条数偏少？**
日报是「宁缺毋滥」策略：来源不明、作者对不上、数字无据的条目会被丢弃。活跃度低的群、广告多的群天然出不了 5 条。可以调大回溯时长，或换信息密度更高的时段。

**能总结入群之前的历史吗？**
不能。Bot 只能看到入群后收到的消息。

**数据存在哪？**
全部在 `data/` 下：`messages.db`（消息）、`state.json`（授权与定时配置）、`llm_endpoints.json`（端点）。备份这三个文件即可迁移。

## 安全

- 代码内不含任何真实 Token、Owner ID、群 ID、私有域名或服务器路径，全部走环境变量。
- 建议 `.env` 权限设为 `600`，且不要提交到 Git。
- 发布前跑一次脱敏扫描：

```bash
python scripts/verify_desensitization.py . --private-file /path/to/private_values.json
```

`private_values.json` 是 0600 权限的字符串数组，填入你自己的 Bot 名、域名、ID 等，脚本会全量比对并拒绝 `.git` 目录。

## 目录结构

```
bot.py                  入口：轮询、handler 注册、指令菜单
config.py               环境变量 + state.json 原子读写
storage.py              SQLite 消息存取
args.py                 /sum /autosum /autodaily 参数解析
summarize.py            取数后的清洗、校验、渲染核心
scheduler.py            槽位判定与定时执行
prompts.py              总结 / 日报提示词
handlers/
  messages.py           群消息采集
  commands.py           指令与面板
services/
  llm.py                多节点兜底、熔断、超时预算
  rich.py               富文本清洗与 HTML 降级
  delivery.py           投递与「不确定不重发」
tests/test_offline.py   离线单测（不联网、不发消息）
```

## 许可

MIT。拿去改，改成适合你自己群的版本就行。
