# 42 成交量实时排行榜

面向 [42.space](https://www.42.space/) 的交易者**成交量**实时排行榜。官方积分页 [42.space/points](https://www.42.space/points) 按积分排名；本项目改用同一套公开 Market API，按成交量（`sort_by=volume`）排序，并把疑似做市地址和散户分开统计。

演示页是纯静态文件，可直接放到 GitHub Pages。

这些做市标签是根据公开文档和交易行为做的推断。42.space **没有**公布具名做市商地址，页面不会把任何钱包写成已确认的官方做市商。

## 数据来源

| 项目 | 说明 |
| --- | --- |
| 接口 | `GET https://rest.ft.42.space/api/v1/market-data/leaderboard` |
| 官方站点里的用法 | 前端 JS 将 `NEXT_PUBLIC_MARKET_API_BASE_URL` 指到 `https://rest.ft.42.space`，对应「Best Traders」排行榜 |
| 查询参数 | `period`：`1d` / `7d` / `30d` / `all`（其他值会返回 `INVALID_PERIOD`）。和 `from`/`to` 互斥<br>`from`、`to`：`YYYY-MM-DD`，UTC 日历日，含首尾。文档：[Get trader leaderboard](https://docs.42.space/for-developers/rest-api-alpha/leaderboard/get-trader-leaderboard)<br>`sort_by`：`volume`（成交量）或 `pnl`（已实现盈亏，官网默认）<br>`limit`（最大 500）、`offset` 分页 |
| 每条记录字段 | `rank`、`walletAddress`、`totalVolume`、`pnl`、`pnlPct`、`winRate`、`tradeCount`、`profitableSells`、`losingSells`、`winningPredictions`、`losingPredictions` |
| 粒度 | **按交易者**的成交量，不是按市场/事件。同接口还提供按盈亏排序；市场列表在 `/api/v1/markets`（含每个市场的 `volume`） |
| 单位 | USDT。`pnlPct` 已经是百分数（`-0.5` 表示 -0.5%）。`winRate` 是 0 到 1 的比例 |
| 跨域 (CORS) | Market API 响应头为 `Access-Control-Allow-Origin: *`，预检允许 `GET`，因此 GitHub Pages 上的页面可以直接在浏览器里拉实时数据 |
| 积分榜 | `https://www.42.space/api/points/leaderboard` 带 `latestWeek`、`snapshotId`、`updatedAt`、`pointsChange`。该接口**没有**对第三方页面开放 CORS。`fetch.py` 读取它并写入快照，页面只读这份副本 |

`1d` / `7d` / `30d` 是接口自己的滚动窗口。本积分周用 `from`/`to`，不是这三档的别名。

## 全平台合计

上方四张卡片是**该窗口每一个交易者**的合计，不是表格里看见的前 200 行：

- 全平台交易者：分页里的 `pagination.totalResults`
- 全平台总成交量：已扫描交易者的 `totalVolume` 之和
- 做市与机器人成交量：Tier A + B + C 的成交量、人数和占比；高频机器人（Tier C）在同一张卡片里单列
- 散户成交量及占比：其余地址，并给出成交量最高的散户

24 小时、7 日、30 日由浏览器按 `limit=500` 把分页拉完，大约每 30 秒刷新。本积分周同样把分页拉完，并大约每 60 秒连同成交量拆分一起刷新。`all` 大约 6 万名交易者，不放在浏览器里反复扫。`fetch.py` 扫完后只保留前 200 行给表格，把全量合计写进 `data/leaderboard.json` 的 `periods.all.aggregate`。页面读这份合计，并写明快照的更新时间。表格前 200 名在 `all` 窗口仍然走实时接口。

启发式也跑在全部已扫描交易者上，不只是前 200 名。

## 钱包标签

可编辑的配置在 [`wallet-labels.json`](wallet-labels.json)。页面和 `fetch.py` 都读这个文件，地址比较忽略大小写。

| 分层 | 页面文字 | 怎么来的 |
| --- | --- | --- |
| Tier A | 疑似官方做市 | 固定 3 个地址。徽章提示里写明「竞速榜已排除」 |
| Tier B | 疑似做市/机器人 | 固定地址，外加下面的自动规则。`0x3cE29…` 只在 `all` 窗口使用名单 |
| Tier C | 疑似高频机器人 | 没有固定名单，只靠当前窗口的字段 |
| 身份 | Team / Community Mod / Treasury | 官方身份徽章。算散户，不算做市 |
| Agent | Agent · codex / claude / grok | `groups.agent` 里的三个钱包。不算散户，也不算做市 |
| 种子 | 42 系统/种子 | Blitz 接口 `excludeSeed` 排除的系统钱包。配置里的做市标签保留，页面改用这个徽章，不计入散户或做市。按规则不计积分 |

Tier A 的依据是竞速榜文档：[Get race leaderboard](https://docs.42.space/for-developers/rest-api-alpha/leaderboard/get-race-leaderboard)。文档写明竞速榜会排除部署时配置的做市钱包和财库。这 3 个地址在官方 Blitz 上大量成交，却没有出现在竞速榜里。这不是官方公布的名单。

Tier B 的名单来自同一种行为：一笔交易买下某个 Blitz 的全部结果，并且从不卖出。三个结果都买的话，预测胜率大约是 33%。`0x3cE29DA6f5fb915Cc92310d77EAe3e924427bb56` 只在「全部」窗口还符合这个行为，所以配置里把它限制为 `periods: ["all"]`。在 24 小时、7 日、30 日和本积分周，它既不会吃到名单标签，也不会被启发式重新标上。

身份徽章和做市标签不要混在一起。就算某个 Team 地址的数字碰巧符合启发式，也仍然算散户。

行高亮：Tier A 是琥珀色，Tier B 是紫色，Tier C 是青色。身份徽章不加行底色。

表格可以切「全部 / 仅散户 / 仅做市」。仅散户时多一列「散户排名」，按成交量在该窗口的散户里排，奖牌打在这一列。这个切换不改变上方的全平台卡片。`all` 窗口的筛选只作用在实时前 200 行上。

### 自动规则

数字都在 `wallet-labels.json` 的 `heuristics` 里，页面上的说明跟着这份配置走。

**疑似做市/机器人**（当前窗口同时满足）：

- `winRate` = 0
- 成交量 ≥ 1,000,000，或成交笔数 ≥ 5,000
- `pnlPct` 在 -0.8 和 -0.2 之间，含两端

**疑似高频机器人**（当前窗口同时满足，单独上色，计入做市/机器人，不计入散户）：

- 成交笔数 ≥ 20,000
- `winningPredictions + losingPredictions` ≤ 笔数的 5%
- `pnlPct` 在 -2 和 -0.8 之间，含两端

一个地址若两套规则都碰上，记为 Tier C。已经在 Tier A 名单里的地址保持 Tier A。

## 本积分周

进行中的积分周不是 `7d`。

**边界从哪里来**

- [42 Points](https://docs.42.space/getting-started/42-points/42-points) 写明每周循环是 **Thursday 00:00 UTC 到 Wednesday 23:59 UTC**，周三 UTC 结束后发放，本周积分要到下一次更新才出现。
- 积分接口没有 `/api/points/epoch`、`/season`、`/week` 这类端点（都是 404）。`GET /api/points/leaderboard` 返回 `latestWeek: {from, to}`，`to` 是不含在内的结束时刻。实现时读到的最近一个已发放周是 `2026-11-04T00:00:00.000Z` → `2026-11-12T00:00:00.000Z`。站点积分页的脚本用 `from` 到 `to - 1ms` 在 UTC 下格式化这周，没有把星期几写死。
- 这 8 天是第一周：2026-11-04 是周三，和官方账号宣布 Week 1 上线的日子重合；`to` 落在周四 00:00 UTC，和文档里的下一周开始重合。之后的周期按文档的 7 天周四规则，不把第一周的 8 天当成模板。
- 官方账号 [@42space](https://x.com/42space) 的公开帖：11 月 4 日宣布可以每周赚取 42 Points、Week 1 上线；11 月 13 日写「Week 1 Points have been distributed」，落在第一个周三收盘之后。本环境的 X 接口没有账单权限，这两条来自账号页的公开内容，不是 API 检索。

配置里只有一个起始值：`wallet-labels.json` 的 `pointsWeekStart`，现在是 `Thursday 00:00 UTC`。页面和 `fetch.py` 都用它推算「最近一个已经开始的这种时刻」，再加 7 天作为不含在内的结束时刻。结束时刻在页面上显示成前一分钟（周三 23:59），并同时给出东京时间（UTC+9）。

**成交量怎么算**

排行榜本身支持自定义日历窗口，所以本周不用活动流，也不用「全历史减周初基线」：

`GET /api/v1/market-data/leaderboard?sort_by=volume&from=<周四日期>&to=<周三日期>`

`from` / `to` 是 UTC 日历日，含首尾，正好盖住周四 00:00 UTC 到周三 23:59 UTC。实测把 `to` 设在未来的周三也能返回，结果和 `to` 设成今天相同，也就是截至目前的本周成交量。启发式用的是这一窗口自己的 `winRate`、`pnlPct` 和笔数。

徽章和倒计时旁边的说明用的就是这段口径。快照里的 `periods.week` 带有 `from`、`to` 和 `start`。周一切换之后，旧周的快照不会被当成新的一周。

## 积分周历史

页面打开后默认停在**进行中的积分周**。第 1 周是上线发放窗口（2026-11-04 00:00 UTC 到 2026-11-12 00:00 UTC）。第 2 周从 2026-11-12 起，之后按周四到周三继续。「上一周」从本周回到第 1 周。更早的周四只有成交量，收在下拉框的「积分前」里，不编号。和上线窗口重叠的 2026-11-05 那一周不单列。

每一周有两张榜，按钱包地址对在一起：

- **成交量排行榜**：该周成交量前 200 名，行上带积分排名、积分和做市徽章（这一周没有积分时，积分两列是「—」）
- **积分排行榜**：该周发放的积分。只有积分接口按**同一起止时间**返回了数据才展示，不拿别的周的数字来填

上方四张卡片始终是**这一周的全平台成交量**：交易者人数、总成交量、做市/机器人占比、散户成交量。做市和散户都不含 Agent，也不含 42 Blitz 种子钱包。

每一周另有一张成交量拆分：普通交易、Blitz 真实玩家、Blitz 42 种子、Agent。数字的算法和 `fetch.py` 相同：普通交易 = 排行榜总成交量 − Blitz 总成交量 − Agent。进行中的一周，页面打开后以及之后大约每 60 秒，浏览器直接请求排行榜（`from`/`to` 与 `fetch.py` 相同）、`/token-races/volume`（带和不带 `excludeSeed=true`）和 Agent 成交量（`/mining/agents` 的累计成交减去周文件里的周初基线；没有基线时改用 `/mining/history`）。四项都取到才替换屏幕上的数字，并显示「实时 · 更新于 HH:MM:SS (东京时间)」。任一请求失败就继续显示周文件里的数字，时间戳旁边标「缓存」，不用 0 填。每钱包的 Blitz 列始终用快照。标签页切到后台时暂停请求。已经结束的一周拆分写好就不再请求。总榜把各周相加，并扣掉 2026-11-04 这一天，因为这一天同时落在积分前的一周和第 1 周。没有上一份拆分时显示「暂无数据」。

拆分下面是这一周的 **Blitz 玩家**表。积分前的周不显示。

| 项目 | 说明 |
| --- | --- |
| 榜单 | `GET https://rest.ft.42.space/api/v1/token-races/leaderboard?from=YYYY-MM-DD&to=YYYY-MM-DD&sort_by=pnl&limit=500&offset=0&include_total=1` |
| 日期 | `from` / `to` 是 UTC 日历日，**含首尾**。积分周是周四到周三，例如第 3 周 `from=2026-11-19&to=2026-11-25`，第 4 周 `from=2026-11-26&to=2026-12-02` |
| `sort_by` | 只接受 `pnl`、`best_streak`、`wins`、`win_rate`。页面默认按盈亏，快照也按 `pnl` 拉取 |
| 分页 | `limit` 最大 500。超过 500 人时用 `offset` 继续。现在一周大约一两百到三百人，通常 1 次请求，最多再补 1 页 |
| 每行 | `walletAddress`、`rank`、`racePnl`（已结算、含手续费的 USDT）、`racesPlayed`、`wins`、`losses`、`winRate`（0 到 1）、`bestStreak`、`longestLossStreak` |
| 交易量 | `GET /api/v1/token-races/volume?from=<unix>&to=<unix>&user=0x…&excludeSeed=true`。`from` / `to` 是 unix 秒，**含起点、不含终点**，边界是周四 00:00 UTC。读 `userVolume`。单次大约 4–5 秒。这个接口没有「每人成交笔数」，表上的「轮次」是 `racesPlayed`（参与轮数），不是笔数 |
| 种子和 Agent | 竞速榜本身会排除种子。快照和页面仍会核对 `data/weeks/index.json` 的 `blitzSeedWallets`，以及 codex / claude / grok 这三个 Agent 钱包；对得上就标出来，不把他们算成普通玩家。种子的 `excludeSeed` 成交量是 0 |
| 跨域和限速 | 响应带 `Access-Control-Allow-Origin: *`，页面可以直接请求。限速按每秒 10 次来对待，不把每钱包成交量一次性打满 |
| 缺数据 | 没查到的交易量在 JSON 里是 `null`，页面显示「—」，不用 0 填 |

`fetch.py` 把这张榜写进该周文件的 `blitzPlayers`。已经结束的周沿用文件里已经有的 `userVolume`，不重复查。进行中的一周，以及还没有交易量的玩家，按盈亏名次和参与轮数靠前的优先查。这一段每钱包请求和上面的拆分归因是分开的，单次 Actions 运行最多大约 **3 分钟**（`BLITZ_PLAYER_BUDGET_S`），到点就停，剩下的下次再补。进行中的一周如果某个体积的记录已经超过 1 小时，下次运行会重查盈亏或轮次靠前的大约 25 名，其余继续用上次的数字。

进行中的一周，浏览器大约每 60 秒重拉同一条 leaderboard URL。成功时表头是「实时 · 更新于 HH:MM:SS (东京时间)」；失败就继续显示周文件，并标成「缓存」。实时模式下交易量用快照里已经有的值，另外按当前排序可见的前 20 行、每次最多 2 个并发，去补还没有交易量的地址。标签页在后台时不请求。

积分池图例分两栏。官方（[Agentic Mining](https://www.42.space/agentic-mining)）：交易 70%，NFT（Agent）池 20%（按已铸造 Pass 分，Agent 钱包本身不拿交易积分，第 1–2 周这一池固定 420,000 分），NFT Pass 排行榜 10%（第 3 周起，按单张 Pass 积分的前 300，不是按成交量）。推断：Token Race 15%（[2026-11-20 的宣布](https://x.com/42space/status/2123678749585514534)，单轮至少 $1，每天 10 轮，每周 3 天，单钱包有上限，没用完回到交易池），普通交易 55% 是从 70% 里扣掉这 15% 之后的推断，不是官方公布的比例。

表格在周视图多两列：该钱包的 Blitz 成交量，以及普通成交量（排行榜成交量减去 Blitz；Agent 再减去挖矿差额）。

周文件：

| 文件 | 内容 |
| --- | --- |
| `data/weeks/index.json` | 全部周的 `start` / `end`、合计、`hasPoints` |
| `data/weeks/<开始日期>.json` | 该周成交量前 200 名、全平台合计、积分榜（如果有），`breakdown` 成交量拆分，以及积分周的 `blitzPlayers` |

下拉框最上面还有「总榜（全部积分）」。积分用站点 Overall 的 `/api/points/leaderboard`（不带 `from`/`to`），包含第1周之前已经发放的积分。成交量用 `period=all`。没有这份总榜时，页面不会把各周积分加总来代替。文件是 `data/weeks/cumulative.json`，每次 `fetch.py` 都会重写。

`fetch.py` 仍从最早有成交的那天列出周四，但分成两组。上线日之前的周四是「积分前」，只保留成交量。积分周从 `wallet-labels.json` 的 `pointsLaunch` 起：第 1 周用那段起止时间，后面的周四从它的结束时刻继续编号。起止时间落在第 1 周内部的标准周四会删掉，不进列表。已经结束、并且积分状态已经确定的周，文件在就不会重拉。进行中的一周每次运行都刷新。刚结束、积分还可能在发放的 48 小时内也会再试，避免把「未发放」冻成永久结果。

积分接口没有按任意一周查询的参数。站点脚本在「Weekly」时只把 `latestWeek` 的 `from` / `to` 传回去，其他起止时间多数是 `400 bad_request`。对得上的窗口会分页拉完积分榜并写入对应的周文件；对不上的周 `hasPoints` 为 false，页面标成「仅成交量」。进行中的一周在发放前没有积分。

## 市场列表成交量

`fetch.py` 会把 `GET /api/v1/markets` 每个市场的 `volume` 加总（`limit` 大于 100 时接口要求 `Accept-Encoding: gzip`）。这是市场上的累计名义金额，没有 1d/7d/30d 分档，所以只和「全部」窗口的交易者成交量对照。

相差不超过 1% 时，全平台总成交量卡片会写明两者一致。差得更多时，卡片上的大数字仍是交易者 `totalVolume` 之和——散户和做市的占比以它为分母——并另写市场列表的合计，不把那个数字换成卡片标题。

两边经常对不上，因为它们不是同一个计数：

- 交易者 `totalVolume` 是每个钱包自己的买入加卖出。把所有钱包加起来，一笔撮合会落在参与的那一侧或两侧。
- 市场 `volume` 是这个市场自己的累计成交，通常一笔只记一次。
- Tier B 那种「一笔买下全部结果」的铸造，记在交易者成交量里的方式和记在市场 `volume` 里的方式不一定相同。
- 市场列表是当前还能查到的市场。已经不在列表里的历史市场，成交量还留在交易者的 `all` 统计里。
- 文档里的 `pnlPct` 约等于 `pnl / totalVolume × 100`，所以交易者成交量是「该钱包经手的名义金额」，不是平台抽完水之后的净额。

具体差多少会随快照变。`data/leaderboard.json` 的 `markets.difference_pct` 和 `markets.consistent` 是最近一次扫描的结果。2026-11-14 那次扫描里，2,879 个市场的 `volume` 合计约 1.224 亿美元，全部时间的交易者成交量合计约 8.469 亿美元，市场合计大约是交易者合计的 14%，相差 -85.6%，因此页面不会把市场合计当成全平台总成交量。

## 更新方式

两套通道同时工作：

1. **浏览器实时请求**（优先）  
   24 小时、7 日、30 日大约每 30 秒刷新，会把交易者分页拉完再算合计。进行中的积分周大约每 60 秒刷新排行榜、「本周成交量拆分」和「Blitz 玩家」。`all` 只实时拉前 200 行，合计用快照。标签页在后台时这些定时请求都暂停，切回来再补一次。成功时状态徽标显示「实时全量」，`all` 显示「表格实时 · 合计来自快照」。拆分或 Blitz 玩家成功时写「实时 · 更新于 HH:MM:SS (东京时间)」；失败时写「缓存」并沿用周文件里的时间戳。玩家表的交易量在实时模式下仍用快照，只为可见的前 20 行补缺失值。

2. **GitHub Actions 快照**（兜底，也是 `all` 的全平台合计）  
   `.github/workflows/update.yml` 的 cron 是 `*/10 * * * *`，另外可以手动 `workflow_dispatch`。GitHub 会拖延 scheduled workflow，这个仓库的定时运行经常隔几小时才真正开始，不是每 10 分钟一次。跑起来之后 `fetch.py` 把每个窗口扫完，表格只留前 200 行，合计、Tier C 地址、市场成交量对照和积分接口的 `latestWeek` 写入 `data/leaderboard.json`，并补齐 `data/weeks/`。已经结束、文件里又已经有全平台拆分的周直接沿用，不再逐钱包重查。实时接口失败时，页面改读这些 JSON。

`fetch.py` 只用 Python 标准库，不需要 API Key 或其他密钥。全量扫描 `all` 大约要一两分钟。每一周都会写全平台拆分（普通交易、Blitz 真实玩家、Blitz 42 种子、Agent）。逐钱包的 Blitz 只查积分周（2026-11-04 起）；积分前的周不逐钱包查。还没有拆分的周，每钱包查询单次运行最多大约 5 分钟，到点就停，没查到的钱包不填 0。这份逐钱包缓存写在运行器本地，不会随仓库留下，所以已经结束的周不再靠「下次继续」把同一批地址重查一遍。进行中的一周若每钱包数字已经齐，定时任务只重拉全平台拆分。

另外，每个积分周（含进行中的一周）都会拉 Blitz 玩家榜，写进该周文件的 `blitzPlayers`。榜本身是 1 到 2 次请求。补 `userVolume` 另有大约 3 分钟的上限，和上面那 5 分钟的归因预算分开，所以一次 Actions 运行在原有耗时之外最多再增加约 3 分钟。已经写进周文件的交易量，周结束后直接复用。只重拉玩家表、不动成交量排行榜时：

```bash
python3 fetch.py --blitz-players
```

本地生成快照：

```bash
python3 fetch.py
```

本地预览页面（避免直接打开 HTML 时相对路径失效）：

```bash
python3 -m http.server 8080
# 打开 http://127.0.0.1:8080/
```

## 启用 GitHub Pages

1. 把本仓库推送到 GitHub（例如 `42-volume-leaderboard`）。
2. 打开仓库 **Settings → Pages**。
3. Source 选 **Deploy from a branch**，Branch 选 `main`（或 `master`），目录选 `/ (root)`，保存。
4. 打开 **Settings → Actions → General**，确保 workflow 有写仓库的权限（Actions 需要把更新后的 JSON 推回来）。本仓库的 workflow 已声明 `permissions: contents: write`。
5. 第一次可在 **Actions** 里手动跑一次 **Update volume leaderboard**，确认 `data/leaderboard.json` 已生成，并且 `periods.all.aggregate` 有全平台合计。
6. 几分钟后访问 `https://<用户名>.github.io/<仓库名>/`。

如果 Pages 构建用的是 `docs/` 目录，请把 `index.html`、`wallet-labels.json` 和 `data/` 一并放到 `docs/`，并相应修改 workflow 的输出路径。

## 项目结构

```
.
├── index.html                 # 中文深色排行榜（可排序、可搜索、自动刷新、全平台合计、本积分周）
├── wallet-labels.json         # 分层、身份徽章、启发式和积分周起始时刻
├── fetch.py                   # 全量分页、合计、市场成交量对照，并写入 JSON
├── data/leaderboard.json      # 各窗口前 200 名，外加全平台合计
├── data/weeks/index.json      # 积分周列表、合计、hasPoints
├── data/weeks/<YYYY-MM-DD>.json
├── .github/workflows/update.yml
└── README.md
```
