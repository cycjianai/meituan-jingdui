---
name: meituanjingdui
description: 美团闪购/超市便利竞对门店全量高维数据采集与比价引擎（meituanjingdui）。适用于任何电脑（macOS/Windows/Linux），通过已登录常规 Chrome 浏览器的美团外卖 H5 端（h5.waimai.meituan.com -> cactivityapi-sc.waimai.meituan.com），内置血泪实战总结的防 418 滑块/防 403 封号/防换号连坐锁号/免 F12 卡片登录/B端C端共存 Token 自愈架构，全量采集竞对门店 SPU/SKU 级核心数据，并严格按写死的 27 列单表规范（完整兼容 unify_price v5 真实折扣价、独立神价到手价、69 条码、月销、点赞、实时库存、多规格展开）自动导出 `{MMDD}{店铺名称}.xlsx`，同时支持二阶段按需拉取新品副图、详情长图与属性参数表。
---

# 美团闪购竞对全量采集与比价引擎（`meituanjingdui`）

> **核心原则**：本 Skill 凝结了在 **8,867 SPU / 11,848 SKU 大型闪购超市** 上历经 3 个账号、数十小时真实风控对抗与 27 列全量数据对账换来的**铁律**。任何 Agent 在任何电脑上执行任务时，**必须直接调用本 Skill 自带脚本，严禁自由发挥改写导出表结构、严禁自己手写裸循环抓接口**。

---

## 0. 跨电脑 60 秒极速启动清单（Agent 首读即跑）

无论运行在 **macOS / Windows / Linux** 哪台电脑上，拿到任务后按以下 5 步即可在 1 分钟内进入稳定抓取阶段：

### Step 1：确认 Python 依赖（仅需 2 个包）
```bash
python3 -c "import websockets, openpyxl; print('DEPS_OK')" || pip3 install websockets openpyxl
```
*(Windows 若只有 `python` 命令，将下文所有 `python3` 替换为 `python` 即可。)*

### Step 2：检查常规 Chrome 远程调试端口（默认 `9222`）
```bash
curl -s http://127.0.0.1:9222/json/version
```
- **若有 JSON 返回**：直接进入 Step 3。
- **若无响应**：按当前系统启动一个**纯常规 Chrome 窗口**（**严禁**使用 Playwright/Puppeteer/Selenium 等带 `webdriver` 标识的自动化浏览器）：
  - **macOS**：
    ```bash
    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
    ```
  - **Windows (PowerShell / CMD)**：
    ```bat
    "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.chrome_meituan_9222"
    ```
  - **Linux**：
    ```bash
    google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
    ```

### Step 3：一键检测浏览器当前状态（登录态 + 是否已在目标门店页）
```bash
python3 scripts/crawl_h5_store.py --inspect-only --port 9222
```
- **情况 A：尚未打开美团 H5 或未登录** $\rightarrow$ 先运行净化与免 F12 登录卡片脚本（自动开标签页、清干净旧指纹、保护 B 端商家后台不掉登、注入 390px 手机登录卡片与鼠标转触屏桥接）：
  ```bash
  python3 scripts/prepare_clean_login.py --port 9222
  ```
  然后请用户在弹出的 390px 卡片里登录美团账号（**切勿按 F12**），切到目标城市/地标并点击进入目标竞对门店页。
- **情况 B：已进入门店页** $\rightarrow$ 核对 `--inspect-only` 返回的 `poi_name`、`address`、`poi_id_str` 是否与用户指定的目标分店 **100% 一致**（严防同名分店爬错，例如 `满佳喜超市（长清店）` vs `满佳喜超市（长清大学城店）`）。

### Step 4：启动全店防风控断点续传采集（后台自动增量导出 27 列标准 Excel）
```bash
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<核验后的poi_id_str>" \
  --poi-name "<核验后的目标店名>" \
  --out-dir "<本地数据目录，如 ./crawl_data/store_a>" \
  --excel-out-dir "<最终Excel输出目录，如 .>"
```
- 脚本每 3 次请求及每个阶段结束都会自动调用 `scripts/export_competitor_excel.py` 增量生成/更新 `{MMDD}{店铺名称}.xlsx`，并打印自检统计。
- 支持随时中断与断点续传（状态持久化在 `<out-dir>/spus.jsonl`、`categories.json`、`timestamps.json`、`progress.json`）。

