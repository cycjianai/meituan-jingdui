#!/usr/bin/env python3
r"""
Export Meituan H5 Shangou Full-Dimension Competitor Data to a Single-Sheet Excel.
Part of the `meituanjingdui` skill.

HARD-LOCKED SPECIFICATION (DO NOT MODIFY — STRICTLY ENFORCED):
1. Worksheet count & name: Strictly 1 worksheet named `全量商品竞对明细`.
2. Column headers: Strictly locked to the exact 27 columns in `LOCKED_27_HEADERS`.
3. `规格类型` values: Strictly locked to ONLY `'单规格'` or `'多规格'`.
4. Output filename format: `{MMDD}{店铺名称}.xlsx` (e.g., `1108满家喜超市长清店.xlsx`, parentheses stripped).

CRITICAL RAW DATA MAPPING RULES (Empirically Verified against Meituan H5 `unify_price` v5):
- Col 3  (`UPC条形码(69码)`): `k.upccode` -> `k.supply_poi_sku.upccode`.
- Col 6  (`子规格名称(spec)`): `k.spec -> k.combine_spec -> k.cspu_grouping_value`
  (multi-pack items often leave `k.spec` empty `""` and store `'9片*4包'` in `k.combine_spec`).
- Col 8  (`店内二级分类`): Resolve via `s.tag` / `k.tagBelongTo` in `sub_tag_lookup`, then `s.id` in
  `sub_tags[].allSortedSpuId` (SPUs fetched via `sub_idx=0` `'全部'` have `s.tag` set to the L1 tag).
- Col 13 (`月销量(数值)`): `s.month_saled` is `0` on ~95% of items; MUST parse `s.month_saled_content` (`"月售9"`).
- Col 14 (`想买人数`): `s.want_to_Buy` is omitted (`None`) on new items; parse `s.want_to_Buy` / `s.want_to_buy_content` -> `0`.
- Col 15 (`点赞数`): `s.praise_num_new` is `0` on `smoothRender` items while `s.praise_num` and `s.praise_content` (`"赞9"`)
  hold the true praise count; take `max(praise_num_new, praise_num, praise_count, parsed(praise_content))`.
- Col 17 (`原价(元)`): Read `k.unify_price.underlined_price` -> `k.unify_price.activity_info.underline_price` -> `k.origin_price`.
- Col 18 (`直接折扣价(元)`): In `unify_price` v5, `k.price` equals the original price (`4.98`) on ~99% of discounted items,
  while the true direct discount price (`3.5`) is inside `k.unify_price.activity_info.activity_price` (or `item_details[0].act_price`).
- Col 19 & 20 (`神价/到手价(元)` & `到手价说明`): Read SKU-level `k.unify_price.actual_price_info.actual_price` and `actual_price_text`
  first (so multi-SKU sub-items each get their own independent hand price), falling back to `secondary_activity_price` / SPU-level.
- Col 22 (`促销标签`): Read `k.promotion_info` / `k.promotion.promotion_text` / `s.promotion_info`; if empty on `unify_price` v5
  discounted items, reconstruct from `activity_info.discount_strength` (`(1 - discount_strength) * 10` 折) + `quota_per_order` (`限N份`).
- Col 23 (`满减商品券`): Read `k.dynamic_act_labels` or `s.dynamic_act_labels` (`sub_tags[].text`).
- Col 24 (`限购份数`): Read `k.restrict` if `> 0`, else `k.unify_price.activity_info.quota_per_order` if `> 0`, else `0`.
- Col 25 (`最小起购数`): `max(k.min_order_count, k.unify_price.activity_info.min_order_count, 1)`.
- Col 27 (`高清无水印主图URL`): Read `k.picture` or `s.picture`, stripping `@watermark=.*$`, `/\d+\.\d+\.\d+/`, and `.webp$`.
"""
import argparse
import json
import os
import re
from datetime import datetime
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill


