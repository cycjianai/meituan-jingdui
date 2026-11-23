#!/usr/bin/env python3
"""
Meituan H5 Pre-Login Device Fingerprint & Cookie Sanitizer (`prepare_clean_login.py`)
Part of the `meituanjingdui` skill.

Why this script MUST be executed before first login or before switching to a new Meituan C-end account after a 403 block:
1. Cross-Subdomain Pollution Trap:
   - Logging out on `h5.waimai.meituan.com` does NOT clear `cactivityapi-sc.waimai.meituan.com` `localStorage`.
   - Residual `shangou_wx_set_info` forces `deal-region-set.js` to append `?ui=<old_banned_userId>` to every request
     of the new account, causing instant HTTP 403.
   - Residual `WEBDFPID`, `uuid`, `iuuid`, `openh5_uuid`, `_lxsdk*`, `localStorage.dfpId`, `localStorage.localId`,
     or `indexedDB("dfp_db")` causes `passport.meituan.com` to associate the new login with the flagged device
     fingerprint and trigger `user_err_user_locked_mobile` ("检测到您的账号存在异常").
   - This script uses CDP `Storage.clearDataForOrigin` across ALL C-end origins (`cactivityapi-sc.waimai.meituan.com`,
     `h5.waimai.meituan.com`, `passport.meituan.com`) regardless of which URL the tab is currently on.
2. Strict B-End Isolation:
   - B-end merchant tabs (`shangoue.meituan.com`, `waimaie.meituan.com`, `qnh.meituan.com`, `e.waimai.meituan.com`)
     in the same Chrome profile are NEVER touched or logged out.
3. No-F12 Mobile Card + Desktop Click-to-Tap Bridge:
   - Opening F12 DevTools Mobile Emulation triggers `H5guard` anti-debugging & UA/Touch mismatch flags.
   - Automatically closes any `devtools://` tab, injects a 390px centered mobile card CSS (`html { font-size: 50px }`),
     and bridges desktop `click` -> mobile `touchstart/touchend` (fixing `#iLoginCompSecMobileButtonYes` "是我的账号"
     and stuck modal overlay `#iLoginCompSecMobileModel`).
"""
import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import websockets

FP_AND_AUTH_COOKIES = {
    'token', 'oops', 'mt_c_token', 'userId', 'u', 'w_token', 'userName', 'userFace',
    'waimai_h5_token', 'WEBDFPID', 'uuid', 'iuuid', 'openh5_uuid',
    '_lxsdk', '_lxsdk_cuid', '_lxsdk_s', 'logan_session_token'
}

C_END_ORIGINS_TO_WIPE = (
    'https://cactivityapi-sc.waimai.meituan.com',
    'https://h5.waimai.meituan.com',
    'https://passport.meituan.com',
)

B_END_PROTECTED_DOMAINS = ('shangoue', 'waimaie', 'qnh', 'e.waimai')

MOBILE_CARD_CSS = (
    "html { font-size: 50px !important; background: #f5f5f5 !important; } "
    "body { max-width: 390px !important; margin: 20px auto !important; background: #fff !important; "
    "min-height: 740px !important; box-shadow: 0 4px 20px rgba(0,0,0,0.12) !important; "
    "border-radius: 12px !important; overflow-x: hidden !important; }"
)


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


