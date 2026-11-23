# 美团 H5 闪购（`wx-shangou`）风控血泪复盘、三账号压测实证与字段逆向手册

本文档记录我们在 **满佳喜超市（长清店，全店 75 个标签 / 68 个有效分类 / `8,867` 个独立 SPU / `11,848` 行 SKU）** 上经历 3 个真实账号、近 40 小时对抗摸清的全部底层真相，**所有旧版错误假设（如“11~16s 间隔”“传 50 ID”“每 30 分钟探测 403 解封”“用 `page_index>=1` 分流”）均已彻底作废删除**。

---

## 一、底层页面与接口架构

1. 入口地址：`https://h5.waimai.meituan.com/waimai/mindex/home` $\rightarrow$ 点击进入任意闪购/超市便利门店后跳转至独立子域：
   `https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?poi_id_str=<门店加密ID>`
2. 该页面运行 Web 化小程序框架（`reco_fe_shangchao_wxapp`），全局 `getCurrentPages()[0]` 暴露全部页面方法与 Redux 缓存。
3. 三个核心底层接口（域名 `https://wx-shangou.meituan.com`）：
   - `/quickbuy/v1/poi/food`：进店首包，含门店信息 `poi_info`、一级与二级分类树 `food_spu_tags`。
   - `/quickbuy/v1/poi/sputag/products`：由 `p.requestSpusAndSortedIds(idx, sub_idx, tag, type, page_index, true)` 调用。**仅允许 `page_index=0`**，单次返回该分类下完整的排序商品 ID 数组 `allSortedSpuId` 及前 20 个商品详情。
   - `/quickbuy/v1/poi/product/smooth/render`：由 `p.requestSmoothSpus(batchIds, idx, sub_idx, tag, type, {})` 调用。官方瀑布流翻页接口，**后端单次硬性截断最多返回 30 个 SPU**。
   - `/quickbuy/v2/poi/product/info`：由 `p.api.productInfoV2(...)` 调用，单品详情接口（用于二阶段按需拉取轮播副图、详情长图、商品属性表）。

---

## 二、3 个账号真实压测死因对照表（彻底破案）

| 账号与阶段 | 实际调用的翻页接口 | 单次请求间隔 | 累计调用次数 | 本阶段采集 SPU | 真实风控结果与死因 |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **账号 1（前半段）** | `spuTagProducts(p=0)` + **`requestSmoothSpus(batch=30)`** | 11~18s | **112 次** | **`+3,993` SPU** | **未封号！** 仅第 113 次对 `smoothRender` 限流，切分类仍 `200 OK` |
| **账号 1（后半段）** | **`spuTagProducts(page_index >= 1)`** | ~36s | **第 50 次** | `+890` SPU | **第 50 次直接触发全账号 `403`（24h 封禁）** |
| **账号 2（全程）** | **`spuTagProducts(page_index >= 1)`** | ~50s | **第 46 次**（6+40） | `+675` SPU | **第 46 次直接全账号 `403`**（另因未清 `cactivityapi-sc` 缓存继承了 1 号风险分，少撑 4 次） |
| **账号 3（极慢速对比）** | **`spuTagProducts(page_index >= 1)`** | **~230s（慢了 6 倍！）** | **第 51 次** | `+953` SPU | **哪怕慢到 16 次/小时，依然每 ~18 次必弹 `418`（第 14、33、51 次），第 51 次依然 `403` 锁号！** |
| **账号 1 次日解封收尾** | 仅 **`spuTagProducts(p=0)`**（L1/L2 首页） | `58~76s` | **112 次** | `+1,170` SPU | **全程 0 次 `418` 滑块**，满 112 次触及单号日度配额返回 `403` 熔断 |
| **账号 2 次日解封收尾** | **`requestSmoothSpus(batch=30)`**（预注入 `c.allSortedSpuId`） | `58~76s` | **43 次** | **`+1,186` SPU（100% 完工）** | **全程 0 次 `418` 滑块、0 次 `403`、账号完好存活！** |

### 核心铁律结论
1. **为什么 `spuTagProducts(page_index >= 1)` 必死？**
   - 美团 H5 正常用户在手机上翻页 100% 走 `smooth/render`，绝不调 `spuTagProducts(page_index >= 1)`。美团网关对 `page_index >= 1` 设的是**与速度无关的硬次数熔断（累计 ~50 次 / 3 次 418 必封 403）**。本 Skill 已彻底删除该调用路径。