### Step 5：若中途触发 `403`（脚本自动以退出码 `43` 熔断停机）
- **脚本已自动完成三件事**：落盘已采数据 + 导出最新 27 列 Excel + 自动运行 `prepare_clean_login.py` 清干净跨子域设备指纹并打开新登录页。
- **严禁做任何定时轮询或重试**！请用户直接登录备用美团账号（若同一宽带 IP 今日已连续触发多次 `403`，请切 **手机 4G/5G 个人热点**，严禁开 VPN/机房代理），重新进入目标门店后**原样重跑 Step 4 命令即可从断点无缝续采**。

---

## 1. 适用场景与触发入口

当用户提出以下任一需求时，立即激活本 Skill：
- 指定抓取某家美团闪购/超市便利/闪电仓竞对门店（如“爬一下某地的满佳喜超市/鲸速达/京东便利店/某竞对仓”）；
- 需要获取竞对门店的全量商品销量、原价、真实直接折扣价、神价到手价、69 条形码（`upccode`）、高清无水印主图、实时库存（`real_stock`）、多规格全明细；
- 需要基于竞对热销品补充本方门店货盘、定价对标或二阶段拉取新品轮播副图、详情长图与属性参数表。

---

## 2. 血和泪换来的 8 条美团 H5 风控铁律（严禁违背）

以下每一条均由 3 个账号、8,867 SPU 全量实战踩坑与前端源码逆向实证得出，**违背任意一条都会直接导致秒锁号或触发 24 小时封禁**：

### 铁律 1：严禁调用 `spuTagProducts(page_index >= 1)` 翻页！（头号杀手）
- **实测血泪教训**：在 3 个不同账号上实测，哪怕把间隔拉慢 6 倍到 230 秒/次（仅 16 次/小时），只要使用 `spuTagProducts(page_index >= 1)` 翻页，依然会每隔约 18 次弹一次 `418`，并在 **第 46 ~ 51 次累计调用时 100% 触发全账号 24 小时 `403` 封禁**！
- **真相**：真实用户在手机上下滑浏览商品时，美团前端根本不调 `spuTagProducts(page_index >= 1)`，而是：
  1. 点一级分类首屏调 **1 次** `spuTagProducts(page_index=0)` $\rightarrow$ 一次性拿到该分类全部商品的排序 ID 数组 `allSortedSpuId` 与前 20 个商品；
  2. 向下滚动翻页 **100% 调用 `requestSmoothSpus`**（`/quickbuy/v1/poi/product/smooth/render`）。
- **强制规范**：`crawl_h5_store.py` 已**彻底删除 `page_index >= 1` 翻页代码**。全店采集严格采用三阶段流水线：
  - **Pass 1**：遍历实体一级分类调用 `requestSpusAndSortedIds(..., page_index=0)`，拿齐 100% `allSortedSpuId` 与首屏商品；
  - **Pass 1.5（主力收割）**：按 `batch=30` 调用 `requestSmoothSpus` 高效收割剩余商品；
  - **Pass 2（安全兜底）**：若 `smoothRender` 触及单号配额返回 `403`，自动改点**二级子分类的 `page_index=0`**（原生切换子标签行为）继续收割，同时记录二级分类归属表。

### 铁律 2：`requestSmoothSpus` 后端单次硬上限 `30` 个 SPU，且必须预注入 `c.allSortedSpuId`
- **坑点 A（静默丢品）**：前端源码虽然写 `slice(0, 50)`，但后端接口单次**硬截断最多返回 30 个 SPU**。传入 50 个 ID 会导致后 20 个被跳过。必须固定传 `missing_ids[:30]`。
- **坑点 B（换号后返回空数组）**：页面刷新或切换新号后，若某分类在当前新会话还没调过 `p=0`，页面内存缓存 `p._getCachedData(idx, sub_idx).allSortedSpuId` 为空，此时调 `requestSmoothSpus` 不会把返回的商品写入 `c.spus`！调用前**必须显式注入**：
  ```js
  if (!Array.isArray(c.allSortedSpuId) || !c.allSortedSpuId.length) {
      c.allSortedSpuId = all_ids;
  }
  ```