def fetch_json(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def print_chrome_launch_help(port):
    print(f"[ERROR] Cannot connect to Chrome remote debugging port {port} (http://127.0.0.1:{port}/json/version).", file=sys.stderr)
    print("Please launch a regular Chrome window with remote debugging (NEVER a webdriver/Playwright-managed browser):", file=sys.stderr)
    print(f'  macOS  : /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port={port} --user-data-dir="$HOME/.chrome_meituan_{port}" &', file=sys.stderr)
    print(f'  Windows: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port={port} --user-data-dir="%USERPROFILE%\\.chrome_meituan_{port}"', file=sys.stderr)
    print(f'  Linux  : google-chrome --remote-debugging-port={port} --user-data-dir="$HOME/.chrome_meituan_{port}" &', file=sys.stderr)


async def prepare_clean_login(port=9222, lat=None, lng=None, poi_name=None, address=None, target_url='https://h5.waimai.meituan.com/waimai/mindex/mine'):
    try:
        version_info = fetch_json(f'http://127.0.0.1:{port}/json/version')
        tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')
    except (urllib.error.URLError, OSError) as e:
        print_chrome_launch_help(port)
        raise SystemExit(2) from e

    # 0. Browser-level setup: keep normal download filenames & open tab if none exists
    browser_ws_url = version_info.get('webSocketDebuggerUrl')
    created_target_id = None

    # Close ALL DevTools windows first so H5guard anti-debugging trap never fires
    closed_devtools = 0
    for t in tabs:
        if 'devtools://' in t.get('url', ''):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=2).read()
                closed_devtools += 1
            except Exception:
                pass
    if closed_devtools:
        print(f"[Init] Closed {closed_devtools} DevTools tab(s) to prevent H5guard detection.", flush=True)
        tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')

    page_tabs = [t for t in tabs if t.get('type') == 'page']
    target = next(
        (t for t in page_tabs if any(d in t.get('url', '') for d in (
            'cactivityapi-sc.waimai.meituan.com',
            'h5.waimai.meituan.com',
            'passport.meituan.com',
            'verify.meituan.com',
        ))),
        None
    )

    if browser_ws_url:
        async with websockets.connect(browser_ws_url, max_size=20 * 1024 * 1024) as bws:
            await send_cdp(bws, 1, 'Browser.setDownloadBehavior', {'behavior': 'default'})
            if not target:
                res = await send_cdp(bws, 2, 'Target.createTarget', {'url': target_url})
                created_target_id = (res.get('result') or {}).get('targetId')
                await asyncio.sleep(1.0)
                tabs = fetch_json(f'http://127.0.0.1:{port}/json/list')
                target = next((t for t in tabs if t.get('id') == created_target_id), None)

    if not target:
        raise RuntimeError(f'Failed to find or open a Meituan H5 tab on port {port}')

    seq = 1
    async with websockets.connect(target['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
        # 1. Backup existing pickedpoi / geopoi before wiping all C-end origins
        seq += 1
        prev_poi_msg = await send_cdp(ws, seq, 'Runtime.evaluate', {
            'expression': '''(() => {
                try {
                    return localStorage.getItem("pickedpoi") || localStorage.getItem("geopoi") || "";
                } catch (e) {
                    return "";
                }
            })()''',
            'returnByValue': True
        })
        saved_poi_str = prev_poi_msg.get('result', {}).get('result', {}).get('value') or ''

        # 2. Wipe localStorage + IndexedDB (`dfp_db`) + cache across ALL 3 C-end origins
        #    (even if current tab is not on cactivityapi-sc.waimai.meituan.com!)
        for origin in C_END_ORIGINS_TO_WIPE:
            seq += 1
            await send_cdp(ws, seq, 'Storage.clearDataForOrigin', {
                'origin': origin,
                'storageTypes': 'local_storage,indexeddb,cache_storage'
            })

        # Also clear current-document sessionStorage & dfp_db defensively
        seq += 1
        await send_cdp(ws, seq, 'Runtime.evaluate', {
            'expression': '''(() => {
                try { localStorage.clear(); } catch (e) {}
                try { sessionStorage.clear(); } catch (e) {}
                try { indexedDB.deleteDatabase("dfp_db"); } catch (e) {}
                return true;
            })()''',
            'returnByValue': True
        })

        # 3. Delete all C-end auth + device fingerprint cookies while strictly protecting B-end domains
        seq += 1
        cmsg = await send_cdp(ws, seq, 'Network.getAllCookies')
        cookies = cmsg.get('result', {}).get('cookies', [])
        deleted = []
        for c in cookies:
            dom = c.get('domain', '')
            name = c.get('name', '')
            if any(b in dom for b in B_END_PROTECTED_DOMAINS):
                continue
            if name in FP_AND_AUTH_COOKIES and 'meituan.com' in dom:
                seq += 1
                await send_cdp(ws, seq, 'Network.deleteCookies', {
                    'name': name,
                    'domain': dom,
                    'path': c.get('path', '/')
                })
                deleted.append(f"{dom}:{name}")

        # 4. Register persistent 390px mobile card CSS + desktop click -> mobile tap bridge
        js_inject = f'''
        (() => {{
            const applyMobileCard = () => {{
                if (location.hostname === "h5.waimai.meituan.com" || location.hostname === "passport.meituan.com") {{
                    let st = document.getElementById("__mt_mobile_card_css__");
                    if (!st && document.head) {{
                        st = document.createElement("style");
                        st.id = "__mt_mobile_card_css__";
                        st.textContent = {json.dumps(MOBILE_CARD_CSS)};
                        document.head.appendChild(st);
                    }}
                }}
            }};
            applyMobileCard();
            document.addEventListener("DOMContentLoaded", applyMobileCard);
            setInterval(applyMobileCard, 300);
            if (!window.__mt_click_to_tap_installed__) {{
                window.__mt_click_to_tap_installed__ = true;
                document.addEventListener("click", (e) => {{
                    const el = e.target;
                    if (!el) return;
                    const x = e.clientX || 100;
                    const y = e.clientY || 100;
                    const ts = new Event("touchstart", {{bubbles: true, cancelable: true}});
                    ts.touches = [{{clientX: x, clientY: y}}];
                    el.dispatchEvent(ts);
                    setTimeout(() => {{
                        const te = new Event("touchend", {{bubbles: true, cancelable: true}});
                        te.changedTouches = [{{clientX: x, clientY: y}}];
                        el.dispatchEvent(te);
                        const secModal = document.getElementById("iLoginCompSecMobileModel");
                        if (secModal && el.id === "iLoginCompSecMobileButtonYes") {{
                            secModal.style.display = "none";
                        }}
                    }}, 30);
                }}, true);
            }}
        }})()
        '''
        seq += 1
        await send_cdp(ws, seq, 'Page.enable')
        seq += 1
        await send_cdp(ws, seq, 'Page.addScriptToEvaluateOnNewDocument', {'source': js_inject})
        seq += 1
        await send_cdp(ws, seq, 'Page.bringToFront')

        # 5. Navigate to clean login entry and restore location bookmark if provided
        seq += 1
        await send_cdp(ws, seq, 'Page.navigate', {'url': target_url})
        await asyncio.sleep(2.5)

        if lat is not None and lng is not None:
            poi_payload = json.dumps({
                'lat': float(lat),
                'lng': float(lng),
                'geotype': 2,
                'poi': poi_name or '目标定位点',
                'address': address or ''
            }, ensure_ascii=False)
        else:
            poi_payload = saved_poi_str

        seq += 1
        res = await send_cdp(ws, seq, 'Runtime.evaluate', {
            'expression': js_inject + f''';
            (() => {{
                const prevPicked = {json.dumps(poi_payload)} || localStorage.getItem("pickedpoi") || "";
                for (const k of ["localId", "dfpId", "dfp_idb_token", "shangou_wx_set_info", "saber-vma:user-info", "saber-vma:uuid"]) {{
                    localStorage.removeItem(k);
                }}
                try {{ indexedDB.deleteDatabase("dfp_db"); }} catch (e) {{}}
                if (prevPicked) {{
                    localStorage.setItem("pickedpoi", prevPicked);
                    localStorage.setItem("geopoi", prevPicked);
                }}
                return {{
                    url: location.href,
                    title: document.title,
                    hasDfpId: !!localStorage.getItem("dfpId"),
                    hasLocalId: !!localStorage.getItem("localId"),
                    hasSavedPoi: !!localStorage.getItem("pickedpoi"),
                    cssInjected: !!document.getElementById("__mt_mobile_card_css__"),
                    tapBridge: !!window.__mt_click_to_tap_installed__
                }};
            }})()''',
            'returnByValue': True
        })
        val = res.get('result', {}).get('result', {}).get('value')
        print(f"[CLEAN_LOGIN_READY] Wiped {len(C_END_ORIGINS_TO_WIPE)} C-end origins & deleted {len(deleted)} cookies (B-end protected). State: {json.dumps(val, ensure_ascii=False)}", flush=True)
        print("[NEXT_STEP] 请在弹出的 390px 卡片中直接登录新账号（切勿按 F12）。若同一宽带 IP 今日已连续触发多次 403，建议切换手机 4G/5G 个人热点（勿用 VPN/机房代理）。", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Pre-Login Device Fingerprint & Cookie Sanitizer')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port (default: 9222)')
    parser.add_argument('--lat', type=float, default=None, help='Optional target landmark latitude (WGS84/GCJ02)')
    parser.add_argument('--lng', type=float, default=None, help='Optional target landmark longitude')
    parser.add_argument('--poi-name', type=str, default=None, help='Optional target landmark name')
    parser.add_argument('--address', type=str, default=None, help='Optional target landmark full address')
    parser.add_argument('--target-url', type=str, default='https://h5.waimai.meituan.com/waimai/mindex/mine', help='Landing URL after cleanup')
    args = parser.parse_args()
    asyncio.run(prepare_clean_login(args.port, args.lat, args.lng, args.poi_name, args.address, args.target_url))


if __name__ == '__main__':
    main()
