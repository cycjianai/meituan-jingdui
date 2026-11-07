#!/usr/bin/env python3
"""
Export Meituan H5 Shangou Full-Dimension Competitor Data to Excel.
Part of the `meituanjingdui` skill.
"""
import os
import re
import json
import argparse
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment


def parse_monthly_sales(saled_content, saled_num=0):
    if saled_num and int(saled_num) > 0:
        return int(saled_num)
    txt = str(saled_content or '').strip()
    if not txt:
        return 0
    m_wan = re.search(r'(\d+(?:\.\d+)?)\s*万', txt)
    if m_wan:
        return int(float(m_wan.group(1)) * 10000)
    m_num = re.search(r'(\d+)', txt)
    if m_num:
        return int(m_num.group(1))
    return 0


def clean_pic_url(url):
    if not url:
        return ''
    return re.sub(r'@watermark=.*$', '', str(url))


def extract_std_categories(spu):
    cats = spu.get('standardCategorys') or []
    l1_id, l1_name = '', ''
    l2_id, l2_name = '', ''
    l3_id, l3_name = '', ''
    for c in cats:
        lvl = c.get('level')
        if lvl == 1:
            l1_id, l1_name = c.get('id', ''), c.get('name', '')
        elif lvl == 2:
            l2_id, l2_name = c.get('id', ''), c.get('name', '')
        elif lvl == 3:
            l3_id, l3_name = c.get('id', ''), c.get('name', '')
    return l1_id, l1_name, l2_id, l2_name, l3_id, l3_name


