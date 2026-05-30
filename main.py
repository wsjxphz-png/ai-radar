#!/usr/bin/env python3
"""
AI 创作者机会雷达
==================
项目定位：AI 创作者参谋部——不是新闻日报，不是科技资讯聚合器，不是程序员技术周刊。

每天回答五个问题：
  今天有什么值得学习？  今天有什么值得模仿？  今天有什么值得做？
  今天有什么值得赚钱？  今天有哪些AI能力可以帮我突破过去做不到的事情？

目标：帮助非技术背景的内容创作者打造一人公司、个人IP、知识产品、高效率创作系统。
"""

import os, sys, re, json, time, hashlib, argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

import requests
import feedparser

# ============================================================
# 配置
# ============================================================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
FEISHU_WEBHOOK   = os.environ.get("FEISHU_WEBHOOK", "")
DEEPSEEK_MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TIMEZONE_OFFSET  = 8
REQUEST_TIMEOUT  = 30
REQUEST_DELAY    = 0.5
MAX_RETRIES      = 2
CARD_MAX_CHARS   = 28000  # 飞书卡片上限约30KB，留余量

_log_lines: List[str] = []
def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    _log_lines.append(line)
    print(line, file=sys.stderr)


# ============================================================
# 用户画像（嵌入 Prompt，也供后续扩展参考）
# ============================================================
USER_PROFILE = """
## 用户画像
- 非技术背景、文科生、不会编程、不希望成为程序员
- 目标身份：AI博主、AI内容创作者、AI创业者、个人IP经营者、一人公司经营者
- 已在用工具：Claude Code、Codex、Obsidian、NotebookLM
- 内容形式：AI教程、AI科普、AI创业内容、AI工具评测、AI工作流分享
- 目标受众：0基础普通人、文科生、非技术用户
- 视频方向：中长视频、精美视频、深度内容——发布到 YouTube/B站/小红书/抖音/视频号
- 创业方向：AI知识产品、AI虚拟产品、AI服务、AI咨询、AI训练营、AI社群
"""


# ============================================================
# 第一步：构建 RSS URL
# ============================================================