# Strictly locked 27 columns — NEVER alter order, names, or count
LOCKED_27_HEADERS = (
    'SPU_ID',
    'SKU_ID',
    'UPC条形码(69码)',
    '商品名称',
    '规格类型',
    '子规格名称(spec)',
    '店内一级分类',
    '店内二级分类',
    '店内分类排名',
    '美团一级类目',
    '美团二级类目',
    '美团三级类目名称',
    '月销量(数值)',
    '想买人数',
    '点赞数',
    '实时库存',
    '原价(元)',
    '直接折扣价(元)',
    '神价/到手价(元)',
    '到手价说明',
    '竞对有效基准价(元)',
    '促销标签',
    '满减商品券',
    '限购份数',
    '最小起购数',
    '售卖属性(温度/提示)',
    '高清无水印主图URL',
)

LOCKED_SPEC_TYPES = ('单规格', '多规格')
PROMO_TAB_KEYWORDS = (
    '推荐', '神价', '活动', '热销', '折扣', '狂欢', '促销', '特惠',
    '满减', '爆品', '秒杀', '囤货', '买过', '常买', '优惠', '新品上新',
)


def format_locked_excel_path(out_dir, store_name, date_mmdd=None):
    """
    Strictly format output filename as: `{MMDD}{店铺名称}.xlsx`
    e.g. `1108满家喜超市长清店.xlsx` (strip parentheses and whitespace).
    """
    mmdd = date_mmdd or datetime.now().strftime('%m%d')
    clean_store = re.sub(r'[（）()\s/\\:*?"<>|]+', '', str(store_name or '竞对门店').strip())
    return os.path.join(out_dir, f"{mmdd}{clean_store}.xlsx")


def parse_int_from_text(txt):
    s = str(txt or '').strip()
    if not s:
        return 0
    m_wan = re.search(r'(\d+(?:\.\d+)?)\s*万', s)
    if m_wan:
        return int(float(m_wan.group(1)) * 10000)
    m_num = re.search(r'(\d+)', s)
    if m_num:
        return int(m_num.group(1))
    return 0


def parse_monthly_sales(saled_content, saled_num=0):
    n1 = int(saled_num) if (saled_num is not None and str(saled_num).isdigit()) else 0
    n2 = parse_int_from_text(saled_content)
    return max(n1, n2)


def parse_max_int_metric(*vals):
    best = 0
    for v in vals:
        if v is None or v == '':
            continue
        if isinstance(v, (int, float)):
            best = max(best, int(v))
        else:
            best = max(best, parse_int_from_text(v))
    return best


def clean_pic_url(url):
    if not url:
        return ''
    u = re.sub(r'@watermark=.*$', '', str(url).strip())
    u = re.sub(r'/\d+\.\d+\.\d+/', '/', u)
    u = re.sub(r'\.webp$', '', u, flags=re.IGNORECASE)
    return u


def extract_std_categories(spu):
    cats = spu.get('standardCategorys') or []
    l1_name, l2_name, l3_name = '', '', ''
    for c in cats:
        lvl = c.get('level')
        if lvl == 1:
            l1_name = c.get('name', '')
        elif lvl == 2:
            l2_name = c.get('name', '')
        elif lvl == 3:
            l3_name = c.get('name', '')
    return l1_name, l2_name, l3_name


def extract_coupon_tags(spu, sku=None):
    labels = (sku.get('dynamic_act_labels') if isinstance(sku, dict) and sku.get('dynamic_act_labels') else None) or spu.get('dynamic_act_labels') or []
    texts = []
    for lb in labels:
        for st in (lb.get('sub_tags') or []):
            t = (st.get('text') or '').strip()
            if t and t not in texts:
                texts.append(t)
    return ' | '.join(texts)


def extract_attrs_str(spu):
    attrs = spu.get('attrs') or []
    parts = []
    for a in attrs:
        aname = a.get('name', '')
        vals = [v.get('value', '') for v in (a.get('values') or []) if v.get('value')]
        if vals:
            parts.append(f"{aname}:{','.join(vals)}")
    return ' ; '.join(parts)


