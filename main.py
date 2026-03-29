#!/usr/bin/env python3
"""
auto_hot_matrix_publish: hot fetch + safety filter + dedupe + DeepSeek + cover + optional Playwright (CDP) publish.

Run from this directory:
  pip install -r requirements.txt
  npm install && npx playwright install chromium
  python3 main.py

See SKILL.md for OpenClaw integration and cron.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# Optional: load .env from skill directory (explicit KEY=VAL lines override the process environment).
def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        # Same as common dotenv: values in .env win over inherited env (e.g. gateway PLAYWRIGHT_HEADLESS=1).
        os.environ[key] = val


_load_env_file()

try:
    import yaml
except ImportError:
    print("Missing PyYAML. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing requests. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


def _skill_dir() -> Path:
    return Path(__file__).resolve().parent


def load_config(name: str = "config.yaml") -> dict[str, Any]:
    path = _skill_dir() / name
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(path: Path, data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_duplicate(title: str, history: list[dict[str, Any]], max_days: int) -> bool:
    now = time.time()
    ttl = max_days * 86400
    for item in history:
        if item.get("title") == title and (now - float(item.get("ts", 0))) < ttl:
            return True
    return False


def log_line(cfg: dict[str, Any], message: str) -> None:
    notify = cfg.get("notify") or {}
    log_file = notify.get("log_file")
    if not log_file:
        return
    p = _skill_dir() / log_file
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def resolve_publish_log_abs_path(cfg: dict[str, Any]) -> str | None:
    notify = cfg.get("notify") or {}
    log_file = notify.get("log_file")
    if not log_file:
        return None
    return str((_skill_dir() / log_file).resolve())


def step_log(cfg: dict[str, Any], message: str) -> None:
    """全流程排查：主进程步骤写入 publish.log。"""
    log_line(cfg, f"step [main] {message}")


def log_subprocess_capture(cfg: dict[str, Any], label: str, text: str) -> None:
    if not text or not text.strip():
        return
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) > 800:
            s = s[:800] + "…"
        log_line(cfg, f"step [main] {label}| {s}")


def notify_webhook(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    url = (cfg.get("notify") or {}).get("webhook_url") or ""
    if not url.strip():
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _ = resp.read()
    except urllib.error.URLError as e:
        log_line(cfg, f"webhook failed: {e}")


def http_get_json(url: str, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "auto-hot-matrix/4.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        return json.loads(raw)
    except urllib.error.URLError as e:
        # Some local environments have incomplete trust stores for urllib HTTPS.
        # Fall back to requests (certifi bundle) for better compatibility.
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "auto-hot-matrix/4.0"})
        resp.raise_for_status()
        return resp.json()


def normalize_hot_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    inner = data.get("data") or data
    if isinstance(inner, dict):
        cards = inner.get("cards") or inner.get("list") or inner.get("items") or []
    elif isinstance(inner, list):
        cards = inner
    else:
        cards = []
    if not isinstance(cards, list):
        return []
    normalized: list[dict[str, Any]] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        d = dict(c)
        # Normalize heterogeneous upstream field names.
        d.setdefault("title", d.get("Title") or d.get("name") or d.get("keyword") or "")
        d.setdefault("desc", d.get("Description") or d.get("desc") or d.get("summary") or "")
        d.setdefault("hot", d.get("HotValue") or d.get("hotValue") or d.get("score") or d.get("hot"))
        d.setdefault("url", d.get("Url") or d.get("url") or d.get("Link") or d.get("link") or "")
        normalized.append(d)
    return normalized


def parse_hot_value(card: dict[str, Any]) -> int:
    raw = (
        card.get("hot")
        or card.get("hotValue")
        or card.get("HotValue")
        or card.get("hotScore")
        or card.get("score")
        or 0
    )
    if isinstance(raw, (int, float)):
        return int(raw)
    if not isinstance(raw, str):
        return 0
    s = raw.strip().lower().replace(",", "")
    m = re.search(r"([\d.]+)\s*万", s)
    if m:
        return int(float(m.group(1)) * 10_000)
    m = re.search(r"([\d.]+)\s*亿", s)
    if m:
        return int(float(m.group(1)) * 100_000_000)
    try:
        return int(float(s))
    except ValueError:
        return 0


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object in model output")
    return json.loads(m.group(0))


def deepseek_generate(cfg: dict[str, Any], prompt: str) -> str:
    ai = cfg.get("ai") or {}
    base = os.environ.get("DEEPSEEK_BASE_URL") or ai.get("base_url") or "https://api.deepseek.com/v1"
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    model = ai.get("model") or "deepseek-chat"
    temperature = float(ai.get("temperature", 0.7))
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _volc_ark_image_api_key() -> str:
    return (
        os.environ.get("VOLC_ARK_API_KEY", "").strip()
        or os.environ.get("ARK_API_KEY", "").strip()
        or os.environ.get("SEEDREAM_API_KEY", "").strip()
        or os.environ.get("VOLCANO_ENGINE_API_KEY", "").strip()
    )


def _normalize_volc_image_post_url(raw: str) -> str:
    """
    Accept full POST URL, or OpenAI-style base ending in /api/v3 (append /images/generations).
    Leaves …/images/generations/tasks unchanged for async task + poll flow.
    """
    u = raw.strip().rstrip("/")
    if not u:
        return "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    low = u.lower()
    if low.endswith("/tasks"):
        return u
    if low.endswith("/images/generations"):
        return u
    if low.endswith("/api/v3"):
        return f"{u}/images/generations"
    return u


def _volc_download_url_to_file(url: str, save_path: Path) -> None:
    ir = requests.get(url, timeout=120)
    ir.raise_for_status()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(ir.content)


def _volc_save_image_item(item: dict[str, Any], save_path: Path) -> bool:
    img_url = item.get("url")
    b64 = item.get("b64_json")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(img_url, str) and img_url.startswith("http"):
        _volc_download_url_to_file(img_url, save_path)
        return True
    if isinstance(b64, str) and b64.strip():
        import base64

        save_path.write_bytes(base64.b64decode(b64))
        return True
    return False


def _try_save_cover_from_volc_body(body: dict[str, Any], save_path: Path) -> bool:
    """Parse OpenAI-style or nested task-result shapes; save first image if found."""
    items = body.get("data")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict) and _volc_save_image_item(first, save_path):
            return True
    for key in ("content", "result", "output"):
        node = body.get(key)
        if not isinstance(node, dict):
            continue
        if _volc_save_image_item(node, save_path):
            return True
        u = node.get("url") or node.get("image_url")
        if isinstance(u, str) and u.startswith("http"):
            _volc_download_url_to_file(u, save_path)
            return True
        nested = node.get("data") or node.get("images")
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            if _volc_save_image_item(nested[0], save_path):
                return True
    u = body.get("url") or body.get("image_url")
    if isinstance(u, str) and u.startswith("http"):
        _volc_download_url_to_file(u, save_path)
        return True
    return False


def _volc_task_id_from_body(body: dict[str, Any]) -> str | None:
    for key in ("id", "task_id"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    inner = body.get("data")
    if isinstance(inner, dict):
        for key in ("id", "task_id"):
            v = inner.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _poll_volc_image_task(
    create_url: str,
    task_id: str,
    headers: dict[str, str],
    save_path: Path,
    *,
    poll_interval: float,
    max_wait: int,
) -> None:
    poll_url = create_url.rstrip("/") + "/" + task_id
    deadline = time.monotonic() + max_wait
    last_status = ""
    while time.monotonic() < deadline:
        gr = requests.get(poll_url, headers=headers, timeout=60)
        gr.raise_for_status()
        body = gr.json()
        if isinstance(body, dict):
            if _try_save_cover_from_volc_body(body, save_path):
                return
            status = str(body.get("status") or body.get("task_status") or "").lower()
            if status:
                last_status = status
            if status in ("failed", "canceled", "cancelled", "error"):
                raise RuntimeError(f"Image task failed (status={status}): {body!r}")
            if status in ("succeeded", "success", "completed"):
                raise RuntimeError(f"Image task done but no downloadable image: {body!r}")
        time.sleep(poll_interval)
    raise RuntimeError(f"Image task poll timeout (last_status={last_status!r})")


def generate_cover(cfg: dict[str, Any], prompt: str, save_path: Path) -> bool:
    """文生图：方舟 OpenAI 兼容同步接口，或创建任务后 GET …/tasks/{id} 轮询（见 config image.api_url）。"""
    key = _volc_ark_image_api_key()
    if not key:
        log_line(
            cfg,
            "VOLC_ARK_API_KEY / ARK_API_KEY / SEEDREAM_API_KEY / VOLCANO_ENGINE_API_KEY 未设置；跳过封面",
        )
        return False
    img_cfg = cfg.get("image") or {}
    url = _normalize_volc_image_post_url(
        str(
            img_cfg.get("api_url")
            or "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        )
    )
    model = (
        str(img_cfg.get("model") or "").strip()
        or os.environ.get("VOLC_ARK_IMAGE_MODEL", "").strip()
    )
    if not model:
        log_line(
            cfg,
            "config image.model 或环境变量 VOLC_ARK_IMAGE_MODEL 未设置；跳过封面",
        )
        return False
    # Seedream/方舟示例常用 1K、2K；部分接入点不支持任意 WxH，易触发非预期 HTTP 状态
    size = img_cfg.get("size") or "2K"
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "size": size, "n": 1}
    extra = img_cfg.get("extra")
    if isinstance(extra, dict):
        payload.update(extra)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=120,
    )
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        snippet = (r.text or "")[:1200].strip()
        extra = f" body={snippet!r}" if snippet else ""
        raise RuntimeError(f"Image API HTTP {r.status_code} url={url!r}{extra}") from e
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected image API response type: {data!r}")
    if _try_save_cover_from_volc_body(data, save_path):
        return True
    task_id = _volc_task_id_from_body(data)
    if task_id:
        poll_max = int(img_cfg.get("poll_max_seconds") or 180)
        interval = float(img_cfg.get("poll_interval_seconds") or 2)
        _poll_volc_image_task(
            url,
            task_id,
            headers,
            save_path,
            poll_interval=interval,
            max_wait=poll_max,
        )
        return True
    raise RuntimeError(f"Unexpected image API response: {data!r}")


def platforms_filtered(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Optional MATRIX_PUBLISH_ONLY=toutiao,zhihu — comma-separated platform `name` values."""
    allp = [p for p in (cfg.get("platforms") or []) if isinstance(p, dict)]
    raw = os.environ.get("MATRIX_PUBLISH_ONLY", "").strip()
    if not raw:
        return allp
    allow = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return [p for p in allp if str(p.get("name") or "").strip().lower() in allow]


