# A 股实时条件推送

这个程序会轮询 A 股实时行情，并推送以下两类股票：

1. 日内上涨超过 5%
2. 日成交额超过 1 亿人民币

> 说明：你提到的“交易量大于 1 亿人民币”在金额口径上通常对应 **成交额**，程序按成交额做筛选。
> 数据源使用东方财富实时行情接口（无需 `akshare`）。

## 1) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) 运行

只跑一次（测试）

```bash
python astock_alert.py --once
```

持续监控（默认每 60 秒轮询）

```bash
python astock_alert.py
```

一键启动（推荐）：

```bash
cp .env.local.example .env.local
# 编辑 .env.local 填入 TELEGRAM_BOT_TOKEN
./start_astock.sh
```

## 3) 可选：企业微信机器人推送

设置 webhook：

```bash
export WECOM_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx'
```

然后运行：

```bash
python astock_alert.py
```

## 4) 可选：Telegram 机器人推送

设置环境变量：

```bash
export TELEGRAM_BOT_TOKEN='你的 bot token'
export TELEGRAM_CHAT_ID='你的 chat id'
```

然后运行：

```bash
python astock_alert.py
```

如果你用的是群组话题（Topics），再加：

```bash
export TELEGRAM_THREAD_ID='话题ID'
```

## 5) 常用参数

```bash
python astock_alert.py \
  --min-pct 5 \
  --min-amount 100000000 \
  --interval 60 \
  --max-push 30 \
  --telegram-token 'xxx' \
  --telegram-chat-id 'xxx'
```

- `--once`：只执行一次
- `--ignore-trading-hours`：忽略交易时段限制
- `--dry-run`：只打印，不发 webhook
- `--telegram-thread-id`：Telegram 话题 ID（可选）

## 6) 去重推送机制

- 同一天内，同一只股票、同一条规则只推送一次
- 程序状态保存在 `.alert_state.json`

## 7) 你当前环境的一键命令

```bash
cd /Users/wendy/Documents/GitHub/AStock
./start_astock.sh
```

只跑一次：

```bash
./start_astock.sh --once --ignore-trading-hours
```

## 8) 读取你发给 Telegram Bot 的消息

> 用的是 Telegram `getUpdates`，可以把你发给 bot 的入站消息拉下来查看。

首次初始化（跳过历史消息，只看之后的新消息）：

```bash
export TELEGRAM_BOT_TOKEN='你的 bot token'
python telegram_read_updates.py --skip-history
```

读取一次新消息：

```bash
python telegram_read_updates.py
```

持续监听新消息：

```bash
python telegram_read_updates.py --follow
```

说明：

- 偏移量保存在 `.telegram_updates_offset`，默认只会读取“新消息”
- 若你在群里测试，需确保 bot 能收到消息（例如关闭隐私模式或通过命令触发）

## 9) Telegram 自动回复（接 OpenAI）

在 `.env.local` 里再补一个：

```bash
OPENAI_API_KEY='你的 OpenAI API Key'
# 可选，默认 gpt-5
OPENAI_MODEL='gpt-5'
```

首次启动，跳过旧消息：

```bash
cd /Users/wendy/Documents/GitHub/AStock
.venv/bin/python tg_openai_bot.py --skip-history
```

持续监听并自动回复：

```bash
cd /Users/wendy/Documents/GitHub/AStock
set -a
source .env.local
set +a
.venv/bin/python tg_openai_bot.py --follow
```

说明：

- 机器人默认只回复 `TELEGRAM_CHAT_ID` 对应的聊天，避免误回其他群或私聊
- 支持 `/start`、`/help`、`/reset`
- 对话上下文保存在 `.telegram_bot_memory.json`
- update offset 保存在 `.telegram_bot_offset`

## 10) A 股看板云端刷新

这套看板现在可以直接放到 GitHub Actions 上跑，电脑关机也能自动刷新。

适用场景：

- 每个交易日下午自动重建 `reports/*.html` 和 `reports/*.json`
- 自动把最新页面发布到 GitHub Pages
- `iFinD` 历史接口失败时，自动回退到公用行情源

### 本地已经准备好的入口

云端入口脚本是：

```bash
python scripts/cloud_refresh_dashboard.py
```

它会：

