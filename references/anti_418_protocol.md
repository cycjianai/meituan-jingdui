# 美团 H5 闪购（`wx-shangou`）风控机制剖析与防 418 / 防 403 压测手册

## 一、核心底层架构发现

当用户在 `https://h5.waimai.meituan.com/waimai/mindex/home` 点击进入任何一家闪购/超市便利门店（如满佳喜超市、鲸速达超市、京东便利店）时：
1. 页面会跳转至 `https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?poi_id_str=<门店加密ID>`；
2. 该页面在浏览器内运行美团闪购 Web 化小程序框架（`reco_fe_shangchao_wxapp`），其页面实例可直接通过全局 `getCurrentPages()[0]` 访问；
3. 页面数据接口直连 `https://wx-shangou.meituan.com/quickbuy/v1/*`，主要包括：
   - `/quickbuy/v1/poi/food`：进店首包（约 1.3 MB），包含门店信息 `poi_info`、全店一级与二级分类树 `food_spu_tags`（含 70+ 个一级分类及 400+ 个子分类）；
   - `/quickbuy/v1/poi/sputag/products`：按分类 ID 拉取该分类下的**全量排序商品 ID 数组（`allSortedSpuId`）**及前 20 个商品详情；
   - `/quickbuy/v1/poi/product/smooth/render`：按传入的 `spuIds` 数组批量返回完整的商品 SPU 与子 SKU 结构化详情。

---

## 二、美团双层频次风控红线（`HTTP 418` 滑块 vs `HTTP 403` 临时封禁）

通过多轮极限压测与逆向前端源码（`main_app/app-service.attached.ed184652.js`、`sub-trade/app-service.attached.4a8e6519.js`），我们精准摸清了美团网关的两层频次限制与四项机器人特征识别机制：

### 2.1 第一层：2 分钟滑动窗口频次门禁（触发 `HTTP 418` -> `verify.meituan.com` 点选/滑块验证）
- **实测触发点**：
  - 当任意 **120 秒（2 分钟）滑动窗口**内的接口请求数达到 **15 ~ 24 次**（平均间隔 $< 6$ 秒）时，网关返回 `Status code not 200[418]` 并跳转 `verify.meituan.com`。
- **安全红线**：
  - 单次请求间隔控制在 **`11.5 ~ 16.5 秒`**，每 **5 次请求**小憩 **`22 ~ 32 秒`**，每 **15 次请求**冷却 **`75 ~ 90 秒`**，使任意 2 分钟窗口内请求数严格 $\le 7$ 次，即可连续几十次请求 **0 次触发 `418`**（实测从第 29 次连续跑到第 112 次请求共采 2,103 个 SPU 零 `418`）。

### 2.2 第二层：30 分钟 / 1 小时累计会话配额门禁（触发 `HTTP 403` 临时静默封禁）
- **实测触发点**：
  - 在避开 2 分钟 `418` 窗口后，若以 ~3.1 次/分钟（约 185 次/小时）的速度连续不间断请求，在累计达到 **第 113 次请求**（单会话 45 分钟内累计约 145~150 次请求）时，网关不再弹 `418` 验证码，而是直接返回 **`Status code not 200[403]: request:ok`**！
  - **关键机制**：`403` 不会跳转 `verify.meituan.com`，而是由前端捕获后在页面弹出报错 Toast。若此时脚本不立即熔断停止、继续每秒/每十秒重试，会不断刷新 WAF 的封禁计时器！
- **安全红线（为什么慢爬一天能稳抓 4~5 家大店）**：
  - 一家 7,500 SPU 的大型超市（约 68 个实体分类），全店共需约 `68` 次 `spuTagProducts`（带出 ~1,360 品）+ `200` 次 `smoothRender`（每批返回 30 品，带出 ~6,000 品）= **合计约 265~270 次请求即可抓完一整家大店**。
  - 只要将每小时请求量严格踩在 **`85 ~ 95 次/小时`**（即平均每分钟 $1.5$ 次请求，每满 **40 次请求**主动休息 **`3.5 ~ 4.3 分钟`**）：
    - 抓完一整家 7,500 品大店约需 **2.2 ~ 2.5 小时**，全程既不触发 `418` 也不触发 `403`；
    - 一天 10~12 小时可稳健抓完 **4 ~ 5 家大型超市（3~4 万个商品）**！

