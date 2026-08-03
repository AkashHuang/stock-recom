#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股周线金叉选股 —— 数据拉取 + 指标计算 + 静态页面渲染

筛选条件（来自 config.json，可配置）:
  - 板块: 证券/电力/电网/医药/广告营销/化工/锂电池
  - 现价 < price_max (默认 10)
  - 周线金叉: 当前 MA5 > MA20
  - 每板块 Top10: 主排序=近4周涨幅(动量), 次排序=金叉强度(MA5离MA20幅度)
  - 默认排除 ST、排除北交所 (可在 config 开关)
  - 数据源: 腾讯财经(qt.gtimg.cn 行情 + web.ifzq.gtimg.cn 周线)。该源在境外 GitHub Actions 可达;
    Eastmoney 在境外被墙, 故板块成分(baked codes)写在 config.json, 可用 `python update.py --resolve`
    在 Eastmoney 可达的环境(如本机)刷新为精确成分。

用法:
  python update.py            # 真实拉数(Tencent)
  python update.py --mock     # 生成本地预览用的合成数据(无需联网)
  python update.py --resolve  # 从 Eastmoney 刷新各板块成分写回 config.json(需 Eastmoney 可达)
  MOCK=1 python update.py     # 同上(合成数据)
"""

import os
import sys
import json
import time
import math
import random
import datetime as dt
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
INDEX_PATH = os.path.join(HERE, "index.html")
SNAPSHOT_PATH = os.path.join(HERE, "data", "snapshot.json")

UP_COLOR = "#ef232a"   # 涨 红 (A股惯例)
DOWN_COLOR = "#14b143" # 跌 绿


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# 工具: 移动平均 / MACD / 动量
# ----------------------------------------------------------------------------
def compute_indicators(close: pd.Series, vol: pd.Series, fast=5, slow=20):
    close = close.astype(float)
    ma_fast = close.rolling(fast).mean()
    ma_slow = close.rolling(slow).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    last = -1
    ma5_v = float(ma_fast.iloc[last]) if not pd.isna(ma_fast.iloc[last]) else None
    ma20_v = float(ma_slow.iloc[last]) if not pd.isna(ma_slow.iloc[last]) else None

    golden = (ma5_v is not None and ma20_v is not None and ma5_v > ma20_v)
    golden_strength = ((ma5_v - ma20_v) / ma20_v * 100) if (ma5_v and ma20_v) else 0.0

    n = len(close)
    momentum = ((close.iloc[-1] / close.iloc[-5] - 1) * 100) if n >= 5 else float("nan")

    return {
        "ma5": [None if pd.isna(x) else round(float(x), 3) for x in ma_fast],
        "ma20": [None if pd.isna(x) else round(float(x), 3) for x in ma_slow],
        "macd": {
            "dif": [None if pd.isna(x) else round(float(x), 4) for x in dif],
            "dea": [None if pd.isna(x) else round(float(x), 4) for x in dea],
            "hist": [None if pd.isna(x) else round(float(x), 4) for x in hist],
        },
        "golden": golden,
        "golden_strength": round(golden_strength, 2),
        "momentum_4w": round(float(momentum), 2) if not math.isnan(momentum) else None,
        "last_volume": int(vol.iloc[-1]) if len(vol) else 0,
    }


# ----------------------------------------------------------------------------
# 真实数据拉取 (AkShare)
# ----------------------------------------------------------------------------
def is_trading_day(tz="Asia/Shanghai"):
    """判断今天(北京时间)是否为交易日; 非交易日返回 False -> 跳过更新。
    简化实现: 跳过周末 + 2026 年大陆法定节假日静态表(无需联网)。"""
    today = dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).date()
    if today.weekday() >= 5:  # 周六/周日
        return False
    holidays_2026 = {
        dt.date(2026, 1, 1), dt.date(2026, 1, 2),
        dt.date(2026, 2, 16), dt.date(2026, 2, 17), dt.date(2026, 2, 18),
        dt.date(2026, 4, 3), dt.date(2026, 4, 4), dt.date(2026, 4, 5), dt.date(2026, 4, 6),
        dt.date(2026, 5, 1), dt.date(2026, 5, 2), dt.date(2026, 5, 3),
        dt.date(2026, 6, 19), dt.date(2026, 6, 20), dt.date(2026, 6, 21),
        dt.date(2026, 9, 25), dt.date(2026, 9, 26), dt.date(2026, 9, 27),
        dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 3),
        dt.date(2026, 10, 4), dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 7),
    }
    if today in holidays_2026:
        return False
    return True


def http_get(url, timeout=25, tries=4, binary=False):
    """带重试的 GET; 失败抛异常。"""
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def code_to_secid(code):
    """纯数字代码 -> 腾讯 secid 前缀 (sh/sz/bj)。"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code


