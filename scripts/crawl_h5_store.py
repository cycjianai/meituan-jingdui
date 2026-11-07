#!/usr/bin/env python3
"""
Meituan Shangou (H5 -> wx-shangou) Full-Store Anti-418 Crawler
Part of the `meituanjingdui` skill.
"""
import os
import sys
import json
import time
import random
import argparse
import asyncio
import urllib.request
import websockets


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


async def eval_js(ws, msg_id, expr, timeout=25.0):
    await ws.send(json.dumps({
        'id': msg_id,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': expr,
            'awaitPromise': True,
            'returnByValue': True
        }
    }))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg.get('id') == msg_id:
            if 'error' in msg:
                raise RuntimeError(f"CDP error: {msg['error']}")
            res = msg.get('result', {})
            if 'exceptionDetails' in res:
                raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
            return res.get('result', {}).get('value')


async def simulate_human_interaction(ws, msg_seq, is_category_switch=False):
    """Emit realistic DOM mouse/touch/scroll events inside JS so H5guard bio/info/report sees human activity."""
    msg_seq += 1
    await eval_js(ws, msg_seq, '''
    (async () => {
        const x0 = Math.floor(150 + Math.random() * 200);
        const y0 = Math.floor(220 + Math.random() * 350);
        const x1 = Math.floor(60 + Math.random() * 280);
        const y1 = Math.floor(200 + Math.random() * 400);
        const steps = 6;
        for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            const cx = Math.floor(x0 + (x1 - x0) * t + (Math.random() * 6 - 3));
            const cy = Math.floor(y0 + (y1 - y0) * t + (Math.random() * 6 - 3));
            document.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            window.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            await new Promise(r => setTimeout(r, 25));
        }
        document.dispatchEvent(new MouseEvent('mousedown', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('mouseup', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('click', {clientX: x1, clientY: y1, bubbles: true}));
        window.dispatchEvent(new Event('scroll', {bubbles: true}));
        return true;
    })()
    ''')
    return msg_seq


async def wait_until_store_ready(ws, msg_seq, expected_poi_id_str=None):
    """Ensure tab is on the target store page and not on verify.meituan.com."""
    logged_wait = False
    while True:
        msg_seq += 1
        try:
            state = await eval_js(ws, msg_seq, '''
            (() => {
                const hasPages = typeof getCurrentPages === 'function' && getCurrentPages().length > 0;
                const p = hasPages ? getCurrentPages()[0] : null;
                const menuLen = p && p.data && Array.isArray(p.data.menuTabs) ? p.data.menuTabs.length : 0;
                const poiIdStr = p && p.data ? p.data.poi_id_str : '';
                const poiName = p && p.data && p.data.poiInfo ? p.data.poiInfo.name : '';
                return {url: location.href, hasPages, menuLen, poiIdStr, poiName};
            })()
            ''')
        except Exception as e:
            state = {'url': f'err:{e}', 'hasPages': False, 'menuLen': 0}

        if (
            state
            and state.get('hasPages')
            and state.get('menuLen', 0) > 0
            and 'verify.meituan.com' not in state.get('url', '')
            and (not expected_poi_id_str or state.get('poiIdStr') == expected_poi_id_str)
        ):
            if logged_wait:
                print(f"[RESUMED] Store page ready again: {state.get('poiName')} ({state.get('poiIdStr')})", flush=True)
            return msg_seq, state

        if not logged_wait:
            print(f"[WAIT_READY] Waiting for store page / verification completion (url={state.get('url', '')[:80]}...)", flush=True)
            logged_wait = True
        await asyncio.sleep(3.0)


async def adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=False):
    """
    Pressure-tested Anti-418 pacing:
    - Meituan wx-shangou triggers HTTP 418 at ~28-30 requests in a 2-minute sliding window (including ~4-5 initial page load requests).
    - Therefore we pause 38~48s every 20 requests (and at request #18 on first burst) to completely reset the sliding window,
      plus a 9~13s breather every 8 requests and 3.5~5.2s base delay per request.
    """
    msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=is_category_switch)
    base_delay = random.uniform(3.5, 5.2)
    await asyncio.sleep(base_delay)

    if req_counter > 0 and (req_counter == 18 or (req_counter > 18 and (req_counter - 18) % 20 == 0)):
        cooldown = random.uniform(38.0, 48.0)
        print(f"  [Anti-418 Window Reset] Reached {req_counter} requests, resting {cooldown:.1f}s to clear Meituan sliding window...", flush=True)
        half = cooldown / 2.0
        await asyncio.sleep(half)
        msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)
        await asyncio.sleep(half)
    elif req_counter > 0 and req_counter % 8 == 0:
        pause = random.uniform(9.0, 13.0)
        print(f"  [Breather] {req_counter} requests, pausing {pause:.1f}s...", flush=True)
        await asyncio.sleep(pause)

    return msg_seq