def playwright_publish_deps_ok() -> bool:
    skill = _skill_dir()
    if shutil.which("node") is None:
        return False
    if not (skill / "node_modules" / "playwright").is_dir():
        return False
    return True


def resolve_chrome_user_data_dir_for_platform(plat: dict[str, Any]) -> str:
    """
    Playwright 持久化目录：优先环境变量（便于 OpenClaw / cron 与本地终端对齐同一登录态）。

    优先级：MATRIX_<PLATFORM>_CHROME_USER_DATA_DIR（PLATFORM 为 config 里 name 的大写，如 TOUTIAO）
           > MATRIX_CHROME_USER_DATA_DIR（覆盖所有平台）
           > config.yaml 的 chrome_user_data_dir
    """
    pname = str(plat.get("name") or "").strip().upper().replace("-", "_")
    if pname:
        per = os.environ.get(f"MATRIX_{pname}_CHROME_USER_DATA_DIR", "").strip()
        if per:
            return str(Path(per).expanduser())
    global_dir = os.environ.get("MATRIX_CHROME_USER_DATA_DIR", "").strip()
    if global_dir:
        return str(Path(global_dir).expanduser())
    raw = str(plat.get("chrome_user_data_dir") or "").strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser())


def log_browser_publish_context(cfg: dict[str, Any], plat: dict[str, Any], resolved_dir: str) -> None:
    """写入 publish.log：与排查 OpenClaw / profile 对齐相关的环境及最终 profile 路径。"""
    parts = [
        "browser env:",
        f"HOME={os.environ.get('HOME', '')}",
        f"USER={os.environ.get('USER', '')}",
        f"PLAYWRIGHT_HEADLESS={os.environ.get('PLAYWRIGHT_HEADLESS', '')}",
    ]
    chrome_bin = os.environ.get("CHROME_BINARY", "").strip()
    if chrome_bin:
        parts.append(f"CHROME_BINARY={chrome_bin}")
    env_line = " ".join(parts)
    pname = str(plat.get("name") or "unknown")
    profile_line = f"browser profile {pname}: {resolved_dir}"
    print(env_line)
    print(profile_line)
    log_line(cfg, env_line)
    log_line(cfg, profile_line)


