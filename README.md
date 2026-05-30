# 🤖 AI 机会雷达 - 日报系统

每天早上自动抓取 30 个 X 账号 + 20 个 YouTube 频道的最新内容，通过 DeepSeek API 进行 AI 筛选、评分、翻译、汇总，生成中文日报并推送到飞书。

**完全免费运行，无需服务器，无需写代码。**

---

## 🚀 5 分钟部署

### 你需要准备

| 准备项 | 说明 | 去哪里获取 |
|--------|------|-----------|
| GitHub 账号 | 免费 | [github.com](https://github.com) |
| DeepSeek API Key | 已充值即可 | [platform.deepseek.com](https://platform.deepseek.com) |
| 飞书机器人 Webhook | 免费 | 飞书 → 搜索「飞书机器人」→ 创建 |

### 步骤

#### 1. Fork 本仓库

点击右上角 **Fork** 按钮，把仓库复制到你自己的 GitHub 账号下。

#### 2. 设置 Secrets

进入你 Fork 后的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Secret 名称 | 填入什么 |
|-------------|---------|
| `DEEPSEEK_API_KEY` | 你的 DeepSeek API Key（格式：`sk-xxxx`） |
| `FEISHU_WEBHOOK` | 你的飞书机器人 Webhook 地址（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`） |

#### 3. 启用 Workflow

进入你 Fork 后的仓库 → **Actions** → 点击 **I understand my workflows, go ahead and enable them** → 点击左侧 **AI 雷达日报** → **Enable workflow**

#### 4. 手动测试运行一次

在 **AI 雷达日报** workflow 页面 → 点击 **Run workflow** → **Run workflow**

等待约 2-3 分钟后，检查你的飞书是否收到日报。

#### ✅ 完成！

之后每天 **北京时间 09:00** 自动运行，**约 10:00 前**你将收到日报。

---

## 🔧 高级用法

### 修改信息源

编辑 `sources.json`，添加或删除信息源：

```json
{
  "name": "名称",
  "platform": "x",           // "x" 或 "youtube"
  "username": "sama",        // X 用户名（不带 @）
  "channel": "ColeMedin",   // YouTube 频道 ID（URL 中 @ 后面的部分）
  "category": "ai-startup",  // 分类：ai-startup / ai-workflow / agent / ai-content / productivity
  "note": "补充说明"
}
```

修改后提交到 GitHub，下次运行自动生效。

### 修改推送时间

编辑 `.github/workflows/daily-report.yml`，修改 cron 表达式：

```yaml
- cron: '0 1 * * *'  # UTC 01:00 = 北京时间 09:00
```

### 更换 AI 模型

如需使用 DeepSeek R1（推理模型）或其他模型，修改 `main.py` 开头的 `DEEPSEEK_MODEL` 环境变量：

在 GitHub Secrets 中添加：
- `DEEPSEEK_MODEL` = `deepseek-reasoner`

---

## 📋 日报样例

参见仓库中的 [SAMPLE.md](SAMPLE.md)

---

## ❓ 常见问题

### Q: 为什么我没有收到日报？
1. 检查 GitHub Actions 是否启用（Settings → Actions → Allow all actions）
2. 检查 Secrets 是否正确设置
3. 查看 Actions 日志是否有错误信息
4. 检查飞书机器人是否在目标群中

### Q: RSSHub 公共实例不稳定怎么办？
系统内置了 3 个 RSSHub 实例自动切换，单个实例不稳定不影响运行。如果所有实例都不可用，可以自建 RSSHub（需要服务器，约 ¥50/月）。

### Q: 每天消耗多少 DeepSeek token？
约 30,000-60,000 输入 token + 3,000-5,000 输出 token。按 DeepSeek V3 价格（¥1/百万输入 token, ¥2/百万输出 token），每天约 ¥0.04-0.07。

### Q: 如何修改日报的 AI 分析风格？
编辑 `main.py` 中的 `SYSTEM_PROMPT` 变量，修改 AI 系统提示词。

---

## 🛠 文件说明

```
ai-radar/
├── .github/workflows/
│   └── daily-report.yml    # GitHub Actions 定时工作流
├── main.py                  # 主脚本（抓取→过滤→AI分析→推送）
├── sources.json             # 50个信息源配置
├── requirements.txt         # Python 依赖
└── README.md                # 本文件
```

---

## ⚠️ 注意事项

- **X 内容依赖 RSSHub**：RSSHub 通过网页抓取获取 X 内容，偶尔可能失败。系统会自动重试。
- **YouTube 内容稳定**：YouTube 原生支持 RSS，稳定可靠。
- **GitHub Actions 免费额度**：每月 2000 分钟，日报每天约 5 分钟 = 150 分钟/月，完全够用。
- **DeepSeek API 费用**：每天约 ¥0.05，月费约 ¥1.5。
