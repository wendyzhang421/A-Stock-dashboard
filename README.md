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
