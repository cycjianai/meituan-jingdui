#!/usr/bin/env python3
"""
Meituan Shangou (H5 -> wx-shangou) Full-Store Anti-418 & Anti-403 Crawler
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
    """
    Emit realistic DOM touch + mouse + scroll events inside JS so both:
    - H5guard (`bio/info/report`)
    - `/api/rohr.js` (`handleTouchMove`, `handleTap` -> `mT`, `tT`, `aT`)
    record genuine human touch/scroll trajectories.
    """
    msg_seq += 1
    await eval_js(ws, msg_seq, '''
    (async () => {
        const x0 = Math.floor(140 + Math.random() * 180);
        const y0 = Math.floor(380 + Math.random() * 220);
        const x1 = Math.floor(130 + Math.random() * 190);
        const y1 = Math.floor(180 + Math.random() * 180);
        const steps = 6;
        const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];

        for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            const cx = Math.floor(x0 + (x1 - x0) * t + (Math.random() * 6 - 3));
            const cy = Math.floor(y0 + (y1 - y0) * t + (Math.random() * 6 - 3));
            document.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            window.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            try {
                if (p && typeof p._onTouchMove === 'function') {
                    p._onTouchMove({touches: [{clientX: cx, clientY: cy}], changedTouches: [{clientX: cx, clientY: cy}]});
                }
            } catch (e) {}
            await new Promise(r => setTimeout(r, 35));
        }
        document.dispatchEvent(new MouseEvent('mousedown', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('mouseup', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('click', {clientX: x1, clientY: y1, bubbles: true}));
        window.dispatchEvent(new Event('scroll', {bubbles: true}));
        return true;
    })()
    ''')
    return msg_seq


async def clean_verify_url_params(ws, msg_seq):
    """Strip residual verify callback params (`code=121044`, `request_code`, `response_code`) from URL and page options."""
    msg_seq += 1
    await eval_js(ws, msg_seq, '''
    (() => {
        try {
            const u = new URL(location.href);
            let changed = false;
            for (const k of ['code', 'msg', 'response_code', 'request_code', 'responseCode', 'requestCode']) {
                if (u.searchParams.has(k)) {
                    u.searchParams.delete(k);
                    changed = true;
                }
            }
            if (changed) {
                history.replaceState(null, '', u.toString());
            }
            const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];
            if (p && p.options) {
                for (const k of ['code', 'msg', 'response_code', 'request_code', 'responseCode', 'requestCode']) {
                    delete p.options[k];
                }
            }
            if (p && p.setData) {
                p.setData({'toast.show': false});
            }
        } catch (e) {}
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
            msg_seq = await clean_verify_url_params(ws, msg_seq)
            if logged_wait:
                print(f"[RESUMED] Store page ready again: {state.get('poiName')} ({state.get('poiIdStr')})", flush=True)
            return msg_seq, state

        if not logged_wait:
            print(f"[WAIT_READY] Waiting for store page / verification completion (url={state.get('url', '')[:80]}...)", flush=True)
            logged_wait = True
        await asyncio.sleep(5.0)


async def adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=False):
    """
    Safe-Boundary Anti-418 & Anti-403 Pacing Model:
    1. Tier-1 (2-min window < 10 reqs to prevent 418 Captcha):
       - Base interval: 11.5 ~ 16.5s per request
       - Every 5 requests: breather 22 ~ 32s
       - Every 15 requests: 2-minute window cooldown 75 ~ 90s
       -> Max 6~7 requests inside any 120s window (safely under the 15~20 request 418 line).
    2. Tier-2 (Hourly session < 95 reqs to prevent 403 Block):
       - Every 40 requests (~20 mins): session rest 210 ~ 260s (3.5~4.3 mins)
       -> Sustained rate ~90 requests/hour (safely under the ~115-150 req/hour 403 line).
    """
    msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=is_category_switch)
    base_delay = random.uniform(11.5, 16.5)
    await asyncio.sleep(base_delay)

    if req_counter > 0 and req_counter % 40 == 0:
        session_rest = random.uniform(210.0, 260.0)
        print(f"  [Anti-403 Hourly Quota Rest] Reached {req_counter} requests, resting {session_rest:.1f}s to stay below hourly 403 ceiling...", flush=True)
        chunk = session_rest / 4.0
        for _ in range(4):
            await asyncio.sleep(chunk)
            msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)
    elif req_counter > 0 and req_counter % 15 == 0:
        cooldown = random.uniform(75.0, 90.0)
        print(f"  [Anti-418 2-Min Window Reset] Reached {req_counter} requests, resting {cooldown:.1f}s...", flush=True)
        third = cooldown / 3.0
        for _ in range(3):
            await asyncio.sleep(third)
            msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)
    elif req_counter > 0 and req_counter % 5 == 0:
        pause = random.uniform(22.0, 32.0)
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
            const rawTags = (p.store && p.store.getState && p.store.getState().poi && p.store.getState().poi.food_spu_tags) || [];
            return {
                poi_id_str: p.data.poi_id_str,
                poiInfo: p.data.poiInfo,
                menuTabs: (p.data.menuTabs || []).map((m, idx) => ({
                    idx,
                    name: m.name,
                    tag: m.tag,
                    type: m.type,
                    hasSub: p._ifHasSubTag(idx),
                    sub_tags: ((rawTags[idx] && rawTags[idx].tags) || []).map(st => ({
                        tag: st.tag,
                        name: st.name
                    }))
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
                        'sub_tags': m.get('sub_tags', []),
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
                            if (typeof p._highlightTag === 'function') {{
                                p._highlightTag({idx}, {sub_idx}, false);
                            }}
                            await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true);
                        }} catch (err) {{
                            return {{error: String(err.message || err)}};
                        }}
                        const c2 = p._getCachedData({idx}, {sub_idx});
                        try {{
                            if (c2.spus && c2.spus.length > 0 && typeof p.setList === 'function') {{
                                const renderObj = p.setList(0, 6, c2.spus, "smoothRender.showList", true);
                                p.setData(Object.assign({{"smoothRender.scrollTop": 0, "smoothRender.currentItemIndex": 0}}, renderObj));
                            }}
                        }} catch (e) {{}}
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
                        err_msg = str(res['error'])
                        print(f"[CIRCUIT_BREAKER] Category [{idx}] {name} spuTagProducts error: {err_msg}", flush=True)
                        out_f.flush()
                        if '403' in err_msg or '406' in err_msg or '418' in err_msg:
                            print(f"[STOP_ON_RISK_CONTROL] Halting immediately on {err_msg} to avoid extending block penalty.", flush=True)
                            sys.exit(43)
                        await asyncio.sleep(10.0)
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
                        'sub_tags': m.get('sub_tags', []),
                        'allSortedSpuId': all_ids
                    }
                    with open(cat_map_json, 'w', encoding='utf-8') as cf:
                        json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                    print(f"[{step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(all_ids)} total IDs, +{added} initial SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

            # Step B: Dual-Endpoint Load Balancing (spuTagProducts on L2 sub-tags + smoothRender on remaining IDs)
            # Meituan rate-limits `/quickbuy/v1/poi/sputag/products` and `/quickbuy/v1/poi/product/smooth/render`
            # on SEPARATE per-endpoint counters (~100 reqs/hour each).
            # Splitting load across both endpoints doubles hourly SPU throughput while keeping each endpoint <50 reqs/hour.
            cinfo = cat_map.get(str_idx) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            sub_tags_list = m.get('sub_tags') or []

            missing_now = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
            if len(missing_now) > 35 and len(sub_tags_list) > 2:
                for st_idx, st_info in enumerate(sub_tags_list):
                    if st_idx == 0 or st_info.get('name') == '全部':
                        continue
                    missing_now = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                    if len(missing_now) <= 20:
                        break
                    req_counter += 1
                    msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=True)
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)

                    msg_seq += 1
                    sub_res = await eval_js(ws, msg_seq, f'''
                    (async () => {{
                        const p = getCurrentPages()[0];
                        const c = p._getCachedData({idx}, {st_idx});
                        try {{
                            if (typeof p._highlightTag === 'function') {{
                                p._highlightTag({idx}, {st_idx}, false);
                            }}
                            await p.requestSpusAndSortedIds({idx}, {st_idx}, c.tag, c.type, 0, true);
                        }} catch (err) {{
                            return {{error: String(err.message || err)}};
                        }}
                        const c2 = p._getCachedData({idx}, {st_idx});
                        try {{
                            if (c2.spus && c2.spus.length > 0 && typeof p.setList === 'function') {{
                                const renderObj = p.setList(0, Math.min(6, c2.spus.length), c2.spus, "smoothRender.showList", true);
                                p.setData(renderObj);
                            }}
                        }} catch (e) {{}}
                        return {{spus: c2.spus || []}};
                    }})()
                    ''')
                    if sub_res and sub_res.get('error'):
                        err_msg = str(sub_res['error'])
                        print(f"  [CIRCUIT_BREAKER] SubCat[{idx}:{st_idx}] {st_info.get('name')} error: {err_msg}", flush=True)
                        out_f.flush()
                        if '403' in err_msg or '406' in err_msg or '418' in err_msg:
                            sys.exit(43)
                        break
                    sub_spus = (sub_res or {}).get('spus') or []
                    added_sub = record_spus(sub_spus, idx, name, tag)
                    rem_after_sub = len([x for x in all_ids if x not in seen_spus and x not in unfetchable_ids])
                    print(f"  -> SubCat[{idx}:{st_idx}] {st_info.get('name')} (spuTagProducts, rem={rem_after_sub}): +{added_sub} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

            batch_idx = 0
            while True:
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    break
                batch_50 = missing_ids[:50]
                expected_first_30 = missing_ids[:30]
                batch_idx += 1

                req_counter += 1
                msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, is_category_switch=False)
                msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id)

                batch_json = json.dumps(batch_50)
                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {sub_idx});
                    const targetIds = {batch_json};
                    try {{
                        if (typeof p._highlightTag === 'function' && p.data.activeTypeIndex !== {idx}) {{
                            p._highlightTag({idx}, {sub_idx}, false);
                        }}
                        await p.requestSmoothSpus(targetIds, {idx}, {sub_idx}, c.tag, c.type, c.extra, true);
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                    const c2 = p._getCachedData({idx}, {sub_idx});
                    const idSet = new Set(targetIds);
                    const matched = (c2.spus || []).filter(s => s && idSet.has(Number(s.id)));
                    try {{
                        if (matched.length > 0 && typeof p.setList === 'function') {{
                            const renderObj = p.setList(0, Math.min(6, matched.length), matched, "smoothRender.showList", true);
                            p.setData(renderObj);
                        }}
                    }} catch (e) {{}}
                    return {{spus: matched}};
                }})()
                ''')

                if res and res.get('error'):
                    err_msg = str(res['error'])
                    print(f"  [CIRCUIT_BREAKER] Cat[{idx}] {name} batch {batch_idx} error: {err_msg}", flush=True)
                    out_f.flush()
                    if '403' in err_msg or '406' in err_msg or '418' in err_msg:
                        print(f"  [STOP_ON_RISK_CONTROL] Halting immediately on {err_msg} to avoid extending block penalty.", flush=True)
                        sys.exit(43)
                    await asyncio.sleep(10.0)
                    continue

                spus = (res or {}).get('spus') or []
                returned_ids = {int(s['id']) for s in spus if isinstance(s, dict) and s.get('id')}
                for bid in expected_first_30:
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
    parser = argparse.ArgumentParser(description='Meituan H5 Shangou Anti-418 & Anti-403 Store Crawler')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port')
    parser.add_argument('--poi-id-str', type=str, default=None, help='Expected poi_id_str (optional)')
    parser.add_argument('--poi-name', type=str, default=None, help='Expected store name substring (optional)')
    parser.add_argument('--out-dir', type=str, required=True, help='Output directory for spus.jsonl and categories.json')
    args = parser.parse_args()
    asyncio.run(run_crawler(args))


if __name__ == '__main__':
    main()
