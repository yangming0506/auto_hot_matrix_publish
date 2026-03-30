---
name: toutiao_hot_auto_publish
description: 今日头条热点自动发文：热搜抓取、安全过滤、去重、DeepSeek 成文、封面图、Playwright（CDP）发文与日志/Webhook；可按配置扩展其他平台。
metadata:
  openclaw:
    emoji: "🔥"
    requires:
      bins: ["python3"]
---

# 今日头条热点自动发布

本技能为 **OpenClaw Agent Skills** 形态：通过 `SKILL.md` 被智能体加载；**可执行流水线**在同目录的 `main.py`（标准 Python，不依赖虚构的 `clawskill` 包）。

## 能力概览

- 多热点源 HTTP 拉取（配置见 `config.yaml` 的 `hot_sources`）
- 敏感词与最低热度过滤、`history.json` 去重（默认 7 天）
- DeepSeek（OpenAI 兼容 Chat Completions）生成标题/正文/标签/封面提示词
- **火山方舟** OpenAI 兼容 `POST .../images/generations` 生成封面（密钥：`VOLC_ARK_API_KEY` 或 `ARK_API_KEY`；兼容旧名 `SEEDREAM_API_KEY`。模型/接入点：`config.yaml` 的 `image.model` 或 `VOLC_ARK_IMAGE_MODEL`）
- 可选 **Playwright `chromium.launchPersistentContext`（CDP，无 WebDriver）**：按 `chrome_user_data_dir` 隔离登录态并填表发文（与 TipKay 类「CDP + Puppeteer/Playwright」同层思路；本仓库用 Node 脚本 `publish_playwright.mjs` 封装）
- 内置登录态探测：每个平台可配置 `login_check.must_be_visible` / `must_be_hidden`，未登录自动截图并跳过该平台
- 本地 `publish.log` 追加日志；可选 `notify.webhook_url` POST JSON 摘要

## 合规与风险（必读）

- 自动化发帖可能违反各平台服务条款；**使用者自行承担封号与法律风险**。
- 热点 API、页面结构会变更；**CSS/XPath 需随平台改版维护**，不存在永久“零维护”。
- `main.py` 在浏览器侧执行点击/填表后，**务必人工核对平台是否真正发布成功**。

## 技术栈（页面自动化，对齐 CDP 方案）

| 层级   | 本 skill 实现                                                                                                                                                                |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 底层   | **Chrome DevTools Protocol**：由 Playwright 通过 `chromium.launchPersistentContext` 连接，不经过 Selenium WebDriver / ChromeDriver                                           |
| 中间层 | **Playwright（Node）**：`publish_playwright.mjs` 负责导航、填表、上传、点击                                                                                                  |
| 上层   | **Python `main.py`** 编排热搜 → LLM → 封面，再 `subprocess` 调用 Node；可选后续由你在 **MCP** 中封装「头条发布」等工具，由 Agent 调 MCP，本脚本仍可作为无 MCP 时的本地批处理 |

**为何不用 Selenium**：WebDriver 需驱动、启动更重；Playwright 默认走 CDP，与本机 Chrome 持久化目录配合更简单。

## 一次性安装

在技能目录执行（建议使用虚拟环境，避免 PEP 668 限制）：

```bash
cd skills/toutiao_hot_auto_publish
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
npx playwright install chromium
cp .env.example .env
# 编辑 .env：DEEPSEEK_API_KEY；封面需 VOLC_ARK_API_KEY（或 ARK_API_KEY）与 VOLC_ARK_IMAGE_MODEL（若 config 未填 image.model）
```

macOS 推荐安装 **Google Chrome**；Playwright 使用 `channel: "chrome"` 时走系统 Chrome。若只用 Playwright 自带 Chromium，可改 `publish_playwright.mjs` 中的启动参数（见脚本内注释）。可选 `CHROME_BINARY` 指定浏览器路径。

## 浏览器登录（每个平台一次）

为 `config.yaml` 里每个 `chrome_user_data_dir` 创建目录后，用 **同一 user-data-dir** 启动 Chrome 手动登录对应平台（Cookie 与 `openclaw browser` 的 profile 是**不同**目录，除非你把路径配成一致）。若 OpenClaw 与终端使用的目录不一致，可在 `.env` 或环境中设置 **`MATRIX_TOUTIAO_CHROME_USER_DATA_DIR`**（或全局 **`MATRIX_CHROME_USER_DATA_DIR`**）覆盖 `config.yaml`，与手动登录所用路径保持一致。

示例（手动）：

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="$HOME/.openclaw/browser-profiles/toutiao"
```

然后打开头条/知乎/小红书创作者后台并完成登录与风控验证。头条图文发布页为 `https://mp.toutiao.com/profile_v4/graphic/publish`（与 `config.yaml` 中 `platforms[].url` 一致）。

### 登录态探测（推荐开启）

`config.yaml` 每个平台支持：

- `login_check.must_be_visible`：应看到的元素（`|` 分隔多个候选）
- `login_check.must_be_hidden`：不应看到的元素（可选；页脚/导航常含「登录」文案，易误判，头条等平台可只配 `must_be_visible`）

如果检测失败，`publish_playwright.mjs` 会：

- 抛出明确错误（提示先手动登录对应 `chrome_user_data_dir`）
- 在技能目录下 `login-failures/` 保存页面截图，便于你调选择器