async def run_crawler(args):
    os.makedirs(args.out_dir, exist_ok=True)
    spus_jsonl = os.path.join(args.out_dir, 'spus.jsonl')
    cat_map_json = os.path.join(args.out_dir, 'categories.json')
    progress_json = os.path.join(args.out_dir, 'progress.json')

    # Set default download behavior on Browser target and close any DevTools tabs targeting H5
    with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/version') as r:
        v = json.loads(r.read().decode())
    async with websockets.connect(v['webSocketDebuggerUrl']) as bws:
        await bws.send(json.dumps({'id': 1, 'method': 'Browser.setDownloadBehavior', 'params': {'behavior': 'default'}}))
        await bws.recv()

    with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/list') as r:
        tabs = json.loads(r.read().decode())

    for t in tabs:
        if 'devtools://' in t.get('url', '') and ('waimai.meituan.com' in t.get('title', '') or 'cactivityapi' in t.get('title', '')):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/close/{t['id']}", timeout=2)
                print(f"[Init] Closed DevTools tab {t['id']} to prevent H5guard debugger trap", flush=True)
            except Exception:
                pass

    target = next((t for t in tabs if 'cactivityapi-sc.waimai.meituan.com' in t.get('url', '') or 'waimai.meituan.com' in t.get('url', '') or 'verify.meituan.com' in t.get('url', '')), None)
    if not target:
        raise RuntimeError("Could not find open Meituan H5 store tab in Chrome!")

    seen_spus = load_existing_spus(spus_jsonl)
    cat_map = {}
    if os.path.exists(cat_map_json):
        try:
            cat_map = json.load(open(cat_map_json, 'r', encoding='utf-8'))
        except Exception:
            cat_map = {}

    msg_seq = 1
    req_counter = 0

    async with websockets.connect(target['webSocketDebuggerUrl'], max_size=50*1024*1024) as ws:
        msg_seq, state = await wait_until_store_ready(ws, msg_seq, args.poi_id_str)
        actual_poi_id = state.get('poiIdStr')
        actual_poi_name = state.get('poiName')
        if args.poi_name and args.poi_name not in (actual_poi_name or ''):
            raise RuntimeError(f"Store name mismatch! Expected '{args.poi_name}', but tab is on '{actual_poi_name}' ({actual_poi_id})")

        msg_seq += 1
        meta = await eval_js(ws, msg_seq, '''
        (() => {
            const p = getCurrentPages()[0];
            return {
                poi_id_str: p.data.poi_id_str,
                poiInfo: p.data.poiInfo,
                menuTabs: (p.data.menuTabs || []).map((m, idx) => ({
                    idx,
                    name: m.name,
                    tag: m.tag,
                    type: m.type,
                    hasSub: p._ifHasSubTag(idx)
                }))
            };
        })()
        ''')
        menu_tabs = meta.get('menuTabs') or []
        poi_info = meta.get('poiInfo') or {}
        print(f"[Store Verified] {poi_info.get('name')} ({meta.get('poi_id_str')}), L1 categories: {len(menu_tabs)}, existing SPUs: {len(seen_spus)}", flush=True)

        out_f = open(spus_jsonl, 'a', encoding='utf-8')
        unfetchable_ids = set()

        def record_spus(spus_list, l1_idx, l1_name, l1_tag):
            added = 0
            for s in (spus_list or []):
                if not isinstance(s, dict) or not s.get('id'):
                    continue
                sid = int(s['id'])
                if sid not in seen_spus:
                    s['_l1_idx'] = l1_idx
                    s['_l1_name'] = l1_name
                    s['_l1_tag'] = l1_tag
                    seen_spus[sid] = s
                    out_f.write(json.dumps(s, ensure_ascii=False) + '\n')
                    added += 1
            if added > 0:
                out_f.flush()
            return added

        regular_tabs = [m for m in menu_tabs if m['idx'] >= 7]
        promo_tabs = [m for m in menu_tabs if m['idx'] < 7 and m['idx'] != 2]
        ordered_tabs = regular_tabs + promo_tabs

        for step_num, m in enumerate(ordered_tabs, 1):
            idx = m['idx']
            name = m['name']
            tag = m['tag']
            has_sub = m['hasSub']
            sub_idx = 0 if has_sub else -1
            str_idx = str(idx)

            msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)

            if str_idx not in cat_map or not cat_map[str_idx].get('allSortedSpuId'):
                msg_seq += 1
                cached_check = await eval_js(ws, msg_seq, f'''
                (() => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {sub_idx});
                    return {{
                        tag: c.tag,
                        type: c.type,
                        allSortedSpuId: c.allSortedSpuId || [],
                        spus: c.spus || []
                    }};
                }})()
                ''')
                if cached_check and cached_check.get('spus'):
                    record_spus(cached_check['spus'], idx, name, tag)

                if cached_check and len(cached_check.get('allSortedSpuId') or []) > 0:
                    cat_map[str_idx] = {
                        'idx': idx,
                        'name': name,
                        'tag': cached_check['tag'],
                        'type': cached_check['type'],
                        'sub_idx': sub_idx,
                        'allSortedSpuId': cached_check['allSortedSpuId']
                    }
                    with open(cat_map_json, 'w', encoding='utf-8') as cf:
                        json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                else:
                    req_counter += 1
                    msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=True)
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)

                    msg_seq += 1
                    res = await eval_js(ws, msg_seq, f'''
                    (async () => {{
                        const p = getCurrentPages()[0];
                        const c = p._getCachedData({idx}, {sub_idx});
                        try {{
                            if (typeof p._lxFirstCategoryClick === 'function') {{
                                p._lxFirstCategoryClick({idx});
                            }}
                            await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true);
                        }} catch (err) {{
                            return {{error: String(err.message || err)}};
                        }}
                        const c2 = p._getCachedData({idx}, {sub_idx});
                        return {{
                            tag: c2.tag,
                            type: c2.type,
                            product_count: c2.product_count,
                            allSortedSpuId: c2.allSortedSpuId || [],
                            spus: c2.spus || []
                        }};
                    }})()
                    ''')

                    if res and res.get('error'):
                        print(f"[WARN] Category [{idx}] {name} spuTagProducts error: {res['error']}", flush=True)
                        await asyncio.sleep(5.0)
                        msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)
                        continue

                    all_ids = (res or {}).get('allSortedSpuId') or []
                    spus = (res or {}).get('spus') or []
                    added = record_spus(spus, idx, name, tag)
                    cat_map[str_idx] = {
                        'idx': idx,
                        'name': name,
                        'tag': (res or {}).get('tag', tag),
                        'type': (res or {}).get('type', 1),
                        'sub_idx': sub_idx,
                        'allSortedSpuId': all_ids
                    }
                    with open(cat_map_json, 'w', encoding='utf-8') as cf:
                        json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                    print(f"[{step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(all_ids)} total IDs, +{added} initial SPUs (unique={len(seen_spus)})", flush=True)

            # Fetch remaining unfetched SPU IDs in exact server-capped batches of 30
            cinfo = cat_map.get(str_idx) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            batch_idx = 0
            while True:
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    break
                batch = missing_ids[:30]
                batch_idx += 1

                req_counter += 1
                msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=False)
                msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)

                batch_json = json.dumps(batch)
                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {sub_idx});
                    const targetIds = {batch_json};
                    try {{
                        await p.requestSmoothSpus(targetIds, {idx}, {sub_idx}, c.tag, c.type, c.extra, true);
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                    const c2 = p._getCachedData({idx}, {sub_idx});
                    const idSet = new Set(targetIds);
                    const matched = (c2.spus || []).filter(s => s && idSet.has(Number(s.id)));
                    return {{spus: matched}};
                }})()
                ''')

                if res and res.get('error'):
                    print(f"  [WARN] Cat[{idx}] {name} batch {batch_idx} error: {res['error']}", flush=True)
                    await asyncio.sleep(5.0)
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)
                    continue

                spus = (res or {}).get('spus') or []
                returned_ids = {int(s['id']) for s in spus if isinstance(s, dict) and s.get('id')}
                for bid in batch:
                    if bid not in returned_ids:
                        unfetchable_ids.add(bid)

                added = record_spus(spus, idx, name, tag)
                rem_after = len([x for x in all_ids if x not in seen_spus and x not in unfetchable_ids])
                print(f"  -> Cat[{idx}] {name} batch {batch_idx} (rem={rem_after}): +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

            with open(progress_json, 'w', encoding='utf-8') as pf:
                json.dump({
                    'poi_id_str': actual_poi_id,
                    'poi_name': actual_poi_name,
                    'step': step_num,
                    'total_steps': len(ordered_tabs),
                    'current_l1_idx': idx,
                    'current_l1_name': name,
                    'unique_spus_collected': len(seen_spus),
                    'requests_made': req_counter,
                    'timestamp': time.time()
                }, pf, ensure_ascii=False, indent=2)

        out_f.close()
        all_discovered_ids = set()
        for k, v in cat_map.items():
            for sid in v.get('allSortedSpuId', []):
                all_discovered_ids.add(int(sid))
        print(f"[ALL DONE] {actual_poi_name}: collected {len(seen_spus)} / {len(all_discovered_ids)} unique SPUs in {req_counter} requests!", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Shangou Anti-418 Store Crawler')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port')
    parser.add_argument('--poi-id-str', type=str, default=None, help='Expected poi_id_str (optional)')
    parser.add_argument('--poi-name', type=str, default=None, help='Expected store name substring (optional)')
    parser.add_argument('--out-dir', type=str, required=True, help='Output directory for spus.jsonl and categories.json')
    args = parser.parse_args()
    asyncio.run(run_crawler(args))


if __name__ == '__main__':
    main()