def compute_effective_competitor_price(orig_price, sku_price, actual_price, promo_str, restrict_cnt):
    """
    Compute realistic competitor benchmark price (excluding 0.01 / <=0.1 yuan 1-piece new-customer loss-leader traps).
    """
    candidates = []
    for p in (actual_price, sku_price):
        if p is not None and p != '' and float(p) > 0:
            candidates.append(float(p))
    orig = float(orig_price) if (orig_price is not None and orig_price != '') else 0.0
    if not candidates:
        return orig
    lowest = min(candidates)
    if lowest <= 0.1 and orig > 1.0 and (restrict_cnt == 1 or '0.0' in str(promo_str or '')):
        return orig
    return lowest


def is_promo_category(cat_obj):
    if 'is_promo' in cat_obj:
        return bool(cat_obj['is_promo'])
    tag_str = str(cat_obj.get('tag') or '').strip()
    name = str(cat_obj.get('name') or '').strip()
    cat_type = int(cat_obj.get('type') or 0)
    is_num = tag_str.isdigit() or (tag_str.endswith('_27') and tag_str[:-3].isdigit())
    if not is_num and cat_type not in (1, 27):
        return True
    return any(kw in name for kw in PROMO_TAB_KEYWORDS)


def resolve_store_l2_name(spu, l1_name, sub_tag_lookup, spu_l2_by_id):
    sid = int(spu.get('id') or 0)
    raw_tag = str(spu.get('tag') or '')
    k0 = (spu.get('skus') or [{}])[0]
    tbt = str((k0.get('tagBelongTo') if isinstance(k0, dict) else '') or '')
    if raw_tag in sub_tag_lookup:
        name = sub_tag_lookup[raw_tag]
        if name and name != '全部':
            return name
    if tbt in sub_tag_lookup:
        name = sub_tag_lookup[tbt]
        if name and name != '全部':
            return name
    if sid in spu_l2_by_id:
        return spu_l2_by_id[sid]
    return l1_name