- 优先跑 `scripts/refresh_ifind_20260512.py`
- 失败后回退到 `scripts/refresh_public_as_of.py`
- 尝试用 `iFinD` 实时行情把当天数据补齐
- 重写 `reports/share_dashboard/index.html`

### GitHub 仓库需要准备的内容

1. 把当前目录推到一个 GitHub 仓库
2. 在仓库里打开 `Settings -> Secrets and variables -> Actions`
3. 新增一个仓库 Secret：

```bash
ASTOCK_IFIND_REFRESH_TOKEN
```

值填你当前可用的 `iFinD refresh token`。

### 启用 GitHub Pages

1. 打开仓库 `Settings -> Pages`
2. `Build and deployment` 选择 `GitHub Actions`

工作流文件已经放在：

[`/Users/wendy/Documents/GitHub/AStock/.github/workflows/refresh-dashboard.yml`](/Users/wendy/Documents/GitHub/AStock/.github/workflows/refresh-dashboard.yml)

默认行为：

- 每个工作日北京时间 `16:10` 自动刷新一次
- 也支持在 GitHub Actions 页面手动触发

### 手动指定日期刷新

在 GitHub Actions 里手动运行 `Refresh A-Share Dashboard` 时，可以填写：

```bash
as_of=2026-05-27
```

不填就默认用运行当天的北京时间日期。

### 发布后的访问方式

工作流会把 `reports/` 作为 Pages 产物发布。

在线入口会是：

```text
https://<你的 GitHub 用户名>.github.io/<仓库名>/share_dashboard/
```

### 注意事项

- GitHub Actions 的定时触发按 `UTC` 运行，存在几分钟级延迟是正常的
- `iFinD token` 过期后，需要去 GitHub Secrets 里手动更新
- 如果 `iFinD` 历史接口额度耗尽，工作流会自动退回公用数据源，但当天某些字段可能没有完整的 `iFinD` 富数据

## 11) 网页端自选/投研报告云端持久化

`stockkiller.xyz` 当前主站仍是静态页面。要让网页端修改的：

- 自选移除状态
- 强势股“加入自选”状态
- 投研报告录入内容

真正跨设备同步，需要一个很小的写回 API。仓库里已经准备好了：

- API 文件：`api/dashboard-state.js`
- 状态文件：
  - `reports/watchlist_state.json`
  - `reports/research_reports.json`

### 推荐部署方式

把当前仓库部署一份到 Vercel，仅用于提供 API。

建议绑定子域名：

```text
api.stockkiller.xyz
```

指向这个 Vercel 项目。

### Vercel 需要配置的环境变量

```bash
GITHUB_TOKEN=你的 GitHub PAT（需要仓库写权限）
GITHUB_REPO=wendyzhang421/A-Stock-dashboard
GITHUB_BRANCH=main
DASHBOARD_ADMIN_TOKEN=你自己设置的一串管理口令
LLM_PROVIDER=openai
LLM_MODEL=gpt-5-mini
OPENAI_API_KEY=你的 OpenAI API Key
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
XAI_API_KEY=你的 xAI / Grok API Key
XAI_MODEL=grok-3-mini
```

说明：

- `GITHUB_TOKEN` 用来把网页端修改写回仓库 JSON
- `DASHBOARD_ADMIN_TOKEN` 用来保护写接口，避免任何访客都能改你的自选/报告
- `LLM_PROVIDER` 支持 `openai` 或 `deepseek`
- `LLM_MODEL` 是当前提取模型名，例如 `gpt-5-mini` 或 `deepseek-v4-flash`
- `OPENAI_API_KEY` 用于 `LLM_PROVIDER=openai`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 用于 `LLM_PROVIDER=deepseek`
- `XAI_API_KEY` / `XAI_MODEL` 用于社交媒体观点提取接口

### 前端如何工作

页面会先立即写本地浏览器缓存，再后台尝试同步到 API：

- 成功：仓库 JSON 更新，其他设备打开网页也能看到
- 失败：当前浏览器本地状态仍保留，不会阻塞操作

如果网页首次写回时没有本地保存过管理口令，页面会弹出一次输入框，要求输入：

```text
Dashboard admin token
```

输入一次后会缓存在当前浏览器。

### 本地调试

可以先在本地运行：

```bash
set -a
source .env.local
set +a
npx vercel dev
```

默认接口路径：

```text
/api/dashboard-state
```
