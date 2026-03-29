#!/usr/bin/env python3
"""
每日 AI 简报 — 手动触发版
用法: python ribao.py
输出: output/ribao_YYYY-MM-DD.html
"""

import anthropic
import feedparser
import io
import json
import os
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
# 屏蔽 requests 的 InsecureRequestWarning（SSL 证书问题在本机环境下常见）
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ── API Key 加载 ──────────────────────────────
# 优先级：环境变量 > 项目根目录 .env 文件
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not os.environ.get(key):
            os.environ[key] = val

# 代理设置（从 .env 的 PROXY 字段读取）
_proxy = os.environ.get("PROXY", "")
if _proxy:
    os.environ.setdefault("HTTP_PROXY",  _proxy)
    os.environ.setdefault("HTTPS_PROXY", _proxy)
    os.environ.setdefault("http_proxy",  _proxy)
    os.environ.setdefault("https_proxy", _proxy)

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────

RSS_FEEDS = [
    {"name": "VentureBeat AI",         "url": "https://feeds.feedburner.com/venturebeat/SZYF"},
    {"name": "MIT Technology Review",  "url": "https://www.technologyreview.com/feed/"},
    {"name": "TechCrunch AI",          "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "Wired AI",               "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    {"name": "Ars Technica Tech Lab",  "url": "https://feeds.arstechnica.com/arstechnica/technology-lab"},
    {"name": "Hugging Face Blog",      "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "Import AI (Jack Clark)", "url": "https://jack-clark.net/feed/"},
    {"name": "The Register AI/ML",     "url": "https://www.theregister.com/software/ai_ml/headlines.atom"},
]

# 三层关键词过滤
# 第一层：AI 相关词（必须包含其中之一）
LAYER1_REQUIRED = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "neural network", "llm", "large language model", "gpt", "claude",
    "gemini", "mistral", "llama", "transformer", "diffusion", "generative",
    "openai", "anthropic", "google deepmind", "meta ai", "chatgpt",
    "embedding", "fine-tuning", "rag", "reinforcement learning",
    "multimodal", "agent", "autonomous", "foundation model",
]

# 第二层：低质量内容排除词
LAYER2_EXCLUDE = [
    "sponsored", "advertisement", "buy now", "discount", "promo code",
    "click here", "subscribe now", "limited time offer",
]

# 第三层：分类标签映射
LAYER3_CATEGORIES = {
    "news":      ["announce", "launch", "release", "introduce", "partner",
                  "fund", "raise", "acquire", "debut", "unveil", "publish",
                  "report", "study", "research", "paper", "benchmark"],
    "knowledge": ["how", "what is", "explain", "understand", "guide",
                  "tutorial", "concept", "theory", "architecture", "method",
                  "technique", "approach", "framework", "insight"],
    "tool":      ["update", "version", "feature", "upgrade", "improve",
                  "api", "sdk", "plugin", "extension", "integration",
                  "tool", "platform", "product", "service", "app"],
}

MAX_ITEMS_PER_FEED = 15   # 每个 RSS 源最多取几条
MAX_FEED_ITEMS_TOTAL = 80 # 送给 Claude 的最大条数


# ─────────────────────────────────────────────
# RSS 抓取
# ─────────────────────────────────────────────

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AIBriefBot/1.0)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
_PROXIES = {"http": _proxy, "https": _proxy} if _proxy else None


def fetch_feed(feed_info: dict) -> list[dict]:
    """抓取单个 RSS 源，返回文章列表。"""
    try:
        resp = requests.get(
            feed_info["url"],
            headers=_HTTP_HEADERS,
            timeout=30,
            verify=False,
            proxies=_PROXIES,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(io.BytesIO(resp.content))
        items = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title   = entry.get("title", "").strip()
            link    = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            # 去掉 HTML 标签
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()[:500]

            pub_date = ""
            if hasattr(entry, "published"):
                pub_date = entry.published

            if title and link:
                items.append({
                    "source": feed_info["name"],
                    "title":   title,
                    "link":    link,
                    "summary": summary,
                    "date":    pub_date,
                })
        return items
    except Exception as e:
        print(f"  [警告] 抓取 {feed_info['name']} 失败: {e}", file=sys.stderr)
        return []


def fetch_all_feeds() -> list[dict]:
    """并发抓取所有 RSS 源（顺序执行，简单可靠）。"""
    all_items = []
    for feed_info in RSS_FEEDS:
        print(f"  → 抓取 {feed_info['name']} ...", end=" ", flush=True)
        items = fetch_feed(feed_info)
        print(f"{len(items)} 条")
        all_items.extend(items)
    return all_items


# ─────────────────────────────────────────────
# 三层过滤
# ─────────────────────────────────────────────

def passes_layer1(item: dict) -> bool:
    """第一层：必须包含 AI 相关词。"""
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in LAYER1_REQUIRED)


