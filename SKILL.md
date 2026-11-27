---
name: meituanjingdui
description: 通过用户本机已登录常规 Chrome 的美团外卖 H5 门店页（h5.waimai.meituan.com -> cactivityapi-sc.waimai.meituan.com），以单账号低频礼貌配速采集闪购/超市便利/闪电仓门店商品与 SKU 数据，自动导出锁定的 27 列 `{MMDD}{店铺名称}.xlsx` 比价表与离线 `{MMDD}{店铺名称}_数据分析报告.md`（支持跨零点防分裂、滚动 24h 配额保护、老号/新号分档、大店单号智能二级分类配额规划、69 码本店同品比价与缺品选品，以及按需提取新品副图/详情长图/属性表）。
---

# 美团闪购竞对采集、比价与货盘分析（合规省力版）

## 核心原则与内置省力防坑能力

1. **合规底线（零反爬伪造、受阻即停）**：
   - 仅读取用户在常规 Chrome（默认调试端口 `9222`）自行登录并打开的目标门店页；不注入模拟触摸/鼠标轨迹、不修改 Cookie/设备指纹、不自动换号轮换。
   - 遇到验证页面（`verify.meituan.com` / `418`）立即保存当前 Excel 与分析报告并以退出码 `42` 停机；遇到访问限制（`HTTP 403 / 406`）立即保存并以退出码 `43` 停机。
2. **六大实战省力与防坑固化（默认全自动生效）**：
   - **极简一行启动**：自动识别 `9222` 当前打开的门店名称、地址与 `poi_id_str`，未传 `--out-dir` 时自动创建 `./crawl_data/{店铺名称}` 独立断点目录。
   - **老号 / 新号分档保护（`--account-tier`）**：默认 `veteran`（老号：`68~88s` 间隔、`≤38次/小时`、满 30 次休眠 `10 分钟`、24h 软上限 `92 / 85`）；若使用刚注册或低活跃新号，传 `--account-tier conservative`（`75~95s`、`≤32次/小时`、24h 软上限 `28 / 35`），防止新号低阈值过早受限。
   - **滚动 24 小时配额账本（防跨零点超额）**：`daily_quota.json` 按真实滚动 `24 小时（86400秒）` 窗口统计调用次数，夜间跨过 `00:00` 不会误清零配额导致超发锁号。
   - **跨零点 / 隔日续跑锁定同一份 Excel**：首次启动自动将日期 `MMDD` 固化进 `progress.json`，跨零点或次日断点续采始终更新同一份 `{MMDD}{店铺名称}.xlsx`，绝不分裂出两个日期文件。
   - **页面重载后 `poi_id_str` 动态重加密自适应**：美团 H5 刷新或隔日重载后会对同一门店重新生成加密 `poi_id_str`；脚本自动校验 `店名 + 详细地址` 100% 一致后无感衔接新 ID，同时严防同名异店误爬。
   - **大店（3,000~4,000 品）单号配额智能规划**：扫完一级分类后自动计算精确剩余批次数 $\sum \lceil \text{rem}_i / 30 \rceil$ 与完工 ETA；若超出剩余 `smooth` 额度，自动优先用剩余 `sputag` 额度点击高产二级子分类首屏分流大类，把 `smooth` 额度留给无子分类的尾部类目（如 `未分类`、`神价`），最大化单账号一次性采全率并提升第 8 列 `店内二级分类` 还原度。
   - **完工/停机自动生成离线数据分析报告**：每次收尾自动调用 `scripts/analyze_competitor_store.py` 输出 `{MMDD}{店铺名称}_数据分析报告.md`。

---

## 1. 一键启动流程

### Step 1：确认依赖与常规 Chrome 门店页
```bash
python3 -c "import websockets, openpyxl; print('DEPS_OK')" || python3 -m pip install websockets openpyxl
```
若常规 Chrome 未开 `9222` 调试端口，启动后手动登录 [美团外卖 H5](https://h5.waimai.meituan.com/waimai/mindex/home)（优先使用活跃老号，勿开 F12 手机模拟）并点进目标门店页：
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
```

### Step 2：核验当前门店（可选）
```bash
python3 ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --inspect-only
```

### Step 3：启动单账号省力采集（自动建断点目录 + 增量导出 27 列 Excel + 自动出分析报告）
```bash
# 老号一键启动（自动锁定当前打开的门店，Excel 与分析报告输出到当前目录）
python3 ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --excel-out-dir .

# 若使用新号/低活跃号，加 conservative 档位保护账号
python3 ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --account-tier conservative --excel-out-dir .
```
*(如需显式指定分店与目录，也可附带 `--poi-name`、`--poi-id-str`、`--out-dir`、`--date-mmdd`。)*

---

## 2. 离线货盘分析与本店 69 码同品对标选品（0 网络请求）

采集完成后会自动生成 `{MMDD}{店铺名称}_数据分析报告.md`。若你有本店导出的商品 Excel（含 69 条码列），可随时离线一键生成**同条码比价清单 + 竞对热销我方缺品 TOP 表**：

```bash
python3 ~/.codex/skills/meituanjingdui/scripts/analyze_competitor_store.py \
  --excel "./1126快趣逛超市龙文店.xlsx" \
  --our-excel "./本店商品导出.xlsx"
```

---

## 3. 27 列硬锁定 Excel 导出规范（`export_competitor_excel.py`）

严禁修改列名、列数与单表结构（表名固定 `全量商品竞对明细`、`规格类型` 仅限 `单规格`/`多规格`、多规格浅黄底高亮，完整兼容 `unify_price` v5 真实折扣价与子规格独立到手价，字段对账见 [product_fields.md](references/product_fields.md)）：

| 列号 | 标准列名 | 列号 | 标准列名 |
| :---: | :--- | :---: | :--- |
| 1 | `SPU_ID` | 15 | `点赞数` |
| 2 | `SKU_ID` | 16 | `实时库存` |
| 3 | `UPC条形码(69码)` | 17 | `原价(元)` |
| 4 | `商品名称` | 18 | `直接折扣价(元)` |
| 5 | `规格类型` | 19 | `神价/到手价(元)` |
| 6 | `子规格名称(spec)` | 20 | `到手价说明` |
| 7 | `店内一级分类` | 21 | `竞对有效基准价(元)` |
| 8 | `店内二级分类` | 22 | `促销标签` |
| 9 | `店内分类排名` | 23 | `满减商品券` |
| 10 | `美团一级类目` | 24 | `限购份数` |
| 11 | `美团二级类目` | 25 | `最小起购数` |
| 12 | `美团三级类目名称` | 26 | `售卖属性(温度/提示)` |
| 13 | `月销量(数值)` | 27 | `高清无水印主图URL` |
| 14 | `想买人数` | — | — |

---

## 4. 二阶段按需提取新品轮播副图、详情长图与属性表

```bash
python3 ~/.codex/skills/meituanjingdui/scripts/fetch_spu_details.py \
  --port 9222 \
  --spus-jsonl "./crawl_data/店铺名称/spus.jsonl" \
  --top-n-sales 30 \
  --out-jsonl "./crawl_data/店铺名称/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx"
```