def fetch_quotes_tencent(codes):
    """批量拉腾讯实时行情: 现价(字段3) + 市盈率TTM(字段39)。返回 {plain_code:{name,price,pe}}。"""
    out = {}
    for i in range(0, len(codes), 80):
        batch = [code_to_secid(c) for c in codes[i:i + 80]]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            txt = http_get(url)
        except Exception as e:
            print(f"[warn] 行情批量拉取失败: {e}")
            continue
        for line in txt.replace("\r", "").split("\n"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            var, val = line.split("=", 1)
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if not val:
                continue
            f = val.split("~")
            if len(f) < 40:
                continue
            secid = var.replace("v_", "").strip()
            plain = secid[2:] if secid[:2] in ("sh", "sz", "bj") else secid
            name = f[1]
            try:
                price = float(f[3])
            except Exception:
                price = None
            try:
                pe = float(f[39]) if f[39] not in ("", "-") else None
            except Exception:
                pe = None
            out[plain] = {"name": name, "price": price, "pe": pe}
    return out


def fetch_weekly_tencent(secid):
    """腾讯周线(前复权): 返回 {dates,open,close,high,low,vol}; 顺序 [date,open,close,high,low,volume]。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={secid},week,,,60,qfq"
    try:
        txt = http_get(url)
        d = json.loads(txt)
    except Exception as e:
        print(f"[warn] {secid} 周线失败: {e}")
        return None
    data = d.get("data", {}) or {}
    node = data.get(secid) if secid in data else (list(data.values())[0] if data else None)
    if not node:
        return None
    arr = node.get("qfqweek") or node.get("week") or []
    res = {"dates": [], "open": [], "close": [], "high": [], "low": [], "vol": []}
    for row in arr:
        if len(row) < 6:
            continue
        res["dates"].append(row[0])
        res["open"].append(float(row[1]))
        res["close"].append(float(row[2]))
        res["high"].append(float(row[3]))
        res["low"].append(float(row[4]))
        res["vol"].append(float(row[5]))
    if not res["close"]:
        return None
    return res


def run_real(cfg):
    sc = cfg["screener"]
    price_max = float(sc["price_max"])
    exclude_st = bool(sc["exclude_st"])
    exclude_bse = bool(sc["exclude_bse"])
    top_n = int(sc["top_n"])
    fast, slow = int(sc["sma_fast"]), int(sc["sma_slow"])
    weeks = int(sc["weeks_history"])

    sectors_out = []
    for spec in cfg["sectors"]:
        codes = spec.get("codes") or []
        print(f"[info] 板块: {spec['name']} (baked {len(codes)} 只)")
        if not codes:
            sectors_out.append({"name": spec["name"], "type": spec["type"],
                                "matched": 0, "stocks": []})
            continue

        quotes = fetch_quotes_tencent(codes)
        candidates = []
        for code in codes:
            q = quotes.get(code)
            if not q:
                continue
            name = q["name"]
            price = q["price"]
            if exclude_st and "ST" in name.upper():
                continue
            if exclude_bse and (code.startswith("8") or code.startswith("4")):
                continue
            if price is None or price >= price_max or price <= 0:
                continue
            candidates.append((code, name, price, q["pe"]))
        print(f"[info]  候选(现价<{price_max}, 排除ST/北交所): {len(candidates)} 只")

        def worker(cand):
            code, name, price, pe = cand
            kl = fetch_weekly_tencent(code_to_secid(code))
            if not kl or len(kl["close"]) < slow:
                return None
            ind = compute_indicators(pd.Series(kl["close"]), pd.Series(kl["vol"]), fast, slow)
            if not ind["golden"]:
                return None
            n = len(kl["close"])
            ohlc = [[round(kl["open"][i], 3), round(kl["close"][i], 3),
                     round(kl["low"][i], 3), round(kl["high"][i], 3)] for i in range(n)]
            return {
                "code": code, "name": name, "price": round(price, 3), "pe": pe,
                "momentum_4w": ind["momentum_4w"],
                "golden_strength": ind["golden_strength"],
                "volume": ind["last_volume"],
                "signal": "金叉",
                "kline": {
                    "dates": kl["dates"], "ohlc": ohlc,
                    "ma5": ind["ma5"], "ma20": ind["ma20"],
                    "volume": [int(v) for v in kl["vol"]],
                    "macd": ind["macd"],
                },
            }

        stocks = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(worker, c) for c in candidates]
            for f in as_completed(futs):
                r = f.result()
                if r:
                    stocks.append(r)

        def sort_key(s):
            m = s["momentum_4w"]
            ok = (m is not None and not (isinstance(m, float) and math.isnan(m)))
            return (0 if ok else 1, -(m if ok else 0), -s["golden_strength"])
        stocks.sort(key=sort_key)
        top = stocks[:top_n]
        for i, s in enumerate(top, 1):
            s["rank"] = i

        sectors_out.append({
            "name": spec["name"], "type": spec["type"],
            "matched": len(stocks), "stocks": top,
        })
        print(f"[info]  金叉命中 {len(stocks)} 只, 展示 Top{len(top)}")

    return sectors_out


def resolve_constituents(cfg):
    """从 Eastmoney(push2) 拉取每个板块成分写回 config.json 的 codes 字段。
    仅在本机 Eastmoney 可达时运行(如用户本地/中国网络); GitHub Actions 上 Eastmoney 被墙, 用 baked codes。"""
    print("[info] --resolve: 尝试从 Eastmoney 刷新板块成分...")
    for spec in cfg["sectors"]:
        bk = spec.get("bk")
        if not bk:
            print(f"[warn] {spec['name']} 无 bk 字段, 跳过")
            continue
        url = f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=2000&po=1&np=1&fltt=2&invt=2&fid=f3&fs=b:{bk}&fields=f12,f14"
        try:
            d = json.loads(http_get(url))
            diff = d["data"]["diff"]
            spec["codes"] = [x["f12"] for x in diff]
            print(f"[info] {spec['name']}: {len(spec['codes'])} 只")
        except Exception as e:
            print(f"[warn] {spec['name']} 解析失败(可能 Eastmoney 不可达): {e}")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("[done] 已写回 config.json 的 codes 字段")


def parse_pe(val):
    try:
        if val is None:
            return None
        s = str(val).strip()
        if s in ("", "--", "None", "nan", "-"):
            return None
        v = float(s)
        if v <= 0 or math.isnan(v):
            return None
        return round(v, 2)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 合成数据 (本地预览, 无需联网)
# ----------------------------------------------------------------------------
def run_mock(cfg):
    random.seed(20260803)
    sc = cfg["screener"]
    top_n = int(sc["top_n"])
    weeks = int(sc["weeks_history"])
    price_max = float(sc["price_max"])

    # 生成连续周日期 (周五)
    end = dt.date(2026, 7, 31)
    dates = []
    d = end
    while len(dates) < weeks:
        if d.weekday() == 4:  # Friday
            dates.append(d)
        d -= dt.timedelta(days=1)
    dates = dates[::-1]
    date_str = [d.isoformat() for d in dates]

    sectors_out = []
    for spec in cfg["sectors"]:
        stocks = []
        n_candidates = random.randint(18, 40)
        for i in range(n_candidates):
            code = f"{random.choice(['60','00','30'])}{random.randint(1000,3999)}"
            name = f"{spec['name']}样本{i+1:02d}"
            price = round(random.uniform(3.0, price_max - 0.01), 2)

            # 随机游走生成周收盘, 确保末段金叉
            base = price * random.uniform(0.8, 1.1)
            closes = [base]
            for _ in range(weeks - 1):
                closes.append(max(0.5, closes[-1] * (1 + random.uniform(-0.06, 0.07))))
            # 末段拉抬形成金叉
            for j in range(weeks - 8, weeks):
                closes[j] *= (1 + random.uniform(0.005, 0.02))
            closes[-1] = price
            closes = np.array(closes)

            opens = closes * (1 + np.random.uniform(-0.02, 0.02, weeks))
            highs = np.maximum(opens, closes) * (1 + np.random.uniform(0, 0.02, weeks))
            lows = np.minimum(opens, closes) * (1 - np.random.uniform(0, 0.02, weeks))
            vol = (np.random.uniform(0.5, 3.0, weeks) * 1e6).astype(int)

            ind = compute_indicators(pd.Series(closes), pd.Series(vol),
                                     int(sc["sma_fast"]), int(sc["sma_slow"]))
            if not ind["golden"]:
                # 强行金叉
                ind["golden"] = True

            ohlc = [[round(opens[k], 3), round(closes[k], 3), round(lows[k], 3), round(highs[k], 3)]
                    for k in range(weeks)]
            pe = round(random.uniform(8, 45), 2)
            momentum = ind["momentum_4w"] if ind["momentum_4w"] is not None else round(random.uniform(2, 25), 2)

            stocks.append({
                "code": code, "name": name, "price": price, "pe": pe,
                "momentum_4w": momentum,
                "golden_strength": ind["golden_strength"] if ind["golden_strength"] else round(random.uniform(1, 8), 2),
                "volume": int(vol[-1]), "signal": "金叉",
                "kline": {
                    "dates": date_str, "ohlc": ohlc,
                    "ma5": ind["ma5"], "ma20": ind["ma20"],
                    "volume": [int(v) for v in vol.tolist()],
                    "macd": ind["macd"],
                },
            })

        def sort_key(s):
            m = s["momentum_4w"]
            return (-(m if m is not None else 0), -s["golden_strength"])
        stocks.sort(key=sort_key)
        top = stocks[:top_n]
        for i, s in enumerate(top, 1):
            s["rank"] = i
        sectors_out.append({
            "name": spec["name"], "type": spec["type"],
            "matched": len(stocks), "stocks": top,
        })
    return sectors_out


# ----------------------------------------------------------------------------
# 渲染 index.html
# ----------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股周线金叉选股</title>
<script src="assets/echarts.min.js"></script>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --border:#30363d;
    --text:#c9d1d9; --muted:#8b949e; --up:#ef232a; --down:#14b143;
    --accent:#58a6ff; --gold:#f0b90b;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.5;}
  header{padding:18px 22px 10px;border-bottom:1px solid var(--border);}
  h1{margin:0 0 6px;font-size:20px;font-weight:600;letter-spacing:.5px;}
  .meta{color:var(--muted);font-size:12.5px;}
  .config-tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;}
  .tag{background:var(--panel2);border:1px solid var(--border);border-radius:20px;
    padding:3px 11px;font-size:12px;color:var(--muted);}
  .tag b{color:var(--text);font-weight:600;}
  nav.sector-tabs{display:flex;flex-wrap:wrap;gap:8px;padding:14px 22px 4px;}
  .tab{background:var(--panel);border:1px solid var(--border);border-radius:8px;
    padding:8px 16px;cursor:pointer;color:var(--muted);font-size:13.5px;
    transition:all .15s;user-select:none;}
  .tab:hover{border-color:var(--accent);color:var(--text);}
  .tab.active{background:var(--accent);border-color:var(--accent);color:#0d1117;font-weight:600;}
  .tab .cnt{font-size:11px;opacity:.8;margin-left:5px;}
  section.panel{padding:8px 22px 30px;}
  .sector-head{margin:12px 0 8px;font-size:15px;font-weight:600;}
  .sector-head .sub{color:var(--muted);font-weight:400;font-size:12.5px;margin-left:8px;}
  .layout{display:grid;grid-template-columns:680px 1fr;gap:18px;align-items:stretch;}
  @media(max-width:1100px){.layout{grid-template-columns:1fr;}}
  .table-wrap{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:auto;height:100%;}
  table{width:100%;border-collapse:collapse;font-size:12.5px;}
  thead th{background:var(--panel2);color:var(--muted);font-weight:600;text-align:right;
    padding:9px 6px;position:sticky;top:0;white-space:nowrap;}
  thead th:first-child,thead th:nth-child(2),thead th:nth-child(3){text-align:left;}
  tbody td{padding:8px 6px;border-top:1px solid var(--border);text-align:right;white-space:nowrap;}
  tbody td:first-child,tbody td:nth-child(2),tbody td:nth-child(3){text-align:left;}
  tbody tr{cursor:pointer;transition:background .12s;}
  tbody tr:hover{background:var(--panel2);}
  tbody tr.sel{background:rgba(88,166,255,.16);}
  tbody tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--accent);}
  .up{color:var(--up);} .down{color:var(--down);}
  .badge{display:inline-block;background:rgba(240,185,11,.15);color:var(--gold);
    border:1px solid rgba(240,185,11,.4);border-radius:5px;padding:1px 7px;font-size:11px;}
  .right-col{display:flex;flex-direction:column;gap:10px;height:100%;min-width:0;}
  .detail-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
  .detail-head .nm{font-size:17px;font-weight:600;}
  .detail-head .cd{color:var(--muted);font-size:13px;}
  .detail-head .kv{color:var(--muted);font-size:12.5px;}
  .detail-head .kv b{color:var(--text);font-weight:600;}
  .detail{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;
    flex:1;display:flex;flex-direction:column;min-height:0;}
  #chart{width:100%;flex:1;min-height:420px;}
  footer{padding:18px 22px 40px;color:var(--muted);font-size:12px;border-top:1px solid var(--border);}
  footer a{color:var(--accent);text-decoration:none;}
  .empty{color:var(--muted);padding:24px;text-align:center;}
</style>
</head>
<body>
<header>
  <h1>A股周线金叉选股</h1>
  <div class="meta">更新时间：<span id="updated"></span> · 数据源 腾讯财经 · 仅供研究，非投资建议</div>
  <div class="config-tags" id="configTags"></div>
</header>
<nav class="sector-tabs" id="sectorTabs"></nav>
<section class="panel">
  <div class="sector-head" id="sectorHead"></div>
  <div class="layout">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>排名</th><th>代码</th><th>名称</th><th>现价</th><th>市盈率TTM</th>
          <th>近4周%</th><th>金叉强度%</th><th>周成交量</th><th>信号</th>
        </tr></thead>
        <tbody id="stockRows"></tbody>
      </table>
    </div>
    <div class="right-col">
      <div class="detail-head" id="detailHead"></div>
      <div class="detail"><div id="chart"></div></div>
    </div>
  </div>
</section>
<footer>
  本页由 GitHub Actions 定时（北京时间每个交易日 16:00）自动生成 · 源码：
  <a href="https://github.com/AkashHuang/stock-recom" target="_blank" rel="noopener">AkashHuang/stock-recom</a><br>
  筛选逻辑：板块 + 现价&lt;阈值 + 周线 MA5&gt;MA20（金叉）；Top10 按近4周涨幅→金叉强度排序。默认排除 ST 与北交所。
</footer>

<script>
window.SNAPSHOT = __SNAPSHOT__;
(function(){
  const S = window.SNAPSHOT;
  const UP='#ef232a', DOWN='#14b143';
  let activeSector=0, activeStock=0, chart=null;

  document.getElementById('updated').textContent = S.generated_at || '';

  // 配置标签
  (function(){
    const c=S.config_summary||{};
    const el=document.getElementById('configTags');
    const tags=[
      ['板块', (c.sectors||[]).length+' 个'],
      ['现价阈值', '< '+ (c.price_max??10)],
      ['金叉', 'MA5 > MA20'],
      ['Top', '每板块 '+ (c.top_n??10)],
      ['排序', '近4周涨幅 → 金叉强度'],
      ['排除', 'ST'+(c.exclude_bse?' + 北交所':'')]
    ];
    el.innerHTML = tags.map(t=>`<span class="tag">${t[0]} <b>${t[1]}</b></span>`).join('');
  })();

  function fmtVol(v){
    if(v==null) return '-';
    if(v>=1e8) return (v/1e8).toFixed(2)+'亿手';
    if(v>=1e4) return (v/1e4).toFixed(1)+'万手';
    return v+'手';
  }
  function fmtNum(x,d=2){return x==null?'—':Number(x).toFixed(d);}
  function pct(x){if(x==null)return '—';const s=x>=0?'+':'';return `<span class="${x>=0?'up':'down'}">${s}${x.toFixed(2)}%</span>`;}

  function buildTabs(){
    const nav=document.getElementById('sectorTabs');
    nav.innerHTML='';
    S.sectors.forEach((sec,i)=>{
      const b=document.createElement('div');
      b.className='tab'+(i===activeSector?' active':'');
      b.innerHTML=`${sec.name}<span class="cnt">${sec.matched}只</span>`;
      b.onclick=()=>{activeSector=i;activeStock=0;renderSector();};
      nav.appendChild(b);
    });
  }

  function renderSector(){
    buildTabs();
    const sec=S.sectors[activeSector];
    document.getElementById('sectorHead').innerHTML=
      `${sec.name} <span class="sub">命中 ${sec.matched} 只金叉股 · 展示 Top ${sec.stocks.length}</span>`;
    const tb=document.getElementById('stockRows');
    tb.innerHTML='';
    if(!sec.stocks.length){
      tb.innerHTML='<tr><td colspan="9" class="empty">该板块当前无符合条件的金叉股</td></tr>';
      document.getElementById('detailHead').innerHTML='';
      if(chart){chart.dispose();chart=null;}
      return;
    }
    sec.stocks.forEach((s,i)=>{
      const tr=document.createElement('tr');
      tr.className=(i===activeStock?'sel':'');
      tr.innerHTML=`
        <td>${s.rank}</td><td>${s.code}</td><td>${s.name}</td>
        <td class="up">${fmtNum(s.price)}</td>
        <td>${s.pe==null?'亏损':fmtNum(s.pe)}</td>
        <td>${pct(s.momentum_4w)}</td>
        <td class="up">${fmtNum(s.golden_strength)}</td>
        <td>${fmtVol(s.volume)}</td>
        <td><span class="badge">${s.signal}</span></td>`;
      tr.onclick=()=>{activeStock=i;renderTableSel();renderDetail();};
      tb.appendChild(tr);
    });
    renderDetail();
  }

  function renderTableSel(){
    const rows=document.getElementById('stockRows').children;
    for(let i=0;i<rows.length;i++) rows[i].classList.toggle('sel', i===activeStock);
  }

  function renderDetail(){
    const sec=S.sectors[activeSector];
    const s=sec.stocks[activeStock];
    if(!s) return;
    document.getElementById('detailHead').innerHTML=
      `<span class="nm">${s.name}</span><span class="cd">${s.code}</span>`+
      `<span class="kv">现价 <b class="up">${fmtNum(s.price)}</b></span>`+
      `<span class="kv">市盈率TTM <b>${s.pe==null?'亏损':fmtNum(s.pe)}</b></span>`+
      `<span class="kv">近4周 <b class="${s.momentum_4w>=0?'up':'down'}">${s.momentum_4w==null?'—':(s.momentum_4w>=0?'+':'')+s.momentum_4w.toFixed(2)+'%'}</b></span>`+
      `<span class="kv">金叉强度 <b class="up">${fmtNum(s.golden_strength)}%</b></span>`;
    renderChart(s);
  }

  function renderChart(s){
    const k=s.kline;
    if(chart) chart.dispose();
    chart=echarts.init(document.getElementById('chart'),'dark');
    const dates=k.dates, ohlc=k.ohlc, vol=k.volume, macd=k.macd;
    const vols=vol.map((v,i)=>({value:v,itemStyle:{color:ohlc[i][1]>=ohlc[i][0]?UP:DOWN}}));
    const hist=macd.hist.map(v=>({value:v,itemStyle:{color:v>=0?UP:DOWN}}));
    const option={
      backgroundColor:'transparent', animation:false,
      legend:{data:['MA5','MA20','DIF','DEA'],top:2,textStyle:{color:'#c9d1d9'},itemWidth:14,itemHeight:8},
      tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#161b22',borderColor:'#30363d',textStyle:{color:'#c9d1d9'}},
      axisPointer:{link:[{xAxisIndex:'all'}]},
      grid:[
        {left:56,right:18,top:34,height:'46%'},
        {left:56,right:18,top:'62%',height:'15%'},
        {left:56,right:18,top:'81%',height:'15%'}
      ],
      xAxis:[
        {type:'category',data:dates,gridIndex:0,axisLabel:{show:false},axisLine:{lineStyle:{color:'#30363d'}}},
        {type:'category',data:dates,gridIndex:1,axisLabel:{show:false},axisLine:{lineStyle:{color:'#30363d'}}},
        {type:'category',data:dates,gridIndex:2,axisLine:{lineStyle:{color:'#30363d'}},axisLabel:{color:'#8b949e',fontSize:11}}
      ],
      yAxis:[
        {scale:true,gridIndex:0,splitLine:{lineStyle:{color:'#21262d'}},axisLabel:{color:'#8b949e'}},
        {scale:true,gridIndex:1,splitLine:{show:false},axisLabel:{color:'#8b949e'}},
        {scale:true,gridIndex:2,splitLine:{show:false},axisLabel:{color:'#8b949e'}}
      ],
      dataZoom:[
        {type:'inside',xAxisIndex:[0,1,2],start:55,end:100},
        {type:'slider',xAxisIndex:[0,1,2],bottom:2,start:55,end:100,height:16,textStyle:{color:'#8b949e'},borderColor:'#30363d'}
      ],
      series:[
        {name:'K线',type:'candlestick',data:ohlc,xAxisIndex:0,yAxisIndex:0,
          itemStyle:{color:UP,color0:DOWN,borderColor:UP,borderColor0:DOWN}},
        {name:'MA5',type:'line',data:k.ma5,xAxisIndex:0,yAxisIndex:0,smooth:true,showSymbol:false,lineStyle:{width:1.2,color:'#f0b90b'}},
        {name:'MA20',type:'line',data:k.ma20,xAxisIndex:0,yAxisIndex:0,smooth:true,showSymbol:false,lineStyle:{width:1.2,color:'#58a6ff'}},
        {name:'成交量',type:'bar',data:vols,xAxisIndex:1,yAxisIndex:1},
        {name:'MACD',type:'bar',data:hist,xAxisIndex:2,yAxisIndex:2},
        {name:'DIF',type:'line',data:macd.dif,xAxisIndex:2,yAxisIndex:2,showSymbol:false,lineStyle:{width:1,color:'#f0b90b'}},
        {name:'DEA',type:'line',data:macd.dea,xAxisIndex:2,yAxisIndex:2,showSymbol:false,lineStyle:{width:1,color:'#58a6ff'}}
      ]
    };
    chart.setOption(option);
  }

  window.addEventListener('resize',()=>{if(chart)chart.resize();});
  renderSector();
})();
</script>
</body>
</html>
"""


def build_snapshot(sectors, cfg):
    sc = cfg["screener"]
    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M (北京时间)"),
        "config_summary": {
            "price_max": sc["price_max"],
            "top_n": sc["top_n"],
            "exclude_bse": sc["exclude_bse"],
            "sectors": [s["name"] for s in cfg["sectors"]],
        },
        "sectors": sectors,
    }