def passes_layer2(item: dict) -> bool:
    """第二层：排除低质量内容。"""
    text = (item["title"] + " " + item["summary"]).lower()
    return not any(kw in text for kw in LAYER2_EXCLUDE)


def classify_layer3(item: dict) -> str:
    """第三层：分类（news / knowledge / tool / general）。"""
    text = (item["title"] + " " + item["summary"]).lower()
    scores = {cat: 0 for cat in LAYER3_CATEGORIES}
    for cat, keywords in LAYER3_CATEGORIES.items():
        scores[cat] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def filter_items(items: list[dict]) -> list[dict]:
    """执行三层过滤，附加分类标签，去重。"""
    seen_titles = set()
    filtered = []
    for item in items:
        title_lower = item["title"].lower()
        if title_lower in seen_titles:
            continue
        if not passes_layer1(item):
            continue
        if not passes_layer2(item):
            continue
        item["category"] = classify_layer3(item)
        seen_titles.add(title_lower)
        filtered.append(item)

    print(f"  过滤后剩余 {len(filtered)} 条（原始 {len(items)} 条）")
    return filtered[:MAX_FEED_ITEMS_TOTAL]


# ─────────────────────────────────────────────
# Claude 生成简报内容
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位资深 AI 领域编辑，负责为中文读者撰写每日 AI 简报。
你的任务是从提供的原始文章列表中，精选并撰写结构化简报内容。

要求：
- 语言：全部使用中文，技术术语可保留英文原名
- 风格：专业、简洁、有洞见，避免机器翻译腔
- 每个板块严格按 JSON 格式输出
- 只输出 JSON，不要输出任何 markdown 代码块或其他文字"""

USER_PROMPT_TEMPLATE = """今天是 {today}。以下是从 8 个精选英文 AI 网站抓取并过滤后的文章列表：

{articles_json}

请从中精选内容，按以下 JSON 结构输出简报：

{{
  "date": "{today}",
  "news": [
    {{
      "title": "新闻标题（中文翻译或概括）",
      "key_point": "一句话要点（≤40字）",
      "insights": [
        "核心观点1（≤30字）",
        "核心观点2（≤30字）",
        "核心观点3（≤30字）"
      ],
      "source_name": "来源网站名",
      "source_url": "原文链接"
    }}
  ],
  "knowledge": [
    {{
      "concept": "概念名称",
      "why_important": "为何重要（≤50字）",
      "learning_path": "学习路径建议（≤60字）",
      "source_name": "来源网站名",
      "source_url": "原文链接"
    }}
  ],
  "tools": [
    {{
      "tool_name": "工具名称",
      "update": "更新内容描述（≤60字）",
      "target_users": "适用人群（≤30字）",
      "official_url": "官方或原文链接"
    }}
  ]
}}

选取规则：
- news：选 3 条最重要的 AI 新闻/研究进展
- knowledge：选 2-4 条有教育价值的技术/概念文章（有则多，无则少）
- tools：选 3 条 AI 工具发布或重要更新

