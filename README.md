# linkedin-cdp-auto-connect

> 给一个公司清单，全自动批量发 LinkedIn connect 邀请（每天 ≤ 10 条）。

[English README](./README_EN.md)

---

只需要一个公司名清单文件（CSV / TXT / XLSX），脚本会：

1. 自动在 LinkedIn 搜索每家公司的关键负责人（CEO / Founder / CTO / VP / Head of Partnerships ...）
2. 为每个候选人生成个性化的 connect 招呼语
3. 通过 [Playwright](https://playwright.dev/python/) + Chrome DevTools Protocol (CDP) 全自动发送邀请
4. 自动控制每天 ≤ 10 条、随机 35–60 秒间隔，避开 LinkedIn 风控

最初是作为 [WorkBuddy](https://workbuddy.dev) skill 打包的，但脚本本身在 macOS 上独立可用。

## 为什么必须走 CDP？

LinkedIn 有反自动化机制：

- **AppleScript 注入 JS 调 `.click()`**：不带 user-activation 标记，LinkedIn 直接拒绝弹邀请框
- **页面右上角"More → Connect"**：实际是个 `<a href="/preload/custom-invite/?vanityName=...">` 链接，点击事件被拦，必须直接 `goto` 这个 href
- **CDP + Playwright**：派发的是真实浏览器事件，带 user-activation，LinkedIn 才认

这套方案是踩了一圈坑后唯一稳定走通的路径。

## 输入：一个公司清单文件

下面三种格式都支持，**只看第一列**：

| 文件名             | 格式                  | 第一列                         |
| ---------------- | ------------------- | --------------------------- |
| `companies.csv`  | CSV                 | `Aylo`、`MindGeek`、…         |
| `companies.txt`  | TXT（一行一个）           | `Aylo`                      |
| `targets.xlsx`   | XLSX（首行可有可无表头）       | 第一列 = 公司名                   |

仓库自带样例：`templates/companies.csv`。

## 前置依赖（第一次跑之前必须装）

这个 skill 是**自包含**的，不依赖任何其他 WorkBuddy 浏览器 skill/插件（不需要 `agent-browser`、`playwright-cli` 或 MCP 浏览器 skill）。那些 skill 驱动的是另一个浏览器；本 skill 直接通过 CDP 接管你自己已登录的 Chrome。真正的依赖是系统级的：

| 依赖 | 作用 | 安装 |
|---|---|---|
| **Google Chrome**（桌面版） | 被 CDP 接管的浏览器 | google.com/chrome |
| **Python 3** | 跑脚本 | macOS 自带，或 Anaconda |
| **playwright**（Python 包） | CDP 自动化库 | `pip install playwright` |
| **openpyxl**（Python 包） | 读 `.xlsx` 公司清单 | `pip install openpyxl` |

一行装齐：

```bash
pip install playwright openpyxl
```

> **不要跑 `playwright install`。** 本 skill 用 `connect_over_cdp` 接管已存在的 Chrome，不用 Playwright 自带的 Chromium，所以不需要下载浏览器那一步。
>
> 装了 WorkBuddy 的浏览器 skill ≠ 装了 Python 的 `playwright` 包，两者是不同环境。务必执行上面的 `pip install`。
>
> 仅支持 macOS。launchd 定时（第 4 步）不需要额外装东西。

## 流水线

```
公司清单文件 ──► find_leads.py ──► queue.jsonl
                                     │
                                     ▼
                               send_daily.py ──► sent.jsonl + runs/run_*.json
                                     │
                                     ▼
                              （LinkedIn 邀请 + 个性化 note 已发送）
```

### 第 1 步：启动专用 Chrome（每个会话一次）

```bash
bash scripts/start_chrome.sh
```

会用 `--remote-debugging-port=9222` 在 `~/.linkedin-chrome` 这个独立 `--user-data-dir` 上拉起 Chrome。**首次**启动需要在弹出的 Chrome 窗口里手动登录一次 LinkedIn；之后这个 profile 保持登录态，后续运行直接复用。

> 为什么要用独立 user-data-dir？Chrome 拒绝在默认 profile 上开启远程调试，9222 端口会静默 404。

验证：

```bash
curl -s http://127.0.0.1:9222/json/version
```

应返回包含 `Browser`、`Protocol-Version`、`webSocketDebuggerUrl` 的 JSON。

### 第 2 步：为每家公司找出关键人物的 LinkedIn 主页

```bash
python scripts/find_leads.py \
    --input /path/to/companies.csv \
    --out-dir ~/.linkedin-outreach
```

每家公司：

1. 打开 `https://www.linkedin.com/search/results/people/?keywords=<公司名>`
2. 抓所有可见的 `/in/<vanity>` 链接和周边文本
3. 按目标头衔（CEO / Founder / CTO / VP / Head of Partnerships ...）命中、是否含公司名、出现位置打分
4. 输出：
   - `~/.linkedin-outreach/leads.json` — 完整候选池（含打分）
   - `~/.linkedin-outreach/queue.jsonl` — 每家公司一行，**最高分** 候选 `{company, name, title, url, vanity, note}`，准备好发送

可重复执行：已经写进 `queue.jsonl` 的公司会跳过。

也可以手动编辑 `queue.jsonl`，换更合适的人或者删掉某行，再继续。

### 第 3 步：发送当日批次（自动停在每日上限）

```bash
python scripts/send_daily.py \
    --out-dir ~/.linkedin-outreach \
    --daily 10
```

它会做的事：

1. 通过 CDP 接管 Chrome
2. 读 `queue.jsonl`，跳过已经在 `sent.jsonl` 里的，并算出今天已经发过几条
3. 最多发送 `(--daily − 今天已发数)` 条邀请：
   - 直接 `goto https://www.linkedin.com/preload/custom-invite/?vanityName=<vanity>`，**唯一**能稳定弹出邀请框的方式
   - 等待 *可见* 的 `div[role="dialog"]`，且文案包含 `Add a note to your invitation`（或 `How do you know` / `invitation limit`）
   - 点 `Add a note` → 在 textarea 填入 note → 点 `Send invitation`（兜底 `Send`）
4. 每条结果 append 到 `sent.jsonl`，整次运行明细写到 `runs/run_<时间戳>.json`
5. 每条之间随机睡 35–60 秒

可能看到的状态：

- `sent` + `note_added: true` — 邀请已带 note 实际发出
- `already_pending` — 之前就邀请过，自动跳过
- `requires_email` / `weekly_limit` / `no_invite_dialog` — 被拦或限流，不计入"已发"

### 第 4 步：（可选）launchd 每天定时跑

模板：`templates/com.linkedin.outreach.daily.plist`。安装：

```bash
SKILL_DIR=$(pwd)
RUN=$SKILL_DIR/scripts/run_daily.sh
sed \
  -e "s|__RUN_DAILY__|$RUN|g" \
  -e "s|__HOUR__|22|g" \
  -e "s|__MINUTE__|0|g" \
  $SKILL_DIR/templates/com.linkedin.outreach.daily.plist \
  > ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
launchctl load ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
```

`scripts/run_daily.sh` 会：

- 确保专用 Chrome 已起（调 `start_chrome.sh`，幂等）
- 调 `send_daily.py`，按配置的每日上限发
- 日志写到 `~/.linkedin-outreach/runs/launchd_<时间戳>.log`

停用：

```bash
launchctl unload ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
```

立即手动触发一次（不用等到点）：

```bash
launchctl start com.linkedin.outreach.daily
```

> 注意：macOS **完全休眠** 时 launchd 不会唤醒电脑跑这个任务。建议笔电插着电源，并让屏幕休眠保持在不进入磁盘休眠的档位。

## 输出目录结构 `~/.linkedin-outreach/`

```
~/.linkedin-outreach/
├── leads.json        # find_leads.py 产生的全量候选池（已排序）
├── queue.jsonl       # 待发队列：每家公司一行（可手动编辑）
├── sent.jsonl        # 仅追加：每次发送的尝试记录
└── runs/             # 每次运行的 JSON 明细 + launchd shell 日志
```

## 仓库结构

```
linkedin-cdp-auto-connect/
├── README.md                                 # 中文说明（默认）
├── README_EN.md                              # English version
├── SKILL.md                                  # WorkBuddy skill 元定义
├── scripts/
│   ├── start_chrome.sh                       # 启动带 CDP 的专用 Chrome
│   ├── find_leads.py                         # 发现每家公司的关键负责人主页
│   ├── send_daily.py                         # 通过 CDP 发当天邀请批次
│   ├── run_daily.sh                          # launchd 包装脚本
│   └── cdp_auto_connect.py                   # 一次性固定列表参考脚本
└── templates/
    ├── companies.csv
    └── com.linkedin.outreach.daily.plist
```

## 踩坑清单（强烈建议看完再用）

- **默认 user-data-dir 不能开远程调试**。Chrome 静默打 `DevTools remote debugging requires a non-default data directory`，9222 直接 404。必须用独立目录（`start_chrome.sh` 已处理）。
- **AppleScript 派发 `.click()` 一定无效**。LinkedIn 要 user-activation 事件；AppleScript 注入的 click 只会让你看到隐藏的字幕设置 dialog，邀请框不会弹。必须走 CDP + Playwright。
- **页面上很少有直接的 Connect 按钮**。一度连接显示 `Message`，三度大多显示 `Follow`。Connect 都藏在 "More" 下拉里，是个 `<a role="menuitem" href="/preload/custom-invite/?vanityName=…">`。**别去模拟点菜单 — 直接 `goto` 这个 href。**
- **隐藏的 dialog 会干扰判断**。LinkedIn 默认就 render 了 `<div role="dialog" aria-label="Modal Window">`（视频播放错误）和 `<div aria-label="Caption Settings Dialog">`，即使不可见。扫 `innerText` 之前先用 `getBoundingClientRect()` 过滤宽高 > 0。
- **每日/每周上限**。免费账号大约每周 100 条上限。`--daily ≤ 10`，每条 35–60s 随机间隔是默认设置。
- **"How do you know X" / 邮箱验证弹窗**。LinkedIn 要求填邮箱或关系类型时，关闭并跳过 — 别瞎填。
- **note 超过 200 字会让 Send 按钮静默禁用**。免费账号 note 上限 200，Premium 300。`find_leads.py` 自动截到 200。
- **发现是尽力而为**。低调或敏感行业的公司可能找不到高置信度匹配 — `find_leads.py` 只写非零分的候选。**第一次** 在新清单上跑 `send_daily.py` 之前，建议先人工过一遍 `queue.jsonl`。

## 免责声明

LinkedIn 的 [用户协议](https://www.linkedin.com/legal/user-agreement) 明确禁止自动化。本项目仅作为 CDP 驱动浏览器自动化的研究/教学示例。**风险自负**：批量外联可能导致账号被限制或封禁。默认参数已经做了相对保守的限流（每天 ≤ 10 条邀请，35–60 秒随机间隔）。

## License

MIT
