# meituan-jingdui (`meituanjingdui`) — 美团闪购/超市便利竞对全量高维采集与比价 Skill

跨平台（macOS / Windows / Linux）美团外卖 H5 端（`https://h5.waimai.meituan.com/waimai/mindex/home` $\rightarrow$ `cactivityapi-sc.waimai.meituan.com`）竞对门店全量采集与 27 列硬锁定 Excel 导出 Skill。经过 **8,867 SPU / 11,848 SKU 大型闪购超市** 全量实测与 3 账号深度风控复盘打磨，让任意电脑上的 AI Agent 读完即可在 1 分钟内安全启动采集。

## 核心特性与实战加固

1. **跨电脑 60 秒即跑**：内置 `--inspect-only` 门店状态一键核验、`prepare_clean_login.py` 自动开标签页/关 F12/注入 390px 免 F12 登录卡片与桌面 `click -> tap` 触屏桥接器，支持 macOS / Windows / Linux。
2. **血泪总结的零 `418` / 防 `403` 三阶段引擎（`scripts/crawl_h5_store.py`）**：
   - **彻底禁用 `spuTagProducts(page_index >= 1)`**（实测 3 账号无论快慢 46~51 次必 24h 封号）；
   - **Pass 1（一级分类 `p=0` 拿全店 `allSortedSpuId`）$\rightarrow$ Pass 1.5（`requestSmoothSpus(batch=30)` + 自动预注入 `c.allSortedSpuId` + UI 缩略图同步）$\rightarrow$ Pass 2（二级子分类 `p=0` 兜底）**；
   - **黄金防封配速**：`58~76s/次` + 持久化滚动窗口时间戳，实测连续 43 次 `smoothRender` **0 滑块 0 报错**；
   - **B/C 端共存自动自愈**：防止同浏览器美团商家后台（`shangoue.meituan.com`）清掉 `.meituan.com` 的 `token` Cookie 导致 `408` 掉登；
   - **403 硬熔断自动护航**：遇 `403` 绝不轮询重试，立刻落盘导出 Excel 并自动清理跨三域 C 端设备指纹（严格保护同浏览器 B 端商家后台不掉登）。
3. **严禁自由发挥的 27 列硬锁定 Excel（`scripts/export_competitor_excel.py`）**：
   - 严格单表 `全量商品竞对明细`、固定 27 列顺序、`单规格`/`多规格` 二选一枚举、`{MMDD}{店铺名称}.xlsx` 命名规则；
   - **完整修复美团 `unify_price` v5 的 9 大原始 JSON 字段陷阱**（攻克外层 `skus[].price` 全为原价导致 99.6% 折扣品漏映射的致命坑，精准还原 `activity_price` 直接折扣价、子规格独立神价到手价、`quota_per_order` 限购、`discount_strength` 促销标签、文本月销、真实点赞数、量贩装 `combine_spec`、二级分类反查及去 `.webp` 高清无水印原图），每次导出自动打印数据自检统计。
4. **二阶段按需拉取新品建品素材（`scripts/fetch_spu_details.py`）**：
   - 针对选出的高销新品，按需调用 `productInfoV2` 拉取顶部轮播副图、图文详情长图、结构化商品属性参数表并一键下载图片包。

## 目录结构

- [`SKILL.md`](SKILL.md)：Agent 首读 60 秒极速启动清单、8 大血泪风控铁律、27 列硬锁定导出规范与 9 大字段防坑映射表
- [`scripts/prepare_clean_login.py`](scripts/prepare_clean_login.py)：跨三域 C 端设备指纹清理（保护 B 端不掉登）+ 免 F12 390px 手机卡片登录与 `click -> tap` 触屏桥接脚本
- [`scripts/crawl_h5_store.py`](scripts/crawl_h5_store.py)：全店防 418/403 三阶段断点续传采集主引擎（含 `--inspect-only` 状态核验）
- [`scripts/export_competitor_excel.py`](scripts/export_competitor_excel.py)：严格锁定 27 列单表全规格 Excel 导出与自动数据自检脚本
- [`scripts/fetch_spu_details.py`](scripts/fetch_spu_details.py)：二阶段按需拉取单品轮播副图、详情长图、属性参数表与下载图片包脚本
- [`references/anti_418_protocol.md`](references/anti_418_protocol.md)：3 账号真实压测对照表、美团双层风控与跨子域污染逆向分析、IP 策略与字段速查手册
- [`agents/openai.yaml`](agents/openai.yaml)：Agent 平台元数据配置

## 快速开始

```bash
# 1. 安装依赖（仅需一次）
pip3 install websockets openpyxl

# 2. 净化 C 端指纹并打开免 F12 登录卡片（若尚未登录或换号前必跑）
python3 scripts/prepare_clean_login.py --port 9222

# 3. 核验当前打开的门店信息（防同名分店爬错）
python3 scripts/crawl_h5_store.py --inspect-only --port 9222

# 4. 启动防风控全店采集（自动增量导出 27 列标准 Excel）
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<poi_id_str>" \
  --poi-name "<目标店铺名称>" \
  --out-dir "./crawl_data/store" \
  --excel-out-dir "."
```