2. **单账号 `spuTagProducts(p=0)` 日度硬上限约为 112 次**：
   - 触及后返回 `openresty` 网关 `403`（带 `X-Forbid-Code`），属于 **12~24 小时/次日恢复的日度封禁**，**绝不是 1 小时恢复**。
   - **严禁每 30 分钟发探针检测是否解封**——中途发请求会刷新滑动封禁计时器并升级阶梯封禁时长！
3. **黄金零滑块/零封控配速**：
   - `58 ~ 76 秒/次`（约 50~53 次/小时，125s 窗口 $\le 2\sim3$ 次，62m 单接口 $\le 55$ 次，62m 总计 $\le 58$ 次）；
   - `requestSmoothSpus` 严格 `batch=30`，且调用前强制检查并注入 `c.allSortedSpuId = all_ids`（防止换号/刷新页面后 `c.allSortedSpuId` 为空导致不写回 `c.spus`）。

---

## 三、换号连坐锁号、免 F12 登录与 B/C 端共存三大工程暗坑

### 1. 跨子域 `localStorage` 污染与设备指纹连坐（踩过两次的大坑）
- **污染链路 1（秒 403）**：在 `h5.waimai.meituan.com` 点退出登录后，独立子域 **`cactivityapi-sc.waimai.meituan.com`** 的 `localStorage` 仍残留 `shangou_wx_set_info` 和 `saber-vma:user-info`。逆向前端 `/utils/business/deal-region-set.js` 证实，只要 `shangou_wx_set_info` 存在，页面就会无视当前登录的新账号，强行把旧被封账号 ID 拼在 URL 后（`?ui=3776626245`），导致新号首包秒 `403`！
- **污染链路 2（登录即触发 `user_err_user_locked_mobile`）**：若只删 `token`/`oops`/`mt_c_token` 而保留 `WEBDFPID`、`uuid`、`iuuid`、`openh5_uuid`、`_lxsdk*`、`localStorage.dfpId`、`localStorage.localId`、`indexedDB("dfp_db")`，美团通行证 `passport.meituan.com` 会把新号与刚被 403 的设备指纹直接关联，短信登录瞬间弹窗锁号（“检测到您的账号存在异常，已将账号锁定”）。
- **彻底根治（`scripts/prepare_clean_login.py`）**：
  - 使用 CDP `Storage.clearDataForOrigin` 一次性清空 `cactivityapi-sc.waimai.meituan.com`、`h5.waimai.meituan.com`、`passport.meituan.com` 三个域的 `local_storage,indexeddb,cache_storage`；
  - 清除全部 17 个 C 端认证与指纹 Cookie，同时**严格跳过 `shangoue`、`waimaie`、`qnh`、`e.waimai` 保护 B 端商家后台不掉登**。

### 2. 为什么绝不能按 F12 开手机模式登录？
- **现象**：全屏 Chrome（1920px）打开登录页时，美团按 `窗口宽度 ÷ 7.5` 把根字号算成了 `256px`（手机正常为 `50px`，放大 5.12 倍），导致手机号输入框被挤出屏幕；但按 F12 开手机模式会被 `H5guard` 立刻抓到 **`devtools://` 窗口 + 桌面 UA 与触屏模拟冲突**，新登录 Token 当场被打高危标。
- **解法**：`prepare_clean_login.py` 自动关闭所有 `devtools://` 窗口，并通过 `Page.addScriptToEvaluateOnNewDocument` 持久化注入 **390px 居中手机卡片 CSS** + **桌面 `click -> touchstart/touchend` 触屏桥接**（同时修复 `mt-login-component/v4.0/index.min.js` 里“是我的账号” `#iLoginCompSecMobileButtonYes` 只听 `tap` 不听鼠标 `click` 且不隐藏 `#iLoginCompSecMobileModel` 遮罩层的问题）。

