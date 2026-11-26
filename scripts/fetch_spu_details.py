#!/usr/bin/env python3
"""
Step 2 On-Demand Single-Product Detail & Image Fetcher (`fetch_spu_details.py`)
Part of the `meituanjingdui` skill.

Fetches carousel sub-images (`pictures`), graphic detail long images (`pic_content.contents`),
structured product attributes (`standard_productinfo_list`), sub-spec images (`skus[].picture`),
and text description (`description`) via `getCurrentPages()[0].api.productInfoV2` (`/quickbuy/v2/poi/product/info`)
for selected new/hot competitor products that need to be assembled into our own store's assortment.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from websockets.exceptions import WebSocketException

from crawl_h5_store import StorePage, connect_store_tab
from export_competitor_excel import (
    clean_pic_url,
    extract_sku_pricing_and_promo,
    parse_monthly_sales,
)


async def fetch_one_spu_detail(page, poi_id, spu_id, fallback):
    result = await page.in_store(poi_id, f"""
        const res = checked(await p.api.productInfoV2({{
            wm_poi_id: p.data.poiId || -100,
            poi_id_str: p.data.poi_id_str,
            spu_id: {json.dumps(spu_id)},
            sku_id: 0,
            spu_tag: {json.dumps(str(fallback.get('tag') or ''))},
            activity_tag: {json.dumps(str(fallback.get('activity_tag') or ''))}
        }}));
        return res && res.data;
    """)
    if not result:
        raise RuntimeError(f'SPU {spu_id}: no product data returned.')
    return result


def extract_detail_fields(raw_detail, fallback_spu=None):
    d = raw_detail or {}
    fb = fallback_spu or {}

    raw_pics = d.get('pictures') or []
    if not raw_pics and d.get('picture'):
        raw_pics = [d.get('picture')]
    if not raw_pics and fb.get('picture'):
        raw_pics = [fb.get('picture')]
    carousel_pics = [clean_pic_url(u) for u in raw_pics if clean_pic_url(u)]

    pic_content = d.get('pic_content') or {}
    raw_detail_pics = pic_content.get('contents') or []
    detail_pics = [clean_pic_url(u) for u in raw_detail_pics if clean_pic_url(u)]

    std_props = d.get('standard_productinfo_list') or []
    props_dict = {}
    props_pairs = []
    for item in std_props:
        fname = str(item.get('fieldName') or '').strip()
        fval = str(item.get('value') or '').strip()
        if fname and fval:
            props_dict[fname] = fval
            props_pairs.append(f'{fname}:{fval}')

    skus = d.get('skus') or fb.get('skus') or []
    sku_pics = []
    for k in skus:
        sp = clean_pic_url(k.get('picture'))
        spec_name = str(k.get('spec') or k.get('combine_spec') or k.get('cspu_grouping_value') or '').strip()
        if sp:
            sku_pics.append(f'{spec_name}:{sp}' if spec_name else sp)

    return {
        'spu_id': str(d.get('id') or fb.get('id') or ''),
        'name': d.get('name') or fb.get('name') or '',
        'main_pic': carousel_pics[0] if carousel_pics else clean_pic_url(fb.get('picture')),
        'sub_pics': carousel_pics[1:] if len(carousel_pics) > 1 else [],
        'all_carousel_pics': carousel_pics,
        'detail_pics': detail_pics,
        'sku_pics': sku_pics,
        'props_dict': props_dict,
        'props_str': ' ; '.join(props_pairs),
        'description': str(d.get('description') or fb.get('description') or '').strip(),
        'unit': str(d.get('unit') or fb.get('unit') or '').strip(),
        'skus': skus,
        'merged_spu': d or fb,
    }


def download_spu_images(parsed, base_img_dir):
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', '_', parsed['name'])[:40]
    spu_dir = os.path.join(base_img_dir, f"{parsed['spu_id']}_{safe_name}")
    os.makedirs(spu_dir, exist_ok=True)
    for idx, url in enumerate(parsed['all_carousel_pics'], 1):
        ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
        fname = f'01_主图_{idx}{ext}' if idx == 1 else f'02_副图_{idx-1}{ext}'
        fpath = os.path.join(spu_dir, fname)
        if not os.path.exists(fpath):
            try:
                urllib.request.urlretrieve(url, fpath)
            except Exception:
                pass
    for idx, url in enumerate(parsed['detail_pics'], 1):
        ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
        fname = f'03_详情图_{idx:02d}{ext}'
        fpath = os.path.join(spu_dir, fname)
        if not os.path.exists(fpath):
            try:
                urllib.request.urlretrieve(url, fpath)
            except Exception:
                pass


def export_details_excel(details_jsonl, out_xlsx):
    if not os.path.exists(details_jsonl):
        return
    records = [json.loads(line) for line in open(details_jsonl, 'r', encoding='utf-8') if line.strip()]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '新品建品详情与图片组装表'

    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)

    headers = [
        'SPU_ID', 'SKU_ID', 'UPC条形码(69码)', '商品名称',
        '规格类型', '子规格名称(spec)', '单位',
        '原价(元)', '直接折扣价(元)', '实时库存',
        '品牌', '产地', '净含量/规格参数', '保质期', '完整商品属性参数表(standard_productinfo_list)',
        '图文卖点描述(description)',
        '高清无水印主图URL', '轮播副图URL列表(竖线分隔)', '商品详情长图URL列表(竖线分隔)', '子规格图片URL'
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    for rec in records:
        fb = rec.get('spu') or {}
        parsed = extract_detail_fields(rec.get('detail'), fb)
        skus = parsed['skus'] or [{}]
        is_multi = len(skus) > 1
        spec_type = '多规格' if is_multi else '单规格'
        props = parsed['props_dict']
        brand = props.get('品牌', '')
        origin = props.get('产地', props.get('国产/进口', ''))
        net_wt = props.get('净含量', props.get('规格', ''))
        shelf_life = props.get('保质期', '')
        fb_skus_by_id = {str(x.get('id')): x for x in (fb.get('skus') or []) if isinstance(x, dict)}

        for k in skus:
            merged_k = dict(fb_skus_by_id.get(str(k.get('id')), {}))
            merged_k.update(k)
            k_orig, k_disc, _, _, _, _, _ = extract_sku_pricing_and_promo(parsed['merged_spu'], merged_k, is_multi)
            spec_str = str(merged_k.get('spec') or merged_k.get('combine_spec') or merged_k.get('cspu_grouping_value') or '').strip()
            upc_str = str(merged_k.get('upccode') or (merged_k.get('supply_poi_sku') or {}).get('upccode') or '').strip()
            raw_rs = merged_k.get('real_stock')
            real_stock_val = int(raw_rs) if (raw_rs is not None and int(raw_rs) >= 0) else int(merged_k.get('stock') or 0)

            ws.append([
                parsed['spu_id'],
                str(merged_k.get('id', '')),
                upc_str,
                parsed['name'],
                spec_type,
                spec_str,
                parsed['unit'],
                k_orig,
                k_disc,
                real_stock_val,
                brand,
                origin,
                net_wt,
                shelf_life,
                parsed['props_str'],
                parsed['description'],
                parsed['main_pic'],
                ' | '.join(parsed['sub_pics']),
                ' | '.join(parsed['detail_pics']),
                clean_pic_url(merged_k.get('picture')) or parsed['main_pic'],
            ])

    os.makedirs(os.path.dirname(os.path.abspath(out_xlsx)), exist_ok=True)
    wb.save(out_xlsx)
    print(f"[Details Excel Saved] {len(records)} SPUs exported to {out_xlsx}", flush=True)


async def fetch_details(args, target_ids, done_ids, spu_lookup):
    pending = [sid for sid in dict.fromkeys(target_ids) if sid not in done_ids]
    if not pending:
        return
    async with connect_store_tab(args.port) as ws:
        page = StorePage(ws)
        state = await page.inspect()
        if not state['inStorePage'] or not state['poi_id_str']:
            raise RuntimeError('Open the target store page in Chrome first.')
        for idx, sid in enumerate(pending, 1):
            fb = spu_lookup.get(sid) or {}
            detail = await fetch_one_spu_detail(page, state['poi_id_str'], sid, fb)
            rec = {'spu_id': sid, 'detail': detail, 'spu': fb}
            os.makedirs(os.path.dirname(os.path.abspath(args.out_jsonl)), exist_ok=True)
            with open(args.out_jsonl, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            parsed = extract_detail_fields(detail, fb)
            print(f"[{idx}/{len(pending)}] SPU {sid} ({parsed['name'][:20]}) -> 副图:{len(parsed['sub_pics'])}张, 详情长图:{len(parsed['detail_pics'])}张, 属性:{len(parsed['props_dict'])}项", flush=True)
            if args.download_images_dir:
                download_spu_images(parsed, args.download_images_dir)


def main():
    parser = argparse.ArgumentParser(description='Fetch Meituan H5 Single-Product Details (Carousel Sub-Images, Detail Images, Attributes)')
    parser.add_argument('--spu-ids', default='', help='Comma-separated SPU IDs to fetch')
    parser.add_argument('--spus-jsonl', default='', help='Optional path to existing spus.jsonl to look up tag/fallback info or filter top sales')
    parser.add_argument('--top-n-sales', type=int, default=0, help='Automatically select top N SPUs by monthly sales from --spus-jsonl')
    parser.add_argument('--out-jsonl', required=True, help='Output JSONL path for fetched product details')
    parser.add_argument('--out-xlsx', required=True, help='Output Excel path for new-product assembly')
    parser.add_argument('--download-images-dir', default='', help='Optional directory to download main/sub/detail images')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port')
    args = parser.parse_args()

    spu_lookup = {}
    if args.spus_jsonl and os.path.exists(args.spus_jsonl):
        for line in open(args.spus_jsonl, 'r', encoding='utf-8'):
            if line.strip():
                s = json.loads(line)
                spu_lookup[str(s.get('id'))] = s

    target_ids = [x.strip() for x in args.spu_ids.split(',') if x.strip()]
    if not target_ids and args.top_n_sales > 0 and spu_lookup:
        sorted_spus = sorted(
            spu_lookup.values(),
            key=lambda x: parse_monthly_sales(x.get('month_saled_content'), x.get('month_saled')),
            reverse=True
        )
        target_ids = [str(s['id']) for s in sorted_spus[:args.top_n_sales]]

    if not target_ids:
        raise SystemExit('Please provide --spu-ids or (--spus-jsonl with --top-n-sales N)')

    done_ids = set()
    if os.path.exists(args.out_jsonl):
        for line in open(args.out_jsonl, 'r', encoding='utf-8'):
            if line.strip():
                obj = json.loads(line)
                done_ids.add(str(obj.get('spu_id')))

    try:
        asyncio.run(fetch_details(args, target_ids, done_ids, spu_lookup))
    except (RuntimeError, OSError, WebSocketException) as exc:
        sys.exit(str(exc))
    finally:
        export_details_excel(args.out_jsonl, args.out_xlsx)


if __name__ == '__main__':
    main()
