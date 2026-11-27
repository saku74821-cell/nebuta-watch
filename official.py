"""
飯店官網空房查詢
-----------------
支援兩種官網訂房系統：
  toyoko : 東横INN 官網（直接讀官網公開的價格/空房資料，不需要瀏覽器）
  tripla : 用 tripla 訂房系統的官網（ルートイン、ダイワロイネット、JALシティ、レンブラントイン 等）
           用無頭瀏覽器（Playwright）打開官網的訂房結果頁，讀畫面上的結果。
  dormy  : ドーミーイン官網（同樣用無頭瀏覽器讀畫面）
  rjp    : reservation.jp 訂房系統（スマイルホテル 等，同樣用無頭瀏覽器）

每個函式回傳 dict 或 None：
  {"available": bool, "count": 方案數或 None, "url": 訂房頁網址}
  None 代表這次查詢失敗（網站沒回應等），不當成「沒空房」。
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- 東横INN
def check_toyoko(hotel, checkin, checkout, people, log):
    code = hotel["code"]
    inp = {"0": {
        "json": {
            "hotelCodes": [code],
            "checkinDate": checkin + "T00:00:00.000Z",
            "checkoutDate": checkout + "T00:00:00.000Z",
            "numberOfPeople": people,
            "numberOfRoom": 1,
            "smokingType": "all",
        },
        "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
    }}
    url = ("https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices?batch=1&input="
           + urllib.parse.quote(json.dumps(inp, separators=(",", ":"))))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
        info = data[0]["result"]["data"]["json"]["prices"][code]
    except Exception as e:
        log(f"⚠️ 東横INN 官網查詢失敗：{e}")
        return None
    ok = bool(info.get("existEnoughVacantRooms")) and not info.get("isUnderMaintenance")
    page = (f"https://www.toyoko-inn.com/search/detail/{code}/"
            f"?start={checkin}&end={checkout}&room=1&people={people}")
    return {"available": ok, "count": None,
            "min_total": info.get("lowestPrice") or None, "url": page}


# ---------------------------------------------------------------- tripla
def tripla_url(hotel, checkin, checkout, people):
    q = {
        "code": hotel["code"],
        "checkin": checkin.replace("-", "/"),
        "checkout": checkout.replace("-", "/"),
        "type": "plan",
        "is_day_use": "false",
        "order": "recommended",
        "is_including_occupied": "false",
        "rooms": json.dumps([{"adults": people}], separators=(",", ":")),
        "mcp_currency": "JPY",
    }
    return hotel["base"].rstrip("/") + "/booking/result?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------- 需要瀏覽器的官網
# tripla / dormy / rjp 都用無頭瀏覽器打開官網頁面讀畫面。
# 為了加快速度，同時開好幾個分頁一起查（PARALLEL 個）。
PARALLEL = 4


async def _read_page(browser, url, done_js, settle_ms=0):
    """打開網址，等到 done_js 成立（最多 35 秒），回傳畫面文字。"""
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        try:
            await page.wait_for_function(done_js, timeout=35000)
        except Exception:
            pass
        if settle_ms:
            await page.wait_for_timeout(settle_ms)
        return await page.inner_text("body")
    finally:
        await page.close()


async def _tripla(browser, hotel, checkin, checkout, people, log):
    url = tripla_url(hotel, checkin, checkout, people)
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        # 一看到「検索結果」就結束；頁面載入中會短暫出現「見つかりませんでした」，
        # 所以要連續 5 秒都是「見つかりませんでした」才算真的沒房。最多等 35 秒。
        text, none_since = "", None
        for i in range(70):
            await page.wait_for_timeout(500)
            text = await page.inner_text("body")
            if re.search(r"検索結果[:：]\s*\d+", text):
                break
            if "見つかりませんでした" in text:
                none_since = none_since if none_since is not None else i
                if i - none_since >= 10:
                    break
            else:
                none_since = None
    finally:
        await page.close()
    m = re.search(r"検索結果[:：]\s*(\d+)", text)
    if m:
        n = int(m.group(1))
        return {"available": n > 0, "count": n, "min_total": None, "url": url}
    if "見つかりませんでした" in text:
        return {"available": False, "count": 0, "min_total": None, "url": url}
    log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
    return None


async def _dormy(browser, hotel, checkin, checkout, people, log):
    """ドーミーイン官網：方案有標價（￥xx,xxx〜）＝有空房；價格顯示「-」＝沒空房。"""
    from datetime import date
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    q = [("checkin", checkin.replace("-", "/")), ("number_of_nights", nights),
         ("search_by_tag", "plan"), ("keyword", hotel["keyword"]),
         ("number_of_rooms", 1), ("stock_check", "true"),
         ("number_of_adults[]", people)]
    url = "https://dormy-hotels.com/reserve/search-plan?" + urllib.parse.urlencode(q)
    text = await _read_page(browser, url,
                            "document.body.innerText.includes('条件にあったプラン')", 3000)
    if "条件にあったプラン" not in text:
        log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
        return None
    prices = [int(p.replace(",", "")) for p in re.findall(r"￥\s*([\d,]+)\s*〜", text)]
    return {"available": bool(prices), "count": len(prices),
            "min_total": min(prices) if prices else None, "url": url}


async def _rjp(browser, hotel, checkin, checkout, people, log):
    """reservation.jp 系統（スマイルホテル 等）：「件数：N」＝有空房；「予約できる客室がありません」＝沒空房。"""
    q = [("checkin_date", checkin.replace("-", "")), ("checkout_date", checkout.replace("-", "")),
         ("adults", people), ("children", 0), ("rooms", 1), ("dayuseFlg", 0), ("sort", 2)]
    url = f"{hotel['base'].rstrip('/')}/ja/hotels/{hotel['slug']}/rooms?" + urllib.parse.urlencode(q)
    text = await _read_page(
        browser, url,
        "document.body.innerText.includes('件数') || "
        "document.body.innerText.includes('予約できる客室がありません')", 2000)
    if "予約できる客室がありません" in text:
        return {"available": False, "count": 0, "min_total": None, "url": url}
    m = re.search(r"件数[:：]\s*(\d+)", text)
    if m:
        n = int(m.group(1))
        return {"available": n > 0, "count": n, "min_total": None, "url": url}
    log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
    return None


_CHECKERS = {"tripla": (_tripla, lambda h: "tripla:" + h["code"]),
             "dormy": (_dormy, lambda h: "dormy:" + h["keyword"]),
             "rjp": (_rjp, lambda h: "rjp:" + h["slug"])}


async def _browser_all(hotels, checkin, checkout, people, log):
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME_PATH") or None)
        except Exception as e:
            log(f"⚠️ 無法啟動瀏覽器（{e}）。請執行：python3 -m playwright install chromium")
            return out
        sem = asyncio.Semaphore(PARALLEL)

        async def one(h):
            fn, keyf = _CHECKERS[h["type"]]
            async with sem:
                try:
                    r = await fn(browser, h, checkin, checkout, people, log)
                except Exception as e:
                    log(f"⚠️ {h['name']} 官網查詢失敗：{e}")
                    r = None
            if r:
                out[keyf(h)] = {"name": h["name"], **r}

        await asyncio.gather(*(one(h) for h in hotels))
        await browser.close()
    return out


def check_all(hotels, checkin, checkout, people, log):
    """查全部官網，回傳 {key: {name, available, ...}}；查詢失敗的不會出現在結果裡。"""
    out = {}
    for h in hotels:
        if h.get("type") == "toyoko":
            r = check_toyoko(h, checkin, checkout, people, log)
            if r:
                out["toyoko:" + h["code"]] = {"name": h["name"], **r}
    browser_hotels = [h for h in hotels if h.get("type") in _CHECKERS]
    if browser_hotels:
        try:
            import playwright  # noqa
        except ImportError:
            log("ℹ️ 沒有安裝 Playwright，略過需要瀏覽器的官網")
            return out
        out.update(asyncio.run(_browser_all(browser_hotels, checkin, checkout, people, log)))
    return out


# ---------------------------------------------------------------- じゃらん（Jalan）
# 用車站的「周邊住宿」頁面查，一頁就能看到整區有空房的旅館。
# 這些頁面會列出整個「青森・浅虫・八甲田」大區，所以用 exclude 關鍵字濾掉八甲田山區等遠的地方。
JALAN_JS = r"""() => {
  const out = []; const seen = new Set();
  document.querySelectorAll('a[href*="/yad"]').forEach(a => {
    const m = a.href.match(/\/yad(\d+)\//); if (!m || seen.has(m[1])) return;
    const name = (a.innerText || '').trim().split('\n')[0]; if (!name || name.length < 2) return;
    seen.add(m[1]);
    let card = a;
    for (let i = 0; i < 10 && card.parentElement; i++) { card = card.parentElement; if (card.innerText.length > 300) break; }
    out.push({id: m[1], name: name, ctx: card.innerText.replace(/\n+/g, ' | ').slice(0, 600)});
  });
  return out;
}"""


def _jalan_dates(checkin, checkout):
    from datetime import date
    d1, d2 = date.fromisoformat(checkin), date.fromisoformat(checkout)
    return [("stayYear", d1.year), ("stayMonth", d1.month), ("stayDay", d1.day),
            ("stayCount", (d2 - d1).days)]


async def _jalan_area(browser, area, checkin, checkout, people, jcfg, log):
    q = _jalan_dates(checkin, checkout) + [("roomCount", 1), ("adultNum", people),
                                          ("dateUndecided", 0)]
    url = f"https://www.jalan.net/020000/STA_{area['station_code']}/?" + urllib.parse.urlencode(q)
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        try:
            await page.wait_for_function(
                "document.body.innerText.includes('軒ありました') || "
                "document.body.innerText.includes('見つかりませんでした')", timeout=30000)
        except Exception:
            pass
        text = await page.inner_text("body")
        cards = await page.evaluate(JALAN_JS) if "軒ありました" in text else []
    finally:
        await page.close()
    if "見つかりませんでした" in text and "軒ありました" not in text:
        return []
    if "軒ありました" not in text:
        raise RuntimeError("Jalan 畫面看不懂（可能改版了）")
    allow_area = jcfg.get("allow_area_keyword", "青森・浅虫・八甲田")
    exclude = jcfg.get("exclude_keywords", [])
    found = []
    for c in cards:
        ctx = c["ctx"]
        if "エリア" in ctx and allow_area not in ctx:
            continue  # 別的大區（三沢、東津軽…）
        if any(k in c["name"] or k in ctx for k in exclude):
            continue
        plan = (f"https://www.jalan.net/yad{c['id']}/plan/?" +
                urllib.parse.urlencode(q))
        found.append({"id": c["id"], "name": c["name"], "area": area["name"], "url": plan})
    return found


async def _jalan_all(areas, checkin, checkout, people, jcfg, log):
    from playwright.async_api import async_playwright
    results, ok = {}, True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        for area in areas:  # 一區一區慢慢查，不同時開
            try:
                for h in await _jalan_area(browser, area, checkin, checkout, people, jcfg, log):
                    results.setdefault(h["id"], h)
            except Exception as e:
                log(f"⚠️ Jalan（{area['name']}）查詢失敗：{e}")
                ok = False
            await asyncio.sleep(3)
        await browser.close()
    return results, ok


def check_jalan(jcfg, checkin, checkout, people, log):
    """回傳 ({hotel_id: {...}}, 是否全部成功)。"""
    try:
        import playwright  # noqa
    except ImportError:
        return {}, False
    return asyncio.run(_jalan_all(jcfg["areas"], checkin, checkout, people, jcfg, log))


# ---------------------------------------------------------------- 慢速來源共用
async def _open_text(browser, url, done_js, timeout=30000, settle=2000):
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        try:
            await page.wait_for_function(done_js, timeout=timeout)
        except Exception:
            pass
        await page.wait_for_timeout(settle)
        return page, await page.inner_text("body")
    except Exception:
        await page.close()
        raise


async def _with_browser(fn):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        try:
            return await fn(browser)
        finally:
            await browser.close()


def _run(fn):
    try:
        import playwright  # noqa
    except ImportError:
        return None
    return asyncio.run(_with_browser(fn))


# ---------------------------------------------------------------- AirHost（TSUNAGIYA 等一棟貸切）
def check_airhost(acfg, checkin, checkout, people, log):
    """回傳 ({house_id: {name, url}}（只含有空房的）, 是否全部成功)。"""
    base = acfg["base"].rstrip("/")

    async def go(browser):
        found, ok = {}, True
        for h in acfg["houses"]:
            url = (f"{base}/ja/houses/{h['id']}?" +
                   urllib.parse.urlencode({"start_date": checkin, "end_date": checkout,
                                           "guests": people}))
            try:
                page, text = await _open_text(
                    browser, url,
                    "document.body.innerText.includes('カートに追加') || "
                    "document.body.innerText.includes('部屋はありません')")
                await page.close()
            except Exception as e:
                log(f"⚠️ {acfg['name']}（{h['name']}）查詢失敗：{e}")
                ok = False
                continue
            if "カートに追加" in text and "部屋はありません" not in text:
                found[str(h["id"])] = {"name": h["name"], "url": url}
            elif "部屋はありません" not in text:
                log(f"⚠️ {acfg['name']}（{h['name']}）畫面看不懂，這次略過")
                ok = False
            await asyncio.sleep(2)
        return found, ok

    r = _run(go)
    return r if r else ({}, False)


# ---------------------------------------------------------------- Airbnb
def check_airbnb(bcfg, checkin, checkout, adults, children, log):
    """可設定多個搜尋地點（queries），結果合併。"""
    qs = bcfg.get("queries") or [bcfg["query"]]
    found, ok = {}, True
    for q in qs:
        f, o = _check_airbnb_one(dict(bcfg, query=q), checkin, checkout, adults, children, log)
        found.update(f); ok = ok and o
    return found, ok


def _check_airbnb_one(bcfg, checkin, checkout, adults, children, log):
    """回傳 ({room_id: {name, url}}, 是否成功)。搜尋結果本身只會列出那幾天能訂的房源。"""
    q = {"checkin": checkin, "checkout": checkout, "adults": adults,
         "children": children, "currency": "JPY"}
    url = (f"https://www.airbnb.jp/s/{urllib.parse.quote(bcfg['query'])}/homes?" +
           urllib.parse.urlencode(q))

    async def go(browser):
        page, text = await _open_text(
            browser, url, "document.querySelectorAll('[itemprop=\"itemListElement\"]').length > 0 "
            "|| document.body.innerText.includes('完全一致する結果はありません')",
            timeout=40000, settle=4000)
        try:
            for _ in range(3):  # 往下捲，載入更多
                await page.mouse.wheel(0, 5000)
                await page.wait_for_timeout(1500)
            cards = await page.eval_on_selector_all(
                '[itemprop="itemListElement"]',
                "els=>els.map(e=>({t:e.innerText.replace(/\\n+/g,' | '),"
                "h:((e.querySelector('a[href*=\"/rooms/\"]')||{}).href||'')}))")
        finally:
            await page.close()
        return cards, text

    try:
        r = _run(go)
    except Exception as e:
        log(f"⚠️ Airbnb 查詢失敗：{e}")
        return {}, False
    if r is None:
        return {}, False
    cards, text = r
    if not cards and "完全一致する結果はありません" not in text:
        log("⚠️ Airbnb 畫面看不懂或被擋，這次略過")
        return {}, False
    must = bcfg.get("must_contain") or []
    found = {}
    for c in cards:
        m = re.search(r"/rooms/(\d+)", c["h"])
        if not m:
            continue
        if must and not any(k in c["t"] for k in must):
            continue
        if re.search(r"\d+月\d+日\s*[～~-]", c["t"]):  # 標了別的日期＝只有部分日期能住，不算
            continue
        parts = [p.strip() for p in c["t"].split("|") if p.strip()]
        title = next((p for p in parts[2:6] if len(p) > 8 and "評価" not in p), parts[0])
        price = next((p for p in parts if "JPY" in p and "泊" in p), "")
        found[m.group(1)] = {
            "name": f"{parts[1] if len(parts) > 1 else ''}｜{title[:40]} {price[:30]}",
            "url": f"https://www.airbnb.jp/rooms/{m.group(1)}?" +
                   urllib.parse.urlencode({"check_in": checkin, "check_out": checkout,
                                           "adults": adults, "children": children})}
    return found, True


# ---------------------------------------------------------------- KNT（旅行社套裝方案上架）
def check_knt(kcfg, log):
    """打開 KNT 睡魔祭特集頁，找出裡面所有「方案搜尋結果」連結，數有幾筆方案。
    （每年的型錄代碼會變，所以每次都從特集頁重新抓連結）。0＝還沒上架，失敗回 None。"""
    async def go(browser):
        page, _ = await _open_text(browser, kcfg["page_url"],
                                   "document.body.innerText.includes('ねぶた')")
        links = await page.eval_on_selector_all(
            "a[href*='searchlist']", "els=>[...new Set(els.map(e=>e.href))]")
        await page.close()
        total = 0
        for u in links[:8]:
            p, text = await _open_text(browser, u, "document.body.innerText.includes('検索条件')")
            await p.close()
            nums = [int(n) for n in re.findall(r"青森\s*\[(\d+)\]", text)]
            total += max(nums) if nums else 0
            await asyncio.sleep(2)
        return total, len(links)
    try:
        r = _run(go)
    except Exception as e:
        log(f"⚠️ KNT 查詢失敗：{e}")
        return None
    if r is None:
        return None
    total, nlinks = r
    if nlinks == 0:
        log("⚠️ KNT 特集頁找不到方案連結（可能改版了）")
    return total


# ---------------------------------------------------------------- Agoda
def check_agoda(gcfg, checkin, checkout, adults, children, child_age, log):
    """回傳 ({slug: {name, url}}（有價格、沒完売的）, 是否成功)。"""
    from datetime import date
    los = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    q = {"city": gcfg["city_id"], "checkIn": checkin, "los": los, "rooms": 1,
         "adults": adults, "children": children, "currency": "JPY"}
    if children:
        q["childAges"] = ",".join([str(child_age)] * children)
    url = "https://www.agoda.com/ja-jp/search?" + urllib.parse.urlencode(q)

    async def go(browser):
        page = await browser.new_page(locale="ja-JP", user_agent=UA)
        try:
            await page.goto(url, timeout=60000)
            try:
                await page.wait_for_selector('[data-selenium="hotel-item"]', timeout=30000)
            except Exception:
                return None
            await page.wait_for_timeout(3000)
            items = page.locator('[data-selenium="hotel-item"]')
            out = []
            for i in range(min(await items.count(), 40)):
                it = items.nth(i)
                try:
                    await it.scroll_into_view_if_needed()
                    await page.wait_for_timeout(800)
                    t = (await it.inner_text()).replace("\n", " | ")
                    href = await it.locator("a").first.get_attribute("href") or ""
                except Exception:
                    continue
                out.append((t, href))
            return out
        finally:
            await page.close()

    try:
        items = _run(go)
    except Exception as e:
        log(f"⚠️ Agoda 查詢失敗：{e}")
        return {}, False
    if items is None:
        log("⚠️ Agoda 沒有搜尋結果或被擋，這次略過")
        return {}, False
    max_km = float(gcfg.get("max_km_from_center", 5))
    keep_kw = gcfg.get("always_keep_keywords") or []
    found = {}
    for t, href in items:
        if "完売" in t or "¥" not in t:
            continue
        m = re.search(r"市内中心まで\s*([\d.]+)\s*km", t)
        if m and float(m.group(1)) > max_km and not any(k in t for k in keep_kw):
            continue
        first = t.split("|")[0].strip()
        name = first.replace("を新しいタブで開く", "").strip() or first
        price = re.search(r"¥\s*[\d,]+", t)
        slug = href.split("?")[0].strip("/")
        if not slug:
            continue
        sep = "&" if "?" in href else "?"
        link = "https://www.agoda.com" + href if href.startswith("/") else href
        found[slug] = {"name": f"{name}（{price.group(0) if price else ''}／晚）", "url": link}
    return found, True


# ---------------------------------------------------------------- Booking.com
def check_booking(kcfg, checkin, checkout, adults, children, child_age, log):
    """可設定多個搜尋地點（queries），距離以各自的搜尋地點計算，結果合併。"""
    qs = kcfg.get("queries") or [kcfg["query"]]
    found, ok = {}, True
    for q in qs:
        f, o = _check_booking_one(dict(kcfg, query=q), checkin, checkout, adults, children,
                                  child_age, log)
        found.update(f); ok = ok and o
    return found, ok


def _check_booking_one(kcfg, checkin, checkout, adults, children, child_age, log):
    """回傳 ({slug: {name, url}}, 是否成功)。Booking 的搜尋結果只會列出那幾天能訂的。"""
    q = [("ss", kcfg["query"]), ("checkin", checkin), ("checkout", checkout),
         ("group_adults", adults), ("group_children", children), ("no_rooms", 1),
         ("selected_currency", "JPY")]
    q += [("age", child_age)] * children
    url = "https://www.booking.com/searchresults.ja.html?" + urllib.parse.urlencode(q)

    async def go(browser):
        page = await browser.new_page(locale="ja-JP", user_agent=UA)
        try:
            await page.goto(url, timeout=60000)
            try:
                await page.wait_for_function(
                    "document.querySelectorAll('[data-testid=\"property-card\"]').length > 0 || "
                    "document.body.innerText.includes('見つかりません')", timeout=30000)
            except Exception:
                pass
            for _ in range(3):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(1500)
            cards = await page.eval_on_selector_all(
                '[data-testid="property-card"]',
                "els=>els.map(e=>({t:e.innerText.replace(/\\n+/g,' | '),"
                "h:((e.querySelector('a[data-testid=\"title-link\"]')||e.querySelector('a')||{}).href||'')}))")
            text = await page.inner_text("body")
            return cards, text
        finally:
            await page.close()

    try:
        r = _run(go)
    except Exception as e:
        log(f"⚠️ Booking 查詢失敗：{e}")
        return {}, False
    if r is None:
        return {}, False
    cards, text = r
    if not cards and "検索結果" not in text:
        log("⚠️ Booking 畫面看不懂或被擋，這次略過")
        return {}, False
    max_km = float(kcfg.get("max_km", 5))
    found = {}
    for c in cards:
        t = c["t"]
        m = re.search(r"から\s*([\d.]+)\s*km", t)
        if m and float(m.group(1)) > max_km:
            continue
        slug = c["h"].split("?")[0].rstrip("/").split("/")[-1].replace(".ja.html", "")
        if not slug:
            continue
        name = t.split("|")[0].strip()
        price = re.search(r"￥\s*[\d,]+", t)
        link = c["h"].split("?")[0] + "?" + urllib.parse.urlencode(
            [("checkin", checkin), ("checkout", checkout), ("group_adults", adults),
             ("group_children", children), ("no_rooms", 1)] + [("age", child_age)] * children)
        found[slug] = {"name": f"{name}（{price.group(0) if price else ''}）", "url": link}
    return found, True


# ---------------------------------------------------------------- 樂天旅遊網站（不用 API 金鑰）
def check_rakuten_web(rcfg, checkin, checkout, adults, upper, lower, log):
    """樂天旅遊的區域搜尋頁（帶日期），只列出那幾天有空房的住宿。回傳 ({hotel_id: {name, url}}, 是否成功)。"""
    from datetime import date
    d1, d2 = date.fromisoformat(checkin), date.fromisoformat(checkout)
    dq = [("f_nen1", d1.year), ("f_tuki1", d1.month), ("f_hi1", d1.day),
          ("f_nen2", d2.year), ("f_tuki2", d2.month), ("f_hi2", d2.day),
          ("f_heya_su", 1), ("f_otona_su", adults), ("f_s1", upper), ("f_s2", lower),
          ("f_y1", 0), ("f_y2", 0), ("f_y3", 0), ("f_y4", 0)]
    exclude = rcfg.get("exclude_keywords") or []

    async def go(browser):
        found, ok = {}, True
        for area in rcfg["areas"]:
            q = [("f_dai", "japan"), ("f_chu", area["chu"]), ("f_shou", area["shou"]),
                 ("f_sai", area.get("sai", "")), ("f_sort", "hotel"), ("f_page", 1),
                 ("f_hyoji", 30)] + dq
            url = (f"https://search.travel.rakuten.co.jp/ds/hotellist/{area['path']}?" +
                   urllib.parse.urlencode(q))
            try:
                page, text = await _open_text(
                    browser, url, "document.body.innerText.includes('件中') || "
                    "document.body.innerText.includes('見つかりません')", settle=3000)
                links = await page.eval_on_selector_all(
                    "a[href*='travel.rakuten.co.jp/HOTEL/']",
                    "els=>els.map(e=>[e.innerText.trim(),e.href])")
                await page.close()
            except Exception as e:
                log(f"⚠️ 樂天網站（{area['name']}）查詢失敗：{e}")
                ok = False
                continue
            if "件中" not in text and "見つかりません" not in text:
                log(f"⚠️ 樂天網站（{area['name']}）畫面看不懂或被擋，這次略過")
                ok = False
                continue
            for name, href in links:
                m = re.search(r"/HOTEL/(\d+)/\1\.html", href)
                if not m or not name or re.match(r"^[\d.（(]", name) or m.group(1) in found:
                    continue
                if any(k in name for k in exclude):
                    continue
                found[m.group(1)] = {
                    "name": name,
                    "url": f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{m.group(1)}?" +
                           urllib.parse.urlencode(dq)}
            await asyncio.sleep(2)
        return found, ok

    r = _run(go)
    return r if r else ({}, False)


# ---------------------------------------------------------------- JTB（るるぶトラベル）
def check_jtb(jcfg, checkin, checkout, log):
    """JTB 國內住宿的區域列表（帶日期），只列出那幾天能訂的。回傳 ({htl_id: {name, url}}, 是否成功)。"""
    from datetime import date
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    d = checkin.replace("-", "")

    async def go(browser):
        found, ok = {}, True
        for area in jcfg["areas"]:
            q = {"multiarea": area["code"], "room": 1, "roomassign": jcfg.get("roomassign", "m2"),
                 "godate": d, "checkindate": d, "staynight": nights}
            url = f"https://www.jtb.co.jp/kokunai-hotel/list/{area['code']}/?" + urllib.parse.urlencode(q)
            try:
                page, text = await _open_text(browser, url,
                                              "/\\d+件/.test(document.body.innerText)", settle=3000)
                links = await page.eval_on_selector_all(
                    "a[href*='/kokunai-hotel/htl/']", "els=>els.map(e=>[e.innerText.trim(),e.href])")
                await page.close()
            except Exception as e:
                log(f"⚠️ JTB（{area['name']}）查詢失敗：{e}")
                ok = False
                continue
            m = re.search(r"(\d+)件", text)
            if not m:
                log(f"⚠️ JTB（{area['name']}）畫面看不懂或被擋，這次略過")
                ok = False
                continue
            if int(m.group(1)) == 0:
                continue
            for name, href in links:
                mm = re.search(r"/kokunai-hotel/htl/(\d{6,8})/?", href)
                name = name.split("\n")[0].strip()
                bad = ("アンケート", "アクセス", "評価", "クチコミ", "プラン", "写真", "地図", "詳細")
                if (not mm or len(name) < 3 or mm.group(1) in found or re.match(r"^[\d.]", name)
                        or any(b in name for b in bad)):
                    continue
                found[mm.group(1)] = {
                    "name": name,
                    "url": f"https://www.jtb.co.jp/kokunai-hotel/htl/{mm.group(1)}/plan/?" +
                           urllib.parse.urlencode({"room": 1, "roomassign": q["roomassign"],
                                                   "godate": d, "checkindate": d,
                                                   "staynight": nights})}
            await asyncio.sleep(2)
        return found, ok

    r = _run(go)
    return r if r else ({}, False)


# ---------------------------------------------------------------- 近畿日本ツーリスト（KNT）住宿
def check_knt_yado(kcfg, checkin, checkout, adults, children, log):
    """KNT 旅館・ホテル的小區域列表（帶日期），只列出那幾天能訂的。回傳 ({宿コード: {name, url}}, 是否成功)。"""
    from datetime import date
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    base_q = {"stay_ymd": checkin.replace("-", ""), "StayCount": nights, "RoomCount": 1,
              "GuestCountM": adults, "GuestCountC": children}

    async def go(browser):
        found, ok = {}, True
        for area in kcfg["areas"]:
            url = (f"https://yado.knt.co.jp/yadolist/?catid={area['catid']}&" +
                   urllib.parse.urlencode(base_q))
            try:
                page, text = await _open_text(browser, url,
                                              "/検索結果\\s*\\d+件/.test(document.body.innerText)",
                                              settle=3000)
                await page.close()
            except Exception as e:
                log(f"⚠️ KNT 住宿（{area['name']}）查詢失敗：{e}")
                ok = False
                continue
            m = re.search(r"検索結果\s*(\d+)件", text)
            if not m:
                log(f"⚠️ KNT 住宿（{area['name']}）畫面看不懂或被擋，這次略過")
                ok = False
                continue
            if int(m.group(1)) == 0:
                continue
            body = text[m.end():]
            prev = 0
            for mm in re.finditer(r"宿コード：\s*(S\d+)", body):
                chunk = body[prev:mm.start()]
                prev = mm.end()
                chunk = re.split(r"タイプ）|人気順", chunk)[-1]
                lines = [x.strip() for x in chunk.split("\n") if x.strip()]
                code = mm.group(1)
                found[code] = {
                    "name": (lines[0][:40] if lines else code),
                    "url": f"https://yado.knt.co.jp/planlist/{code}/?" + urllib.parse.urlencode(base_q)}
            await asyncio.sleep(2)
        return found, ok

    r = _run(go)
    return r if r else ({}, False)


# ---------------------------------------------------------------- 日本旅行（NTA）住宿
def check_nta(ncfg, checkin, checkout, adults, children, log):
    """日本旅行的區域搜尋（只顯示有空房），回傳 ({宿ID: {name, url}}, 是否成功)。"""
    from datetime import date
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    d = checkin.replace("-", "")
    exclude_ids = tuple(ncfg.get("exclude_id_prefix") or [])
    q = {"Page": 1, "SortOrder": "Popular", "Pax": adults + children, "pax_adm": adults,
         "pax_adf": 0, "pax_ca": children, "pax_cb": 0, "pax_if": 0, "Days": d, "Month": d[:6],
         "StayNights": nights, "NumberOfRooms": 1, "Direction": ncfg.get("direction", "02"),
         "Pref": ncfg.get("pref", "02"), "Area": ncfg["area"], "Condition": "naviPlan",
         "StockOnly": 1, "ShowSD": 0}
    url = "https://www.nta.co.jp/nta_yado/?" + urllib.parse.urlencode(q)

    async def go(browser):
        page, text = await _open_text(browser, url,
                                      "/検索結果：\\s*\\d+件/.test(document.body.innerText)", settle=3000)
        links = await page.eval_on_selector_all(
            "a[href*='/nta_yado/hotel/']", "els=>els.map(e=>[e.innerText.trim(),e.href])")
        await page.close()
        return text, links

    try:
        r = _run(go)
    except Exception as e:
        log(f"⚠️ 日本旅行 查詢失敗：{e}")
        return {}, False
    if r is None:
        return {}, False
    text, links = r
    m = re.search(r"検索結果：\s*(\d+)件", text)
    if not m:
        log("⚠️ 日本旅行 畫面看不懂或被擋，這次略過")
        return {}, False
    found = {}
    if int(m.group(1)) == 0:
        return found, True
    for name, href in links:
        mm = re.search(r"/nta_yado/hotel/(\d+-\d+)/info", href)
        if not mm or not name or mm.group(1) in found or mm.group(1).startswith(exclude_ids):
            continue
        found[mm.group(1)] = {
            "name": name.split("\n")[0][:40],
            "url": f"https://www.nta.co.jp/nta_yado/hotel/{mm.group(1)}/?" + urllib.parse.urlencode(
                {k: q[k] for k in ("Pax", "pax_adm", "pax_adf", "pax_ca", "pax_cb", "pax_if",
                                   "Days", "StayNights", "NumberOfRooms")})}
    return found, True