### 铁律 3：零 `418` 滑块 / 零 `403` 封控的黄金配速与单号日配额真相
- **黄金节奏**：单次请求间隔 **`58 ~ 76 秒`**（约 `50 ~ 53` 次/小时），125 秒滑动窗口内 $\le 2\sim3$ 次请求，62 分钟单接口上限 `55` 次、总请求上限 `58` 次（时间戳持久化至 `timestamps.json` 防重启超速）。实测连续 43 次 `requestSmoothSpus(batch=30)` **0 次 418 滑块、0 次 403**，1 小时稳收 1,300~1,500 个 SPU。
- **单账号日度配额真相**：`spuTagProducts` 单账号单日硬上限约 **`112 次请求`**（且属于日度/12~24 小时跨天封禁，**不是** 1 小时解封！）。一家 8,000+ SPU 超大店通常需 1~2 个账号接力或跨天续跑，请提前规划，绝不可盲目提速硬冲。

### 铁律 4：触发 `403` 立即硬熔断停机——严禁轮询探测、严禁买“逆向签名”
- **严禁每 30 分钟发探针检测是否解封**：实测证实第 1 次 `403` 若不停机或每 30 分钟发请求探测，会不断刷新冷却窗口并将单接口限流升级为全账号 `openresty` `X-Forbid-Code` 阶梯长封（12~24 小时）！
- **不要花钱买第三方“mtgsig / _token 逆向参数”**：本 Skill 直接在真实 Chrome 内调用美团页面原生函数，本身就是 100% 正版签名；`403` 是美团网关按服务端 `userId` 配额记账拦截，任何前端签名算法都无法绕过。
- **正确动作**：`crawl_h5_store.py` 捕获 `403`/`406` 后立即落盘导出当前 Excel + 自动执行 `prepare_clean_login.py` 清指纹 + 以退出码 `43` 立即停机。

### 铁律 5：换号前必须全量清理跨三域设备指纹（严禁只删 `token` 就换号！）
- **实测双坑（踩过两次的惨痛教训）**：
  1. **跨子域 `localStorage` 污染秒 403**：在 `h5.waimai.meituan.com` 退出登录时，闪购独立子域名 **`cactivityapi-sc.waimai.meituan.com`** 的 `localStorage` 完全不会被清空！其中残留的 `shangou_wx_set_info` 会被前端 `deal-region-set.js` 强行把旧被封账号 ID 拼成 `?ui=<旧号ID>` 附到新号所有请求后，导致新号刚进店就秒 `403`。
  2. **残留设备指纹触发连坐锁号**：若只删 `token`/`oops`/`mt_c_token` 而留下 `WEBDFPID`、`uuid`、`iuuid`、`openh5_uuid`、`_lxsdk*`、`localStorage.dfpId`、`localStorage.localId`、`indexedDB("dfp_db")`，新号验证码一登录就会被 `passport.meituan.com` 直接触发 **`user_err_user_locked_mobile`（“检测到您的账号存在异常，已将账号锁定”）**！
- **强制动作**：每次首次登录或 `403` 换号前，**必须运行 `python3 scripts/prepare_clean_login.py --port 9222`**。该脚本通过 CDP `Storage.clearDataForOrigin` 一次性清空 `cactivityapi-sc.waimai.meituan.com`、`h5.waimai.meituan.com`、`passport.meituan.com` 三个域的全部存储与 `dfp_db`，清除 17 个 C 端认证/指纹 Cookie，同时**严格跳过并保护同浏览器的 B 端商家后台（`shangoue`、`waimaie`、`qnh`、`e.waimai`）不掉登**。

