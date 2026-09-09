# data/ 重建手册（全量数据目录恢复）

> 适用场景：备份仓库 clone 到新机器后，工程根 `data/` 为空或损坏，需要从零重建行情数据与推送配置。
> 运行前提：`pip install akshare pandas requests`（Python 3.9+）。全部脚本在工程根目录执行（`cd 工程根`，即含 `stock_bot/`、`data/` 的目录），脚本写相对路径 `data/...`。
> 若源机器 `data/` 仍在，直接整目录拷贝过来最省事（见主 README）；本手册用于不可拷贝时的重建。

## 一、重建清单总表

| 数据项 | 用途 | 重建方式 | 脚本 | 耗时/备注 |
|---|---|---|---|---|
| `data/stock_universe.csv` | 全市场主板选股池（剔除ST/退市/创业/科创/北交） | 运行 `_fetch_universe.py` | 本目录 `_fetch_universe.py` | 秒级，akshare 拉列表 |
| `data/fin_problem_codes.csv`（写回 universe） | 财报问题股过滤（亏损/资不抵债） | universe 建好后运行 `_filter_universe_fin.py` | 本目录 `_filter_universe_fin.py` | 需先有 universe；可选步骤 |
| `data/csv/{code}.csv` | 全市场个股日线（2018 起，12列） | 运行 `_fetch_history.py` | 本目录 `_fetch_history.py` | 约 2600+~3000+ 只，约 30 分钟（多进程6）；依赖 universe |
| `data/csv/hs300_pe.csv` | 沪深300 估值（S7/大盘） | 运行 `download_data.py`（或单独拉取） | 本目录 `download_data.py` | akshare 乐咕源 |
| `data/csv/hsly_pb.csv` | 上证红利估值（S8） | 同上 `download_data.py` | 同上 | 同上 |
| `data/csv/sh000001.csv` | 上证指数日线（大盘过滤） | 一次性生成初档（见下节）后由服务自动补当日 | 无独立脚本，见下方命令 | 全历史约 1990 起 |
| `data/notify_config.json` | 推送渠道+凭证 | 手动填写 | 模板 `notify_config.example.json` | 无凭证则推送静默跳过 |
| `data/strategy_daily.json` | 各策略每日记录 | 启动服务自动重建 / 手动触发 build | `stock_bot/app.py`（build 逻辑） | 30~60s+ 联网回放 |

以下均为服务运行期自动生成、无需手工重建（缺失会自动再建）：
`data/today_picks.json`、`data/dynamic_pool_index.json`、`data/dynamic_pool_bt.json`、
`data/pool_index.json`、`data/notify_signal_sent.json`、`data/notify_brief_sent.json`、
`data/last_build_date.json`、`data/last_intra_build_date.json`、`data/emotion_state.json`。

> 说明：本目录另含 `data/imgs`、`items_extracted.json`、`zh_answers.json` 等**知识库素材**，不属于股票服务运行必需；若需完整还原一并从源机拷贝即可，与回放/推送无关。

## 二、sh000001 初档生成

`history_engine.refresh_sh_index()` 只会在文件存在时补缺失交易日，**不会从零建档**，因此新机器需先一次性生成全历史初档。格式：`date,open,high,low,close,volume`（utf-8-sig）。示例：

```python
# 在工程根执行
import akshare as ak, pandas as pd, os
os.makedirs('data/csv', exist_ok=True)
df = ak.stock_zh_index_daily(symbol='sh000001')   # 上证指数全历史
d = pd.DataFrame({
    'date': pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d'),
    'open': df['open'], 'high': df['high'], 'low': df['low'],
    'close': df['close'], 'volume': df['volume'],
})
d.to_csv('data/csv/sh000001.csv', index=False, encoding='utf-8-sig')
print('rows:', len(d))
```

> 若接口不可用/格式变动，也可在源机拷贝 `data/csv/sh000001.csv`（约 500KB）替代。

## 三、推荐重建顺序

1. `python _fetch_universe.py`  → 得到 `stock_universe.csv`
2. `python _filter_universe_fin.py` → 财报过滤（可选，建议）
3. `python _fetch_history.py` → 全市场日线（约 30 分钟，中途断点续跑：已存在的 csv 会跳过）
4. 按第二节生成 `data/csv/sh000001.csv`
5. `python download_data.py` → 生成 `hs300_pe.csv` / `hsly_pb.csv`（其 15 只样板股可跳过，全市场已覆盖）
6. `copy data_rebuild/notify_config.example.json data/notify_config.json` 并填入真实凭证
7. 启动服务 `cd stock_bot && start.bat` → 收盘后/手动触发 build 自动滚出 `strategy_daily.json`

## 四、脚本与数据源

| 脚本 | 数据源 | 说明 |
|---|---|---|
| `_fetch_universe.py` | akshare `stock_info_a_code_name` | 主板白名单过滤 |
| `_filter_universe_fin.py` | akshare `stock_yjbb_em` | 需按当季报告期改 `date=`（默认 20260331/20251231） |
| `_fetch_history.py` | akshare 新浪源（多进程避开 mini_racer） | 读 `stock_universe.csv`，断点续跑 |
| `download_data.py` | akshare 乐咕源 | 15 只样板股 + 指数 PE/PB |

> 财务过滤脚本内的报告期参数（`date='20260331'`）会随季过期，重建时按最新财报期手动更新。
