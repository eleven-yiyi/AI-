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
- news：选 3-6 条最重要的 AI 新闻/研究进展（有则多，无则少）
- knowledge：选 3-6 条有教育价值的技术/概念文章（有则多，无则少）
- tools：选 3-6 条 AI 工具发布或重要更新（有则多，无则少）

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
  :root {{
    --indigo: #4f46e5; --indigo-dark: #3730a3; --indigo-light: #eef2ff;
    --green: #16a34a;  --green-light: #f0fdf4;
    --orange: #ea580c; --orange-light: #fff7ed;
    --bg: #f4f5f9; --card: #ffffff; --text: #111827; --muted: #6b7280;
    --border: #e5e7eb; --radius: 14px;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f0f13; --card: #1a1a24; --text: #e5e7eb; --muted: #9ca3af;
      --border: #2d2d3d; --indigo-light: #1e1b4b; --green-light: #052e16;
      --orange-light: #1c0a00; --indigo-dark: #a5b4fc;
    }}
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text);
    line-height: 1.75; padding-bottom: 80px;
  }}
  a {{ color: var(--indigo); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── 顶部导航 ── */
  .topnav {{
    position: sticky; top: 0; z-index: 100;
    background: rgba(79,70,229,.95);
    backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,.15);
  }}
  .topnav-logo {{ color: #fff; font-weight: 700; font-size: 0.95rem; letter-spacing:.03em; }}
  .topnav-links {{ display: flex; gap: 6px; }}
  .topnav-links a {{
    color: rgba(255,255,255,.85); font-size: 0.8rem; font-weight: 500;
    padding: 4px 12px; border-radius: 20px;
    transition: background .15s;
  }}
  .topnav-links a:hover {{ background: rgba(255,255,255,.15); text-decoration: none; }}
  .topnav-links .active {{ background: rgba(255,255,255,.2); color:#fff; }}

  /* ── 进度条 ── */
  #progress {{ position:fixed; top:0; left:0; height:3px;
               background: linear-gradient(90deg,#818cf8,#a78bfa);
               width:0%; z-index:200; transition:width .1s; }}

  /* ── Container ── */
  .container {{ max-width: 700px; margin: 0 auto; padding: 24px 16px; }}

  /* ── Hero ── */
  .hero {{
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 60%, #a855f7 100%);
    border-radius: var(--radius); padding: 36px 28px; text-align: center;
    color: #fff; margin-bottom: 28px;
    box-shadow: 0 8px 32px rgba(79,70,229,.25);
    position: relative; overflow: hidden;
  }}
  .hero::before {{
    content: ""; position: absolute; inset: 0;
    background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.04'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
  }}
  .hero-label {{ font-size: .75rem; font-weight:600; letter-spacing:.12em;
                 opacity:.75; text-transform:uppercase; margin-bottom:8px; }}
  .hero h1 {{ font-size: 1.75rem; font-weight: 800; letter-spacing:.02em; }}
  .hero-date {{ margin-top:10px; font-size:.85rem; opacity:.8; }}
  .hero-tags {{ margin-top:14px; display:flex; gap:8px; justify-content:center; flex-wrap:wrap; }}
  .hero-tag {{
    background: rgba(255,255,255,.15); color:#fff;
    font-size:.72rem; padding:3px 10px; border-radius:20px; font-weight:500;
  }}

  /* ── 板块胶囊标签 ── */
  .sec-badge {{
    display: inline-block; font-size: .62rem; font-weight: 800;
    letter-spacing: .1em; padding: 2px 7px; border-radius: 4px;
    color: #fff; vertical-align: middle; margin-right: 6px;
  }}
  .nb-news      {{ background: var(--indigo); }}
  .nb-knowledge {{ background: var(--green); }}
  .nb-tools     {{ background: var(--orange); }}

  /* ── 快速导航 ── */
  .toc {{
    display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap;
  }}
  .toc a {{
    flex: 1; min-width: 100px;
    display: flex; align-items: center; gap: 6px; justify-content: center;
    background: var(--card); border: 1px solid var(--border);
    color: var(--text); font-size: .82rem; font-weight: 600;
    padding: 10px 12px; border-radius: 10px;
    transition: all .15s;
  }}
  .toc a:hover {{ text-decoration:none; border-color: var(--indigo);
                  box-shadow: 0 0 0 3px rgba(79,70,229,.1); }}

  /* ── Section ── */
  .section {{ margin-bottom: 24px; }}
  .section-header {{
    display: flex; align-items: center; gap: 10px;
    padding: 13px 18px; border-radius: var(--radius) var(--radius) 0 0;
    font-weight: 700; font-size: .92rem; letter-spacing:.02em;
  }}
  .section-news      .section-header {{ background:var(--indigo-light); color:var(--indigo-dark); border-left:4px solid var(--indigo); }}
  .section-knowledge .section-header {{ background:var(--green-light);  color:var(--green);       border-left:4px solid var(--green); }}
  .section-tools     .section-header {{ background:var(--orange-light); color:var(--orange);      border-left:4px solid var(--orange); }}
  .section-count {{
    margin-left: auto; font-size:.72rem; font-weight:600;
    padding:2px 8px; border-radius:20px; background:rgba(0,0,0,.06);
  }}

  /* ── Card ── */
  .card {{
    background: var(--card); padding: 20px 22px 16px;
    border-bottom: 1px solid var(--border);
    transition: background .15s;
  }}
  .card:hover {{ background: color-mix(in srgb, var(--card) 97%, var(--indigo)); }}
  .card:last-child {{ border-bottom:none; border-radius:0 0 var(--radius) var(--radius); }}

  .card-header {{ display:flex; align-items:flex-start; gap:10px; margin-bottom:10px; }}
  .card-num {{
    flex-shrink:0; width:24px; height:24px; line-height:24px;
    text-align:center; border-radius:50%; font-size:.72rem; font-weight:700;
    color:#fff; margin-top:2px;
  }}
  .num-blue   {{ background: var(--indigo); }}
  .num-green  {{ background: var(--green); }}
  .num-orange {{ background: var(--orange); }}
  .card-title {{ font-size:1rem; font-weight:700; color:var(--text); line-height:1.4; }}

  .card-keypoint {{
    font-size:.875rem; color:var(--muted);
    padding:9px 14px; margin:0 0 12px;
    background: color-mix(in srgb, var(--bg) 60%, var(--card));
    border-left:3px solid var(--indigo);
    border-radius:0 8px 8px 0;
  }}
  .section-knowledge .card-keypoint {{ border-left-color: var(--green); }}
  .section-tools     .card-keypoint {{ border-left-color: var(--orange); }}

  .insights {{ list-style:none; margin-bottom:10px; }}
  .insights-label {{
    font-size:.7rem; font-weight:700; color:var(--muted);
    text-transform:uppercase; letter-spacing:.08em; margin-bottom:5px;
  }}
  .insights li {{
    font-size:.86rem; color:var(--text); padding:3px 0 3px 18px; position:relative;
  }}
  .insights li::before {{
    content:"▸"; position:absolute; left:0; top:4px;
    color:var(--indigo); font-size:.72rem;
  }}
  .section-knowledge .insights li::before {{ color:var(--green); }}
  .section-tools     .insights li::before {{ color:var(--orange); }}

  .kv {{ display:flex; gap:8px; font-size:.86rem; margin:6px 0; }}
  .kv-label {{ flex-shrink:0; font-weight:600; color:var(--muted); min-width:4.5em; }}

  .card-footer {{
    display:flex; align-items:center; justify-content:space-between;
    margin-top:12px; padding-top:10px; border-top:1px solid var(--border);
    font-size:.76rem; color:var(--muted);
  }}
  .card-footer a {{
    display:inline-flex; align-items:center; gap:4px;
    color:var(--indigo); font-weight:600; font-size:.78rem;
  }}
  .card-footer a:hover {{ text-decoration:none; opacity:.8; }}

  /* ── 回到顶部 ── */
  #backtop {{
    position:fixed; bottom:24px; right:20px;
    width:40px; height:40px; border-radius:50%;
    background:var(--indigo); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-size:1.1rem; cursor:pointer; border:none;
    box-shadow:0 4px 16px rgba(79,70,229,.35);
    opacity:0; transform:translateY(8px);
    transition:opacity .2s, transform .2s;
  }}
  #backtop.show {{ opacity:1; transform:translateY(0); }}

  /* ── Footer ── */
  .footer {{
    text-align:center; font-size:.76rem; color:var(--muted);
    margin-top:36px; padding-top:20px; border-top:1px solid var(--border);
  }}

  /* ── Mobile ── */
  @media (max-width:480px) {{
    .container {{ padding:16px 12px; }}
    .hero {{ padding:24px 16px; }}
    .hero h1 {{ font-size:1.4rem; }}
    .card {{ padding:16px 14px 12px; }}
    .toc a {{ font-size:.78rem; padding:8px; }}
  }}
</style>
</head>
<body>
<div id="progress"></div>

<!-- 顶部导航 -->
<nav class="topnav">
  <span class="topnav-logo">每日 AI 简报</span>
  <div class="topnav-links">
    <a href="history.html">历史存档</a>
  </div>
</nav>

<div class="container">

  <!-- Hero -->
  <div class="hero">
    <div class="hero-label">Daily AI Brief</div>
    <h1>每日 AI 简报</h1>
    <div class="hero-date">{date}</div>
    <div class="hero-tags">
      <span class="hero-tag">8 个精选信源</span>
      <span class="hero-tag">三层关键词过滤</span>
      <span class="hero-tag">AI 自动生成</span>
    </div>
  </div>

  <!-- 快速跳转 -->
  <div class="toc">
    <a href="#news"><span class="sec-badge nb-news">NEWS</span>看·风向</a>
    <a href="#knowledge"><span class="sec-badge nb-knowledge">EDU</span>获·新知</a>
    <a href="#tools"><span class="sec-badge nb-tools">TOOL</span>试·利器</a>
  </div>

  <!-- 板块一：看·风向 -->
  <div id="news" class="section section-news">
    <div class="section-header">
      <span class="sec-badge nb-news">NEWS</span> 看·风向
      <span class="section-count">{news_count} 条</span>
    </div>
    {news_html}
  </div>

  <!-- 板块二：获·新知 -->
  <div id="knowledge" class="section section-knowledge">
    <div class="section-header">
      <span class="sec-badge nb-knowledge">EDU</span> 获·新知
      <span class="section-count">{knowledge_count} 条</span>
    </div>
    {knowledge_html}
  </div>

  <!-- 板块三：试·利器 -->
  <div id="tools" class="section section-tools">
    <div class="section-header">
      <span class="sec-badge nb-tools">TOOL</span> 试·利器
      <span class="section-count">{tools_count} 条</span>
    </div>
    {tools_html}
  </div>

  <div class="footer">
    由 AI 自动生成 &nbsp;·&nbsp; {date} &nbsp;·&nbsp;
    <a href="history.html">查看历史记录 →</a>
  </div>
</div>

<button id="backtop" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</button>

<script>
  // 阅读进度条
  window.addEventListener('scroll', () => {{
    const el = document.getElementById('progress');
    const bt = document.getElementById('backtop');
    const pct = window.scrollY / (document.body.scrollHeight - window.innerHeight) * 100;
    el.style.width = Math.min(pct, 100) + '%';
    bt.classList.toggle('show', window.scrollY > 300);
  }});
  // 导航高亮
  const sections = document.querySelectorAll('.section');
  const links = document.querySelectorAll('.topnav-links a');
  const obs = new IntersectionObserver(entries => {{
    entries.forEach(e => {{
      if (e.isIntersecting) {{
        links.forEach(l => l.classList.toggle('active', l.getAttribute('href') === '#' + e.target.id));
      }}
    }});
  }}, {{ threshold: 0.4 }});
  sections.forEach(s => obs.observe(s));
</script>
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
    (output_dir / "index.html").write_text(latest_html, encoding="utf-8")
    print(f"  ✅ 历史索引已更新（共 {len(history)} 期）")


INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日 AI 简报 · 历史记录</title>
<style>
  :root {{
    --indigo: #4f46e5; --indigo-dark: #3730a3; --indigo-light: #eef2ff;
    --green: #16a34a;  --green-light: #f0fdf4;
    --orange: #ea580c; --orange-light: #fff7ed;
    --bg: #f4f5f9; --card: #ffffff; --text: #111827; --muted: #6b7280;
    --border: #e5e7eb; --radius: 14px;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f0f13; --card: #1a1a24; --text: #e5e7eb; --muted: #9ca3af;
      --border: #2d2d3d; --indigo-light: #1e1b4b; --green-light: #052e16;
      --orange-light: #1c0a00; --indigo-dark: #a5b4fc;
    }}
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text);
    line-height: 1.75; padding-bottom: 80px;
  }}
  a {{ color: var(--indigo); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── 顶部导航 ── */
  .topnav {{
    position: sticky; top: 0; z-index: 100;
    background: rgba(79,70,229,.95);
    backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,.15);
  }}
  .topnav-logo {{ color: #fff; font-weight: 700; font-size: 0.95rem; letter-spacing:.03em; }}
  .topnav-links {{ display: flex; gap: 6px; }}
  .topnav-links a {{
    color: rgba(255,255,255,.85); font-size: 0.8rem; font-weight: 500;
    padding: 4px 12px; border-radius: 20px;
    transition: background .15s;
  }}
  .topnav-links a:hover {{ background: rgba(255,255,255,.15); text-decoration: none; }}
  .topnav-links .active {{ background: rgba(255,255,255,.2); color:#fff; }}

  /* ── Container ── */
  .container {{ max-width: 700px; margin: 0 auto; padding: 24px 16px; }}

  /* ── Hero ── */
  .hero {{
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 60%, #a855f7 100%);
    border-radius: var(--radius); padding: 36px 28px; text-align: center;
    color: #fff; margin-bottom: 28px;
    box-shadow: 0 8px 32px rgba(79,70,229,.25);
    position: relative; overflow: hidden;
  }}
  .hero::before {{
    content: ""; position: absolute; inset: 0;
    background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.04'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
  }}
  .hero-label {{ font-size:.75rem; font-weight:600; letter-spacing:.12em;
                 opacity:.75; text-transform:uppercase; margin-bottom:8px; }}
  .hero h1 {{ font-size:1.75rem; font-weight:800; letter-spacing:.02em; }}
  .hero-sub {{ margin-top:10px; font-size:.85rem; opacity:.8; }}
  .hero-stats {{
    margin-top: 18px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;
  }}
  .hero-stat {{
    background: rgba(255,255,255,.12); border-radius: 12px;
    padding: 10px 20px; text-align: center; min-width: 90px;
  }}
  .hero-stat-num {{ font-size: 1.5rem; font-weight: 800; line-height: 1; }}
  .hero-stat-label {{ font-size: .7rem; opacity: .8; margin-top: 3px; letter-spacing:.04em; }}

  /* ── 搜索框 ── */
  .search-wrap {{ position: relative; margin-bottom: 20px; }}
  .search-wrap input {{
    width: 100%; padding: 12px 16px 12px 42px;
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--card); color: var(--text);
    font-size: .9rem; outline: none;
    transition: border-color .15s, box-shadow .15s;
  }}
  .search-wrap input:focus {{
    border-color: var(--indigo);
    box-shadow: 0 0 0 3px rgba(79,70,229,.1);
  }}
  .search-icon {{
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    color: var(--muted); font-size: .95rem; pointer-events: none;
  }}
  #no-result {{
    text-align: center; color: var(--muted); padding: 32px;
    font-size: .9rem; display: none;
  }}

  /* ── 月份分组标题 ── */
  .month-group {{ margin-bottom: 6px; margin-top: 24px; }}
  .month-group:first-child {{ margin-top: 0; }}
  .month-label {{
    font-size: .78rem; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em;
    padding: 4px 0 8px; border-bottom: 1px solid var(--border);
    margin-bottom: 10px;
  }}

  /* ── 条目 ── */
  .entry {{
    background: var(--card);
    border-radius: var(--radius);
    padding: 18px 20px;
    margin-bottom: 12px;
    display: block;
    border-left: 4px solid var(--indigo);
    transition: box-shadow .15s, transform .15s, background .15s;
    color: var(--text);
  }}
  .entry:hover {{
    box-shadow: 0 6px 24px rgba(79,70,229,.12);
    transform: translateY(-2px);
    text-decoration: none;
    background: color-mix(in srgb, var(--card) 97%, var(--indigo));
  }}
  .entry-top {{
    display: flex; align-items: center;
    justify-content: space-between; margin-bottom: 10px; gap: 8px;
  }}
  .entry-date {{ font-size: .95rem; font-weight: 700; color: var(--text); white-space: nowrap; }}
  .entry-arrow {{ color: var(--indigo); font-size: .9rem; flex-shrink: 0; }}

  .entry-meta {{ font-size: .76rem; color: var(--muted); margin-bottom: 8px; }}

  .entry-headlines {{ list-style: none; margin-top: 8px; }}
  .entry-headlines li {{
    font-size: .84rem; color: var(--muted);
    padding: 3px 0 3px 16px; position: relative;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .entry-headlines li::before {{
    content: "▸"; position: absolute; left: 0; top: 4px;
    color: var(--indigo); font-size: .68rem; opacity: .7;
  }}

  /* ── 回到顶部 ── */
  #backtop {{
    position: fixed; bottom: 24px; right: 20px;
    width: 40px; height: 40px; border-radius: 50%;
    background: var(--indigo); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem; cursor: pointer; border: none;
    box-shadow: 0 4px 16px rgba(79,70,229,.35);
    opacity: 0; transform: translateY(8px);
    transition: opacity .2s, transform .2s;
  }}
  #backtop.show {{ opacity: 1; transform: translateY(0); }}

  /* ── Footer ── */
  .footer {{
    text-align: center; font-size: .76rem; color: var(--muted);
    margin-top: 36px; padding-top: 20px; border-top: 1px solid var(--border);
  }}

  /* ── Mobile ── */
  @media (max-width: 480px) {{
    .container {{ padding: 16px 12px; }}
    .hero {{ padding: 24px 16px; }}
    .hero h1 {{ font-size: 1.4rem; }}
    .hero-stats {{ gap: 8px; }}
    .hero-stat {{ padding: 8px 14px; min-width: 70px; }}
    .entry-top {{ flex-wrap: wrap; }}
  }}
</style>
</head>
<body>

<!-- 顶部导航 -->
<nav class="topnav">
  <span class="topnav-logo">每日 AI 简报</span>
  <div class="topnav-links">
    <a href="index.html">最新一期</a>
    <a href="history.html" class="active">历史存档</a>
  </div>
</nav>

<div class="container">

  <!-- Hero -->
  <div class="hero">
    <div class="hero-label">Daily AI Brief · Archive</div>
    <h1>历史记录存档</h1>
    <div class="hero-sub">每日自动生成，记录 AI 世界的每一天</div>
    <div class="hero-stats">
      <div class="hero-stat">
        <div class="hero-stat-num">{total}</div>
        <div class="hero-stat-label">累计期数</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-num">{total_articles}</div>
        <div class="hero-stat-label">累计文章数</div>
      </div>
    </div>
  </div>

  <!-- 搜索 -->
  <div class="search-wrap">
    <span class="search-icon">🔍</span>
    <input type="text" id="search" placeholder="搜索日期或标题关键词…" autocomplete="off">
  </div>

  <!-- 条目列表 -->
  <div id="entry-list">
    {entries_html}
  </div>
  <div id="no-result">没有找到匹配的记录</div>

  <div class="footer">由 AI 自动生成 &nbsp;·&nbsp; GitHub Actions 每日驱动</div>
</div>

<button id="backtop" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</button>

<script>
  // 回到顶部按钮
  window.addEventListener('scroll', () => {{
    document.getElementById('backtop').classList.toggle('show', window.scrollY > 300);
  }});

  // 搜索过滤
  const searchEl = document.getElementById('search');
  const entries  = document.querySelectorAll('.entry');
  const noResult = document.getElementById('no-result');
  searchEl.addEventListener('input', () => {{
    const q = searchEl.value.trim().toLowerCase();
    let visible = 0;
    entries.forEach(el => {{
      const match = !q || el.textContent.toLowerCase().includes(q);
      el.style.display = match ? '' : 'none';
      if (match) visible++;
    }});
    noResult.style.display = visible === 0 ? '' : 'none';
  }});
</script>
</body>
</html>
"""


def render_index(history: list[dict]) -> str:
    total = len(history)
    total_articles = sum(
        h.get("news_count", 0) + h.get("knowledge_count", 0) + h.get("tools_count", 0)
        for h in history
    )

    # 按 slug（YYYY-MM-DD）提取年月，分组
    from itertools import groupby
    def month_key(h: dict) -> str:
        slug = h.get("slug", "")
        return slug[:7]  # "YYYY-MM"

    def month_label(ym: str) -> str:
        try:
            y, m = ym.split("-")
            return f"{y}年{int(m)}月"
        except Exception:
            return ym

    parts = []
    for ym, group in groupby(history, key=month_key):
        entries = []
        for h in group:
            headlines_html = "\n".join(
                f'        <li>{_esc(hl)}</li>' for hl in h.get("headlines", []) if hl
            )
            total_items = h.get('news_count', 0) + h.get('knowledge_count', 0) + h.get('tools_count', 0)
            entries.append(f"""\
  <a class="entry" href="{h['filename']}">
    <div class="entry-top">
      <span class="entry-date">📅 {_esc(h['date'])}</span>
      <span class="entry-arrow">→</span>
    </div>
    <div class="entry-meta">共 {total_items} 篇精选内容</div>
    <ul class="entry-headlines">
{headlines_html}
    </ul>
  </a>""")
        parts.append(f"""\
<div class="month-group">
  <div class="month-label">{month_label(ym)}</div>
{"".join(entries)}
</div>""")

    return INDEX_TEMPLATE.format(
        total=total,
        total_articles=total_articles,
        entries_html="\n".join(parts),
    )


def render_html(brief: dict) -> str:
    date_str = brief.get("date", datetime.now().strftime("%Y年%m月%d日"))
    news      = brief.get("news", [])[:6]
    knowledge = brief.get("knowledge", [])[:6]
    tools     = brief.get("tools", [])[:6]
    return HTML_TEMPLATE.format(
        date=date_str,
        news_html=render_news(news),
        knowledge_html=render_knowledge(knowledge),
        tools_html=render_tools(tools),
        news_count=len(news),
        knowledge_count=len(knowledge),
        tools_count=len(tools),
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

    date_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief["date"] = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
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

    # 板块一：看·风向
    lines.append("## 📰 看·风向")
    for item in brief.get("news", []):
        lines.append(f"**{item.get('title', '')}**")
        lines.append(f"> {item.get('key_point', '')}")
        for ins in item.get("insights", []):
            lines.append(f"- {ins}")
        lines.append(f"[原文链接]({item.get('source_url', '#')})")
        lines.append("")

    # 板块二：获·新知
    lines.append("## 🧠 获·新知")
    for item in brief.get("knowledge", []):
        lines.append(f"**{item.get('concept', '')}**")
        lines.append(f"为何重要：{item.get('why_important', '')}")
        lines.append(f"学习路径：{item.get('learning_path', '')}")
        lines.append(f"[原文链接]({item.get('source_url', '#')})")
        lines.append("")

    # 板块三：试·利器
    lines.append("## 🛠️ 试·利器")
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