def build_rss_urls(config: Dict) -> List[Dict]:
    tasks = []
    sources = config.get("sources", config)

    for yt in sources.get("youtube", []):
        cid = yt.get("channel_id", "")
        if cid:
            tasks.append({"url": f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", "platform": "youtube", "source_name": yt["name"], "category": yt.get("category", "other"), "note": yt.get("note","")})

    for rd in sources.get("reddit", []):
        sub = rd.get("subreddit", "")
        if sub:
            tasks.append({"url": f"https://www.reddit.com/r/{sub}/.rss", "platform": "reddit", "source_name": f"r/{sub}", "category": rd.get("category", "other"), "note": rd.get("note",""), "extra_headers": {"User-Agent": "AI-Radar/1.0"}})

    hn = sources.get("hackernews", {})
    if hn.get("enabled"):
        tasks.append({"url": hn.get("url", "https://news.ycombinator.com/rss"), "platform": "hackernews", "source_name": "Hacker News", "category": "ai-startup", "note": hn.get("note","")})

    ph = sources.get("producthunt", {})
    if ph.get("enabled"):
        tasks.append({"url": ph.get("url", "https://www.producthunt.com/feed"), "platform": "producthunt", "source_name": "Product Hunt", "category": "ai-startup", "note": ph.get("note","")})

    # 微信公众号（可选，不稳定）
    wx = sources.get("wechat", {})
    if wx.get("enabled"):
        for acct in wx.get("accounts", []):
            aid = acct.get("account_id", "")
            if aid:
                tasks.append({"url": f"https://wechat2rss.xlab.app/feed/{aid}.xml", "platform": "wechat", "source_name": acct["name"], "category": acct.get("category","other"), "note": acct.get("note",""), "optional": True})

    return tasks


# ============================================================
# 第二步：抓取
# ============================================================

def fetch_rss(task: Dict) -> Optional[feedparser.FeedParserDict]:
    url = task["url"]
    headers = {"User-Agent": "AI-Radar/1.0", "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*"}
    headers.update(task.get("extra_headers", {}))
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.content)
                if feed.entries: return feed
                return None
            elif resp.status_code in (429,):
                time.sleep((attempt + 1) * 5); continue
            elif resp.status_code in (403, 404):
                return None
            else:
                time.sleep(REQUEST_DELAY); continue
        except Exception:
            time.sleep(REQUEST_DELAY); continue
    return None


def fetch_all(tasks: List[Dict]) -> List[Dict]:
    all_items = []
    ok, fail, opt_fail = 0, 0, 0
    log(f"\n🔍 抓取 {len(tasks)} 个信息源...")
    for i, task in enumerate(tasks):
        is_opt = task.get("optional", False)
        feed = fetch_rss(task)
        if feed is None:
            if is_opt: opt_fail += 1
            else: fail += 1
            continue
        added = 0
        for entry in feed.entries:
            item = _parse(entry, task)
            if item:
                all_items.append(item)
                added += 1
        ok += 1
        if added: log(f"   [{i+1}/{len(tasks)}] {task['platform']}:{task['source_name']} ✅ {added}")
        time.sleep(REQUEST_DELAY)
    log(f"\n📊 {ok}成功 {fail}失败 {opt_fail}可选跳过 → {len(all_items)}条")
    return all_items


def _parse(entry, task: Dict) -> Optional[Dict]:
    pub_date = None
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val: pub_date = datetime(*val[:6], tzinfo=timezone.utc); break
    if not pub_date: return None
    title = re.sub(r"<[^>]+>", "", entry.get("title", "")).strip()
    title = re.sub(r"\s+", " ", title)
    desc = ""
    if hasattr(entry, "content") and entry.content: desc = entry.content[0].get("value", "")
    if not desc and hasattr(entry, "summary"): desc = entry.get("summary", "")
    if not desc: desc = title
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    link = entry.get("link", "")
    raw_id = entry.get("id") or link or title + str(pub_date)
    uid = hashlib.md5(raw_id.encode()).hexdigest()[:12]
    return {"id": uid, "source_name": task["source_name"], "platform": task["platform"], "category": task.get("category","other"), "title": title[:200] if title else "(无标题)", "description": desc[:2000] if desc else "", "url": link, "pub_date": pub_date.isoformat(), "pub_date_display": pub_date.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET))).strftime("%m-%d %H:%M")}


# ============================================================
# 第三步：预过滤
# ============================================================

SPAM_KW = [r"\bsponsor(ed)?\b", r"\b#ad\b", r"\baffiliate\b", r"\bdiscount\s+code\b", r"\bpromo\s+code\b", r"\bshop\s+now\b", r"\bbuy\s+now\b", r"\bguaranteed\s+profit\b", r"限时优惠", r"免费领取", r"注册即送", r"优惠码", r"佣金", r"购买链接", r"报名我的课程", r"加入我的社群", r"扫码进群", r"日入过?万", r"月入百?万", r"躺赚", r"暴富"]
SPAM_RE = [re.compile(kw, re.IGNORECASE) for kw in SPAM_KW]


def pre_filter(items: List[Dict]) -> Tuple[List[Dict], int, int]:
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=24)
    filtered, seen = [], set()
    old_c, spam_c = 0, 0
    for item in items:
        try:
            if datetime.fromisoformat(item["pub_date"]) < cutoff: old_c += 1; continue
        except: pass
        url = item.get("url","")
        if url and url in seen: continue
        if url: seen.add(url)
        th = hashlib.md5(item.get("title","").encode()).hexdigest()
        if th in seen: continue
        seen.add(th)
        txt = f"{item['title']} {item.get('description','')[:500]}"
        if any(p.search(txt) for p in SPAM_RE): spam_c += 1; continue
        if len(item["title"].strip()) < 5: continue
        filtered.append(item)
    log(f"\n🔎 预过滤: 去旧{old_c} 去营销{spam_c} | {len(items)}→{len(filtered)}")
    return filtered, old_c, spam_c


# ============================================================
# 第四步：DeepSeek AI 分析 —— 核心 Prompt
# ============================================================