def extract_coupon_tags(spu):
    labels = spu.get('dynamic_act_labels') or []
    texts = []
    for lb in labels:
        for st in (lb.get('sub_tags') or []):
            t = (st.get('text') or '').strip()
            if t:
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
    Compute realistic competitor benchmark price (excluding 0.01 / <0.5折 1-piece new-customer loss-leader traps).
    """
    candidates = []
    for p in (actual_price, sku_price):
        if p is not None and p != '' and float(p) > 0:
            candidates.append(float(p))
    orig = float(orig_price) if (orig_price is not None and orig_price != '') else 0.0
    if not candidates:
        return orig
    lowest = min(candidates)
    # If 0.01 or <= 0.1 yuan new-customer loss-leader trap with restrict==1, fall back to origin_price
    if lowest <= 0.1 and orig > 1.0 and (restrict_cnt == 1 or '0.0' in str(promo_str or '')):
        return orig
    return lowest


def build_excel(spus_jsonl, cat_json, out_xlsx):
    spus = [json.loads(line) for line in open(spus_jsonl, 'r', encoding='utf-8') if line.strip()]
    cat_map = json.load(open(cat_json, 'r', encoding='utf-8')) if (cat_json and os.path.exists(cat_json)) else {}

    # Build category rank lookup from regular categories (idx >= 7 first, then 0..6)
    spu_cat_info = {}
    sorted_cat_keys = sorted(cat_map.keys(), key=lambda k: (0 if int(k) >= 7 else 1, int(k)))
    for k in sorted_cat_keys:
        c = cat_map[k]
        cname = c.get('name', '')
        for rank_idx, sid in enumerate(c.get('allSortedSpuId') or [], 1):
            sid_int = int(sid)
            if sid_int not in spu_cat_info:
                spu_cat_info[sid_int] = (int(k), cname, rank_idx, len(c.get('allSortedSpuId') or []))

    # Enrich and sort SPUs
    for s in spus:
        sid = int(s['id'])
        s['_sales_num'] = parse_monthly_sales(s.get('month_saled_content'), s.get('month_saled'))
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

    spus.sort(key=lambda x: (-x['_sales_num'], -int(x.get('want_to_Buy') or 0), x['_store_cat_idx'], x['_store_cat_rank']))

    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)

    # Sheet 1: 全量SPU竞对主表 (按销量降序)
    ws1 = wb.active
    ws1.title = '全量SPU竞对总表(按销量)'
    headers1 = [
        'SPU_ID', '主SKU_ID', 'UPC条形码(69码)', '商品名称', '规格类型', '子规格数',
        '店内一级分类', '店内分类排名', '美团一级类目', '美团二级类目', '美团三级类目ID', '美团三级类目名称',
        '月售文本', '月销量(数值)', '想买人数', '点赞数', '实时总库存',
        '原价(元)', '直接折扣价(元)', '神价/到手价(元)', '到手价说明', '竞对有效基准价(元)',
        '建议我方引流定价(95折)', '30%保本毛利允许最高进价',
        '促销标签', '满减商品券', '限购份数', '最小起购数', '包装费(元)',
        '多规格全明细(规格:折后价/原价/条码/库存)', '售卖属性(温度/提示)', '卖点描述', '高清无水印主图URL'
    ]
    ws1.append(headers1)
    for cell in ws1[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    # Sheet 2: 全量SKU子规格拆解明细表
    ws2 = wb.create_sheet('SKU子规格级比价与条码明细')
    headers2 = [
        'SPU_ID', 'SKU_ID', 'UPC条形码(69码)', '商品名称', '规格类型', '子规格名称(spec)', '组合规格(combine_spec)',
        '店内一级分类', '美团三级类目ID', '美团三级类目名称',
        'SPU月销量(数值)', '想买人数', '子SKU实时库存', '子SKU活动库存',
        '子SKU原价(元)', '子SKU直接折扣价(元)', 'SPU神价到手价(元)', '竞对有效基准价(元)',
        '子SKU促销标签', '限购份数', '起购数', '包装费(元)', '子规格高清图URL'
    ]
    ws2.append(headers2)
    for cell in ws2[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    l3_stats = defaultdict(lambda: {'count': 0, 'pos_sales_cnt': 0, 'total_sales': 0, 'total_stock': 0, 'top_name': '', 'top_sales': -1, 'l1_name': '', 'l2_name': '', 'l3_name': ''})

    for s in spus:
        skus = s.get('skus') or [{}]
        first_sku = skus[0] if skus else {}
        sku_prices = set(k.get('price') for k in skus if k.get('price') is not None)
        if len(skus) <= 1:
            spec_type = '单规格'
        elif len(sku_prices) <= 1:
            spec_type = f'多规格同价({len(skus)}个子规格)'
        else:
            spec_type = f'多规格不同价({len(skus)}个子规格)'

        l1_id, l1_name, l2_id, l2_name, l3_id, l3_name = extract_std_categories(s)
        unify = s.get('unify_price') or {}
        actual_info = unify.get('actual_price_info') or {}
        actual_price = actual_info.get('actual_price')
        actual_text = actual_info.get('actual_price_text', '')

        orig_p = first_sku.get('origin_price', s.get('min_price'))
        disc_p = first_sku.get('price', s.get('min_price'))
        restrict_cnt = first_sku.get('restrict', 0)
        promo_str = s.get('promotion_info') or first_sku.get('promotion_info') or ''

        eff_price = compute_effective_competitor_price(orig_p, disc_p, actual_price, promo_str, restrict_cnt)
        our_target_price = round(eff_price * 0.95, 2) if eff_price > 0 else ''
        max_allow_cost = round(our_target_price * 0.70, 2) if isinstance(our_target_price, float) else ''

        total_stock = sum(int(k.get('real_stock') or k.get('stock') or 0) for k in skus)
        multi_parts = []
        for k in skus:
            multi_parts.append(
                f"{k.get('spec') or '默认'}:¥{k.get('price')}(原¥{k.get('origin_price')}|码:{k.get('upccode') or '无'}|库:{k.get('real_stock', k.get('stock', 0))})"
            )
        multi_str = ' ; '.join(multi_parts) if len(skus) > 1 else ''

        ws1.append([
            str(s.get('id', '')),
            str(first_sku.get('id', '')),
            str(first_sku.get('upccode') or ''),
            s.get('name', ''),
            spec_type,
            len(skus),
            s.get('_store_cat_name', ''),
            s.get('_store_cat_rank', ''),
            l1_name,
            l2_name,
            str(l3_id),
            l3_name,
            s.get('month_saled_content', ''),
            s.get('_sales_num', 0),
            s.get('want_to_Buy', 0),
            s.get('praise_num_new', 0),
            total_stock,
            orig_p,
            disc_p,
            actual_price if actual_price is not None else '',
            actual_text,
            eff_price,
            our_target_price,
            max_allow_cost,
            promo_str,
            extract_coupon_tags(s),
            restrict_cnt,
            first_sku.get('min_order_count', 1),
            first_sku.get('box_price', 0),
            multi_str,
            extract_attrs_str(s),
            ' | '.join(s.get('selling_points') or []) or s.get('description', ''),
            clean_pic_url(s.get('picture') or first_sku.get('picture'))
        ])

        for k in skus:
            k_orig = k.get('origin_price', orig_p)
            k_disc = k.get('price', disc_p)
            k_rest = k.get('restrict', 0)
            k_promo = k.get('promotion_info') or promo_str
            k_eff = compute_effective_competitor_price(k_orig, k_disc, actual_price if len(skus) == 1 else None, k_promo, k_rest)
            ws2.append([
                str(s.get('id', '')),
                str(k.get('id', '')),
                str(k.get('upccode') or ''),
                s.get('name', ''),
                spec_type,
                k.get('spec', ''),
                k.get('combine_spec', ''),
                s.get('_store_cat_name', ''),
                str(l3_id),
                l3_name,
                s.get('_sales_num', 0),
                s.get('want_to_Buy', 0),
                k.get('real_stock', k.get('stock', 0)),
                k.get('activity_stock', 0),
                k_orig,
                k_disc,
                actual_price if (actual_price is not None and len(skus) == 1) else '',
                k_eff,
                k_promo,
                k_rest,
                k.get('min_order_count', 1),
                k.get('box_price', 0),
                clean_pic_url(k.get('picture') or s.get('picture'))
            ])

        # Update L3 stats
        st = l3_stats[l3_id or 'UNKNOWN']
        st['l1_name'] = l1_name
        st['l2_name'] = l2_name
        st['l3_name'] = l3_name or '未分类'
        st['count'] += 1
        if s['_sales_num'] > 0:
            st['pos_sales_cnt'] += 1
        st['total_sales'] += s['_sales_num']
        st['total_stock'] += total_stock
        if s['_sales_num'] > st['top_sales']:
            st['top_sales'] = s['_sales_num']
            st['top_name'] = s.get('name', '')

    # Sheet 3: 美团三级标准类目动销汇总
    ws3 = wb.create_sheet('美团三级标准类目汇总')
    headers3 = [
        '美团三级类目ID', '美团三级类目名称', '美团一级类目', '美团二级类目',
        'SPU总数', '有销量SPU数', '动销率', '类目总月销', '类目总实时库存', '类目销冠商品名', '销冠月销'
    ]
    ws3.append(headers3)
    for cell in ws3[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    for cid, st in sorted(l3_stats.items(), key=lambda item: (-item[1]['total_sales'], -item[1]['count'])):
        cnt = st['count']
        pos = st['pos_sales_cnt']
        ws3.append([
            str(cid),
            st['l3_name'],
            st['l1_name'],
            st['l2_name'],
            cnt,
            pos,
            f"{pos / max(1, cnt) * 100:.1f}%",
            st['total_sales'],
            st['total_stock'],
            st['top_name'],
            st['top_sales']
        ])

    wb.save(out_xlsx)
    print(f"[Export Done] Saved {len(spus)} SPUs across 3 sheets to {out_xlsx}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Export Meituan H5 Competitor JSONL to Excel')
    parser.add_argument('--spus-jsonl', required=True, help='Path to spus.jsonl')
    parser.add_argument('--cat-json', default=None, help='Path to categories.json')
    parser.add_argument('--out-xlsx', required=True, help='Output Excel path')
    args = parser.parse_args()
    build_excel(args.spus_jsonl, args.cat_json, args.out_xlsx)


if __name__ == '__main__':
    main()
