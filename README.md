# meituanjingdui — 美团闪购/超市便利竞对全量高维采集与比价 Skill

通过已登录常规 Chrome 浏览器的美团外卖 H5 端（`https://h5.waimai.meituan.com/waimai/mindex/home` $\rightarrow$ `wx-shangou.meituan.com`），引导或协助用户定位并进入目标竞对超市门店，在防 `418` 滑块风控的安全压测阈值下，全量采集店内商品 SPU/SKU 级核心数据并导出标准 Excel 竞对报表。

## 核心优势（对比微信小程序 / 手机 App 抓屏）

1. **直接获取底层 69 条形码（`upccode`）**：无需靠标题模糊猜测，直接按厂商条形码 100% 精准匹配同款商品。
2. **多规格子 SKU 一次性全量带出**：无需逐个点开“选规格”弹窗，`skus[]` 数组直接包含所有子规格的独立条码、原价、直接折扣价、实时库存与高清图。
3. **穿透神价/券后到手价与实时库存**：直接提取 `unify_price.actual_price_info.actual_price`（神价到手价）、满减商品券（`dynamic_act_labels`）及对手当前真实库存（`real_stock`）。
4. **高清无水印主图与官方三级类目**：自带原图 URL 与 `standardCategorys`（美团官方一/二/三级类目 ID 与名称），支持无码百货的视觉比对与按三级类目均衡选品。
5. **防 418 拟人呼吸限速架构**：内置灵犀点击埋点（`_lxFirstCategoryClick`）、DOM 生物行为注入（`mousemove/click/scroll`）与 10/32 次三级滑动窗口冷却机制，单次批量拉取 50 个 SPU，15~18 分钟稳健抓完 8,000+ SKU 大店，支持一天连续采集多家门店。

## 目录结构

- `SKILL.md`：Skill 核心指令、定位防错规范、防 418 压测准则与导出字段定义
- `scripts/crawl_h5_store.py`：防 418 全店断点续传采集脚本
- `scripts/export_competitor_excel.py`：全维度 Excel 竞对总表与子 SKU 拆解导出脚本
- `references/anti_418_protocol.md`：美团 H5 闪购风控机制与压测调优手册
- `agents/openai.yaml`：Agent 平台元数据配置
