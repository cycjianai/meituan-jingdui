#!/usr/bin/env python3
"""
Meituan H5 Multi-Account Isolated Session Pool Manager (`account_pool.py`)
Part of the `meituanjingdui` skill.

Stores each Meituan C-end account's COMPLETE isolated session package in a private local vault
(`~/.meituan_jingdui_accounts.json`, chmod 0600, NEVER committed to git):
- All C-end `*.meituan.com` cookies (auth tokens + THAT account's own paired device fingerprint cookies
  such as `WEBDFPID`, `uuid`, `iuuid`, `openh5_uuid`, `_lxsdk*`) — strictly excluding B-end domains
  (`shangoue`, `waimaie`, `qnh`, `e.waimai`).
- Paired `localStorage` entries on `h5.waimai.meituan.com` and `cactivityapi-sc.waimai.meituan.com`.
- Per-account daily quota ledger (`quota[YYYY-MM-DD] = {"sputag": N, "smooth": M}`).
- User-defined label (e.g. phone number / last 4 digits / alias).

When switching accounts, wipes all 3 C-end origins (`Storage.clearDataForOrigin`) and deletes all C-end
cookies first (preserving B-end logins), then restores the target account's own cookies + paired
fingerprint + localStorage cleanly with ZERO cross-account contamination.
"""
import argparse
import asyncio
import datetime
import json
import os
import stat
import sys
import urllib.error
import urllib.request
import websockets

DEFAULT_VAULT_PATH = os.path.expanduser('~/.meituan_jingdui_accounts.json')

B_END_PROTECTED_DOMAINS = ('shangoue', 'waimaie', 'qnh', 'e.waimai')
C_END_ORIGINS = (
    'https://cactivityapi-sc.waimai.meituan.com',
    'https://h5.waimai.meituan.com',
    'https://passport.meituan.com',
)
C_END_WIPE_COOKIES = {
    'token', 'oops', 'mt_c_token', 'userId', 'u', 'w_token', 'userName', 'userFace',
    'waimai_h5_token', 'WEBDFPID', 'uuid', 'iuuid', 'openh5_uuid',
    '_lxsdk', '_lxsdk_cuid', '_lxsdk_s', 'logan_session_token'
}
C_END_LS_KEYS = (
    'dfpId', 'localId', 'dfp_idb_token', 'dfp_timestamp',
    'shangou_user', 'shangou_wx_set_info',
    'saber-vma:user-info', 'saber-vma:uuid',
    '__lxsdk__lxsdk_cuid', 'pickedpoi', 'geopoi',
)