只输出纯 JSON，不要包含任何其他文字。"""


def build_articles_summary(items: list[dict]) -> str:
    """把过滤后的文章列表格式化为 Claude 输入。"""
    simplified = []
    for i, item in enumerate(items):
        simplified.append({
            "id":       i + 1,
            "source":   item["source"],
            "category": item.get("category", "general"),
            "title":    item["title"],
            "summary":  item["summary"][:300],
            "url":      item["link"],
        })
    return json.dumps(simplified, ensure_ascii=False, indent=2)


def generate_brief(items: list[dict]) -> dict:
    """调用阿里云百炼 API，生成结构化简报内容。"""
    from openai import OpenAI

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    today = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    articles_json = build_articles_summary(items)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        today=today,
        articles_json=articles_json,
    )

    print("  → 调用 qwen-plus 生成简报内容 ...", flush=True)

    stream = client.chat.completions.create(
        model="qwen-plus",
        max_tokens=4096,
        stream=True,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
    )

    full_text = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="", flush=True)
        full_text += delta

    print("\n")

    # 解析 JSON
    json_str = full_text.strip()
    json_str = re.sub(r"^```(?:json)?\s*", "", json_str)
    json_str = re.sub(r"\s*```$", "", json_str)

    return json.loads(json_str)


# ─────────────────────────────────────────────
# HTML 渲染
# ─────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日 AI 简报 · {date}</title>
<style>
  /* ── Reset & Base ── */
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    line-height: 1.7;
    padding: 20px 12px 48px;
  }}
  a {{ color: #4f46e5; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── Container ── */
  .container {{
    max-width: 680px;
    margin: 0 auto;
  }}

  /* ── Header ── */
  .header {{
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    border-radius: 16px;
    padding: 32px 28px;
    text-align: center;
    margin-bottom: 24px;
    color: #fff;
  }}
  .header h1 {{
    font-size: 1.6rem;
    font-weight: 700;
    letter-spacing: 0.05em;
  }}
  .header .subtitle {{
    margin-top: 6px;
    font-size: 0.88rem;
    opacity: 0.85;
    letter-spacing: 0.02em;
  }}
  .header .meta {{
    margin-top: 12px;
    font-size: 0.78rem;
    opacity: 0.7;
  }}

  /* ── Section ── */
  .section {{
    margin-bottom: 20px;
  }}
  .section-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 18px;
    border-radius: 12px 12px 0 0;
    font-weight: 700;
    font-size: 0.95rem;
    letter-spacing: 0.03em;
  }}
  .section-news .section-header    {{ background: #eef2ff; color: #3730a3; border-left: 4px solid #4f46e5; }}
  .section-knowledge .section-header {{ background: #f0fdf4; color: #166534; border-left: 4px solid #16a34a; }}
  .section-tools .section-header   {{ background: #fff7ed; color: #9a3412; border-left: 4px solid #ea580c; }}

  .section-icon {{ font-size: 1.1rem; }}

  /* ── Card ── */
  .card {{
    background: #fff;
    padding: 20px 20px 16px;
    border-bottom: 1px solid #f1f1f1;
  }}
  .card:last-child {{
    border-bottom: none;
    border-radius: 0 0 12px 12px;
  }}
  .card-number {{
    display: inline-block;
    background: #4f46e5;
    color: #fff;
    font-size: 0.7rem;
    font-weight: 700;
    width: 20px; height: 20px;
    line-height: 20px;
    text-align: center;
    border-radius: 50%;
    margin-right: 8px;
    vertical-align: middle;
  }}
  .card-number.green  {{ background: #16a34a; }}
  .card-number.orange {{ background: #ea580c; }}

  .card-title {{
    font-size: 1rem;
    font-weight: 700;
    color: #111;
    margin-bottom: 6px;
    display: inline;
    vertical-align: middle;
  }}
  .card-key-point {{
    font-size: 0.88rem;
    color: #555;
    margin: 8px 0 10px;
    padding: 8px 12px;
    background: #f8f8fb;
    border-left: 3px solid #4f46e5;
    border-radius: 0 6px 6px 0;
  }}
  .insights-label {{
    font-size: 0.75rem;
    font-weight: 600;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }}
  .insights {{
    list-style: none;
    padding: 0;
  }}
  .insights li {{
    font-size: 0.875rem;
    color: #333;
    padding: 3px 0 3px 16px;
    position: relative;
  }}
  .insights li::before {{
    content: "▸";
    position: absolute;
    left: 0;
    color: #4f46e5;
    font-size: 0.75rem;
    top: 4px;
  }}

  /* knowledge 颜色 */
  .section-knowledge .card-key-point {{ border-left-color: #16a34a; }}
  .section-knowledge .insights li::before {{ color: #16a34a; }}

  /* tools 颜色 */
  .section-tools .card-key-point {{ border-left-color: #ea580c; }}
  .section-tools .insights li::before {{ color: #ea580c; }}

  .card-source {{
    margin-top: 12px;
    font-size: 0.78rem;
    color: #999;
  }}
  .card-source a {{
    color: #6366f1;
    font-weight: 500;
  }}

  /* knowledge / tool specific */
  .kv-row {{
    display: flex;
    gap: 6px;
    font-size: 0.875rem;
    margin: 5px 0;
  }}
  .kv-label {{
    flex-shrink: 0;
    font-weight: 600;
    color: #666;
    min-width: 5em;
  }}
  .kv-value {{ color: #222; }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    font-size: 0.78rem;
    color: #aaa;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid #e5e7eb;
  }}

  /* ── Mobile ── */
  @media (max-width: 480px) {{
    body {{ padding: 12px 8px 40px; }}
    .header {{ padding: 22px 16px; }}
    .header h1 {{ font-size: 1.3rem; }}
    .card {{ padding: 16px 14px 12px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div class="subtitle">DAILY AI BRIEF</div>
    <h1>每日 AI 简报</h1>
    <div class="meta">{date} &nbsp;·&nbsp; 精选自 8 个英文信源 &nbsp;·&nbsp; 三层关键词过滤</div>
  </div>

  <!-- Section 1: AI 新闻 -->
  <div class="section section-news">
    <div class="section-header">
      <span class="section-icon">📰</span>
      板块一：AI 新闻
    </div>
    {news_html}
  </div>

  <!-- Section 2: AI 知识 -->
  <div class="section section-knowledge">
    <div class="section-header">
      <span class="section-icon">🧠</span>
      板块二：AI 知识
    </div>
    {knowledge_html}
  </div>

  <!-- Section 3: AI 工具更新 -->
  <div class="section section-tools">
    <div class="section-header">
      <span class="section-icon">🛠️</span>
      板块三：AI 工具更新
    </div>
    {tools_html}
  </div>

  <div class="footer">
    由 Claude Opus 4.6 生成 &nbsp;·&nbsp; {date} &nbsp;·&nbsp; AI 简报自动生成系统
  </div>

</div>
</body>
</html>
"""