SYSTEM_PROMPT = f"""你是「AI 创作者参谋部」的首席分析师。你的用户不是程序员，不是技术人员，而是一个**非技术背景、文科出身的内容创作者**。他靠内容赚钱。

{USER_PROFILE}

## 项目定位

这不是 AI 新闻日报，不是科技资讯聚合器。这是一个**创作者参谋部**。

每天帮助用户回答五个问题：
1. 今天有什么值得**学习**？
2. 今天有什么值得**模仿**？
3. 今天有什么值得**做**？
4. 今天有什么值得**赚钱**？
5. 今天有哪些 AI 能力可以帮我**突破过去做不到的事情**？

## 核心原则

日报不是告诉用户「今天发生了什么」，而是告诉用户「今天什么值得我投入时间」。
如果某条信息无法帮助用户：提升内容能力、提升效率、提升收入、提升影响力 → 降低优先级或丢弃。

---

## 🚫 直接丢弃（不评分）

以下类型的内容**只看标题就能丢**：
- 模型评测 / Benchmark / 跑分 / 榜单
- 学术论文 / ArXiv 论文解读 / 技术原理深入分析
- 融资新闻 / 谁又融了多少钱
- 大厂公关稿 / OpenAI-Google-Meta 产品发布通稿
- 纯编程教程 / API 开发 / MCP 开发 / Agent 开发框架
- 技术架构 / 代码实现 / 训练细节
- 和 AI 及内容创作都无关的通用内容
- 纯新闻播报（没有分析、没有观点、没有「所以呢」）

## ⚠️ 降权但不丢弃

以下内容默认降 5 分（但若与内容创作或商业机会**直接强相关**，可以豁免）：
- 编程教程 / API 教程
- MCP 开发 / Agent 开发
- 技术框架讨论
- 公司 PR 稿

---

## ✅ SSS 级优先（最高权重）

### AI 内容创作
- 视频制作/剪辑、AI 脚本/选题/封面/配音
- AI 图像/动画/播客/短视频/长视频
- 能提升视频质量、视频效率、封面质量的任何工具或方法

### AI 个人生产力
- 知识管理/信息管理/自动化工作流
- 内容研究系统/学习系统/笔记系统/第二大脑
- Obsidian、NotebookLM 等工具的新用法

### AI 创业（一人公司方向）
- 数字产品/虚拟产品/AI 服务/咨询/训练营/社群/内容变现
- 个人 IP 打造、增长、变现

## ✅ S 级优先

### 创作者商业案例
- 真实案例：如何涨粉、如何变现、如何获客、如何销售
- 要求：有具体数字或可复制的策略

### AI 工作流
- 普通人能复制、不要求编程的自动化工作流
- n8n / Make 等零代码工具的实际案例

### Agent 应用
- Agent 帮普通人创造价值：调研、选题、写作、视频生产、客户服务、知识管理
- **不是** Agent 开发教程

## ✅ A 级优先

### 新 AI 工具
- 真正能提升效率的工具：写作、视频、研究、自动化
- 不是为炫技而存在的工具

---

## 📊 四维度评分（每项 1-10，总分 40）

| 维度 | 1分 | 10分 |
|------|-----|------|
| **内容价值** | 和内容创作无关 | 直接提升内容质量/产量/效率 |
| **商业价值** | 没有变现可能 | 直接关联收入或商业模式 |
| **可复制性** | 需要特殊条件或编程能力 | 今天就能照着做，零门槛 |
| **与用户相关** | 和非技术创作者无关 | 文科生/个人创作者刚需 |

**总分 < 30 → 丢弃。**
**总分 ≥ 36 → 头条级（今日最重要机会）。**
**总分 30-35 → 精选级（速览/工作流/工具/商业模式）。**

---

## 📰 日报栏目

### 🔥 今日最重要机会（最多 3 条，总分 ≥ 35）
每条输出：
- title_zh, summary_zh（100字）
- opportunity_type: "赚钱" / "涨粉" / "提效" / "影响力"
- importance: "为什么这是机会"
- difficulty: "低/中/高"
- recommended: "推荐指数" (⭐⭐⭐⭐⭐)
- action: "具体行动建议（1句话）"

### 🎯 内容机会（最多 3 条，分析正在爆发的内容方向）
- topic, growth_reason, content_format, should_follow (true/false), competition_level

### 🎬 爆款内容实验室（最多 3 条）
分析爆款内容（视频/推文），拆解：标题、开头、结构、情绪、传播原因

### 🛠 今日最佳工作流（最多 3 条）
- workflow_name, steps, tools, efficiency_gain, suitable_for_ordinary_people (true/false)

### 🤖 AI 能力突破（最多 3 条）
寻找"以前很难做，现在 AI 能做到"的事情。输出：why_important, how_to_leverage

### 💰 商业模式观察（最多 3 条）
分析 AI 产品/服务/创业案例。输出：customer, pricing, acquisition, replicable (true/false)

### 🧠 最佳工具发现（最多 5 条）
- tool_name, solves_what, for_who, worth_learning (true/false), fits_current_stack (true/false)

### 📈 趋势雷达（1 条）
过去 7-30 天，哪些趋势升温/降温/值得布局。

### 💡 给我的建议（**最重要，必须输出，不能为空**）

结合用户背景（文科生、非技术、内容创作者），回答：
如果我是这个用户，今天最值得投入 2 小时研究什么？为什么？具体行动步骤是什么？

**必须输出。** 即使今天的内容质量不高、没有明显的机会，也要基于抓取到的内容给出至少一条务实的建议。例如：
- 「今天没有什么重磅机会，但可以花 2 小时研究 Product Hunt 上新出现的 XX 工具」
- 「今天的 Reddit 讨论集中在 XX 话题，建议做一期相关科普内容」

要求：务实、具体、可执行、不要空话。不要说「持续关注」「保持学习」这种正确的废话。

---

## 翻译

英文 → 中文，保留英文关键术语。翻译要自然流畅。

---

## 输出 JSON 格式

{{
  "meta": {{"total_scored": 0, "kept": 0, "discarded": 0}},
  "top_opportunities": [
    {{"scores": {{"content_value": 9, "business_value": 9, "replicability": 8, "relevance": 10, "total": 36}}, "title_zh": "", "summary_zh": "", "opportunity_type": "赚钱/涨粉/提效/影响力", "importance": "", "difficulty": "低/中/高", "recommended": "⭐⭐⭐⭐⭐", "action": "", "sources": [""], "links": [""]}}
  ],
  "content_opportunities": [
    {{"topic": "", "growth_reason": "", "content_format": "", "should_follow": true, "competition_level": "低/中/高"}}
  ],
  "viral_lab": [
    {{"title": "", "platform": "", "hook_analysis": "", "structure_analysis": "", "emotion_analysis": "", "viral_reason": "", "link": ""}}
  ],
  "best_workflows": [
    {{"workflow_name": "", "steps": "", "tools": "", "efficiency_gain": "", "suitable_for_ordinary": true, "link": ""}}
  ],
  "ai_breakthroughs": [
    {{"what_changed": "", "why_important": "", "how_to_leverage": "", "link": ""}}
  ],
  "business_models": [
    {{"case_name": "", "customer": "", "pricing": "", "acquisition": "", "replicable": true, "creator_insight": ""}}
  ],
  "tool_discoveries": [
    {{"tool_name": "", "solves_what": "", "for_who": "", "worth_learning": true, "fits_current_stack": true, "link": ""}}
  ],
  "trend_radar": {{"heating_up": [""], "cooling_down": [""], "worth_positioning": [""]}},
  "advice_for_me": {{"today_focus": "", "why": "", "steps": [""], "time_investment": ""}}
}}

宁缺毋滥。但如果某个栏目真的没有足够好的内容，输出空数组 []。
**例外：「给我的建议」不能为空，必须输出有实质内容的建议。**"""