### 铁律 6：严禁按 F12 打开 DevTools / 手机模式登录！
- **原因**：全屏桌面浏览器访问美团 H5 登录页时，页面按 `窗口宽度 ÷ 7.5` 动态算字号（1920px 宽下根字号被放大 5 倍到 256px），导致输入框顶出屏幕。但一旦按 F12 开启手机模拟器，`H5guard` 立刻检测到 **`devtools://` 窗口 + 桌面 UA 与触屏模拟冲突**，登录瞬间即给新 Token 打高危标签！
- **零风控解决方案（已固化在 `prepare_clean_login.py`）**：
  - 自动关闭所有 `devtools://` 窗口；
  - 自动在登录页注入 **390px 居中手机卡片 CSS**（`html { font-size: 50px !important; }`）与 **桌面鼠标 `click -> touchstart/touchend (tap)` 触屏桥接器**（同步修复 `#iLoginCompSecMobileButtonYes` “是我的账号”鼠标点不动及 `#iLoginCompSecMobileModel` 遮罩层不消失的问题）；
  - 手动操作时如需缩小页面，只需用鼠标拖动 Chrome 窗口右边缘拉窄成手机宽度即可，**永远不要按 F12**。

### 铁律 7：B 端商家后台与 C 端同浏览器共存的 Token 自动自愈（防 `408` 自动退出）
- **坑点**：同一 Chrome 内开着美团闪购商家后台（`shangoue.meituan.com`）时，B 端心跳会不定时清空根域 `.meituan.com` 下的 `token` Cookie，导致 H5 外卖端中途突然报 `code=408`（“自动退出”），但 C 端真实的 `oops` / `mt_c_token` / `localStorage.shangou_user` 依然完好。
- **已固化**：`crawl_h5_store.py` 与 `fetch_spu_details.py` 每次请求前都会自动检查并用 `oops`/`mt_c_token`/`localStorage.shangou_user` 通过 `document.cookie` + CDP `Network.setCookie` 双写回 `.meituan.com`，彻底免重登自愈。

### 铁律 8：换号防 IP 关联嫌疑分 + 拟人触摸/UI 同步
- **IP 策略**：网关硬 `403` 锁的是 `userId` 而非宽带 IP，但同一宽带 IP 在 24 小时内连挂多号会抬高 Yoda/H5guard 初始嫌疑分（更易弹滑块）。**绝对不要用 VPN 或机房代理 IP**（美团零容忍）；最稳妥方案是连 **手机 4G/5G 个人热点**（运营商原生 CGNAT 高信誉池，开关飞行模式 5 秒即换新基站 IP）。
- **前端真实行为特征**：脚本内置 `_onTouchMove` 移动端触摸轨迹（填充 `/api/rohr.js` 的 `mT`/`tT`）、`MouseEvent`/`scroll`、切换分类调用 `_lxFirstCategoryClick` / `_lxSecondCategoryClick` / `_highlightTag`、渲染 DOM 卡片触发商品缩略图加载（消除“百次请求 0 图片加载”特征），以及自动清除 `verify.meituan.com` 回跳残留的 `code=121044`/`request_code`/`response_code`。

---

## 3. 严禁自由发挥！27 列硬锁定 Excel 导出规范与 9 大字段防坑铁律

**强制要求**：导出竞对总表时，**必须且只能调用 `scripts/export_competitor_excel.py`**，严禁任何 Agent 另写脚本、严禁增删改列、严禁拆分多 Sheet、严禁更改文件名规则！

### 3.1 四条写死不可变的输出铁律
1. **文件命名铁律**：严格按 **`{MMDD}{店铺名称}.xlsx`** 自动命名（去掉括号、空格及非法字符，例如 `1124满佳喜超市长清店.xlsx`）。
2. **工作表铁律**：整个 Excel **只允许 1 张工作表**，表名固定为 **`全量商品竞对明细`**（所有单规格与多规格按子规格逐行展开，同一多规格商品的各子规格行紧邻排列并浅黄底高亮）。
3. **27 列严格锁定（`LOCKED_27_HEADERS`）**：列名、列数（27 列）、列顺序已在代码 assert 中焊死，任何偏离直接报错中止。
4. **`规格类型` 枚举铁律**：第 5 列只允许出现 **`单规格`** 或 **`多规格`** 两个值（严禁出现“多规格(3个子规格)”等任何衍生文字）。