def render_news(news_list: list[dict]) -> str:
    html_parts = []
    for i, item in enumerate(news_list, 1):
        insights_html = "\n".join(
            f'<li>{ins}</li>' for ins in item.get("insights", [])
        )
        html_parts.append(f"""\
    <div class="card">
      <span class="card-number">{i}</span>
      <span class="card-title">{_esc(item.get('title', ''))}</span>
      <div class="card-key-point">{_esc(item.get('key_point', ''))}</div>
      <div class="insights-label">核心观点</div>
      <ul class="insights">
        {insights_html}
      </ul>
      <div class="card-source">
        来源：{_esc(item.get('source_name', ''))}
        &nbsp;·&nbsp;
        <a href="{item.get('source_url', '#')}" target="_blank" rel="noopener">原文链接 →</a>
      </div>
    </div>""")
    return "\n".join(html_parts)


def render_knowledge(knowledge_list: list[dict]) -> str:
    html_parts = []
    for i, item in enumerate(knowledge_list, 1):
        html_parts.append(f"""\
    <div class="card">
      <span class="card-number green">{i}</span>
      <span class="card-title">{_esc(item.get('concept', ''))}</span>
      <div class="kv-row" style="margin-top:10px">
        <span class="kv-label">为何重要</span>
        <span class="kv-value">{_esc(item.get('why_important', ''))}</span>
      </div>
      <div class="kv-row">
        <span class="kv-label">学习路径</span>
        <span class="kv-value">{_esc(item.get('learning_path', ''))}</span>
      </div>
      <div class="card-source">
        来源：{_esc(item.get('source_name', ''))}
        &nbsp;·&nbsp;
        <a href="{item.get('source_url', '#')}" target="_blank" rel="noopener">原文链接 →</a>
      </div>
    </div>""")
    return "\n".join(html_parts)


