# 42 成交量实时排行榜

面向 [42.space](https://www.42.space/) 的交易者**成交量**实时排行榜。官方积分页 [42.space/points](https://www.42.space/points) 按积分排名；本项目改用同一套公开 Market API，按成交量（`sort_by=volume`）排序。

演示页是纯静态文件，可直接放到 GitHub Pages。

## 数据来源

| 项目 | 说明 |
| --- | --- |
| 接口 | `GET https://rest.ft.42.space/api/v1/market-data/leaderboard` |
| 官方站点里的用法 | 前端 JS 将 `NEXT_PUBLIC_MARKET_API_BASE_URL` 指到 `https://rest.ft.42.space`，对应「Best Traders」排行榜 |
| 查询参数 | `period`：`1d` / `7d` / `30d` / `all`（其他值会返回 `INVALID_PERIOD`）<br>`sort_by`：`volume`（成交量）或 `pnl`（已实现盈亏，官网默认）<br>`limit`、`offset` 分页 |
| 每条记录字段 | `rank`、`walletAddress`、`totalVolume`、`pnl`、`pnlPct`、`winRate`、`tradeCount`、`profitableSells`、`losingSells`、`winningPredictions`、`losingPredictions` |
| 粒度 | **按交易者**的成交量，不是按市场/事件。同接口还提供按盈亏排序；市场列表在 `/api/v1/markets`（含每个市场的 `volume`） |
| 单位 | USDT |
| 跨域 (CORS) | Market API 响应头为 `Access-Control-Allow-Origin: *`，预检允许 `GET`，因此 GitHub Pages 上的页面可以直接在浏览器里拉实时数据 |
| 积分榜 | `https://www.42.space/api/points/leaderboard` 是积分快照，**没有**对第三方源开放 CORS，本页不依赖它 |

接口本身不返回交易者成交量的 24 小时涨跌幅，页面展示的是所选时间窗口内的累计成交量、已实现盈亏和胜率。

## 更新方式

两套通道同时工作：

1. **浏览器实时请求**（优先）  
   `index.html` 每 30 秒请求一次上述 API（接口本身有约 30 秒的 CDN 缓存）。成功时状态徽标显示「实时 API」。

2. **GitHub Actions 快照**（兜底）  
   `.github/workflows/update.yml` 每 10 分钟（以及手动 `workflow_dispatch`）运行 `fetch.py`，把四个时间窗口各前 200 名写入 `data/leaderboard.json` 并提交。若实时接口失败，页面会自动改读这份 JSON，徽标显示「本地快照」。

`fetch.py` 只用 Python 标准库，不需要 API Key 或其他密钥。

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
5. 第一次可在 **Actions** 里手动跑一次 **Update volume leaderboard**，确认 `data/leaderboard.json` 已生成。
6. 几分钟后访问 `https://<用户名>.github.io/<仓库名>/`。

如果 Pages 构建用的是 `docs/` 目录，请把 `index.html` 和 `data/` 一并放到 `docs/`，并相应修改 workflow 的输出路径。

## 项目结构

```
.
├── index.html                 # 中文深色排行榜页面（可排序、可搜索、自动刷新）
├── fetch.py                   # 拉取成交量并写入 JSON
├── data/leaderboard.json      # 四个时间窗口的快照
├── .github/workflows/update.yml
└── README.md
```