### 3.2 必须规避的 9 大原始 JSON 映射陷阱（已 100% 固化至 `export_competitor_excel.py`）

> **血泪教训**：美团闪购 H5 升级为 `unify_price` v5 后，直接读外层 `skus[].price` 会把全店 **99.6% 的直接折扣商品（9,065 / 9,101 行）全部错导成原价**（如实卖 `3.5` 元的鞋刷导成原价 `4.98` 元）！导出脚本完成后**必须核对终端打印的 `[Self-Check]` 统计**，若大店出现 `折扣价<原价: 0 行` 必须立即停下排查。

| 序号 | 陷阱字段 | 常见错误读法（严禁！） | 正确强制映射链（已写死在代码中） |
| :---: | :--- | :--- | :--- |
| 1 | **`直接折扣价(元)`**（第 18 列） | 直接读 `skus[].price`（在 v5 下等于原价！） | `skus[].unify_price.activity_info.item_details[0].act_price` $\rightarrow$ `skus[].unify_price.activity_info.activity_price`（若旧 `skus[].price` 更低取更低值）$\rightarrow$ `skus[].price` |
| 2 | **`原价(元)`**（第 17 列） | 仅读 `skus[].origin_price` | `skus[].unify_price.underlined_price` $\rightarrow$ `activity_info.underline_price` $\rightarrow$ `skus[].origin_price` $\rightarrow$ `spu.min_price` |
| 3 | **`神价/到手价(元)` & 说明**（第 19~20 列） | 只读主商品 `spu.unify_price.actual_price_info`（多规格非最低价子规格全丢） | 优先读子规格独立 `skus[].unify_price.actual_price_info.actual_price`（及 `actual_price_text`）$\rightarrow$ `secondary_activity_price` $\rightarrow$ 单规格/最低价规格兜底 SPU 层 |
| 4 | **`限购份数` & `促销标签`**（第 24、22 列） | 直接读 `skus[].restrict`（全为 `0`）与 `skus[].promotion_info`（全为 `""`） | 限购优先 `restrict` 否则 `activity_info.quota_per_order`；标签为空且 `折扣价 < 原价` 时自动由 `(1 - discount_strength) * 10` 折 + `限N份` 合成（如 `7折 限1份`） |
| 5 | **`月销量(数值)`**（第 13 列） | 直接读 `spu.month_saled`（~95% 商品为 `0`） | `max(解析(spu.month_saled_content), spu.month_saled)`（支持 `"月售9"`、`"月售1.2万"`） |
| 6 | **`点赞数` & `想买人数`**（第 15、14 列） | 只读 `praise_num_new`（`smoothRender` 下常为 `0`）；新品 `want_to_Buy` 为 `None` | 点赞：`max(praise_num_new, praise_num, praise_count, 解析(praise_content))`；想买：`max(want_to_Buy, 解析(want_to_buy_content), 0)`，严禁写空值 |
| 7 | **`子规格名称(spec)`**（第 6 列） | 仅读 `skus[].spec`（量贩装常为空 `""`） | `skus[].spec` $\rightarrow$ `skus[].combine_spec` $\rightarrow$ `skus[].cspu_grouping_value` |
| 8 | **`店内二级分类`**（第 8 列） | 仅用 `spu.tag` 查表（`sub_idx=0` 拉取时 `spu.tag` 会变成一级分类 ID） | `spu.tag` $\rightarrow$ `skus[0].tagBelongTo` $\rightarrow$ `categories.json` 内 `sub_tags[].allSortedSpuId` 反查 $\rightarrow$ 实体一级分类名兜底 |
| 9 | **`满减商品券`、`69码`、`主图`**（第 23、3、27 列） | 只读 SPU 层或保留 `.webp` 尾缀 | 券优先 `skus[].dynamic_act_labels` 再兜底 SPU；69 码优先 `skus[].upccode` 再兜底 `supply_poi_sku.upccode`；主图优先 `skus[].picture` 再兜底 SPU，剥离 `@watermark=...`、尺寸路径与 `.webp` |