def render_tools(tools_list: list[dict]) -> str:
    html_parts = []
    for i, item in enumerate(tools_list, 1):
        html_parts.append(f"""\
    <div class="card">
      <span class="card-number orange">{i}</span>
      <span class="card-title">{_esc(item.get('tool_name', ''))}</span>
      <div class="kv-row" style="margin-top:10px">
        <span class="kv-label">更新内容</span>
        <span class="kv-value">{_esc(item.get('update', ''))}</span>
      </div>
      <div class="kv-row">
        <span class="kv-label">适用人群</span>
        <span class="kv-value">{_esc(item.get('target_users', ''))}</span>
      </div>
      <div class="card-source">
        <a href="{item.get('official_url', '#')}" target="_blank" rel="noopener">官方链接 →</a>
      </div>
    </div>""")
    return "\n".join(html_parts)


def _esc(text: str) -> str:
    """HTML 转义。"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def update_index(output_dir: Path, brief: dict, date_slug: str) -> None:
    """更新 history.json 和 index.html 历史索引。"""
    history_file = output_dir / "history.json"
    history = json.loads(history_file.read_text(encoding="utf-8")) if history_file.exists() else []

    entry = {
        "date":            brief.get("date", date_slug),
        "slug":            date_slug,
        "filename":        f"ribao_{date_slug}.html",
        "news_count":      len(brief.get("news", [])),
        "knowledge_count": len(brief.get("knowledge", [])),
        "tools_count":     len(brief.get("tools", [])),
        "headlines":       [i.get("title", "") for i in brief.get("news", [])[:3]],
    }
    history = [h for h in history if h.get("slug") != date_slug]
    history.append(entry)
    history.sort(key=lambda x: x.get("slug", ""), reverse=True)

    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    # history.html = 历史列表；index.html = 最新简报（根域名直接可达）
    (output_dir / "history.html").write_text(render_index(history), encoding="utf-8")
    latest_html = (output_dir / f"ribao_{date_slug}.html").read_text(encoding="utf-8")
    (output_dir / "index.html").write_text(
        latest_html.replace(
            "</title>",
            "</title>\n<base href=\"./\">",
            1,
        ).replace(
            "<div class=\"header\">",
            "<div style=\"text-align:center;padding:8px;background:#4f46e5\">"
            "<a href=\"history.html\" style=\"color:#fff;font-size:0.85rem;"
            "text-decoration:none;opacity:0.9\">📚 查看历史记录 →</a></div>"
            "<div class=\"header\">",
            1,
        ),
        encoding="utf-8",
    )
    print(f"  ✅ 历史索引已更新（共 {len(history)} 期）")


INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日 AI 简报 · 历史记录</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    line-height: 1.7;
    padding: 20px 12px 48px;
  }}
  a {{ color: inherit; text-decoration: none; }}
  .container {{ max-width: 680px; margin: 0 auto; }}

  .header {{
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    border-radius: 16px;
    padding: 32px 28px;
    text-align: center;
    margin-bottom: 28px;
    color: #fff;
  }}
  .header h1 {{ font-size: 1.6rem; font-weight: 700; }}
  .header .sub {{ margin-top: 6px; font-size: 0.88rem; opacity: 0.8; }}

  .stats {{
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
  }}
  .stat-card {{
    flex: 1;
    background: #fff;
    border-radius: 12px;
    padding: 16px;
    text-align: center;
  }}
  .stat-num {{ font-size: 1.8rem; font-weight: 700; color: #4f46e5; }}
  .stat-label {{ font-size: 0.78rem; color: #888; margin-top: 2px; }}

  .entry {{
    background: #fff;
    border-radius: 12px;
    padding: 18px 20px;
    margin-bottom: 12px;
    display: block;
    transition: box-shadow .15s, transform .15s;
    border-left: 4px solid #4f46e5;
  }}
  .entry:hover {{
    box-shadow: 0 4px 20px rgba(79,70,229,.12);
    transform: translateY(-1px);
  }}
  .entry-top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }}
  .entry-date {{ font-size: 1rem; font-weight: 700; color: #111; }}
  .entry-badges {{ display: flex; gap: 6px; }}
  .badge {{
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 20px;
  }}
  .badge-news      {{ background: #eef2ff; color: #4f46e5; }}
  .badge-knowledge {{ background: #f0fdf4; color: #16a34a; }}
  .badge-tools     {{ background: #fff7ed; color: #ea580c; }}
  .entry-headlines {{ list-style: none; }}
  .entry-headlines li {{
    font-size: 0.85rem;
    color: #555;
    padding: 2px 0 2px 14px;
    position: relative;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .entry-headlines li::before {{
    content: "▸";
    position: absolute;
    left: 0;
    color: #a5b4fc;
    font-size: 0.7rem;
    top: 4px;
  }}
  .footer {{
    text-align: center;
    font-size: 0.78rem;
    color: #aaa;
    margin-top: 32px;
  }}
  @media (max-width: 480px) {{
    .stats {{ flex-direction: column; }}
    .header {{ padding: 22px 16px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div style="text-align:center;padding:8px;background:#4f46e5;border-radius:12px 12px 0 0;margin-bottom:-4px">
    <a href="index.html" style="color:#fff;font-size:0.85rem;text-decoration:none;opacity:0.9">← 返回最新简报</a>
  </div>
  <div class="header" style="border-radius:0 0 16px 16px">
    <div class="sub">DAILY AI BRIEF · HISTORY</div>
    <h1>每日 AI 简报</h1>
    <div class="sub" style="margin-top:8px">历史记录存档</div>
  </div>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-num">{total}</div>
      <div class="stat-label">累计期数</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{latest}</div>
      <div class="stat-label">最新一期</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{total_articles}</div>
      <div class="stat-label">累计文章</div>
    </div>
  </div>

  {entries_html}

  <div class="footer">由 AI 自动生成 · GitHub Actions 每日驱动</div>
</div>
</body>
</html>
"""


