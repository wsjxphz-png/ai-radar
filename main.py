#!/usr/bin/env python3
"""
AI 机会雷达 - 日报自动生成系统
=================================
每天自动抓取 30 个 X 账号 + 20 个 YouTube 频道的内容，
通过 DeepSeek API 进行筛选、评分、去重、合并、翻译，
生成中文日报并推送到飞书。

使用方式：
  python main.py                    # 跑一次（GitHub Actions 中用这个）
  python main.py --dry-run          # 只抓取，不调 AI，不推送（测试用）
  python main.py --test-feishu      # 测试飞书推送是否正常
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote, urlparse

import feedparser
import requests


# ============================================================
# 配置
# ============================================================

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
FEISHU_WEBHOOK   = os.environ.get("FEISHU_WEBHOOK", "")
DEEPSEEK_MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TIMEZONE_OFFSET  = 8   # UTC+8 北京时间

# RSSHub 公共实例列表（按优先级，一个挂了自动换下一个）
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com",
]

# 最大重试次数
MAX_RETRIES = 3
# 请求间隔(秒)，避免被限流
REQUEST_DELAY = 0.5

# 日志收集（最后统一输出）
_log_lines: List[str] = []


def log(msg: str):
    """记录日志，同时输出到 stderr 以便 GitHub Actions 查看"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    _log_lines.append(line)
    print(line, file=sys.stderr)


# ============================================================
# 第一步：加载信息源
# ============================================================

