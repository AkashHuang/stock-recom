# A股周线金叉选股 (stock-recom)

静态选股报表，由 GitHub Actions 每个交易日北京时间 16:00 自动生成并发布到 GitHub Pages。

## 筛选逻辑（config.json 可配置）

- 板块：证券 / 电力 / 电网 / 医药 / 广告营销 / 化工 / 锂电池
- 现价 `<` `price_max`（默认 10 元）
- 周线金叉：当前 `MA5 > MA20`
- 每板块 Top10：主排序 = 近4周涨幅（动量），次排序 = 金叉强度（MA5 相对 MA20 幅度）
- 默认排除 ST 股 与 北交所（config 开关）
- 数据源：AkShare

## 页面交互

- 顶部一排 **板块标签**，点击切换当前板块（只显示一个板块的清单）
- 下方 **Top10 清单**（表格），点击任一行 → 右侧详情面板显示该股票的
  **周K线（含 MA5/MA20）+ 成交量 + MACD**（ECharts，可缩放）

## 仓库结构

```
config.json                      筛选配置（板块/价阈值/开关）
update.py                        拉数(AkShare) → 计算指标 → 渲染 index.html
requirements.txt                 akshare / pandas / numpy
.github/workflows/stock-screener.yml   定时工作流（周一~五 北京16:00）
index.html                       生成产物（commit 回仓库，GitHub Pages 托管）
data/snapshot.json               生成产物，完整数据备查
assets/echarts.min.js            ECharts 本地内置（离线可看）
```

## 本地预览

```bash
pip install -r requirements.txt
python update.py --mock     # 生成合成数据预览，无需联网
# 用浏览器打开 index.html
```

## 免责声明

本页仅供研究与学习，所有筛选结果不构成任何投资建议。