def render_index(history: list[dict]) -> str:
    total = len(history)
    latest = history[0]["date"] if history else "—"
    total_articles = sum(
        h.get("news_count", 0) + h.get("knowledge_count", 0) + h.get("tools_count", 0)
        for h in history
    )
    parts = []
    for h in history:
        headlines_html = "\n".join(
            f'<li>{_esc(hl)}</li>' for hl in h.get("headlines", []) if hl
        )
        parts.append(f"""\
  <a class="entry" href="{h['filename']}">
    <div class="entry-top">
      <span class="entry-date">📅 {_esc(h['date'])}</span>
      <span class="entry-badges">
        <span class="badge badge-news">📰 {h.get('news_count',0)}</span>
        <span class="badge badge-knowledge">🧠 {h.get('knowledge_count',0)}</span>
        <span class="badge badge-tools">🛠 {h.get('tools_count',0)}</span>
      </span>
    </div>
    <ul class="entry-headlines">{headlines_html}</ul>
  </a>""")

    return INDEX_TEMPLATE.format(
        total=total,
        latest=latest,
        total_articles=total_articles,
        entries_html="\n".join(parts),
    )


def render_html(brief: dict) -> str:
    date_str = brief.get("date", datetime.now().strftime("%Y年%m月%d日"))
    return HTML_TEMPLATE.format(
        date=date_str,
        news_html=render_news(brief.get("news", [])),
        knowledge_html=render_knowledge(brief.get("knowledge", [])),
        tools_html=render_tools(brief.get("tools", [])),
    )


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  每日 AI 简报生成器")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 52)

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("\n❌ 未找到 DASHSCOPE_API_KEY")
        print("   请在项目目录的 .env 文件中添加：")
        print("   DASHSCOPE_API_KEY=sk-你的百炼密钥")
        print("   （在 https://bailian.console.aliyun.com 获取）")
        sys.exit(1)

    # 0. 代理检测
    if _proxy:
        print(f"\n[代理] 已配置：{_proxy}")
        try:
            r = requests.get("https://httpbin.org/ip", proxies=_PROXIES,
                             timeout=5, verify=False)
            ip = r.json().get("origin", "未知")
            print(f"[代理] ✅ 连通，出口 IP：{ip}")
        except Exception:
            print("[代理] ⚠️  无法连通，请检查代理是否开启")
    else:
        print("\n[代理] 未配置，直连网络")

    # 1. 抓取 RSS
    print("\n[1/4] 抓取 RSS 信源...")
    raw_items = fetch_all_feeds()

    # 2. 三层过滤
    print("\n[2/4] 执行三层关键词过滤...")
    filtered_items = filter_items(raw_items)

    if not filtered_items:
        print("错误：过滤后没有可用文章，请检查网络或调整过滤规则。", file=sys.stderr)
        sys.exit(1)

    # 3. Claude 生成简报
    print("\n[3/4] 调用 Claude 生成结构化简报...")
    try:
        brief = generate_brief(filtered_items)
    except json.JSONDecodeError as e:
        print(f"错误：Claude 输出无法解析为 JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIError as e:
        print(f"错误：Claude API 调用失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 4. 渲染 HTML 并保存
    print("[4/4] 渲染 HTML 邮件...")
    output_dir = Path(__file__).parent / "docs"
    output_dir.mkdir(exist_ok=True)

    date_slug = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"ribao_{date_slug}.html"
    html_content = render_html(brief)
    output_path.write_text(html_content, encoding="utf-8")

    print(f"\n✅ 简报已生成：{output_path}")
    print(f"   新闻: {len(brief.get('news', []))} 条")
    print(f"   知识: {len(brief.get('knowledge', []))} 条")
    print(f"   工具: {len(brief.get('tools', []))} 条")
    print(f"   文件大小: {output_path.stat().st_size / 1024:.1f} KB")

    # 5. 更新历史索引
    print("\n[5/6] 更新历史索引...")
    update_index(output_dir, brief, date_slug)

    # 6. 推送到微信（Server酱）
    serverchan_key = os.environ.get("SERVERCHAN_KEY")
    if serverchan_key:
        print("\n[6/6] 推送到微信...")
        push_to_wechat(brief, serverchan_key)
    else:
        print("\n（未配置 SERVERCHAN_KEY，跳过微信推送）")

    # 可选：自动在浏览器中打开历史索引
    if "--open" in sys.argv:
        import subprocess
        subprocess.run(["open", str(output_dir / "index.html")])


def push_to_wechat(brief: dict, send_key: str) -> None:
    """通过 Server酱 推送简报摘要到微信。"""
    date_str = brief.get("date", "")

    lines = []

    # 板块一：AI 新闻
    lines.append("## 📰 AI 新闻")
    for item in brief.get("news", []):
        lines.append(f"**{item.get('title', '')}**")
        lines.append(f"> {item.get('key_point', '')}")
        for ins in item.get("insights", []):
            lines.append(f"- {ins}")
        lines.append(f"[原文链接]({item.get('source_url', '#')})")
        lines.append("")

    # 板块二：AI 知识
    lines.append("## 🧠 AI 知识")
    for item in brief.get("knowledge", []):
        lines.append(f"**{item.get('concept', '')}**")
        lines.append(f"为何重要：{item.get('why_important', '')}")
        lines.append(f"学习路径：{item.get('learning_path', '')}")
        lines.append(f"[原文链接]({item.get('source_url', '#')})")
        lines.append("")

    # 板块三：AI 工具
    lines.append("## 🛠️ AI 工具更新")
    for item in brief.get("tools", []):
        lines.append(f"**{item.get('tool_name', '')}**")
        lines.append(f"{item.get('update', '')}")
        lines.append(f"适用人群：{item.get('target_users', '')}")
        lines.append(f"[官方链接]({item.get('official_url', '#')})")
        lines.append("")

    content = "\n".join(lines)
    title = f"每日 AI 简报 · {date_str}"

    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{send_key}.send",
            data={"title": title, "desp": content},
            timeout=10,
        )
        result = resp.json()
        if result.get("code") == 0:
            print("  ✅ 微信推送成功")
        else:
            print(f"  ⚠️  微信推送失败: {result.get('message', '未知错误')}")
    except Exception as e:
        print(f"  ⚠️  微信推送异常: {e}")


if __name__ == "__main__":
    main()