def fetch_json(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


async def send_cdp(ws, seq, method, params=None, timeout=15.0):
    payload = {'id': seq, 'method': method}
    if params is not None:
        payload['params'] = params
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg.get('id') == seq:
            return msg


def load_vault(vault_path):
    if os.path.exists(vault_path):
        try:
            data = json.load(open(vault_path, 'r', encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('accounts'), dict):
                return data
        except Exception:
            pass
    return {'version': 1, 'accounts': {}}


def save_vault(vault_path, data):
    os.makedirs(os.path.dirname(os.path.abspath(vault_path)), exist_ok=True)
    tmp_path = vault_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, vault_path)
    os.chmod(vault_path, stat.S_IRUSR | stat.S_IWUSR)


def today_key():
    return datetime.date.today().isoformat()


def is_b_end_domain(dom):
    d = str(dom or '').lower()
    return any(b in d for b in B_END_PROTECTED_DOMAINS)


def find_meituan_tab(port):
    tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')
    page_tabs = [t for t in tabs if t.get('type') == 'page']
    for pat in ('cactivityapi-sc.waimai.meituan.com', 'h5.waimai.meituan.com', 'wx-shangou.meituan.com', 'passport.meituan.com'):
        t = next((x for x in page_tabs if pat in x.get('url', '')), None)
        if t:
            return t
    return page_tabs[0] if page_tabs else None


def resolve_account_id(vault, query):
    q = str(query or '').strip()
    if not q:
        return None
    accounts = vault.get('accounts') or {}
    if q in accounts:
        return q
    for uid, acc in accounts.items():
        if str(acc.get('label') or '').strip() == q or str(acc.get('phone') or '').strip() == q or str(acc.get('userName') or '').strip() == q:
            return uid
    for uid, acc in accounts.items():
        if q in str(acc.get('label') or '') or q in str(acc.get('phone') or ''):
            return uid
    return None


async def cmd_save(args):
    tab = find_meituan_tab(args.port)
    if not tab:
        raise SystemExit(f"[ERROR] No open Chrome tab found on port {args.port}")

    vault = load_vault(args.vault)
    seq = 1
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
        # 1. Capture ALL C-end meituan.com cookies (strictly excluding B-end)
        seq += 1
        cmsg = await send_cdp(ws, seq, 'Network.getAllCookies')
        all_cookies = cmsg.get('result', {}).get('cookies', [])
        c_cookies = [
            c for c in all_cookies
            if 'meituan.com' in (c.get('domain') or '') and not is_b_end_domain(c.get('domain'))
        ]

        cookie_map = {c['name']: c.get('value', '') for c in c_cookies}
        user_id = str(cookie_map.get('userId') or cookie_map.get('u') or '').strip()
        user_name = str(cookie_map.get('userName') or '').strip()
        has_token = bool(cookie_map.get('oops') or cookie_map.get('mt_c_token') or cookie_map.get('token'))

        # 2. Capture current origin's localStorage (and extract userId if not in cookie)
        seq += 1
        ls_msg = await send_cdp(ws, seq, 'Runtime.evaluate', {
            'expression': f'''(() => {{
                const keys = {json.dumps(list(C_END_LS_KEYS))};
                const ls = {{}};
                for (const k of keys) {{
                    const v = localStorage.getItem(k);
                    if (v !== null) ls[k] = v;
                }}
                let su_uid = '', su_name = '', su_tok = '';
                try {{
                    const su = JSON.parse(localStorage.getItem('shangou_user') || '{{}}');
                    if (su && su.data) {{
                        su_uid = String(su.data.userId || su.data.id || '');
                        su_name = String(su.data.userName || su.data.nickName || '');
                        su_tok = String(su.data.token || '');
                    }}
                }} catch (e) {{}}
                return {{origin: location.origin, url: location.href, ls, su_uid, su_name, hasSuTok: !!su_tok}};
            }})()''',
            'returnByValue': True
        })
        ls_info = ls_msg.get('result', {}).get('result', {}).get('value') or {}
        if not user_id:
            user_id = ls_info.get('su_uid') or ''
        if not user_name:
            user_name = ls_info.get('su_name') or ''
        if not user_id or not (has_token or ls_info.get('hasSuTok')):
            raise SystemExit("[ERROR] No logged-in Meituan C-end account detected in Chrome cookies/localStorage!")

        existing = vault['accounts'].get(user_id) or {}
        ls_by_origin = dict(existing.get('local_storage') or {})
        cur_origin = ls_info.get('origin') or ''
        if cur_origin:
            ls_by_origin[cur_origin] = ls_info.get('ls') or {}

        quota = dict(existing.get('quota') or {})
        if args.sync_quota_from and os.path.exists(args.sync_quota_from):
            try:
                dq = json.load(open(args.sync_quota_from, 'r', encoding='utf-8'))
                for day_k, vals in dq.items():
                    if isinstance(vals, dict):
                        cur_d = quota.get(day_k) or {'sputag': 0, 'smooth': 0}
                        cur_d['sputag'] = max(int(cur_d.get('sputag', 0)), int(vals.get('sputag', 0)))
                        cur_d['smooth'] = max(int(cur_d.get('smooth', 0)), int(vals.get('smooth', 0)))
                        quota[day_k] = cur_d
            except Exception:
                pass

        label = args.label or existing.get('label') or f"账号_{user_id[-4:]}"
        phone = args.phone or existing.get('phone') or ''

        vault['accounts'][user_id] = {
            'userId': user_id,
            'userName': user_name,
            'label': label,
            'phone': phone,
            'saved_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'cookie_count': len(c_cookies),
            'cookies': c_cookies,
            'local_storage': ls_by_origin,
            'quota': quota,
        }
        save_vault(args.vault, vault)
        today_q = quota.get(today_key()) or {'sputag': 0, 'smooth': 0}
        print(
            f"[SAVED] Account '{label}' (userId={user_id}, userName={user_name}, phone={phone or '-'}) | "
            f"cookies={len(c_cookies)}, origins={list(ls_by_origin.keys())}, "
            f"today({today_key()}) sputag={today_q['sputag']}, smooth={today_q['smooth']} -> {args.vault}",
            flush=True
        )


def cmd_label(args):
    vault = load_vault(args.vault)
    uid = resolve_account_id(vault, args.account)
    if not uid:
        raise SystemExit(f"[ERROR] Account '{args.account}' not found in {args.vault}")
    if args.label:
        vault['accounts'][uid]['label'] = args.label
    if args.phone:
        vault['accounts'][uid]['phone'] = args.phone
    save_vault(args.vault, vault)
    acc = vault['accounts'][uid]
    print(f"[UPDATED] userId={uid} -> label='{acc.get('label')}', phone='{acc.get('phone')}'", flush=True)


def cmd_list(args):
    vault = load_vault(args.vault)
    accounts = vault.get('accounts') or {}
    tk = today_key()
    if not accounts:
        print(f"[EMPTY] No saved accounts in {args.vault}")
        return
    print(f"Saved Meituan C-end Accounts ({len(accounts)}) — Date: {tk} (Safe daily caps: sputag<={args.max_sputag_daily}, smooth<={args.max_smooth_daily})")
    print(f"{'UserID':<12} | {'Label':<18} | {'Phone':<13} | {'UserName':<16} | {'Sputag':<9} | {'Smooth':<9} | {'Status'}")
    print("-" * 105)
    for uid, acc in accounts.items():
        q = (acc.get('quota') or {}).get(tk) or {'sputag': 0, 'smooth': 0}
        sp = int(q.get('sputag', 0))
        sm = int(q.get('smooth', 0))
        rem_sp = max(0, args.max_sputag_daily - sp)
        rem_sm = max(0, args.max_smooth_daily - sm)
        if rem_sm > 0:
            st = f"READY (smooth +{rem_sm}, sputag +{rem_sp})"
        elif rem_sp > 0:
            st = f"L2_ONLY (sputag +{rem_sp})"
        else:
            st = "EXHAUSTED_TODAY"
        print(f"{uid:<12} | {str(acc.get('label',''))[:18]:<18} | {str(acc.get('phone','') or '-'):<13} | {str(acc.get('userName',''))[:16]:<16} | {sp:2d}/{args.max_sputag_daily:<5d} | {sm:2d}/{args.max_smooth_daily:<5d} | {st}")


def pick_best_account(vault, max_sputag_daily=92, max_smooth_daily=85, need='smooth', exclude_uids=None):
    tk = today_key()
    exclude = set(exclude_uids or [])
    candidates = []
    for uid, acc in (vault.get('accounts') or {}).items():
        if uid in exclude:
            continue
        q = (acc.get('quota') or {}).get(tk) or {'sputag': 0, 'smooth': 0}
        rem_sm = max(0, max_smooth_daily - int(q.get('smooth', 0)))
        rem_sp = max(0, max_sputag_daily - int(q.get('sputag', 0)))
        if need == 'smooth' and rem_sm > 0:
            candidates.append((rem_sm, rem_sp, uid, acc))
        elif need == 'sputag' and rem_sp > 0:
            candidates.append((rem_sp, rem_sm, uid, acc))
        elif need == 'any' and (rem_sm > 0 or rem_sp > 0):
            candidates.append((rem_sm * 2 + rem_sp, rem_sm, uid, acc))
    candidates.sort(key=lambda x: (-x[0], -x[1]))
    if not candidates:
        return None
    _, _, uid, acc = candidates[0]
    return uid, acc


async def switch_to_account(port, vault_path, account_query, target_url=None):
    vault = load_vault(vault_path)
    uid = resolve_account_id(vault, account_query)
    if not uid:
        raise RuntimeError(f"Account '{account_query}' not found in {vault_path}")
    acc = vault['accounts'][uid]

    v = fetch_json(f'http://127.0.0.1:{port}/json/version')
    tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')
    for t in tabs:
        if 'devtools://' in t.get('url', ''):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=2).read()
            except Exception:
                pass

    tab = find_meituan_tab(port)
    if not tab:
        async with websockets.connect(v['webSocketDebuggerUrl'], max_size=20 * 1024 * 1024) as bws:
            res = await send_cdp(bws, 1, 'Target.createTarget', {'url': target_url or 'https://h5.waimai.meituan.com/waimai/mindex/home'})
            tid = (res.get('result') or {}).get('targetId')
            await asyncio.sleep(1.0)
            tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')
            tab = next((t for t in tabs if t.get('id') == tid), None)

    seq = 1
    async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
        # 1. Backup current pickedpoi/geopoi and URL if target_url not given
        seq += 1
        cur_msg = await send_cdp(ws, seq, 'Runtime.evaluate', {
            'expression': '''(() => ({
                url: location.href,
                pickedpoi: localStorage.getItem('pickedpoi') || '',
                geopoi: localStorage.getItem('geopoi') || ''
            }))()''',
            'returnByValue': True
        })
        cur_info = cur_msg.get('result', {}).get('result', {}).get('value') or {}
        final_url = target_url or cur_info.get('url') or 'https://h5.waimai.meituan.com/waimai/mindex/home'

        # 2. Wipe all 3 C-end origins' storage + dfp_db (zero cross-account contamination!)
        for origin in C_END_ORIGINS:
            seq += 1
            await send_cdp(ws, seq, 'Storage.clearDataForOrigin', {
                'origin': origin,
                'storageTypes': 'local_storage,indexeddb,cache_storage'
            })

        # 3. Delete all existing C-end cookies (strictly preserving B-end shangoue/waimaie/qnh)
        seq += 1
        cmsg = await send_cdp(ws, seq, 'Network.getAllCookies')
        for c in cmsg.get('result', {}).get('cookies', []):
            dom = c.get('domain', '')
            if 'meituan.com' in dom and not is_b_end_domain(dom):
                seq += 1
                await send_cdp(ws, seq, 'Network.deleteCookies', {
                    'name': c['name'],
                    'domain': dom,
                    'path': c.get('path', '/')
                })

        # 4. Restore target account's complete C-end cookies (auth + paired device fingerprint)
        restored = 0
        oops_val = ''
        for c in (acc.get('cookies') or []):
            dom = c.get('domain', '')
            if is_b_end_domain(dom):
                continue
            if c.get('name') in ('oops', 'mt_c_token') and c.get('value'):
                oops_val = c['value']
            params = {
                'name': c['name'],
                'value': c.get('value', ''),
                'domain': dom,
                'path': c.get('path', '/'),
                'secure': bool(c.get('secure', False)),
                'httpOnly': bool(c.get('httpOnly', False)),
            }
            if c.get('sameSite') in ('Strict', 'Lax', 'None'):
                params['sameSite'] = c['sameSite']
            if c.get('expires') and float(c['expires']) > 0:
                params['expires'] = float(c['expires'])
            seq += 1
            await send_cdp(ws, seq, 'Network.setCookie', params)
            restored += 1

        # Ensure .meituan.com token cookie is also set from oops/mt_c_token
        if oops_val:
            seq += 1
            await send_cdp(ws, seq, 'Network.setCookie', {
                'name': 'token', 'value': oops_val, 'domain': '.meituan.com',
                'path': '/', 'secure': True, 'sameSite': 'None'
            })

        # 5. Restore per-origin paired localStorage & navigate to final_url
        ls_by_origin = acc.get('local_storage') or {}
        for origin, kv in ls_by_origin.items():
            if not kv or not origin.startswith('https://'):
                continue
            seq += 1
            await send_cdp(ws, seq, 'Page.navigate', {'url': origin + '/favicon.ico'})
            await asyncio.sleep(0.8)
            seq += 1
            await send_cdp(ws, seq, 'Runtime.evaluate', {
                'expression': f'''(() => {{
                    const kv = {json.dumps(kv, ensure_ascii=False)};
                    for (const [k, v] of Object.entries(kv)) {{
                        if (k === 'pickedpoi' || k === 'geopoi') continue;
                        localStorage.setItem(k, v);
                    }}
                    return true;
                }})()'''
            })

        seq += 1
        await send_cdp(ws, seq, 'Page.navigate', {'url': final_url})
        await asyncio.sleep(3.5)

        # Keep active store delivery POI if present
        if cur_info.get('pickedpoi'):
            seq += 1
            await send_cdp(ws, seq, 'Runtime.evaluate', {
                'expression': f'''(() => {{
                    localStorage.setItem('pickedpoi', {json.dumps(cur_info['pickedpoi'])});
                    localStorage.setItem('geopoi', {json.dumps(cur_info['geopoi'] or cur_info['pickedpoi'])});
                }})()'''
            })

        print(
            f"[SWITCHED] Active account is now '{acc.get('label')}' (userId={uid}, userName={acc.get('userName')}) | "
            f"restored {restored} cookies, navigated to {final_url[:90]}",
            flush=True
        )
        return uid, acc


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Multi-Account Isolated Session Pool Manager')
    parser.add_argument('--vault', default=DEFAULT_VAULT_PATH, help=f'Path to private account vault (default: {DEFAULT_VAULT_PATH})')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_save = sub.add_parser('save', help='Save currently logged-in C-end Meituan account into private vault')
    p_save.add_argument('--port', type=int, default=9222)
    p_save.add_argument('--label', default='', help='Human label (e.g. phone number or nickname)')
    p_save.add_argument('--phone', default='', help='Phone number tag for this account')
    p_save.add_argument('--sync-quota-from', default='', help='Optional daily_quota.json path to import usage into this account')

    p_lbl = sub.add_parser('label', help='Update label/phone tag for a saved account')
    p_lbl.add_argument('--account', required=True, help='userId or existing label')
    p_lbl.add_argument('--label', default='')
    p_lbl.add_argument('--phone', default='')

    p_list = sub.add_parser('list', help='List all saved accounts and remaining daily quotas (masked, no tokens printed)')
    p_list.add_argument('--max-sputag-daily', type=int, default=92)
    p_list.add_argument('--max-smooth-daily', type=int, default=85)

    p_sw = sub.add_parser('switch', help='Cleanly wipe current C-end session and restore a saved account (B-end safe)')
    p_sw.add_argument('--account', required=True, help='Target userId, phone, or label')
    p_sw.add_argument('--port', type=int, default=9222)
    p_sw.add_argument('--target-url', default=None, help='Optional URL to open after switching')

    p_next = sub.add_parser('next', help='Print best available account with remaining daily quota')
    p_next.add_argument('--need', choices=('smooth', 'sputag', 'any'), default='smooth')
    p_next.add_argument('--max-sputag-daily', type=int, default=92)
    p_next.add_argument('--max-smooth-daily', type=int, default=85)

    args = parser.parse_args()
    if args.cmd == 'save':
        asyncio.run(cmd_save(args))
    elif args.cmd == 'label':
        cmd_label(args)
    elif args.cmd == 'list':
        cmd_list(args)
    elif args.cmd == 'switch':
        asyncio.run(switch_to_account(args.port, args.vault, args.account, args.target_url))
    elif args.cmd == 'next':
        vault = load_vault(args.vault)
        res = pick_best_account(vault, args.max_sputag_daily, args.max_smooth_daily, need=args.need)
        if not res:
            print("NO_AVAILABLE_ACCOUNT")
            sys.exit(1)
        uid, acc = res
        print(json.dumps({'userId': uid, 'label': acc.get('label'), 'phone': acc.get('phone'), 'userName': acc.get('userName')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
