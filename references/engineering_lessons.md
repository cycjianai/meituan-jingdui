# 美团 H5 闪购（`wx-shangou`）协议架构、单号容量基线与实战避坑经验

本文档完整保留 `8,867 SPU / 11,848 SKU`（满佳喜长清店）与 `4,622 SPU / 7,823 SKU`（吉友林芝店）及 `6,303 SPU`（快趣逛龙文店）多轮实测沉淀的**纯技术协议规律与合规操作经验**（不含任何反爬伪造、指纹擦除或多号绕限流逻辑）。

---

## 一、底层页面与四接口职责边界

1. **页面形态**：从 `https://h5.waimai.meituan.com/waimai/mindex/home` 点进闪购/超市便利门店后，运行于独立子域 `https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?poi_id_str=<加密门店ID>`（Web 化小程序框架 `reco_fe_shangchao_wxapp`，`getCurrentPages()[0]` 持有页面实例与 Redux 状态）。
2. **四个核心接口（`https://wx-shangou.meituan.com`）**：
   - `/quickbuy/v1/poi/food`：进店首包，返回门店基础信息 `poi_info` 及一级/二级分类树 `food_spu_tags`（含部分首屏预加载商品，可 0 请求直接从内存回收）。
   - `/quickbuy/v1/poi/sputag/products`（1 号门，由 `p.requestSpusAndSortedIds(idx, sub_idx, tag, type, 0, true)` 调用）：**仅限 `page_index=0`**，单次返回该分类完整的排序商品 ID 数组 `allSortedSpuId` 及排名前 20 的商品详情。
   - `/quickbuy/v1/poi/product/smooth/render`（2 号门，由 `p.requestSmoothSpus(batchIds, idx, sub_idx, tag, type, {})` 调用）：官方列表下滑翻页接口，**后端单次硬性截断最多返回 `30` 个 SPU**。
   - `/quickbuy/v2/poi/product/info`（由 `p.api.productInfoV2(...)` 调用）：单品详情接口，仅用于二阶段按需提取少量重点新品的轮播副图、详情长图与 `standard_productinfo_list` 标准属性表。

---

## 二、五条核心接口与容量铁律（实测验证）

### 经验 1：严禁调用 `spuTagProducts(page_index >= 1)` 翻页
- **实测结论**：正常手机用户在列表下滑时，前端 100% 走 `requestSmoothSpus`，从不调用 `spuTagProducts(page_index >= 1)`。实测即使把间隔放慢到 230 秒/次（16 次/小时），调用 `page_index >= 1` 依然会在累计约 46~51 次时触发整店/整号 `403` 限制。
- **规范**：`spuTagProducts` 永远只传 `page_index = 0`（用于一级分类首屏和二级子分类首屏）；翻页一律走 `requestSmoothSpus(batch=30)`。

### 经验 2：`requestSmoothSpus` 必须固定 `batch = 30` 且确保内存 `c.allSortedSpuId` 已就绪
- **坑点 A（传 50 丢后 20）**：虽然前端源码写了 `slice(0, 50)`，但服务端接口硬截断最多返回 30 个 SPU；传 50 个会导致后 20 个被静默跳过，必须固定传 `missing_ids[:30]`。
- **坑点 B（重载页面后缓存为空）**：跨天续跑或刷新页面后，若某分类在当前会话尚未点过 `p=0`，页面内存 `p._getCachedData(idx, sub_idx).allSortedSpuId` 为空，此时调用 `requestSmoothSpus` 不会把返回结果写回 `c.spus`；调用前须先把已存的 `allSortedSpuId` 赋给 `c.allSortedSpuId`。

### 经验 3：单账号双接口 24h 容量基线与精准产能核算
- **双接口独立记账上限（活跃老号实测）**：
  1. **2 号门 `requestSmoothSpus(batch=30)`**：单号 24h 约 `90` 次触顶（第 91 次返回 `403`，静默 2 小时以上仍不恢复，属 24h 级周期） $\rightarrow$ 代码强制软锁 **`≤ 85 次`**（约产出 `2,550` 款 SPU）。
  2. **1 号门 `spuTagProducts(p=0)`**：单号 24h 约 `~112` 次触顶 $\rightarrow$ 代码强制软锁 **`≤ 92 次`**（约产出 `1,300 ~ 1,600` 款 SPU）。