### 3.3 锁定 27 列完整顺序总表（与 `LOCKED_27_HEADERS` 逐字一致）

| 列号 | 标准列名 | 业务说明 | 列号 | 标准列名 | 业务说明 |
| :---: | :--- | :--- | :---: | :--- | :--- |
| 1 | `SPU_ID` | 美团主商品 ID | 15 | `点赞数` | SPU 聚合好评点赞数 |
| 2 | `SKU_ID` | 子规格唯一 ID | 16 | `实时库存` | SKU 独立实时库存 |
| 3 | `UPC条形码(69码)` | 跨店 100% 精准同品匹配依据 | 17 | `原价(元)` | SKU 独立划线原价 |
| 4 | `商品名称` | 完整商品标题 | 18 | `直接折扣价(元)` | **SKU 独立真实直接折扣价** |
| 5 | `规格类型` | 仅 `单规格` / `多规格` | 19 | `神价/到手价(元)` | SKU 独立券后/神价到手价 |
| 6 | `子规格名称(spec)` | 区分多规格子项（含 `combine_spec` 兜底） | 20 | `到手价说明` | 到手价标签说明 |
| 7 | `店内一级分类` | 优先归属真实货架实体分类 | 21 | `竞对有效基准价(元)` | 剔除 $\le 0.1$ 元单件霸王餐后的最低有效价 |
| 8 | `店内二级分类` | 还原竞对细分货架分类 | 22 | `促销标签` | 如 `7折 限1份` |
| 9 | `店内分类排名` | 该商品在店内分类的排序位置 | 23 | `满减商品券` | 品类券/满减券标签 |
| 10 | `美团一级类目` | 美团官方标准一级类目 | 24 | `限购份数` | 单笔限购份数（含 `quota_per_order`） |
| 11 | `美团二级类目` | 美团官方标准二级类目 | 25 | `最小起购数` | 最小起购件数 |
| 12 | `美团三级类目名称` | 美团官方标准三级类目 | 26 | `售卖属性(温度/提示)` | 如 `温度:常温,冰镇` |
| 13 | `月销量(数值)` | SPU 聚合月售（解析自文本） | 27 | `高清无水印主图URL` | 去水印/裁剪/`.webp` 的原图 URL |
| 14 | `想买人数` | SPU 聚合想买人数（无空值） | — | — | — |

> **多规格数据边界说明**：`SKU_ID`、69 条码、子规格名、原价、直接折扣价、神价到手价、实时库存、限购份数、最小起购数均由批量接口按子规格独立返回；`月销量`、`想买人数`、`点赞数` 美团底层仅在 SPU 维度披露，因此同 SPU 下各子规格行保持一致。

---

## 4. 二阶段按需能力：单品轮播副图、详情长图与属性参数表（新品建品素材）

日常比价只需执行阶段一全店批量采集；当从 27 列竞对表中筛出我方缺失的高销新品、需要引进建品时，按需调用 `scripts/fetch_spu_details.py`（底层 `getCurrentPages()[0].api.productInfoV2` $\rightarrow$ `/quickbuy/v2/poi/product/info`），内置同样安全的防 `418`/`403` 节奏（`18~28s` + 125s 滑窗 + 403 硬熔断 + C 端 Token 自愈）和 `unify_price` v5 折扣价解析：

```bash
# 方式 A：按指定 SPU_ID 列表拉取副图、详情长图与属性表，并下载分类图片包
python3 scripts/fetch_spu_details.py \
  --port 9222 \
  --spu-ids "26963575572,26963143789" \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --out-jsonl "./crawl_data/store_a/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx" \
  --download-images-dir "./新品素材图包"

# 方式 B：按真实解析月销自动筛 Top N 热销品拉取详情与图片
python3 scripts/fetch_spu_details.py \
  --port 9222 \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --top-n-sales 30 \
  --out-jsonl "./crawl_data/store_a/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx"
```

---

## 5. 延伸参考文档

- 完整风控逆向证据、三账号压测对照表与排错速查：阅读 `references/anti_418_protocol.md`。