### 3. B 端与 C 端同浏览器共存导致 `408` 自动退出
- **根因**：同一 Chrome 里同时开着美团闪购商家后台（`shangoue.meituan.com`）时，B 端脚本会定时清除根域 `.meituan.com` 下的 `token` Cookie，触发 C 端报 `code=408`，但 C 端实际凭证 `oops`、`mt_c_token` 与 `localStorage.shangou_user` 完好无损。
- **解法**：`crawl_h5_store.py` 与 `fetch_spu_details.py` 每次请求前自动回写 `.meituan.com` `token` Cookie（`document.cookie` + CDP `Network.setCookie`），免扫码无感自愈。

### 4. IP 风险分与换 IP 最佳实践
- **对比实验证实**：同浏览器去掉 Cookie 裸调网关仍返回 `200 OK (未登录)`，说明 `403` 硬拦截锁的是 `userId` 而非宽带 IP；但同一宽带 IP 24 小时内连挂 2~3 个号会抬高 Yoda/H5guard 关联嫌疑分。
- **严禁使用 VPN / 机房代理 IP**（美团对机房/海外段零容忍，一登必锁）。
- **最佳实践**：换号时让电脑连接 **手机 4G/5G 个人热点**（三大运营商 CGNAT 池与真实手机点外卖用户同段，风控信誉最高；手机开关飞行模式 5 秒即换全新基站 IP）。

---

## 四、`unify_price` v5 底层字段映射防坑速查（8,867 SPU 全量对账结论）

| 业务字段 | 旧版错误外层字段（严禁单读！） | 实测错漏规模 | 正确底层字段路径（`export_competitor_excel.py` 已焊死） |
| :--- | :--- | :--- | :--- |
| **`直接折扣价(元)`** | `skus[].price`（在 v5 下等于原价！） | **9,065 / 9,101 行全错成原价** | `item_details[0].act_price` $\rightarrow$ `skus[].unify_price.activity_info.activity_price` $\rightarrow$ `skus[].price` |
| `原价(元)` | 仅读 `skus[].origin_price` | 部分活动划线价不同步 | `skus[].unify_price.underlined_price` $\rightarrow$ `activity_info.underline_price` $\rightarrow$ `skus[].origin_price` |
| **`神价/到手价(元)`** | 只读 SPU 层 `actual_price_info` | 多规格非最低价子规格全丢 | 优先子规格 `skus[].unify_price.actual_price_info.actual_price`（覆盖 5,224+ 行）$\rightarrow$ `secondary_activity_price` |
| **`限购份数`** | `skus[].restrict`（v5 下全为 `0`） | **9,100 行漏掉限购** | `skus[].restrict` ($>0$) $\rightarrow$ `skus[].unify_price.activity_info.quota_per_order` ($>0$) |
| **`促销标签`** | `skus[].promotion_info`（v5 下为 `""`） | **9,102 行标签为空** | 外层为空时由 `(1 - discount_strength) * 10` 折 + `quota_per_order` 合成（如 `7折 限1份`） |
| **`月销量(数值)`** | `spu.month_saled`（95%+ 为 `0`） | **8,492 SPU 销量变 0** | `max(解析(spu.month_saled_content), spu.month_saled)` |
| **`点赞数`** | `spu.praise_num_new`（`smoothRender` 下常为 `0`） | **405 SPU 漏掉真实点赞** | `max(praise_num_new, praise_num, praise_count, 解析(praise_content))` |
| `想买人数` | 直接取 `spu.want_to_Buy` | **378 个新品返回 `None`** | `max(want_to_Buy, 解析(want_to_buy_content), 0)` |
| `子规格名称(spec)` | 仅读 `skus[].spec` | **135 行量贩装为空** | `skus[].spec` $\rightarrow$ `skus[].combine_spec` $\rightarrow$ `skus[].cspu_grouping_value` |
| `店内二级分类` | 仅用 `spu.tag` 查表 | **502 SPU 退化为一级名** | `spu.tag` $\rightarrow$ `skus[0].tagBelongTo` $\rightarrow$ `categories.json` 的 `sub_tags[].allSortedSpuId` 反查 |
| `满减券` / `69码` / `主图` | 只读 SPU 层 / 保留 `.webp` | 6 行子规格券 + 多规格图 | 子规格优先读 `dynamic_act_labels` / `picture`（去 `@watermark`、尺寸、`.webp`）；69 码兜底 `supply_poi_sku.upccode` |