def extract_sku_pricing_and_promo(spu, sku, is_multi):
    """
    Unified Meituan `unify_price` v5 extraction shared across Excel export and detail export.
    Returns: (orig_price, direct_disc_price, actual_price, actual_text, restrict_cnt, min_order, promo_str)
    """
    spu_unify = spu.get('unify_price') or {}
    spu_act_info = spu_unify.get('activity_info') or {}
    spu_actual_info = spu_unify.get('actual_price_info') or {}
    spu_actual_price = spu_actual_info.get('actual_price')
    spu_actual_text = spu_actual_info.get('actual_price_text', '')
    spu_promo_str = spu.get('promotion_info') or spu.get('recommend_promotion_info') or ''

    ku = sku.get('unify_price') or {}
    kai = ku.get('activity_info') or (spu_act_info if not is_multi else {})
    k_actual_info = ku.get('actual_price_info') or (spu_actual_info if not is_multi else {})

    # 1. 原价(元)
    raw_orig = (
        ku.get('underlined_price')
        if (ku.get('underlined_price') is not None and float(ku.get('underlined_price')) > 0)
        else (
            kai.get('underline_price')
            if (kai.get('underline_price') is not None and float(kai.get('underline_price')) > 0)
            else sku.get('origin_price', spu.get('min_price'))
        )
    )
    k_orig = float(raw_orig) if (raw_orig is not None and raw_orig != '') else 0.0

    # 2. 直接折扣价(元): `item_details[0].act_price` -> `activity_info.activity_price` -> `sku.price`
    item_details = kai.get('item_details') or []
    act_p = kai.get('activity_price')
    legacy_p = sku.get('price', spu.get('min_price'))
    if item_details and isinstance(item_details[0], dict) and (item_details[0].get('act_price') or 0) > 0:
        k_disc = float(item_details[0]['act_price'])
    elif act_p is not None and float(act_p) > 0:
        if legacy_p is not None and 0 < float(legacy_p) < float(act_p):
            k_disc = float(legacy_p)
        else:
            k_disc = float(act_p)
    else:
        k_disc = float(legacy_p) if (legacy_p is not None and legacy_p != '') else k_orig

    # 3. 神价/到手价(元) & 到手价说明 (SKU-level first)
    k_actual_val = k_actual_info.get('actual_price')
    k_sec_val = kai.get('secondary_activity_price')
    if k_actual_val is not None and float(k_actual_val) > 0:
        sku_actual_p = float(k_actual_val)
        actual_text = k_actual_info.get('actual_price_text') or '到手价'
    elif k_sec_val is not None and float(k_sec_val) > 0:
        sku_actual_p = float(k_sec_val)
        actual_text = '到手价'
    elif spu_actual_price is not None and float(spu_actual_price) > 0 and (not is_multi or k_disc == spu.get('min_price')):
        sku_actual_p = float(spu_actual_price)
        actual_text = spu_actual_text or '到手价'
    else:
        sku_actual_p = ''
        actual_text = ''

    # 4. 限购份数: `restrict` or `activity_info.quota_per_order`
    legacy_rest = int(sku.get('restrict') or 0)
    quota_rest = int(kai.get('quota_per_order') or 0) if (kai.get('quota_per_order') or -1) > 0 else 0
    k_rest = legacy_rest if legacy_rest > 0 else quota_rest

    # 5. 最小起购数
    k_min_order = max(int(sku.get('min_order_count') or 1), int(kai.get('min_order_count') or 1))

    # 6. 促销标签: fallback to `discount_strength` + `quota_per_order`
    k_prom_obj = sku.get('promotion') or {}
    k_promo = (sku.get('promotion_info') or k_prom_obj.get('promotion_text') or spu_promo_str or '').strip()
    if not k_promo and k_orig > 0 and k_disc < k_orig:
        disc_strength = float(kai.get('discount_strength') or 0)
        if 0 < disc_strength < 1:
            fold = round((1.0 - disc_strength) * 10, 2)
        else:
            fold = round((k_disc / k_orig) * 10, 2)
        fold_str = f"{fold:g}折"
        k_promo = f"{fold_str} 限{k_rest}份" if k_rest > 0 else fold_str

    return k_orig, k_disc, sku_actual_p, actual_text, k_rest, k_min_order, k_promo


