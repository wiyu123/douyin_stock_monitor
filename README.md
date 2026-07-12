# 抖音博主"模型先生"视频监控与股票推荐邮件系统

自动抓取抖音博主**模型先生**的最新视频，用 Claude API 概括视频要点，识别提及的板块和个股，通过阿里云邮件推送给订阅者。

## 功能

1. **视频抓取**：用 Playwright 自动化浏览器，搜索并获取"模型先生"最新视频
2. **AI 分析**：Claude API 概括视频要点，识别板块并推荐核心标的，提取具体个股
3. **邮件推送**：通过阿里云邮件推送（SMTP），发送格式精美的 HTML 邮件
4. **去重管理**：SQLite 记录已处理视频，避免重复发送
5. **收件人管理**：CSV 文件管理，支持失效日期，自动过滤过期+去重

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

编辑 `config.yaml`：

```yaml
llm:
  api_key: "sk-ant-api03-xxxxxxxx"  # Anthropic API Key

smtp:
  username: "your@email.com"          # 阿里云发信地址
  password: "your-smtp-password"      # SMTP 密码
```

### 3. 管理收件人

编辑 `emails.csv`：

```csv
email,expire_date
user1@qq.com,2027-12-31
user2@qq.com,2026-08-09
```

- `expire_date` 为失效日期 (YYYY-MM-DD)，只发送给当前日期在失效日期之前的收件人
- 自动按邮箱去重

### 4. 运行

```bash
python main.py
```

## 定时运行

### Windows 任务计划程序

```powershell
# 每天 9:00 和 15:00 运行
schtasks /create /tn "DouyinStockMonitor" /tr "python D:\MYCODE\douyin_stock_monitor\main.py" /sc daily /st 09:00
```

### Linux cron

```bash
# 每个交易日 9:00 和 15:00 运行
0 9,15 * * 1-5 cd /path/to/douyin_stock_monitor && python main.py
```

## 项目结构

```
douyin_stock_monitor/
├── main.py              # 主入口
├── config.yaml          # 配置文件
├── douyin_scraper.py    # 抖音视频抓取（Playwright）
├── llm_summarizer.py    # Claude API 概括与标的识别
├── mailer.py            # 阿里云邮件发送
├── tracker.py           # 视频去重（SQLite）
├── emails.csv           # 收件人列表
├── requirements.txt     # 依赖
└── README.md
```

## 邮件内容示例

每封邮件包含：
- 📊 板块分析与推荐标的（含股票代码和名称）
- 🎯 视频中提及的具体个股
- 📝 AI 要点概括
- ▶️ 视频链接（一键跳转抖音）

## 故障排查

1. **抓取不到视频**：设置 `douyin.headless: false` 观察浏览器运行情况
2. **API 调用失败**：检查 `llm.api_key` 是否正确，网络是否能访问 `api.anthropic.com`
3. **邮件发送失败**：检查 SMTP 配置，阿里云邮件推送需要专用的 SMTP 密码（不是账号密码）
4. **反爬问题**：抖音页面结构可能变化，如抓取失败可能是选择器需要更新