---

## 三、逆向前端源码发现的 4 项“机器人行为特征”及修复方案

除请求速度外，逆向 `smooth-render.js` 与 `/api/rohr.js` 发现了裸调页面函数时的 4 个机器人暴露特征，已在 `crawl_h5_store.py` 中全部修复：

1. **特征 1：`requestSmoothSpus` 传入的 `spuIds` 数组长度异常（30 vs 50）**
   - **源码真相**：在 `smooth-render.js` 的 `_requestSmoothMenuData` 中，官方前端代码写死为 `g = m.slice(f + 1, f + 50 + 1)`，即**前端每次固定向 `smooth/render` 发送最多 50 个 SPU ID**，服务端截取并返回其中前 30 个商品；下次滚动时前端从第 30 个商品之后继续切片发送 50 个 ID。
   - **修复**：脚本每次向 `requestSmoothSpus` 传入 `missing_ids[:50]`（与官方前端请求包体 100% 一致），同时按服务端实际返回的 30 个商品更新 `seen_spus`，下一次自动从第 31 个未采 ID 起再传 50 个 ID。

2. **特征 2：只调底层数据请求、不更新 UI 分类高亮与商品卡片渲染（0 图片加载异常）**
   - **源码真相**：真人点击分类下滑时，会触发 `_highlightTag(idx, sub_idx)`（更新 `activeTypeIndex` 并上报二级分类曝光 `_lxSecondCategoryView`）以及 `setList(0, 10, showList, "smoothRender.showList", true)`（将商品渲染到 DOM 并触发浏览器加载 `p0.meituan.net` 商品缩略图）。若连续发出 100+ 次 API 请求却从不切换 UI 分类、从不加载任何商品图片，极易被识别为无头/内存脚本。
   - **修复**：每次切换分类时同步调用 `p._highlightTag(idx, sub_idx, false)`，每次拉回批次商品后调用 `p.setList(0, 6, matched, "smoothRender.showList", true)` 驱动页面真实渲染商品卡片并加载缩略图。

3. **特征 3：`/api/rohr.js` 的移动端触摸轨迹数组（`mT`、`tT`、`aT`）为空**
   - **源码真相**：在 `main_app/app-service.attached.ed184652.js` 的 `/api/rohr.js` 中，防爬签名对象 `k = {rId: 100016, mT: [], kT: [], aT: [], tT: []}` 通过 `handleTouchMove(e)` 收集 `e.touches[0].clientX/clientY` 填入 `mT` 和 `tT`。仅派发桌面端 `MouseEvent('mousemove')` 不会填充 `Touches` 轨迹。
   - **修复**：在 `simulate_human_interaction` 中同时触发 `p._onTouchMove({touches: [{clientX, clientY}], changedTouches: [...]})` 与 `MouseEvent` / `scroll`。

4. **特征 4：验证回跳后 URL 残留过期参数（`code=121044&request_code=...&response_code=...`）及 `403` 循环重试**
   - **源码真相**：当 `verify.meituan.com` 回跳门店页后，URL 与 `p.options` 中会残留 `request_code`、`response_code` 甚至 `code=121044（授权码已过期）`，随后续每次请求头上报。且当触发 `403` 时若不立即停止，连续重试会延长封禁时间。
   - **修复**：
     - 每次就绪检查时通过 `history.replaceState` 与 `delete p.options[k]` 自动清理残留验证参数；
     - **硬熔断机制（Circuit Breaker）**：一旦捕获 `403` / `406` / `418`，立即落盘导出当前 Excel 并停止请求，绝不循环重试刷报错弹窗。