任意一步失败（填标题、正文、上传、点击发布等）也会在同目录保存 `*-error-*.png`。无界面环境可加 `PLAYWRIGHT_HEADLESS=1` 再运行 `publish_playwright.mjs`。由 OpenClaw 触发时若进程已带 `PLAYWRIGHT_HEADLESS=1`，仅注释 `.env` 无法取消，须在 `.env` 写 **`PLAYWRIGHT_HEADLESS=0`**（`main.py` 会用工目录 `.env` 覆盖进程环境）。

## 运行

```bash
cd skills/toutiao_hot_auto_publish
source .venv/bin/activate
.venv/bin/python main.py
```

仅发单个平台（例如只要头条）：

```bash
MATRIX_PUBLISH_ONLY=toutiao .venv/bin/python main.py
```

**跳过热搜，按指定主题成文并发布**：设置 `MATRIX_ARTICLE_TOPIC`，或 `.venv/bin/python main.py --topic "…"`（二者等价，CLI 优先写入环境变量）。优先级：`MATRIX_PUBLISH_ARTICLE_JSON`（调试）→ 主题模式 → 热搜模式。

**跳过热搜与 DeepSeek，只测发文**（正文来自 JSON）：

```bash
MATRIX_PUBLISH_ARTICLE_JSON=fixtures/publish_only_article.json MATRIX_PUBLISH_ONLY=toutiao .venv/bin/python main.py
```

JSON 字段：`title`、`body` 必填；可选 `tags`、`cover_prompt`、`history_title`（写入 `history.json` 的去重键，默认用 `title`）。

仅拉取与成文、**不打开发布**（调试）：

```bash
ENABLE_BROWSER_PUBLISH=0 .venv/bin/python main.py
```

**不调线上热搜 API**（离线 / 沙箱 / 接口故障时测后面链路）：

```bash
AUTO_HOT_MOCK=1 ENABLE_BROWSER_PUBLISH=0 .venv/bin/python main.py
```

或使用仓库内示例 fixture：

```bash
AUTO_HOT_FIXTURE="$(pwd)/fixtures/debug_hot.json" ENABLE_BROWSER_PUBLISH=0 .venv/bin/python main.py
```

## OpenClaw 中如何触发

用户或定时任务可用自然语言触发意图，例如：

- 「今日头条热点自动发文」
- 「运行今日头条热点发布」
- 「按主题写文章发今日头条」等（Agent 从用户话中提取主题后设置 `MATRIX_ARTICLE_TOPIC` 或传 `--topic`）

智能体应：**读取本 `SKILL.md`**，并 **`exec` 运行** 本技能目录内的 `main.py`（仓库克隆目录名可能为 `auto_hot_matrix_publish` 或已改名为 `toutiao_hot_auto_publish`；复制到 OpenClaw 时建议目录名与技能 id 一致：`~/.openclaw/skills/toutiao_hot_auto_publish/main.py`），而不是调用不存在的 `openclaw skill run`。

重新加载技能后对新会话生效：

```bash
openclaw skills list
# 或重启 gateway / 新开会话
```

## 定时（每日 9:00，上海时区）

使用 **真实存在的** `openclaw cron`（没有 `openclaw skill reload` 子命令）。示例：

```bash
openclaw cron add \
  --name "今日头条热点自动发布" \
  --cron "0 9 * * *" \
  --tz "Asia/Shanghai" \
  --session isolated \
  --message "执行技能 toutiao_hot_auto_publish：在技能目录运行 .venv/bin/python main.py（先 source 虚拟环境若需要），完成后汇报结果。"
```

请把 `message` 改成与你本机路径、venv 一致的可执行说明。

## 配置说明

| 文件           | 作用                                                                                                |
| -------------- | --------------------------------------------------------------------------------------------------- |
| `config.yaml`  | 热点源、模型、安全词、去重、封面 API、各平台 URL 与选择器                                           |
| `.env`         | `DEEPSEEK_API_KEY`、`VOLC_ARK_API_KEY` / `ARK_API_KEY`（封面）、`VOLC_ARK_IMAGE_MODEL` 等（勿提交） |
| `history.json` | 自动生成，用于去重                                                                                  |
| `publish.log`  | 运行日志                                                                                            |
| `skill.json`   | 人类可读的元数据与触发意图（**不参与** OpenClaw 技能解析）                                          |

## 故障排查

- **PEP 668 / externally-managed-environment**：请使用 `python3 -m venv` 虚拟环境安装依赖。
- **跳过浏览器发布**：确认已安装 **Node.js**、`npm install` 且存在 `node_modules/playwright`；并执行 `npx playwright install chromium`（或依赖系统 Chrome + `channel: chrome`）。
- **热搜为空**：第三方接口变更或限流；可换源或降低 `min_hot_score`。
- **发文失败**：更新 `selectors` / `publish_btn_xpath`；平台常改 DOM。
- **封面失败**：核对 [方舟 Base URL 与鉴权](https://www.volcengine.com/docs/82379/1298459)、[图片生成 API](https://www.volcengine.com/docs/82379/1541523)；`image.model` / `VOLC_ARK_IMAGE_MODEL` 是否为有效接入点；响应是否含 `data[].url` 或 `b64_json`。

## 相关文档

- [Skills](/tools/skills)
- [Creating Skills](/tools/creating-skills)
- [`openclaw browser`](/cli/browser)
- [`openclaw cron`](/cli/cron)
