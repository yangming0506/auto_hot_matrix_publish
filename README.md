# toutiao_hot_auto_publish（今日头条热点自动发布）

多源热搜抓取 → 安全过滤与去重 → **DeepSeek** 成文 → **火山方舟** 封面图 → **Playwright（CDP）** 发文（**主推今日头条** `mp.toutiao.com`，`config.yaml` 可扩展其他平台）。可作为独立 Python 流水线运行，也可作为 **OpenClaw Agent Skill**（见 `SKILL.md`，技能 id：`toutiao_hot_auto_publish`）。

## 功能概览

- 可配置热点源（`config.yaml` → `hot_sources`）
- 敏感词、最低热度、`history.json` 时间窗去重
- OpenAI 兼容接口调用 DeepSeek 生成标题、正文、标签与封面提示词
- 火山方舟图片生成 API 产出封面
- 按平台隔离 `chrome_user_data_dir`，持久化登录态；支持登录态探测与失败截图

## 合规与风险

自动化发帖可能违反各平台服务条款；热点接口与页面 DOM 会变更。**使用本工具产生的封号、内容与法律风险由使用者自行承担**；发布后请人工核对是否真正成功。

## 环境要求

- Python 3（建议虚拟环境）
- Node.js（用于 `publish_playwright.mjs` 与 Playwright）
- Google Chrome（推荐，脚本默认 `channel: "chrome"`；也可按脚本注释改用 Playwright Chromium）

## 快速开始

在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm install
npx playwright install chromium
cp .env.example .env
```

编辑 `.env`：至少配置 `DEEPSEEK_API_KEY`；若 `config.yaml` 中 `image.model` 为空，还需配置封面相关密钥与 `VOLC_ARK_IMAGE_MODEL`（详见 `.env.example` 注释）。

运行：

```bash
python main.py
```

常用环境变量（完整列表见 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `ENABLE_BROWSER_PUBLISH=0` | 只跑热搜 + LLM + 封面，不打开浏览器发文 |
| `MATRIX_PUBLISH_ONLY=toutiao` | 仅发布单个平台（名称与 `config.yaml` 中 `platforms[].name` 一致） |
| `MATRIX_ARTICLE_TOPIC=...` 或 `python main.py --topic "..."` | 跳过热搜，按主题成文 |
| `PLAYWRIGHT_HEADLESS=1` | 无头浏览器 |

## 配置说明

| 文件 | 作用 |
|------|------|
| `config.yaml` | 热点源、模型、安全策略、去重、封面 API、各平台 URL 与选择器 |
| `.env` | API 密钥与运行开关（**勿提交仓库**） |
| `history.json` | 自动生成，用于去重 |
| `publish.log` | 运行日志 |

## 与 OpenClaw 集成

定时任务、自然语言触发、浏览器登录目录与故障排查等，以 **`SKILL.md`** 为准。

## 许可证

若仓库根目录未包含 `LICENSE` 文件，使用前请自行确认是否适合你的场景。