def publish_platform_playwright(
    plat: dict[str, Any],
    article: dict[str, Any],
    cover_path: Path | None,
    wait_cfg: dict[str, Any],
    cfg: dict[str, Any],
) -> None:
    """One platform: Node runs publish_playwright.mjs (Playwright CDP, persistent profile)."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 node：请先安装 Node.js 18+")
    script = _skill_dir() / "publish_playwright.mjs"
    if not script.is_file():
        raise RuntimeError(f"缺少 {script}")
    if not playwright_publish_deps_ok():
        raise RuntimeError(
            "未安装 Playwright：请在技能目录执行 npm install && npx playwright install chromium",
        )

    cover_arg: str | None = None
    if cover_path and cover_path.is_file():
        cover_arg = str(cover_path.resolve())

    plat_pub = dict(plat)
    resolved_dir = resolve_chrome_user_data_dir_for_platform(plat)
    if not resolved_dir:
        raise RuntimeError("missing chrome_user_data_dir（请配置 config.yaml 或 MATRIX_*_CHROME_USER_DATA_DIR）")
    plat_pub["chrome_user_data_dir"] = resolved_dir
    cfg_raw = str(plat.get("chrome_user_data_dir") or "").strip()
    cfg_exp = str(Path(cfg_raw).expanduser()) if cfg_raw else ""
    if cfg_exp and resolved_dir != cfg_exp:
        print(f"[{plat.get('name')}] chrome_user_data_dir 使用环境覆盖: {cfg_exp} → {resolved_dir}")
    else:
        print(f"[{plat.get('name')}] chrome_user_data_dir={resolved_dir}")

    log_browser_publish_context(cfg, plat, resolved_dir)

    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "").strip().lower() in ("1", "true", "yes")
    publish_log_abs = resolve_publish_log_abs_path(cfg)
    payload: dict[str, Any] = {
        "platforms": [plat_pub],
        "article": {"title": article.get("title") or "", "body": article.get("body") or ""},
        "coverPath": cover_arg,
        "wait": wait_cfg,
        "headless": headless,
    }
    if publish_log_abs:
        payload["publishLogPath"] = publish_log_abs
    payload_path = _skill_dir() / ".publish_payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    step_log(
        cfg,
        f"spawn node publish_playwright.mjs cwd={_skill_dir()} platform={plat.get('name')} headless={headless}",
    )
    proc = subprocess.run(
        [node, str(script), str(payload_path)],
        cwd=str(_skill_dir()),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    log_subprocess_capture(cfg, "playwright stdout", proc.stdout or "")
    log_subprocess_capture(cfg, "playwright stderr", proc.stderr or "")
    step_log(cfg, f"playwright exit_code={proc.returncode}")
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"playwright publish exit {proc.returncode}")


def resolve_publish_article_json_path() -> Path | None:
    """MATRIX_PUBLISH_ARTICLE_JSON：跳过热搜与 LLM，仅发布 JSON 内 title/body。"""
    # Production default: disable fixed-template article unless explicitly enabled.
    if os.environ.get("AUTO_HOT_DEBUG", "").strip().lower() not in ("1", "true", "yes"):
        if os.environ.get("MATRIX_PUBLISH_ARTICLE_JSON", "").strip():
            print("⚠️ 已忽略 MATRIX_PUBLISH_ARTICLE_JSON（生产模式默认禁用固定模板）。")
        return None
    raw = os.environ.get("MATRIX_PUBLISH_ARTICLE_JSON", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_file():
        return p
    p2 = _skill_dir() / raw
    if p2.is_file():
        return p2
    raise FileNotFoundError(f"MATRIX_PUBLISH_ARTICLE_JSON 文件不存在: {raw}")


def load_publish_article_file(path: Path) -> tuple[dict[str, Any], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("发布用 JSON 须为对象")
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        raise ValueError("JSON 中 title、body 不能为空")
    article: dict[str, Any] = {
        "title": title,
        "body": body,
        "tags": data.get("tags") if isinstance(data.get("tags"), list) else [],
        "cover_prompt": str(data.get("cover_prompt") or "").strip(),
    }
    hist = str(data.get("history_title") or title).strip()
    return article, hist


def resolve_article_topic() -> str | None:
    """MATRIX_ARTICLE_TOPIC 或 --topic：跳过热搜，按主题走 DeepSeek。"""
    raw = os.environ.get("MATRIX_ARTICLE_TOPIC", "").strip()
    return raw or None


def load_debug_hot_list() -> list[dict[str, Any]] | None:
    """
    Offline / CI debugging without hot APIs:
    - AUTO_HOT_MOCK=1  — inject one safe synthetic item
    - AUTO_HOT_FIXTURE=/path/to.json — full API-shaped JSON (see fixtures/debug_hot.json)
    """
    if os.environ.get("AUTO_HOT_DEBUG", "").strip().lower() not in ("1", "true", "yes"):
        if os.environ.get("AUTO_HOT_MOCK", "").strip() or os.environ.get("AUTO_HOT_FIXTURE", "").strip():
            print("⚠️ 已忽略 AUTO_HOT_MOCK/AUTO_HOT_FIXTURE（生产模式默认禁用调试热搜）。")
        return None

    mock = os.environ.get("AUTO_HOT_MOCK", "").strip().lower()
    if mock in ("1", "true", "yes"):
        return [
            {
                "title": "人工智能助力教育公平",
                "desc": "多地探索智慧课堂与个性化学习实践",
                "hot": 999_999_999,
                "source": "mock",
            }
        ]
    fix = os.environ.get("AUTO_HOT_FIXTURE", "").strip()
    if not fix:
        return None
    p = Path(fix).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"AUTO_HOT_FIXTURE not found: {p}")
    raw_data: Any = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw_data, list):
        cards = raw_data
    else:
        cards = normalize_hot_cards(raw_data)
    out: list[dict[str, Any]] = []
    for c in cards:
        if isinstance(c, dict):
            d = dict(c)
            d.setdefault("source", "fixture")
            out.append(d)
    return out


def run() -> str:
    cfg = load_config()
    unique = cfg.get("unique") or {}
    hist_name = unique.get("history_file") or "history.json"
    hist_path = _skill_dir() / hist_name
    history = load_history(hist_path)
    wait_cfg = cfg.get("wait") or {}

    log_line(cfg, "run start")
    step_log(cfg, f"history_file={hist_path}")

    article: dict[str, Any]
    hot_title: str

    article_json_path = resolve_publish_article_json_path()
    if article_json_path is not None:
        print("🔧 MATRIX_PUBLISH_ARTICLE_JSON：跳过热搜与 DeepSeek，仅发布 JSON 内正文")
        step_log(cfg, "branch=article_json (skip hot + LLM)")
        log_line(cfg, f"article-only file: {article_json_path}")
        try:
            article, hot_title = load_publish_article_file(article_json_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            msg = f"❌ 读取发布用 JSON 失败: {e}"
            log_line(cfg, msg)
            notify_webhook(cfg, {"ok": False, "error": str(e)})
            return msg
        print(f"已加载待发布文章：{article.get('title', '')[:60]}…")
        step_log(cfg, f"article loaded title={str(article.get('title', ''))[:120]}")
    elif (topic := resolve_article_topic()) is not None:
        print(f"主题模式：跳过热搜，主题={topic[:80]}{'…' if len(topic) > 80 else ''}")
        step_log(cfg, "branch=topic (skip hot)")
        log_line(cfg, f"topic mode: {topic[:200]}")
        safety = cfg.get("safety") or {}
        forbidden = [x.lower() for x in (safety.get("forbidden_keywords") or [])]
        tl = topic.lower()
        if any(kw in tl for kw in forbidden):
            msg = "主题命中安全词列表，已中止"
            log_line(cfg, msg)
            notify_webhook(cfg, {"ok": False, "error": msg})
            return f"❌ {msg}"
        max_days = int(unique.get("max_days") or 7)
        if is_duplicate(topic, history, max_days):
            msg = "该主题在近期 history 中已处理过，已跳过发布"
            log_line(cfg, msg)
            notify_webhook(cfg, {"ok": False, "skipped": True, "reason": msg})
            return f"⚠️ {msg}"
        hot_title = topic
        hot_content = f"主题：{topic}"
        ai_cfg = cfg.get("ai") or {}
        prompt_tpl = ai_cfg.get("prompt") or ""
        prompt = prompt_tpl.replace("{hot_content}", hot_content)
        print("DeepSeek 创作中...")
        step_log(cfg, f"deepseek request model={ai_cfg.get('model') or 'deepseek-chat'}")
        raw = deepseek_generate(cfg, prompt)
        article = extract_json_object(raw)
        step_log(cfg, f"deepseek done article_title={str(article.get('title', ''))[:120]}")
    else:
        print("正在获取多平台热搜...")
        step_log(cfg, "branch=hot_sources")
        all_hots: list[dict[str, Any]] = []
        debug_list = load_debug_hot_list()
        if debug_list is not None:
            print("🔧 调试模式：已跳过线上热搜 API（AUTO_HOT_MOCK / AUTO_HOT_FIXTURE）")
            log_line(cfg, "debug hot list (mock/fixture)")
            step_log(cfg, f"hot list source=mock/fixture count={len(debug_list)}")
            all_hots = debug_list
        else:
            for src in cfg.get("hot_sources") or []:
                name = src.get("name") or "unknown"
                url = src.get("url") or ""
                try:
                    data = http_get_json(url)
                    cards = normalize_hot_cards(data)
                    for c in cards:
                        if isinstance(c, dict):
                            c = dict(c)
                            c["source"] = name
                            all_hots.append(c)
                    step_log(cfg, f"hot fetch ok name={name} cards={len(cards)} url={url[:80]}")
                except Exception as e:
                    print(f"⚠️ {name} 抓取失败: {e}")
                    log_line(cfg, f"hot source {name} failed: {e}")
                    step_log(cfg, f"hot fetch fail name={name}: {e!s}"[:480])

        if not all_hots:
            msg = "未获取到任何热点"
            log_line(cfg, msg)
            notify_webhook(cfg, {"ok": False, "error": msg})
            return f"❌ {msg}"

        safety = cfg.get("safety") or {}
        forbidden = [x.lower() for x in (safety.get("forbidden_keywords") or [])]
        min_hot = int(safety.get("min_hot_score") or 0)
        max_days = int(unique.get("max_days") or 7)

        sorted_hots = sorted(all_hots, key=parse_hot_value, reverse=True)
        selected = None
        for h in sorted_hots:
            if not isinstance(h, dict):
                continue
            title = str(h.get("title") or "").strip()
            desc = str(h.get("desc") or h.get("description") or "")
            hot_val = parse_hot_value(h)
            text = (title + " " + desc).lower()
            if any(kw in text for kw in forbidden):
                continue
            if hot_val < min_hot:
                continue
            if is_duplicate(title, history, max_days):
                continue
            selected = h
            break

        if not selected:
            msg = "今日无合适、安全、不重复的热点，已跳过发布"
            log_line(cfg, msg)
            notify_webhook(cfg, {"ok": False, "skipped": True, "reason": msg})
            return f"⚠️ {msg}"

        hot_title = str(selected.get("title") or "")
        hot_content = f"标题：{hot_title}\n描述：{selected.get('desc', '')}"
        print(f"选定安全热点：{hot_title}")
        log_line(cfg, f"selected hot: {hot_title}")
        step_log(cfg, f"hot selected source={selected.get('source')} score={parse_hot_value(selected)}")

        ai_cfg = cfg.get("ai") or {}
        prompt_tpl = ai_cfg.get("prompt") or ""
        # Do not use str.format: prompt JSON example contains { ... } braces.
        prompt = prompt_tpl.replace("{hot_content}", hot_content)
        print("DeepSeek 创作中...")
        step_log(cfg, f"deepseek request model={ai_cfg.get('model') or 'deepseek-chat'}")
        raw = deepseek_generate(cfg, prompt)
        article = extract_json_object(raw)
        step_log(cfg, f"deepseek done article_title={str(article.get('title', ''))[:120]}")

    cover_path: Path | None = None
    img_cfg = cfg.get("image") or {}
    rel_img = img_cfg.get("save_path") or "./cover_hot.jpg"
    cover_path = _skill_dir() / Path(rel_img).name
    step_log(
        cfg,
        f"cover generate prompt_len={len(str(article.get('cover_prompt') or article.get('title') or ''))} path={cover_path.name}",
    )
    try:
        if generate_cover(
            cfg,
            str(article.get("cover_prompt") or article.get("title") or ""),
            cover_path,
        ):
            print(f"封面已保存: {cover_path}")
            step_log(cfg, f"cover saved ok path={cover_path}")
        else:
            step_log(cfg, "cover skipped (no api key or API returned empty)")
    except Exception as e:
        print(f"⚠️ 封面生成失败（将继续尝试发文）: {e}")
        log_line(cfg, f"cover error: {e}")
        step_log(cfg, f"cover exception: {e!s}"[:500])
        cover_path = None

    results: list[dict[str, Any]] = []
    enable_browser = os.environ.get("ENABLE_BROWSER_PUBLISH", "1").strip() not in ("0", "false", "no")
    if enable_browser and not playwright_publish_deps_ok():
        enable_browser = False
        step_log(cfg, "browser publish disabled: missing node or node_modules/playwright")
        print(
            "⚠️ 未检测到 Node + Playwright（npm install）。跳过浏览器发布。"
            " 参见 SKILL.md：npm install && npx playwright install chromium",
        )
    else:
        step_log(cfg, f"browser publish enabled={enable_browser} deps_ok={playwright_publish_deps_ok()}")

    to_publish = platforms_filtered(cfg)
    step_log(
        cfg,
        f"platforms to_publish={[p.get('name') for p in to_publish]} MATRIX_PUBLISH_ONLY={os.environ.get('MATRIX_PUBLISH_ONLY', '')!r}",
    )
    if os.environ.get("MATRIX_PUBLISH_ONLY", "").strip():
        print(f"MATRIX_PUBLISH_ONLY：仅发布 {[p.get('name') for p in to_publish]}")

    if enable_browser:
        for plat in to_publish:
            pname = plat.get("name") or "unknown"
            print(f"\n发布到 {pname}（Playwright / CDP 持久化配置）...")
            step_log(cfg, f"publish loop start platform={pname} retries={int(plat.get('retry') or 0)}")
            ok = False
            retries = int(plat.get("retry") or 0)
            for attempt in range(retries + 1):
                try:
                    step_log(cfg, f"publish attempt {attempt + 1}/{retries + 1} platform={pname}")
                    publish_platform_playwright(plat, article, cover_path, wait_cfg, cfg)
                    ok = True
                    print(f"✅ {pname} 发布流程已执行（请人工核对平台是否成功）")
                    log_line(cfg, f"publish {pname} playwright ok")
                    break
                except Exception as e:
                    print(f"❌ {pname} 第 {attempt + 1} 次失败: {e}")
                    log_line(cfg, f"publish {pname} attempt {attempt + 1}: {e}")
                    time.sleep(2)
            results.append({"platform": pname, "success": ok})
            step_log(cfg, f"publish loop end platform={pname} success={ok}")
    else:
        print("ENABLE_BROWSER_PUBLISH=0 或未安装 Node/Playwright：跳过浏览器发布")
        step_log(cfg, "skip all browser publish (disabled or no deps)")
        for plat in to_publish:
            results.append({"platform": plat.get("name"), "success": False, "skipped": True})

    history.append(
        {
            "title": hot_title,
            "ts": time.time(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "article_title": article.get("title"),
            "results": results,
        }
    )
    save_history(hist_path, history)
    step_log(cfg, f"history saved entries={len(history)} last_title={hot_title[:80]!r}")

    notify_webhook(
        cfg,
        {
            "ok": True,
            "hot_title": hot_title,
            "article_title": article.get("title"),
            "results": results,
        },
    )
    log_line(cfg, "run complete")
    return "\n🎉 今日热点自动发布流程已完成（请核对各平台实际发布状态）。"


def _normalize_cli_argv_topic_flag(argv: list[str]) -> None:
    """输入法或复制粘贴可能把 `--topic` 打成 `-—topic`（中间为 Unicode 长横线），argparse 无法识别。将常见横线类字符统一为 ASCII `-`。"""
    dash_like = frozenset("-−－‒–—―\u2212\uff0d")
    for i, tok in enumerate(argv):
        if not tok or tok[0] not in dash_like:
            continue
        j = 0
        while j < len(tok) and tok[j] in dash_like:
            j += 1
        body = tok[j:]
        if body == "topic" or body.startswith("topic="):
            argv[i] = "-" * min(2, j) + body if j >= 2 else "--" + body


if __name__ == "__main__":
    import argparse

    _normalize_cli_argv_topic_flag(sys.argv)

    _ap = argparse.ArgumentParser(description="auto_hot_matrix_publish")
    _ap.add_argument(
        "-t",
        "--topic",
        default=None,
        help="创作主题（非空时跳过热搜，等价于设置 MATRIX_ARTICLE_TOPIC）",
    )
    _args, _unknown = _ap.parse_known_args()
    if _args.topic and str(_args.topic).strip():
        os.environ["MATRIX_ARTICLE_TOPIC"] = str(_args.topic).strip()

    try:
        out = run()
        print(out)
        if isinstance(out, str) and out.startswith("❌"):
            sys.exit(1)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        try:
            cfg = load_config()
            log_line(cfg, f"fatal: {e}")
            notify_webhook(cfg, {"ok": False, "error": str(e)})
        except Exception:
            pass
        sys.exit(1)