- **单老号单日安全总产能**：
  - 两扇门合计 `177` 次请求（按 `68~88s` 间隔 + 每满 30 次休眠 `10 分钟` + `≤38次/小时`，全程约 **5 ~ 5.5 小时**），单老号一天可 0 滑块稳采 **`~3,850 ~ 4,500` 款独立 SPU**（展开多规格约 **`7,000 ~ 8,000` 行 Excel SKU**）。
  - **门店规模预期管理**：`≤ 4,000` 款 SPU 的常规超市/闪电仓，1 个老号 1 天 1 轮即可 100% 采完；`6,000 ~ 9,000` 款 SPU 的超大型百货仓，超出单号 24h 物理容量，天然需分 **2 个自然日**同号断点续跑收官。
- **精准 ETA 估算公式**：
  - 严禁简单用 `总剩余商品数 ÷ 30` 估算；必须按**逐分类向上取整批次数** $\sum \lceil \text{rem}_i / 30 \rceil \times 92\text{秒/批}$（含休眠、网络往返与每 3 批导出 Excel 耗时）+ 每 30 批 `600 秒`波次休眠计算。

### 经验 4：遇 `403` 严禁定时发探针轮询试探
- 一旦接口返回 `HTTP 403`，必须立即落盘停机（退出码 `43`），**严禁写循环每隔 30 分钟发请求探测是否解封**——中途探测会不断重置网关的滑动冷却计时器，导致原本 24 小时解除的冷却被无限顺延。

### 经验 5：商家中途动态增删分类的 `tag` 对齐
- 门店营业期间商家可能随时上下架活动分类（如临时插入「临期处理」），导致 `menuTabs` 下标整体顺移 `+1`。`categories.json` 续传时必须按分类唯一 `tag`（剥离 `_27` 后缀）与当前实时菜单重新对齐下标，不可盲信旧数字下标 `idx`。

---

## 三、常规浏览器登录与环境隔离三大实用技巧（零违规纯正道用法）

### 1. 专属 `--user-data-dir` 物理隔离 B 端商家后台（根治 `code=408` 互踢）
- **原因**：如果在同一个 Chrome 用户配置里既开着美团闪购商家端（`shangoue.meituan.com`）/ 牵牛花（`qnh.meituan.com`），又开美团外卖 H5 端，B 端后台脚本会定时清理 `.meituan.com` 根域 `token` Cookie，导致 H5 端中途突然报 `code=408` 退出；同时多业务混用也容易残留跨子域 `localStorage`（如 `cactivityapi-sc.waimai.meituan.com` 下的 `shangou_wx_set_info`）。
- **正道做法**：直接为美团 H5 采集固定使用**独立的常规 Chrome 配置目录**（如 `--user-data-dir="$HOME/.chrome_meituan_9222"`），长期固定登录同一个活跃老号，不与 B 端后台混开在同一浏览器配置中。

### 2. 登录页看不到手机号输入框：直接拉窄窗口，绝不按 F12！
- **原因**：美团 H5 登录页按 `浏览器窗口宽度 ÷ 7.5` 动态计算字号。在 1920px 全屏桌面上字号会被放大 5 倍（256px），把顶部图标撑满全屏、手机号输入框顶出屏幕外；而按 `F12` 开启开发者工具手机模拟会触发前端环境异常。
- **正道做法**：手动登录时**不要按 F12**，直接用鼠标拖动 Chrome 窗口右边缘，**把窗口拉窄成手机宽度（约 `400 ~ 430px`）**，页面排版就会立刻恢复正常手机比例，输入框与按钮一目了然。

### 3. 同名分店核验与多规格字段边界
- **严防爬错同名分店**：连锁超市常有多家同名分店（如 `满佳喜超市（长清店）` vs `满佳喜超市（长清大学城店）`），开爬前必须同时核对 `poi_name` 与详细 `address`。
- **多规格数据边界**：在美团底层接口中，`SKU_ID`、69 条码、子规格名、原价、直接折扣价、神价到手价、实时库存、限购份数、最小起购数均按子规格（SKU）独立返回；而 **`月销量`、`想买人数`、`点赞数`** 美团仅在主商品（SPU）维度披露，因此同一多规格商品展开的多行子规格共享相同的 SPU 月销/想买/点赞值（分析分类总销量时须按 `SPU_ID` 去重累加，已在 `analyze_competitor_store.py` 中固化）。
