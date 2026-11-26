#!/usr/bin/env python3
"""Collect the current Meituan H5 store's catalog and export its SKU workbook."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import websockets
from websockets.exceptions import WebSocketException

from export_competitor_excel import PROMO_TAB_KEYWORDS, build_excel, format_locked_excel_path


SKIP_VIRTUAL_TAGS = {'op0050'}
STORE_HOSTS = (
    'cactivityapi-sc.waimai.meituan.com',
    'wx-shangou.meituan.com',
    'h5.waimai.meituan.com',
)


def load_existing_spus(spus_jsonl):
    seen = {}
    if os.path.exists(spus_jsonl):
        with open(spus_jsonl, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    sid = obj.get('id')
                    if sid:
                        seen[int(sid)] = obj
                except Exception:
                    pass
    return seen


def classify_menu_tab(tab):
    """
    Dynamically classify any Meituan Shangou store's L1 menuTabs without store-specific index hardcoding:
    - skip: non-numeric virtual container tabs like `op0050` ("活动") that hold no direct product list
    - promo: virtual tabs (`new_op0080`, `god_price_tag`) or promotional aggregate categories
    - entity: standard numeric-tag shelf categories (crawled FIRST so every SPU maps to its real shelf L1)
    """
    tag_str = str(tab.get('tag') or '').strip()
    name = str(tab.get('name') or '').strip()
    tab_type = int(tab.get('type') or 0)

    if tag_str in SKIP_VIRTUAL_TAGS:
        return 'skip'
    is_numeric_tag = tag_str.isdigit() or (tag_str.endswith('_27') and tag_str[:-3].isdigit())
    if not is_numeric_tag and tab_type not in (1, 27):
        return 'promo'
    if any(kw in name for kw in PROMO_TAB_KEYWORDS):
        return 'promo'
    return 'entity'


def connect_store_tab(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
            tabs = json.load(response)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f'Cannot reach Chrome on port {port}. Start Chrome with --remote-debugging-port={port}.') from exc
    for host in STORE_HOSTS:
        for tab in tabs:
            if tab.get('type') == 'page' and urlsplit(tab.get('url', '')).hostname == host:
                return websockets.connect(tab['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024)
    raise RuntimeError('Open the target Meituan H5 store in Chrome first.')


class StorePage:
    def __init__(self, ws):
        self.ws = ws
        self.msg_id = 0

    async def evaluate(self, expression):
        self.msg_id += 1
        await self.ws.send(json.dumps({
            'id': self.msg_id,
            'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'awaitPromise': True, 'returnByValue': True},
        }))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            if message.get('id') != self.msg_id:
                continue
            if 'error' in message:
                raise RuntimeError(message['error'].get('message', 'Chrome evaluation failed'))
            result = message['result']
            if 'exceptionDetails' in result:
                error = result['exceptionDetails']
                raise RuntimeError((error.get('exception') or {}).get('description') or error.get('text'))
            return result['result'].get('value')

    async def inspect(self):
        return await self.evaluate(r'''
        (() => {
            const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];
            const data = (p && p.data) || {};
            const info = data.poiInfo || {};
            const count = (data.menuTabs || []).length;
            return {
                url: location.href,
                hasAuth: /(?:^|;\s*)token=/.test(document.cookie),
                inStorePage: count > 0,
                poi_id_str: String(data.poi_id_str || ''),
                poi_name: info.name || '',
                address: info.address || '',
                l1_category_count: count
            };
        })()
        ''')

    async def in_store(self, poi_id, body):
        return await self.evaluate(f'''
        (async () => {{
            const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];
            if (!p || !p.data || !p.data.poi_id_str || String(p.data.poi_id_str) !== {json.dumps(poi_id)}) {{
                throw new Error('The selected store is no longer open.');
            }}
            const checked = response => {{
                if (response) {{
                    const status = Number(response.statusCode || response.status);
                    const code = response.code;
                    if (status >= 400 || (code != null && Number(code) !== 0)) {{
                        throw new Error(response.msg || response.message || String(status >= 400 ? status : code));
                    }}
                }}
                return response;
            }};
            {body}
        }})()
        ''')


def subcategory_ids(subs):
    return list(dict.fromkeys(int(sid) for sub in subs for sid in sub.get('allSortedSpuId') or []))


def save_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


async def read_category(page, poi_id, idx, sub_idx):
    return await page.in_store(poi_id, f'''
        const current = () => p._getCachedData({idx}, {sub_idx});
        let c = current();
        if (!c) throw new Error('Category data is unavailable.');
        const requested = !(c.allSortedSpuId && c.allSortedSpuId.length);
        if (requested) {{
            checked(await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true));
            c = current();
        }}
        const ids = c.allSortedSpuId || [];
        const spus = c.spus || [];
        if (!ids.length && c.product_count > spus.length) throw new Error('Category product list is incomplete.');
        return {{ids: ids.length ? ids : spus.map(s => s.id), spus, requested}};
    ''')


async def read_products(page, poi_id, idx, sub_idx, batch):
    return await page.in_store(poi_id, f'''
        const ids = {json.dumps(batch)};
        const c = p._getCachedData({idx}, {sub_idx});
        checked(await p.requestSmoothSpus(ids, {idx}, {sub_idx}, c.tag, c.type, {{}}));
        const wanted = new Set(ids);
        return (p._getCachedData({idx}, {sub_idx}).spus || []).filter(s => s && wanted.has(String(s.id)));
    ''')


async def collect(page, state, args):
    poi_id = state['poi_id_str']
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spus_path = out_dir / 'spus.jsonl'
    cat_path = out_dir / 'categories.json'
    progress_path = out_dir / 'progress.json'
    if progress_path.exists():
        prior = json.loads(progress_path.read_text(encoding='utf-8'))
        if prior.get('poi_id_str') and str(prior['poi_id_str']) != poi_id:
            raise RuntimeError('This output directory contains a different store.')
    seen = load_existing_spus(spus_path)
    categories = json.loads(cat_path.read_text(encoding='utf-8')) if cat_path.exists() else {}
    excel_path = format_locked_excel_path(args.excel_out_dir or args.out_dir, args.poi_name or state['poi_name'])

    menu = await page.in_store(poi_id, '''
        const poi = p.store && typeof p.store.getState === 'function' && p.store.getState().poi;
        const tags = (poi && poi.food_spu_tags) || [];
        return p.data.menuTabs.map((m, idx) => {
            const t = tags[idx] || {};
            const subs = t.tags || [];
            return {
                idx, name: m.name, tag: m.tag, type: m.type,
                hasSub: typeof p._ifHasSubTag === 'function' && p._ifHasSubTag(idx),
                sub_tags: subs.map(s => ({tag: s.tag, name: s.name, allSortedSpuId: s.allSortedSpuId || []})),
                spus: (t.spus || []).concat(...subs.map(s => s.spus || []))
            };
        });
    ''')
    tabs = [m for m in menu if classify_menu_tab(m) != 'skip']
    tabs.sort(key=lambda m: classify_menu_tab(m) == 'promo')
    requests_made = 0
    step = 0
    current = {}
    phase = 'categories'

    def discovered_ids():
        return {int(sid) for cat in categories.values() for sid in cat.get('allSortedSpuId', [])}

    def checkpoint():
        save_json(cat_path, categories)
        save_json(progress_path, {
            'phase': phase,
            'poi_id_str': poi_id,
            'poi_name': state['poi_name'],
            'step': step,
            'total_steps': len(tabs),
            'current_l1_idx': current.get('idx'),
            'current_l1_name': current.get('name'),
            'unique_spus_collected': len(seen),
            'total_discovered_ids': len(discovered_ids()),
            'requests_made': requests_made,
            'timestamp': time.time(),
        })

    def export_excel():
        build_excel(spus_path, cat_path, excel_path)

    with spus_path.open('a', encoding='utf-8') as out:
        def record(spus, tab):
            for spu in spus:
                if not isinstance(spu, dict) or not spu.get('id'):
                    continue
                sid = int(spu['id'])
                if sid not in seen:
                    spu.update(_l1_idx=tab['idx'], _l1_name=tab['name'], _l1_tag=tab['tag'])
                    out.write(json.dumps(spu, ensure_ascii=False) + '\n')
                    seen[sid] = spu
            out.flush()

        try:
            for tab in tabs:
                record(tab.pop('spus'), tab)
            for step, current in enumerate(tabs, 1):
                idx = current['idx']
                key = str(idx)
                prior = categories.get(key, {})
                if prior.get('name') != current['name']:
                    prior = {}
                old_subs = {str(s.get('tag')): s for s in prior.get('sub_tags', [])}
                subs = current['sub_tags'] or prior.get('sub_tags', [])
                for sub in subs:
                    if not sub.get('allSortedSpuId'):
                        sub['allSortedSpuId'] = old_subs.get(str(sub.get('tag')), {}).get('allSortedSpuId', [])
                category = {
                    'idx': idx, 'name': current['name'], 'tag': current['tag'], 'type': current['type'],
                    'is_promo': classify_menu_tab(current) == 'promo', 'sub_idx': 0 if current['hasSub'] else -1,
                    'sub_tags': subs, 'allSortedSpuId': prior.get('allSortedSpuId') or [],
                }
                categories[key] = category
                # Some shelves have an "all" tab; others list their products only in individual subcategories.
                split = current['hasSub'] and subs and subs[0].get('name') not in ('全部', '全部商品')
                if split:
                    category['allSortedSpuId'] = subcategory_ids(subs)
                sections = enumerate(subs) if split else [(category['sub_idx'], category)]
                for sub_idx, section in sections:
                    known = section.get('allSortedSpuId') or []
                    if known and all(int(sid) in seen for sid in known):
                        continue

                    # A resumed browser session needs its own category data before loading further products.
                    data = await read_category(page, poi_id, idx, sub_idx)
                    requests_made += int(data['requested'])
                    ids = data['ids']
                    if known and not ids:
                        raise RuntimeError(f"{current['name']}: the page returned no catalog for saved listings.")
                    section['allSortedSpuId'] = ids
                    if split:
                        category['allSortedSpuId'] = subcategory_ids(subs)
                    record(data['spus'], current)
                    checkpoint()
                    if data['requested'] and requests_made % 3 == 0:
                        export_excel()

                    missing = [str(sid) for sid in ids if int(sid) not in seen]
                    for start in range(0, len(missing), 30):
                        batch = missing[start:start + 30]
                        products = await read_products(page, poi_id, idx, sub_idx, batch)
                        requests_made += 1
                        record(products, current)
                        checkpoint()
                        if requests_made % 3 == 0:
                            export_excel()
                print(f"[{step}/{len(tabs)}] {current['name']} | {len(seen)} unique SPUs", flush=True)

            remaining = discovered_ids().difference(seen)
            phase = 'partial' if remaining else 'complete'
        finally:
            if phase != 'complete':
                phase = 'partial'
            checkpoint()
            if seen or phase == 'complete':
                export_excel()

    if remaining:
        raise RuntimeError(f'{len(remaining)} listed SPUs have no product data. Partial results saved.')
    print(f"[DONE] {state['poi_name']}: {len(seen)} unique SPUs", flush=True)


async def run_crawler(args):
    async with connect_store_tab(args.port) as ws:
        page = StorePage(ws)
        state = await page.inspect()
        if args.inspect_only:
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return
        if not state['inStorePage'] or not state['poi_id_str']:
            raise RuntimeError('Open the target store page in Chrome first.')
        if args.poi_id_str and args.poi_id_str != state['poi_id_str']:
            raise RuntimeError(f"Unexpected store ID: {state['poi_id_str']}")
        if args.poi_name and args.poi_name not in state['poi_name']:
            raise RuntimeError(f"Unexpected store name: {state['poi_name']}")
        await collect(page, state, args)


def main():
    parser = argparse.ArgumentParser(description='Collect a Meituan H5 store catalog')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port')
    parser.add_argument('--inspect-only', action='store_true', help='Print the current store information and exit')
    parser.add_argument('--poi-id-str', help='Expected store ID')
    parser.add_argument('--poi-name', help='Expected store name substring, also used for the Excel filename')
    parser.add_argument('--out-dir', help='Directory for spus.jsonl, categories.json and progress.json')
    parser.add_argument('--excel-out-dir', help='Directory for the workbook (defaults to --out-dir)')
    args = parser.parse_args()
    if not args.inspect_only and not args.out_dir:
        parser.error('--out-dir is required for collection')
    try:
        asyncio.run(run_crawler(args))
    except (RuntimeError, OSError, WebSocketException) as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