def render_html(snapshot):
    js = json.dumps(snapshot, ensure_ascii=False)
    return HTML_TEMPLATE.replace("__SNAPSHOT__", js)


def main():
    cfg = load_config()
    if "--resolve" in sys.argv:
        resolve_constituents(cfg)
        return
    mock = ("--mock" in sys.argv) or (os.environ.get("MOCK") == "1")

    if not mock:
        if not is_trading_day(cfg["screener"]["timezone"]):
            print("[skip] 今天非交易日，跳过更新（保留上次页面）。")
            sys.exit(0)

    print(f"[info] 模式: {'MOCK' if mock else 'REAL'}")
    sectors = run_mock(cfg) if mock else run_real(cfg)
    snapshot = build_snapshot(sectors, cfg)

    # 安全护栏: REAL 模式下若全部板块 0 命中(多为网络被墙/超时), 不覆盖已有页面, 避免发布空白页。
    if not mock:
        total_matched = sum(s.get("matched", 0) for s in sectors)
        if total_matched == 0:
            print("[warn] REAL 模式全部板块 0 命中，疑似数据源不可达；保留上次页面，不覆盖。")
            sys.exit(0)

    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(snapshot))
    print(f"[done] 生成 index.html + data/snapshot.json (板块 {len(sectors)})")


if __name__ == "__main__":
    main()