def build_ai_input(items: List[Dict]) -> str:
    cats = {}
    for item in items:
        cats.setdefault(item.get("category", "other"), []).append(item)
    cat_labels = {"ai-startup": "AI创业/新工具", "ai-workflow": "AI工作流", "ai-content": "AI内容创作", "productivity": "个人生产力", "agent": "Agent应用"}
    parts = ["以下是从海量信息中筛选的待分析条目。请以「AI 创作者参谋部」视角分析。\n"]
    idx = 0
    for cat_key, cat_items in cats.items():
        label = cat_labels.get(cat_key, cat_key)
        parts.append(f"\n## {label} ({len(cat_items)}条)\n")
        for item in cat_items:
            idx += 1
            parts.append(f"[{idx}]【{item['platform']}】{item['source_name']} | {item.get('pub_date_display','?')}\n    {item['title']}\n    {item['description'][:500]}\n    {item.get('url','')}\n")
    text = "\n".join(parts)
    log(f"📝 AI输入: {idx}条, ~{len(text)//2}tokens")
    return text


def call_deepseek(user_content: str) -> Optional[Dict]:
    if not DEEPSEEK_API_KEY:
        log("❌ DEEPSEEK_API_KEY 未设置"); return None
    payload = {"model": DEEPSEEK_MODEL, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}], "temperature": 0.3, "max_tokens": 8000, "response_format": {"type": "json_object"}}
    log(f"\n🤖 调用 DeepSeek...")
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post("https://api.deepseek.com/v1/chat/completions", headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                if content.startswith("```"): content = content.split("\n", 1)[1] if "\n" in content else content; content = content[:-3].strip() if content.endswith("```") else content; content = content[4:].strip() if content.startswith("json") else content
                result = json.loads(content)
                log(f"   ✅ Tokens: {data.get('usage',{}).get('total_tokens','?')}")
                return result
            elif resp.status_code == 429: time.sleep((attempt+1)*10); continue
            elif resp.status_code >= 500: time.sleep((attempt+1)*5); continue
            else: log(f"   ❌ HTTP {resp.status_code}"); return None
        except Exception as e: log(f"   ⚠️ {e}"); time.sleep(2); continue
    return None


# ============================================================
# 第五步：飞书日报格式化
# ============================================================

def section(title: str, body: str) -> str:
    return f"\n━━━━━━━━━━━━━━━━━━━\n\n{title}\n\n{body}"


def format_feishu(ai: Dict, stats: Dict) -> Dict:
    d = datetime.now()
    date_str = d.strftime("%Y.%m.%d")
    weekday = ["一","二","三","四","五","六","日"][d.weekday()]
    md = [f"🤖 **AI 创作者机会雷达** · {date_str} 周{weekday}", f"从 {stats['filtered']} 条信息中为你精选", "━━━━━━━━━━━━━━━━━━━"]

    # 🔥 今日最重要机会
    tops = ai.get("top_opportunities", [])
    if tops:
        body_lines = []
        for t in tops[:3]:
            s = t.get("scores", {})
            body_lines.append(f"**{t.get('title_zh','')}**  ⭐{s.get('total','?')}/40")
            body_lines.append(f"{t.get('summary_zh','')}")
            body_lines.append(f"🎯 {t.get('opportunity_type','')} | 难度:{t.get('difficulty','?')} | {t.get('recommended','')}")
            body_lines.append(f"💡 {t.get('importance','')}")
            body_lines.append(f"👉 {t.get('action','')}")
            srcs = t.get("sources",[]); links = t.get("links",[])
            if srcs: body_lines.append(f"📎 {' · '.join(srcs[:3])}")
            for link in links[:2]: body_lines.append(f"   [{link}]({link})")
            body_lines.append("")
        md.append(section("🔥 今日最重要机会", "\n".join(body_lines)))

    # 🎯 内容机会
    co = ai.get("content_opportunities", [])
    if co:
        lines = []
        for c in co[:3]:
            follow = "✅ 值得跟进" if c.get("should_follow") else "⚠️ 观望"
            lines.append(f"**{c.get('topic','')}** | {follow} | 竞争:{c.get('competition_level','?')}")
            lines.append(f"   📈 {c.get('growth_reason','')}")
            lines.append(f"   🎬 形式:{c.get('content_format','')}")
            lines.append("")
        md.append(section("🎯 内容机会", "\n".join(lines)))

    # 🎬 爆款内容实验室
    vl = ai.get("viral_lab", [])
    if vl:
        lines = []
        for v in vl[:3]:
            lines.append(f"**{v.get('title','')}** ({v.get('platform','')})")
            lines.append(f"   🪝 开头:{v.get('hook_analysis','')}")
            lines.append(f"   🏗️ 结构:{v.get('structure_analysis','')}")
            lines.append(f"   💭 情绪:{v.get('emotion_analysis','')}")
            lines.append(f"   🚀 传播原因:{v.get('viral_reason','')}")
            link = v.get("link","");
            if link: lines.append(f"   [链接]({link})")
            lines.append("")
        md.append(section("🎬 爆款内容实验室", "\n".join(lines)))

    # 🛠 今日最佳工作流
    wf = ai.get("best_workflows", [])
    if wf:
        lines = []
        for w in wf[:3]:
            suit = "✅ 普通人可用" if w.get("suitable_for_ordinary") else "⚠️ 需要一定基础"
            lines.append(f"**{w.get('workflow_name','')}** | {suit}")
            lines.append(f"   步骤:{w.get('steps','')}")
            lines.append(f"   工具:{w.get('tools','')}")
            lines.append(f"   提升:{w.get('efficiency_gain','')}")
            link = w.get("link","");
            if link: lines.append(f"   [链接]({link})")
            lines.append("")
        md.append(section("🛠 今日最佳工作流", "\n".join(lines)))

    # 🤖 AI 能力突破
    ab = ai.get("ai_breakthroughs", [])
    if ab:
        lines = []
        for a in ab[:3]:
            lines.append(f"**{a.get('what_changed','')}**")
            lines.append(f"   💡 {a.get('why_important','')}")
            lines.append(f"   🎯 {a.get('how_to_leverage','')}")
            link = a.get("link","");
            if link: lines.append(f"   [链接]({link})")
            lines.append("")
        md.append(section("🤖 AI 能力突破", "\n".join(lines)))

    # 💰 商业模式观察
    bm = ai.get("business_models", [])
    if bm:
        lines = []
        for b in bm[:3]:
            rep = "✅ 可复制" if b.get("replicable") else "⚠️ 难复制"
            lines.append(f"**{b.get('case_name','')}** | {rep}")
            lines.append(f"   客户:{b.get('customer','')} | 定价:{b.get('pricing','')} | 获客:{b.get('acquisition','')}")
            lines.append(f"   💭 {b.get('creator_insight','')}")
            lines.append("")
        md.append(section("💰 商业模式观察", "\n".join(lines)))

    # 🧠 最佳工具发现
    td = ai.get("tool_discoveries", [])
    if td:
        lines = []
        for t in td[:5]:
            learn = "✅ 值得学" if t.get("worth_learning") else "📌 可关注"
            fit = "🔧 适合你的工具栈" if t.get("fits_current_stack") else ""
            lines.append(f"**{t.get('tool_name','')}** | {learn} {fit}")
            lines.append(f"   解决:{t.get('solves_what','')} | 适合:{t.get('for_who','')}")
            link = t.get("link","");
            if link: lines.append(f"   [链接]({link})")
            lines.append("")
        md.append(section("🧠 最佳工具发现", "\n".join(lines)))

    # 📈 趋势雷达
    tr = ai.get("trend_radar", {})
    if tr:
        lines = []
        hot = tr.get("heating_up", [])
        cold = tr.get("cooling_down", [])
        pos = tr.get("worth_positioning", [])
        if hot: lines.append(f"🔴 升温: {' | '.join(hot)}")
        if cold: lines.append(f"🔵 降温: {' | '.join(cold)}")
        if pos: lines.append(f"🟢 值得布局: {' | '.join(pos)}")
        if lines: md.append(section("📈 趋势雷达", "\n".join(lines)))

    # 💡 给我的建议（最重要）
    advice = ai.get("advice_for_me", {})
    if advice and advice.get("today_focus"):
        lines = []
        lines.append(f"**🎯 今天最值得投入 2 小时：{advice.get('today_focus','')}**")
        lines.append(f"")
        why = advice.get("why", "")
        if why:
            lines.append(f"为什么：{why}")
            lines.append(f"")
        steps = advice.get("steps", [])
        if steps:
            lines.append(f"行动步骤：")
            for i, step in enumerate(steps, 1):
                lines.append(f"  {i}. {step}")
            lines.append(f"")
        lines.append(f"⏱️ 时间投入：{advice.get('time_investment','2小时')}")
        md.append(section("💡 给我的建议", "\n".join(lines)))
    if not md:
        # 兜底：如果所有栏目都空，至少给一个提示
        md.append("⚠️ 今日内容质量未达日报标准，建议直接查看 Product Hunt 和 Reddit 发现新工具。")
        md.append(f"\n今日共抓取 {stats['filtered']} 条内容，来自 {stats['ok_sources']} 个信息源。")

    # 尾部
    md.append("\n━━━━━━━━━━━━━━━━━━━")
    md.append(f"🤖 AI 创作者机会雷达 · 每日 {d.strftime('%H:%M')} 自动生成")
    md.append(f"AI: DeepSeek · {stats['ok_sources']}/{stats['total_sources']} 源抓取成功 · 关注 Reddit/YouTube/ProductHunt")
    md.append("")

    content = "\n".join(md)
    if len(content) > CARD_MAX_CHARS:
        content = content[:CARD_MAX_CHARS-200]
        idx = content.rfind("\n")
        if idx > 0: content = content[:idx]
        content += "\n\n⚠️ 内容过长已截断"

    return {"msg_type": "interactive", "card": {"header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🤖 AI 创作者机会雷达 · {date_str} 周{weekday}"}}, "elements": [{"tag": "markdown", "content": content}]}}


