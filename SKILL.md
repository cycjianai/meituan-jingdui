---
name: meituanjingdui
description: 通过用户本机已登录常规 Chrome 的美团外卖 H5 门店页（h5.waimai.meituan.com -> cactivityapi-sc.waimai.meituan.com），强制执行慢速休眠配速（68~88s/次、≤38次/小时、满30次休10分钟、滚动24h上限92/85）采集闪购/超市便利/闪电仓门店全量商品与 SKU 数据，自动导出锁定 27 列 `{MMDD}{店铺名称}.xlsx` 比价表与 `{MMDD}{店铺名称}_数据分析报告.md`（支持跨零点防分裂、动态分类位移自愈、老号/新号分档、大店单号智能二级分类配额规划、69 码同品比价与按需提取新品详情图）。
---

# 美团闪购竞对采集、比价与货盘分析（强制慢速休眠版）

## 【最高优先级强制红线】慢速休眠与单号配额铁律（每次执行 100% 强制遵守）

以下规则是保证**一个账号稳定跑出整店几千款商品最终成绩表、不触发网关封锁**的核心生命线，所有 AI Agent 每次执行时**必须无条件遵守，严禁变通或绕开**：

1. **严禁自写快速循环或裸调页面接口**：
   - 采集门店商品**必须且只能**调用本技能官方脚本 `scripts/crawl_h5_store.py`（二阶段单品详情只能调用 `scripts/fetch_spu_details.py`）。
   - **绝对禁止**另写临时 Python/JS 脚本在后台循环裸调 `requestSpusAndSortedIds`、`requestSmoothSpus` 或 `productInfoV2`；除 `--inspect-only`（纯读页面内存、0 网络请求）外，**不允许脱离慢速休眠与 24h 配额账本发送任何试探请求**。
2. **强制慢速休眠底线（已在代码层物理焊死，传参也无法调快）**：
   - **老号档（`--account-tier veteran`，默认主力档）**：
     - **单次间隔**：每次请求前**强制纯休眠 `68 ~ 88 秒`**（代码硬下限 `≥ 68s`，传入任何更小数值都会被代码强制拉回 `68s`）；
     - **短时限流**：任意 `2 分钟（125 秒）` 内最多 `≤ 2~3 次`；
     - **小时上限**：任意 `62 分钟` 滑动窗口硬锁 **`≤ 38 次`**；
     - **波次深休**：每累计满 **`30 次`** 请求，**强制纯休眠 `10 分钟（600 秒）`**；
     - **滚动 24h 单号配额硬上限**：1 号门分类接口 `sputag ≤ 92 次`、2 号门下拉翻页接口 `smooth ≤ 85 次`（两扇门合计 `177 次`，稳跑约 5~5.5 小时，单老号一天可稳采约 4,000~4,600 款 SPU / 7,000~8,000 行多规格 SKU）。
   - **新号 / 低活跃号档（`--account-tier conservative`）**：
     - 若用户使用的是新注册或低活跃账号，必须传 `--account-tier conservative`（强制 `75 ~ 95 秒` 间隔、`≤ 32 次/小时`、满 `25 次` 休眠 `11 分钟`、滚动 24h 上限 `sputag ≤ 28 / smooth ≤ 35`）。不确定账号新老时先向用户确认一句。
3. **后台耐心长跑（严禁嫌慢改快）**：
   - 大店采集天然耗时 **2.5 ~ 5.5 小时**。启动后台任务后静默等待系统完成通知即可，**严禁嫌慢去缩短休眠秒数或取消 10 分钟波次深休**（一旦过快把 2 号门 `smooth` 刷出网关 `403`，同浏览器/IP 24 小时内都无法再下拉翻页）。
4. **合规底线与受阻即停（零反爬伪造）**：
   - 仅读取用户在常规 Chrome（默认端口 `9222`）手动登录并打开的目标门店页；**绝不注入模拟触摸/鼠标轨迹、绝不擦除或改写 Cookie/设备指纹、绝不执行多账号轮换**。
   - 遇到验证页面（`verify.meituan.com` / `418`）：立即保存当前 Excel 与分析报告并以**退出码 `42`** 停机报告用户；
   - 遇到访问限制（`HTTP 403 / 406`）：立即保存并以**退出码 `43`** 停机，绝不轮询重试（若仅因早先遗留导致 2 号门 `smooth` 处于 `403` 冷却、而 1 号门 `sputag` 正常，可显式传 `--max-smooth-daily 0` 跳过 2 号门，仅在当日剩余 `sputag` 额度内慢速采集分类与二级子分类）；
   - 触达滚动 24h 配额上限：自动保存并以**退出码 `44`** 优雅停机，待满 24 小时配额释放后用同一命令断点续采。