def load_sources(path: str = "sources.json") -> List[Dict]:
    """加载 sources.json，验证格式"""
    if not os.path.exists(path):
        log(f"❌ 找不到 {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sources = data.get("sources", [])
    log(f"📋 加载了 {len(sources)} 个信息源")

    x_count = sum(1 for s in sources if s["platform"] == "x")
    yt_count = sum(1 for s in sources if s["platform"] == "youtube")
    log(f"   X(Twitter): {x_count} 个")
    log(f"   YouTube:    {yt_count} 个")

    return sources


# ============================================================
# 第二步：构建 RSS 地址
# ============================================================

def build_rss_url(source: Dict, instance: str = "https://rsshub.app") -> str:
    """为单个信息源构建 RSSHub RSS 地址"""
    platform = source["platform"]

    if platform == "x":
        username = source["username"]
        return f"{instance}/twitter/user/{username}"

    elif platform == "youtube":
        channel = source["channel"]
        return f"{instance}/youtube/user/{channel}"

    else:
        raise ValueError(f"未知平台: {platform}")


# ============================================================
# 第三步：抓取 RSS 内容
# ============================================================

def fetch_rss(url: str, source: Dict, timeout: int = 30) -> Optional[feedparser.FeedParserDict]:
    """
    抓取单个 RSS 源，支持：
    - 多个 RSSHub 实例自动回退
    - 指数退避重试
    - 超时保护
    """
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            # 每次重试用不同的 RSSHub 实例
            instance_idx = attempt % len(RSSHUB_INSTANCES)
            instance = RSSHUB_INSTANCES[instance_idx]

            # 如果传入的 URL 已经是完整 URL，使用原 URL
            if url.startswith("http"):
                final_url = url
            else:
                final_url = build_rss_url(source, instance)

            resp = requests.get(
                final_url,
                timeout=timeout,
                headers={
                    "User-Agent": "AI-Radar/1.0 (Daily News Digest Bot; contact@example.com)",
                    "Accept": "application/rss+xml, application/xml, text/xml, */*"
                }
            )

            if resp.status_code == 200:
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    return feed
                else:
                    # RSS 成功获取但内容为空
                    last_error = f"RSS 内容为空 ({instance})"
                    log(f"   ⚠️ {source['platform']}:{source.get('username') or source.get('channel')} - {last_error}")
                    continue
            elif resp.status_code == 429:
                last_error = f"429 被限流 ({instance})"
                wait = (attempt + 1) * 5
                log(f"   ⏳ 被限流，等待 {wait}s...")
                time.sleep(wait)
                continue
            elif resp.status_code == 404:
                # 这个源可能不存在，换实例试试
                last_error = f"404 不存在 ({instance})"
                continue
            else:
                last_error = f"HTTP {resp.status_code} ({instance})"
                time.sleep(REQUEST_DELAY)
                continue

        except requests.Timeout:
            last_error = f"超时 (尝试 {attempt + 1}/{MAX_RETRIES})"
            time.sleep(REQUEST_DELAY)
            continue
        except Exception as e:
            last_error = str(e)[:80]
            time.sleep(REQUEST_DELAY)
            continue

    # 所有重试都失败
    src_id = source.get("username") or source.get("channel") or source["name"]
    log(f"   ❌ {source['platform']}:{src_id} - {last_error}")
    return None


def fetch_all_sources(sources: List[Dict]) -> List[Dict]:
    """
    并行（顺序执行，间隔 0.5s）抓取所有信息源的 RSS。
    返回结构化的内容列表。
    """
    all_items = []
    success_count = 0
    fail_count = 0

    log(f"\n🔍 开始抓取 {len(sources)} 个信息源...")

    for i, src in enumerate(sources):
        src_id = src.get("username") or src.get("channel") or src["name"]
        log(f"   [{i+1}/{len(sources)}] {src['platform']}:{src_id} ...")

        url = build_rss_url(src)
        feed = fetch_rss(url, src)

        if feed is None:
            fail_count += 1
            # 为失败的源记录一条日志，但不阻塞
            continue

        source_entries = 0
        for entry in feed.entries:
            # 标准化内容条目
            item = _parse_entry(entry, src)
            if item:
                all_items.append(item)
                source_entries += 1

        success_count += 1
        if source_entries > 0:
            log(f"       ✅ 获取到 {source_entries} 条")

        # 避免对 RSSHub 造成压力
        time.sleep(REQUEST_DELAY)

    log(f"\n📊 抓取完成: {success_count} 成功, {fail_count} 失败, 共 {len(all_items)} 条原始内容")
    return all_items


def _parse_entry(entry: feedparser.FeedParserDict, source: Dict) -> Optional[Dict]:
    """将 RSS 条目标准化为统一格式"""
    # 提取发布时间
    pub_date = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
        pub_date = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    else:
        # 没有时间戳的内容不处理
        return None

    # 提取标题
    title = entry.get("title", "").strip()

    # 提取描述（优先使用 content，其次 summary，最后 title）
    description = ""
    if hasattr(entry, "content") and entry.content:
        description = entry.content[0].get("value", "")
    if not description and hasattr(entry, "summary"):
        description = entry.get("summary", "")
    if not description:
        description = title

    # 清理 HTML 标签
    import re as _re
    description = _re.sub(r"<[^>]+>", " ", description)
    description = _re.sub(r"\s+", " ", description).strip()

    # 生成唯一 ID
    raw_id = entry.get("id") or entry.get("link") or f"{title}{pub_date}"
    unique_id = hashlib.md5(raw_id.encode()).hexdigest()[:12]

    return {
        "id": unique_id,
        "source_name": source["name"],
        "platform": source["platform"],
        "category": source.get("category", "other"),
        "source_note": source.get("note", ""),
        "title": title[:200] if title else "(无标题)",
        "description": description[:2000] if description else "",
        "url": entry.get("link", ""),
        "pub_date": pub_date.isoformat(),
        "pub_date_display": pub_date.astimezone(
            timezone(timedelta(hours=TIMEZONE_OFFSET))
        ).strftime("%m-%d %H:%M"),
    }


# ============================================================
# 第四步：预过滤（不消耗 AI）
# ============================================================

# 营销/广告关键词黑名单（命中即丢弃）
SPAM_KEYWORDS = [
    # 英文
    r"\bsponsor(ed)?\b", r"\b#ad\b", r"\baffiliate\b",
    r"\bdiscount\s+code\b", r"\bpromo\s+code\b",
    r"\bshop\s+now\b", r"\bbuy\s+now\b",
    r"\bguaranteed\s+profit\b", r"\b\d{2,3}x\b", r"\bto\s+the\s+moon\b",
    # 中文
    r"限时优惠", r"免费领取", r"注册即送", r"点击链接",
    r"使用我的优惠码", r"佣金", r"购买链接",
    r"报名我的课程", r"加入我的社群", r"扫码进群",
    r"日入过?万", r"月入百?万", r"躺赚", r"暴富", r"轻松赚",
    r"\d+天变现", r"\d+天涨粉",
]

SPAM_PATTERNS = [re.compile(kw, re.IGNORECASE) for kw in SPAM_KEYWORDS]


def pre_filter(items: List[Dict]) -> Tuple[List[Dict], int, int]:
    """
    预过滤（不消耗 AI Token）：
    1. 时间窗口：只保留过去 24 小时
    2. URL 去重
    3. 营销内容过滤
    4. 纯转发/无实质内容过滤
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=24)

    filtered = []
    seen_urls = set()
    spam_count = 0
    old_count = 0

    for item in items:
        # === 时间检查 ===
        try:
            pub_date = datetime.fromisoformat(item["pub_date"])
        except (ValueError, KeyError):
            # 无法解析时间的条目放行（保守策略）
            pass
        else:
            if pub_date < cutoff:
                old_count += 1
                continue

        # === URL 去重 ===
        url = item.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)

        # === 标题完全重复去重 ===
        title = item.get("title", "")
        title_hash = hashlib.md5(title.encode()).hexdigest()
        if title_hash in seen_urls:
            continue
        seen_urls.add(title_hash)

        # === 营销过滤 ===
        content = f"{title} {item.get('description', '')[:500]}"
        is_spam = False
        for pattern in SPAM_PATTERNS:
            if pattern.search(content):
                is_spam = True
                break
        if is_spam:
            spam_count += 1
            continue

        # === 纯转发过滤（X 平台）===
        if item["platform"] == "x":
            if title.startswith("RT @") or title.startswith("//@"):
                continue
            # 只有 emoji 或很短的内容
            if len(title.strip()) < 5:
                continue

        filtered.append(item)

    log(f"\n🔎 预过滤: 去旧 {old_count} | 去重+短内容 | 去营销 {spam_count}")
    log(f"   过滤前 {len(items)} → 过滤后 {len(filtered)} 条")
    return filtered, old_count, spam_count


# ============================================================
# 第五步：DeepSeek API AI 处理
# ============================================================

SYSTEM_PROMPT = """你是一个 AI 情报分析系统，负责将原始信息转化为「AI 机会雷达」日报。

你的核心原则：**不要只写「发生了什么」，必须写「这意味着什么」和「你应该关注什么」。**

---

## 你的任务

对输入的内容列表，执行以下操作：

### 1. 去营销（再次确认）
标记并移除任何隐藏的营销/推广内容。即使是行业人士发的，如果 80% 以上是产品推销，也移除。

### 2. 六维度评分（0-10 分）

| 维度 | 分值 | 标准 |
|------|:--:|------|
| 信息增量 | 0-4 | 0=老生常谈；2=有新角度；4=首发/独家/反常识 |
| 可操作性 | 0-3 | 0=看完不知道能干什么；2=给出方向；3=具体步骤/今天就能落地 |
| 来源权威 | 0-2 | 0=匿名/营销号；1=从业者；2=创始人/CEO/一手实践 |
| 长期价值 | 0-1 | 0=48h过期；1=范式级/可迁移/底层规律 |
| 关联性 | -1~1 | -1=完全无关AI；0=泛AI；1=直接命中你关注领域 |
| 清晰度 | -1~1 | -1=不知所云；0=需要猜；1=信息完整自包含 |

**总分计算方式**：六项直接相加（不是加权平均）。
- ≥8 分：头条候选
- 6-7 分：今日速览
- 4-5 分：值得深读（内容好但不需要紧迫关注）
- <4 分：丢弃

### 3. 主题合并
同一事件/同一话题的内容合并为一组，取最高分的那条为主条目，其余作为「相关来源」列出。

### 4. 翻译
所有英文内容翻译为高质量中文，**保留英文关键术语**（如 Agent、RAG、Fine-tuning）。
翻译要求：自然流畅、符合中文阅读习惯、不翻译腔。

### 5. 机会话改写
每条保留的内容，必须包含**至少一个**以下视角：
- 🔴 机会信号：「这意味着 XX 领域正在变化 → 如果你在关注这个方向…」
- 🟡 方法信号：「他分享的核心方法是… → 你可以用在…」
- 🟢 趋势信号：「这个方向 7 天内被多人提及 → 值得深入关注…」

如果一条内容完全无法写出以上视角，标记为「价值不足」并降分。

---

## 输出格式

严格按以下 JSON 格式输出（不要输出 markdown 代码块标记）：

{
  "meta": {
    "date": "日期",
    "total_fetched": 原始条数,
    "after_filter": 过滤后条数,
    "ai_scored": AI评分条数,
    "report_items": 日报条数
  },
  "headlines": [
    {
      "score": 8.5,
      "title_zh": "中文标题",
      "summary_zh": "120字以内中文摘要，必须包含所以呢",
      "why_matters": "为什么这件事对关注AI的人来说重要（1-2句话）",
      "action_hint": "你应该关注什么/可以做什么（1句话）",
      "sources": ["来源名称"],
      "links": ["链接"]
    }
  ],
  "quick_scan": [
    {
      "score": 7.0,
      "category": "agent / ai-workflow / ai-startup / ai-content / productivity",
      "title_zh": "中文标题",
      "one_liner_zh": "40字以内一句话，带判断而非客观播报",
      "actionable": true,
      "link": "链接"
    }
  ],
  "deep_read": [
    {
      "score": 6.5,
      "title_zh": "中文标题",
      "recommendation": "为什么推荐深读（1句话）",
      "ai_insight": "AI 的深度分析（2-3句话，有判断有观点）",
      "source": "来源名称",
      "link": "链接",
      "estimated_minutes": 15
    }
  ],
  "trend_signals": [
    {
      "signal": "信号描述",
      "strength": "强 / 中 / 弱",
      "evidence": "支撑证据（1句话列出提及的来源）"
    }
  ],
  "try_this_week": {
    "title": "本周值得试的事",
    "description": "1-2句话描述",
    "why_now": "为什么这周做（关联到日报中出现的趋势）",
    "time_cost": "预估时间"
  }
}

---

## 特别提醒

1. **不要做客观新闻播报**。你是机会雷达，不是路透社。
2. **宁可少写，不写废话**。如果某条内容你写不出有价值的判断，直接丢弃。
3. **英文术语保留**。Agent、RAG、Fine-tuning、Prompt Engineering 等术语不翻译。
4. **头条最多 2 条**。不是所有高分内容都值得上头条。
5. **速览条目每条不超过 40 字**。言简意赅，直击要害。"""


def build_ai_input(items: List[Dict]) -> str:
    """将过滤后的内容列表构建为 AI 可处理的输入文本"""

    # 按类别分组
    categories = {}
    for item in items:
        cat = item.get("category", "other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)

    parts = [
        f"以下是过去 24 小时内从 {len(items)} 条内容中筛选出的待分析条目。\n",
        f"分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC+8)\n",
    ]

    index = 0
    for cat_name, cat_items in categories.items():
        cat_labels = {
            "ai-startup": "AI 创业",
            "ai-workflow": "AI 工作流",
            "agent": "Agent 应用",
            "ai-content": "AI 内容创作",
            "productivity": "个人生产力",
        }
        label = cat_labels.get(cat_name, cat_name)
        parts.append(f"\n--- {label} ({len(cat_items)} 条) ---\n")

        for item in cat_items:
            index += 1
            parts.append(
                f"[{index}] 来源: {item['source_name']} ({item['platform'].upper()})\n"
                f"    类别: {label}\n"
                f"    时间: {item.get('pub_date_display', '未知')}\n"
                f"    标题: {item['title']}\n"
                f"    内容: {item['description'][:800]}\n"
                f"    链接: {item.get('url', '无')}\n"
            )

    # 计算 token 估算
    text = "\n".join(parts)
    estimated_tokens = len(text) // 2  # 粗略估算：中英文混合约 2 字符/token
    if estimated_tokens > 60000:
        log(f"⚠️ 输入约 {estimated_tokens} tokens，接近 DeepSeek 限制。将截断描述文本。")
        # 截断策略：减少每条描述的字符数
        # 这里简单粗暴：只保留前 300 字
        for i, item in enumerate(items):
            if len(item.get("description", "")) > 300:
                items[i]["description"] = item["description"][:300] + "..."

    log(f"📝 AI 输入: {index} 条内容, 约 {estimated_tokens} tokens")
    return text


def call_deepseek(system_prompt: str, user_content: str) -> Optional[Dict]:
    """
    调用 DeepSeek API 进行 AI 分析。
    返回解析后的 JSON，或 None（失败时）。
    """
    if not DEEPSEEK_API_KEY:
        log("❌ DEEPSEEK_API_KEY 未设置")
        return None

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,  # 低温度，保证稳定性和一致性
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},  # 强制 JSON 输出
    }

    log(f"\n🤖 调用 DeepSeek API ({DEEPSEEK_MODEL})...")

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,  # 长超时，AI 需要时间思考
            )

            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # 解析 JSON
                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    # 可能包裹在 markdown 代码块中
                    content = content.strip()
                    if content.startswith("```"):
                        content = content.split("\n", 1)[1]
                        if content.endswith("```"):
                            content = content[:-3]
                        content = content.strip()
                        if content.startswith("json"):
                            content = content[4:].strip()
                    try:
                        result = json.loads(content)
                    except json.JSONDecodeError:
                        log(f"⚠️ DeepSeek 返回的不是有效 JSON，重试 {attempt + 1}/{MAX_RETRIES}")
                        time.sleep(2)
                        continue

                # 验证必要字段
                if "headlines" not in result and "quick_scan" not in result:
                    log(f"⚠️ 返回 JSON 缺少必要字段，重试 {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(2)
                    continue

                # 提取用量信息
                usage = data.get("usage", {})
                log(f"   ✅ Token 用量: {usage.get('total_tokens', '?')} "
                    f"(入 {usage.get('prompt_tokens', '?')} / 出 {usage.get('completion_tokens', '?')})")

                return result

            elif resp.status_code == 429:
                wait = (attempt + 1) * 10
                log(f"   ⏳ DeepSeek 限流，等待 {wait}s...")
                time.sleep(wait)
                continue
            elif resp.status_code >= 500:
                wait = (attempt + 1) * 5
                log(f"   ⏳ DeepSeek 服务端错误 {resp.status_code}，等待 {wait}s...")
                time.sleep(wait)
                continue
            else:
                log(f"   ❌ DeepSeek API 错误 {resp.status_code}: {resp.text[:200]}")
                return None

        except requests.Timeout:
            log(f"   ⏳ DeepSeek 超时，重试 {attempt + 1}/{MAX_RETRIES}")
            time.sleep(5)
            continue
        except Exception as e:
            log(f"   ❌ 调用异常: {e}")
            time.sleep(2)
            continue

    log("❌ DeepSeek API 调用失败，已达最大重试次数")
    return None


# ============================================================
# 第六步：格式化飞书消息
# ============================================================

def format_feishu_card(ai_result: Dict, filtered_count: int, success_sources: int, total_sources: int) -> Dict:
    """
    将 AI 分析结果格式化为飞书卡片消息。
    """
    meta = ai_result.get("meta", {})
    headlines = ai_result.get("headlines", [])
    quick_scan = ai_result.get("quick_scan", [])
    deep_read = ai_result.get("deep_read", [])
    trend_signals = ai_result.get("trend_signals", [])
    try_this = ai_result.get("try_this_week", {})

    date_str = datetime.now().strftime("%Y.%m.%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()]

    # 按类别分组速览
    qs_by_cat = {}
    cat_order = ["agent", "ai-workflow", "ai-startup", "ai-content", "productivity"]
    cat_emojis = {
        "agent": "🤖",
        "ai-workflow": "🔧",
        "ai-startup": "💡",
        "ai-content": "🎨",
        "productivity": "⚡",
    }
    for item in quick_scan:
        cat = item.get("category", "other")
        if cat not in qs_by_cat:
            qs_by_cat[cat] = []
        qs_by_cat[cat].append(item)

    # ====== 构建 markdown 内容 ======
    md = []

    # 头部
    md.append(f"🤖 **AI 机会雷达** · {date_str} 周{weekday}")
    md.append(f"从 {filtered_count} 条信息中为你精选 {len(headlines) + len(quick_scan)} 条\n")
    md.append("━━━━━━━━━━━━━━━━━━━")

    # ── 今日头条 ──
    if headlines:
        md.append("\n📌 **今日头条**\n")
        for h in headlines[:2]:
            score = h.get("score", 0)
            fire = "🔥" * min(5, max(1, int(score)))
            md.append(f"**{h.get('title_zh', '')}**  {fire}")
            md.append(f"{h.get('summary_zh', '')}\n")
            md.append(f"💭 {h.get('why_matters', '')}")
            md.append(f"👉 {h.get('action_hint', '')}")

            sources = h.get("sources", [])
            links = h.get("links", [])
            if sources:
                src_str = " · ".join(sources[:3])
                md.append(f"📎 {src_str}")
            if links:
                for link in links[:2]:
                    md.append(f"   [{link}]({link})")
            md.append("")

    # ── 今日速览 ──
    md.append("━━━━━━━━━━━━━━━━━━━")
    md.append("\n📊 **今日速览**\n")

    for cat in cat_order:
        items = qs_by_cat.get(cat, [])
        if not items:
            continue
        emoji = cat_emojis.get(cat, "📌")
        cat_labels = {
            "agent": "Agent 应用",
            "ai-workflow": "AI 工作流",
            "ai-startup": "AI 创业",
            "ai-content": "AI 内容创作",
            "productivity": "个人生产力",
        }
        label = cat_labels.get(cat, cat)
        md.append(f"**{emoji} {label}**")

        for item in items[:5]:
            action = "⚡" if item.get("actionable") else "📌"
            md.append(f"• {action} {item.get('one_liner_zh', '')}")
            link = item.get("link", "")
            if link:
                md.append(f"  [{link}]({link})")
        md.append("")

    # ── 值得深读 ──
    if deep_read:
        md.append("━━━━━━━━━━━━━━━━━━━")
        md.append("\n🔍 **值得深读**\n")
        for i, d in enumerate(deep_read[:3]):
            score = d.get("score", 0)
            mins = d.get("estimated_minutes", 10)
            md.append(f"{i+1}. **{d.get('title_zh', '')}**  ⏱️ ~{mins}分钟")
            md.append(f"   {d.get('recommendation', '')}")
            md.append(f"   💬 {d.get('ai_insight', '')}")
            link = d.get("link", "")
            source = d.get("source", "")
            if link and source:
                md.append(f"   📎 {source} · [链接]({link})")
            elif link:
                md.append(f"   📎 [链接]({link})")
            md.append("")

    # ── 趋势信号 ──
    if trend_signals:
        md.append("━━━━━━━━━━━━━━━━━━━")
        md.append("\n📈 **趋势信号**\n")
        strength_map = {"强": "🔴", "中": "🟡", "弱": "🟢"}
        for s in trend_signals[:5]:
            sig_strength = s.get("strength", "中")
            icon = strength_map.get(sig_strength, "🟡")
            md.append(f"{icon} {s.get('signal', '')}")

    # ── 本周试试 ──
    if try_this and try_this.get("title"):
        md.append("\n━━━━━━━━━━━━━━━━━━━")
        md.append(f"\n📋 **{try_this.get('title', '本周试试')}**")
        md.append(f"{try_this.get('description', '')}")
        md.append(f"⏱️ {try_this.get('time_cost', '30分钟')}")
        reason = try_this.get("why_now", "")
        if reason:
            md.append(f"💡 {reason}")

    # 尾部
    md.append("\n━━━━━━━━━━━━━━━━━━━")
    md.append(f"🤖 AI 机会雷达 · 每日 {datetime.now().strftime('%H:%M')} 自动生成")
    md.append(f"AI 引擎: DeepSeek · {success_sources}/{total_sources} 源抓取成功")
    md.append("")

    content_md = "\n".join(md)

    # 飞书卡片消息格式
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"🤖 AI 机会雷达 · {date_str} 周{weekday}"
                }
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content_md
                }
            ]
        }
    }


# ============================================================
# 第七步：推送到飞书
# ============================================================

def send_to_feishu(card_msg: Dict) -> bool:
    """通过飞书 Webhook 推送日报"""
    if not FEISHU_WEBHOOK:
        log("❌ FEISHU_WEBHOOK 未设置")
        return False

    log("\n📤 推送到飞书...")

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                FEISHU_WEBHOOK,
                json=card_msg,
                timeout=30,
                headers={"Content-Type": "application/json"}
            )

            if resp.status_code == 200:
                result = resp.json()
                if result.get("code") == 0 or result.get("StatusCode") == 0:
                    log("   ✅ 飞书推送成功")
                    return True
                else:
                    log(f"   ⚠️ 飞书返回错误: {result}")
                    return False
            else:
                log(f"   ⚠️ HTTP {resp.status_code}: {resp.text[:100]}")
                time.sleep(2)
                continue

        except Exception as e:
            log(f"   ❌ 推送异常: {e}")
            time.sleep(2)
            continue

    log("❌ 飞书推送失败")
    return False


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AI 机会雷达 - 日报系统")
    parser.add_argument("--dry-run", action="store_true", help="只抓取，不调 AI，不推送")
    parser.add_argument("--test-feishu", action="store_true", help="测试飞书推送")
    parser.add_argument("--sources", default="sources.json", help="信息源配置文件")
    args = parser.parse_args()

    log("=" * 50)
    log("🤖 AI 机会雷达 - 日报系统启动")
    log("=" * 50)

    # 测试飞书
    if args.test_feishu:
        test_card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "🧪 AI 机会雷达 · 推送测试"}
                },
                "elements": [
                    {"tag": "markdown", "content": "✅ 飞书推送测试成功！\n\n如果你看到这条消息，说明飞书 Webhook 配置正确。\n\n明天 10:00 你将收到第一份日报。"}
                ]
            }
        }
        if send_to_feishu(test_card):
            log("✅ 测试成功")
        else:
            log("❌ 测试失败，请检查 FEISHU_WEBHOOK")
        return

    # 1. 加载信息源
    sources = load_sources(args.sources)

    # 2. 抓取内容
    all_items = fetch_all_sources(sources)

    if not all_items:
        log("\n❌ 没有获取到任何内容。可能原因：")
        log("   1. RSSHub 公共实例暂时不可用")
        log("   2. 所有信息源在 24h 内都没有更新")
        log("   建议稍后重试或检查 sources.json")
        sys.exit(1)

    # 3. 预过滤
    filtered_items, old_count, spam_count = pre_filter(all_items)

    if not filtered_items:
        log("\n⚠️ 过滤后没有剩余内容。")
        log(f"   {old_count} 条已被过滤（过去24h无更新）")
        log(f"   {spam_count} 条已被标记为营销")
        sys.exit(0)

    log(f"\n📊 预过滤后剩余 {len(filtered_items)} 条，准备发送给 DeepSeek 分析...")

    # Dry run 模式：只抓取，不分析
    if args.dry_run:
        log("\n--- DRY RUN: 以下是过滤后的内容预览 ---\n")
        for i, item in enumerate(filtered_items[:20]):
            log(f"[{i+1}] {item['platform'].upper()} | {item['source_name']}")
            log(f"    标题: {item['title'][:100]}")
            log(f"    时间: {item.get('pub_date_display', '?')}")
            log(f"    链接: {item.get('url', '无')[:80]}")
            if len(filtered_items) > 20:
                log(f"\n... 还有 {len(filtered_items) - 20} 条")
        return

    # 4. 构建 AI 输入
    ai_input = build_ai_input(filtered_items)

    # 5. 调用 DeepSeek
    ai_result = call_deepseek(SYSTEM_PROMPT, ai_input)

    if ai_result is None:
        log("\n❌ AI 分析失败。将发送简化版日报。")
        # 降级方案：发送简单的来源统计
        fallback = _build_fallback_report(filtered_items, sources)
        send_to_feishu(fallback)
        sys.exit(1)

    # 6. 格式化飞书消息
    success_sources = len(set(
        item["source_name"] for item in all_items
    ))
    card = format_feishu_card(ai_result, len(filtered_items), success_sources, len(sources))

    # 7. 推送
    if not send_to_feishu(card):
        log("\n⚠️ 日报生成完成但推送失败。请检查飞书 Webhook。")
        # 打印日报内容到日志，方便排查
        for elem in card.get("card", {}).get("elements", []):
            if elem["tag"] == "markdown":
                log("\n--- 日报内容预览 ---\n")
                print(elem["content"][:1000])
        sys.exit(1)

    log("\n" + "=" * 50)
    log("✅ 日报生成并推送完成")
    log("=" * 50)


def _build_fallback_report(items: List[Dict], sources: List[Dict]) -> Dict:
    """降级方案：当 AI 不可用时，发送简化的内容摘要"""
    date_str = datetime.now().strftime("%Y.%m.%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()]

    md = [
        f"🤖 **AI 机会雷达** · {date_str} 周{weekday}",
        f"⚠️ AI 分析暂时不可用，以下是原始内容速览\n",
    ]

    # 按源列出
    source_counts = {}
    for item in items:
        src = item["source_name"]
        if src not in source_counts:
            source_counts[src] = []
        source_counts[src].append(item)

    for src, src_items in list(source_counts.items())[:15]:
        md.append(f"**{src}** ({len(src_items)} 条)")
        for item in src_items[:2]:
            md.append(f"• {item['title'][:80]}")
            if item.get("url"):
                md.append(f"  [{item['url'][:60]}]({item['url']})")
        md.append("")

    md.append(f"\n📊 共 {len(items)} 条 · {len(sources)} 个信息源")

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"🤖 AI 机会雷达 · {date_str} 周{weekday}"}
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(md)}
            ]
        }
    }


if __name__ == "__main__":
    main()