def send_feishu(card: Dict) -> bool:
    if not FEISHU_WEBHOOK: log("❌ FEISHU_WEBHOOK 未设置"); return False
    log("\n📤 推送飞书...")
    for _ in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(FEISHU_WEBHOOK, json=card, timeout=30)
            if resp.status_code == 200 and resp.json().get("code") == 0: log("   ✅ 推送成功"); return True
            time.sleep(2)
        except: time.sleep(2)
    log("   ❌ 推送失败"); return False


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AI 创作者机会雷达")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-feishu", action="store_true")
    parser.add_argument("--sources", default="sources.json")
    args = parser.parse_args()

    log("="*50)
    log("🤖 AI 创作者机会雷达")
    log("="*50)

    if args.test_feishu:
        test = {"msg_type":"interactive","card":{"header":{"template":"blue","title":{"tag":"plain_text","content":"🧪 AI 创作者机会雷达 · 测试"}},"elements":[{"tag":"markdown","content":"✅ 飞书推送测试成功！\n\n系统已就绪，明天 10:00 你将收到第一份创作者日报。"}]}}
        ok = send_feishu(test)
        log("✅ 测试成功" if ok else "❌ 失败")
        return

    with open(args.sources, "r", encoding="utf-8") as f:
        config = json.load(f)
    sources_config = config.get("sources", config)
    tasks = build_rss_urls(sources_config)
    log(f"📋 {len(tasks)} 个抓取任务")

    all_items = fetch_all(tasks)
    if not all_items: log("\n❌ 无内容"); sys.exit(1)

    filtered, _, _ = pre_filter(all_items)
    if not filtered: log("\n⚠️ 过滤后无内容"); sys.exit(0)

    if args.dry_run:
        log("\n--- DRY RUN 预览 ---")
        for i, item in enumerate(filtered[:30]): log(f"[{i+1}] {item['platform']} | {item['source_name']} | {item['title'][:80]}")
        if len(filtered) > 30: log(f"... 还有 {len(filtered)-30} 条")
        return

    ai_input = build_ai_input(filtered)
    ai_result = call_deepseek(ai_input)

    stats = {"filtered": len(filtered), "ok_sources": len(set(it["source_name"] for it in all_items)), "total_sources": len(tasks)}

    if ai_result is None:
        content_md = f"🤖 AI 创作者机会雷达 · {datetime.now().strftime('%Y.%m.%d')}\n\n⚠️ AI 分析暂不可用。今日抓取 {len(filtered)} 条内容。\n\n请检查 DeepSeek API。"
        card = {"msg_type":"interactive","card":{"header":{"template":"blue","title":{"tag":"plain_text","content":"🤖 AI 创作者机会雷达"}},"elements":[{"tag":"markdown","content":content_md}]}}
    else:
        card = format_feishu(ai_result, stats)

    send_feishu(card)
    log("\n"+"="*50)
    log("✅ 完成")
    log("="*50)


if __name__ == "__main__":
    main()