---

## 内置六大防坑机制（默认全自动生效）

1. **极简一行启动**：自动识别 `9222` 当前门店名称、地址与 `poi_id_str`，未传 `--out-dir` 时自动创建 `./crawl_data/{店铺名称}` 独立断点目录。
2. **代码级限速硬钳位（Hard Clamp）**：所有间隔、每小时上限、波次休眠与 24h 配额均在 `crawl_h5_store.py` / `fetch_spu_details.py` 入口做物理上下界钳位，杜绝误传快参数毁号。
3. **滚动 24 小时配额账本（防跨零点超发）**：`daily_quota.json` 按真实 `86400 秒` 滚动窗口统计调用时间戳，跨过 `00:00` 不会误清零配额。
4. **跨零点 / 隔日续跑锁定同一份 Excel**：首次启动自动将日期 `MMDD` 固化进 `progress.json`，隔日续采始终更新同一份 `{MMDD}{店铺名称}.xlsx`。
5. **页面重载 `poi_id_str` 重加密与门店动态改分类自愈**：核验 `店名 + 详细地址` 100% 一致后自动衔接新加密 `poi_id_str`；若商家中途新增/调整分类导致下标偏移，自动按分类唯一 `tag` 重新对齐 `categories.json`，绝不中途崩断。
6. **大店单号配额智能规划 + 自动出分析报告**：扫完一级分类后若剩余批次数超出 `smooth` 额度，自动优先用剩余 `sputag` 额度采集高产二级子分类首屏分流大类；收尾自动调用 `scripts/analyze_competitor_store.py` 输出 `{MMDD}{店铺名称}_数据分析报告.md`。

> **实战协议与操作避坑手册（必读）**：关于「为什么严禁调 `spuTagProducts(page_index>=1)` 翻页」「为什么 `smoothRender` 必须固定 `batch=30`」「为什么单老号一天能稳跑 7,000~8,000 行 SKU 而 6,000+ SPU 超大仓需分 2 天」「为什么绝不能按 F12 登录（拉窄窗口至 420px 即可）」「为什么要用独立 `--user-data-dir` 与商家端后台隔离防 `408` 掉线」，详见 [engineering_lessons.md](references/engineering_lessons.md)。

---

## 1. 标准启动流程

### Step 1：确认依赖与常规 Chrome 门店页
```bash
python3 -c "import websockets, openpyxl; print('DEPS_OK')" || python3 -m pip install websockets openpyxl
```
若常规 Chrome 未开 `9222` 调试端口，启动后手动登录 [美团外卖 H5](https://h5.waimai.meituan.com/waimai/mindex/home)（优先使用活跃老号，勿开 F12 手机模拟）并点进目标门店页：
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
```

### Step 2：纯内存核验当前门店（0 网络请求）
```bash
python3 ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --inspect-only
```

### Step 3：启动强制慢速采集（自动建断点目录 + 增量导出 27 列 Excel + 自动出分析报告）
```bash
# 活跃老号启动（强制 68~88s 间隔、≤38次/h、满30次休10m、24h上限 92/85）
python3 -u ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --excel-out-dir .

# 新号 / 低活跃号启动（强制 75~95s 间隔、≤32次/h、满25次休11m、24h上限 28/35）
python3 -u ~/.codex/skills/meituanjingdui/scripts/crawl_h5_store.py --port 9222 --account-tier conservative --excel-out-dir .
```
*(如需显式指定分店与目录，可附带 `--poi-name`、`--poi-id-str`、`--out-dir`、`--date-mmdd`；若 2 号门 `smooth` 正处冷却期仅走 1 号门，可加 `--max-smooth-daily 0`。)*

---

## 2. 离线货盘分析与本店 69 码同品对标选品（0 网络请求）

采集收尾会自动生成 `{MMDD}{店铺名称}_数据分析报告.md`。若有本店商品 Excel（含 69 条码列），可随时离线生成**同条码比价清单 + 竞对热销我方缺品 TOP 表**：

```bash
python3 ~/.codex/skills/meituanjingdui/scripts/analyze_competitor_store.py \
  --excel "./1127快趣逛超市龙文店.xlsx" \
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

## 4. 二阶段按需提取新品轮播副图、详情长图与属性表（强制慢速休眠）

仅对筛选出的少量重点引进品按需执行（代码已强制 `≥25~35s/次`、`≤38次/小时`、24h 上限 `≤60次`）：

```bash
python3 -u ~/.codex/skills/meituanjingdui/scripts/fetch_spu_details.py \
  --port 9222 \
  --spus-jsonl "./crawl_data/店铺名称/spus.jsonl" \
  --top-n-sales 30 \
  --out-jsonl "./crawl_data/店铺名称/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx"
```