def build_excel(spus_jsonl, cat_json, out_xlsx):
    # Deduplicate by SPU ID in case jsonl was appended across retries
    spus_by_id = {}
    with open(spus_jsonl, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            sid = obj.get('id')
            if sid:
                spus_by_id[int(sid)] = obj
    spus = list(spus_by_id.values())

    cat_map = json.load(open(cat_json, 'r', encoding='utf-8')) if (cat_json and os.path.exists(cat_json)) else {}

    # Build L1 category rank lookup and L2 sub_tag_lookup + spu_l2_by_id from cat_map
    # Entity shelf categories ALWAYS take priority over promo aggregate categories
    spu_cat_info = {}
    sub_tag_lookup = {}
    spu_l2_by_id = {}
    sorted_cat_keys = sorted(
        [k for k in cat_map.keys() if str(k).isdigit()],
        key=lambda k: (1 if is_promo_category(cat_map[k]) else 0, int(k))
    )
    for k in sorted_cat_keys:
        c = cat_map[k]
        cname = c.get('name', '')
        for st in (c.get('sub_tags') or []):
            if isinstance(st, dict) and st.get('name') and st.get('name') != '全部':
                st_name = st['name']
                if st.get('tag'):
                    sub_tag_lookup[str(st['tag'])] = st_name
                for sub_sid in (st.get('allSortedSpuId') or []):
                    sub_sid_int = int(sub_sid)
                    if sub_sid_int not in spu_l2_by_id:
                        spu_l2_by_id[sub_sid_int] = st_name
        for rank_idx, sid in enumerate(c.get('allSortedSpuId') or [], 1):
            sid_int = int(sid)
            if sid_int not in spu_cat_info:
                spu_cat_info[sid_int] = (int(k), cname, rank_idx, len(c.get('allSortedSpuId') or []))

    # Enrich and sort SPUs
    for s in spus:
        sid = int(s['id'])
        s['_sales_num'] = parse_monthly_sales(s.get('month_saled_content'), s.get('month_saled'))
        s['_want_num'] = parse_max_int_metric(s.get('want_to_Buy'), s.get('want_to_buy_content'))
        s['_praise_num'] = parse_max_int_metric(
            s.get('praise_num_new'),
            s.get('praise_num'),
            s.get('praise_count'),
            s.get('praise_content')
        )
        if sid in spu_cat_info:
            c_idx, c_name, c_rank, c_tot = spu_cat_info[sid]
            s['_store_cat_idx'] = c_idx
            s['_store_cat_name'] = c_name
            s['_store_cat_rank'] = c_rank
            s['_store_cat_total'] = c_tot
        else:
            s['_store_cat_idx'] = s.get('_l1_idx', 999)
            s['_store_cat_name'] = s.get('_l1_name', '')
            s['_store_cat_rank'] = 9999
            s['_store_cat_total'] = 0
        s['_store_l2_name'] = resolve_store_l2_name(s, s['_store_cat_name'], sub_tag_lookup, spu_l2_by_id)

    spus.sort(key=lambda x: (-x['_sales_num'], -x['_want_num'], x['_store_cat_idx'], x['_store_cat_rank']))

    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)
    multi_fill = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')

    # Single Worksheet ONLY: 全量商品竞对明细 (包含所有规格逐行展开，严格锁定27列)
    ws = wb.active
    ws.title = '全量商品竞对明细'
    headers = list(LOCKED_27_HEADERS)
    assert len(headers) == 27, f"Header count must be 27, got {len(headers)}"
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    total_sku_rows = 0
    multi_spu_count = 0
    multi_sku_rows = 0
    stats = {
        'discounted_rows': 0,
        'hand_price_rows': 0,
        'promo_label_rows': 0,
        'restrict_rows': 0,
        'upc_rows': 0,
        'sales_gt0_spus': 0,
        'praise_gt0_spus': 0,
        'l2_diff_l1_rows': 0,
    }

    for s in spus:
        if s['_sales_num'] > 0:
            stats['sales_gt0_spus'] += 1
        if s['_praise_num'] > 0:
            stats['praise_gt0_spus'] += 1

        skus = s.get('skus') or [{}]
        # Strictly two options for 规格类型: '单规格' or '多规格'
        is_multi = len(skus) > 1
        spec_type = '多规格' if is_multi else '单规格'
        assert spec_type in LOCKED_SPEC_TYPES
        if is_multi:
            multi_spu_count += 1

        l1_name, l2_name, l3_name = extract_std_categories(s)
        attrs_str = extract_attrs_str(s)
        spu_pic = clean_pic_url(s.get('picture'))

        for k in skus:
            k_orig, k_disc, sku_actual_p, actual_text, k_rest, k_min_order, k_promo = extract_sku_pricing_and_promo(s, k, is_multi)
            spec_str = str(k.get('spec') or k.get('combine_spec') or k.get('cspu_grouping_value') or '').strip()
            upc_str = str(k.get('upccode') or (k.get('supply_poi_sku') or {}).get('upccode') or '').strip()
            raw_rs = k.get('real_stock')
            real_stock_val = int(raw_rs) if (raw_rs is not None and int(raw_rs) >= 0) else int(k.get('stock') or 0)
            coupon_tags = extract_coupon_tags(s, k)
            pic_url = clean_pic_url(k.get('picture')) or spu_pic
            k_eff = compute_effective_competitor_price(k_orig, k_disc, sku_actual_p if sku_actual_p != '' else None, k_promo, k_rest)

            if k_orig > 0 and k_disc < k_orig:
                stats['discounted_rows'] += 1
            if sku_actual_p != '':
                stats['hand_price_rows'] += 1
            if k_promo:
                stats['promo_label_rows'] += 1
            if k_rest > 0:
                stats['restrict_rows'] += 1
            if upc_str:
                stats['upc_rows'] += 1
            if s.get('_store_l2_name') and s.get('_store_l2_name') != s.get('_store_cat_name'):
                stats['l2_diff_l1_rows'] += 1

            row_data = [
                str(s.get('id', '')),
                str(k.get('id', '')),
                upc_str,
                s.get('name') or k.get('name') or '',
                spec_type,
                spec_str,
                s.get('_store_cat_name', ''),
                s.get('_store_l2_name', ''),
                s.get('_store_cat_rank', ''),
                l1_name,
                l2_name,
                l3_name,
                s.get('_sales_num', 0),
                s.get('_want_num', 0),
                s.get('_praise_num', 0),
                real_stock_val,
                k_orig,
                k_disc,
                sku_actual_p,
                actual_text if sku_actual_p != '' else '',
                k_eff,
                k_promo,
                coupon_tags,
                k_rest,
                k_min_order,
                attrs_str,
                pic_url
            ]
            assert len(row_data) == 27, f"Row length must be 27, got {len(row_data)}"
            ws.append(row_data)
            total_sku_rows += 1
            if is_multi:
                for cell in ws[ws.max_row]:
                    cell.fill = multi_fill
                multi_sku_rows += 1

    # Hard-locked contract assertions before saving
    assert len(wb.sheetnames) == 1 and wb.sheetnames[0] == '全量商品竞对明细', "Must have strictly 1 sheet named '全量商品竞对明细'"
    actual_headers = tuple(cell.value for cell in ws[1])
    assert actual_headers == LOCKED_27_HEADERS, "Headers violated LOCKED_27_HEADERS contract!"

    os.makedirs(os.path.dirname(os.path.abspath(out_xlsx)), exist_ok=True)
    wb.save(out_xlsx)
    print(
        f"[Export Done] Single sheet '{ws.title}' (27 locked cols): {len(spus)} SPUs -> {total_sku_rows} SKU rows "
        f"(单规格: {len(spus)-multi_spu_count}, 多规格: {multi_spu_count} SPUs / {multi_sku_rows} rows) -> {out_xlsx}",
        flush=True
    )
    print(
        f"[Self-Check] 折扣价<原价: {stats['discounted_rows']} 行 | 神价/到手价: {stats['hand_price_rows']} 行 | "
        f"促销标签: {stats['promo_label_rows']} 行 | 限购>0: {stats['restrict_rows']} 行 | "
        f"69条码: {stats['upc_rows']} 行 | 月销>0: {stats['sales_gt0_spus']} SPU | 点赞>0: {stats['praise_gt0_spus']} SPU",
        flush=True
    )
    if total_sku_rows >= 100 and stats['discounted_rows'] == 0:
        print("[WARNING] 0 discounted rows detected on a store with >=100 SKUs! Inspect raw JSON `unify_price` structure immediately!", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Export Meituan H5 Competitor JSONL to Single-Sheet 27-Col Excel')
    parser.add_argument('--spus-jsonl', required=True, help='Path to spus.jsonl')
    parser.add_argument('--cat-json', default=None, help='Path to categories.json')
    parser.add_argument('--out-xlsx', default=None, help='Explicit output Excel path (optional if --out-dir and --store-name are given)')
    parser.add_argument('--out-dir', default='.', help='Output directory when auto-naming {MMDD}{店铺名称}.xlsx')
    parser.add_argument('--store-name', default='', help='Store name for locked {MMDD}{店铺名称}.xlsx naming convention')
    parser.add_argument('--date-mmdd', default=None, help='Optional MMDD override (defaults to today MMDD)')
    args = parser.parse_args()

    out_xlsx = args.out_xlsx
    if not out_xlsx:
        out_xlsx = format_locked_excel_path(args.out_dir, args.store_name, args.date_mmdd)
    build_excel(args.spus_jsonl, args.cat_json, out_xlsx)


if __name__ == '__main__':
    main()
