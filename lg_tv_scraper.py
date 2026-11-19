# ==============================================================================
# LG TV PRICE SCRAPER - DMX / DMCL / NguyenKim (Playwright)
# ------------------------------------------------------------------------------
# Cai dat:
#   pip install playwright gspread
#   playwright install chromium            (Colab/Linux: them  playwright install-deps chromium)
#
# Chay:
#   python lg_tv_scraper.py                         # quet ca 3 site, co buoc gio hang/voucher
#   python lg_tv_scraper.py --sites DMX,NK          # chon site
#   python lg_tv_scraper.py --no-checkout           # chi lay gia niem yet (nhanh)
#   python lg_tv_scraper.py --limit 3 --headed --debug   # thu 3 sp/site, hien trinh duyet, luu HTML/anh
#   python lg_tv_scraper.py --sheet                 # ghi them vao Google Sheet (tab TV_SHEET_NAME)
#
# Bien moi truong (tuy chon): SPREADSHEET_URL, TV_SHEET_NAME, GOOGLE_SERVICE_ACCOUNT_JSON,
#                             PROXY_SERVER (vd http://ip:port, dung khi bi chan IP nuoc ngoai)
# ==============================================================================
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright

TZ_VN = timezone(timedelta(hours=7))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MORE_RE = re.compile(r"xem thêm.{0,25}(sản phẩm|tivi|tv)", re.I)
BUY_RE = re.compile(r"^\s*(mua ngay|đặt mua|thêm vào giỏ)", re.I)
VOUCHER_OPEN_RE = re.compile(r"mã giảm giá|voucher|nhập mã|chọn mã|mã ưu đãi|phiếu giảm", re.I)
VOUCHER_APPLY_RE = re.compile(r"^\s*(áp dụng|dùng ngay|chọn)\s*$", re.I)
VOUCHER_LINE_RE = re.compile(
    r"voucher|mã giảm|giảm thêm|giảm đến|giảm tối đa|coupon|phiếu mua hàng|ưu đãi thanh toán|"
    r"thanh toán qua|quà tặng|giảm \d", re.I)
MONEY_RE = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
STOCK_RE = re.compile(r"ngừng kinh doanh|hết hàng|tạm hết|ngưng kinh doanh", re.I)

CART_PATTERNS = [  # thu tu uu tien: so tien cuoi cung o cong thanh toan
    ("final", r"(?:cần thanh toán|tổng thanh toán|tổng tiền thanh toán|thanh toán ngay)"),
    ("total", r"(?:tổng tiền|tổng cộng|tổng)"),
    ("sub", r"(?:thành tiền|tạm tính)"),
]

# ------------------------------------------------------------------------------
# Cau hinh tung site
# ------------------------------------------------------------------------------
SITES = {
    "DMX": {
        "title": "DMX",
        "list_url": "https://www.dienmayxanh.com/tivi-lg",
        "link_sel": "[data-product-code] a[title], a.main-contain",
        "accept": lambda path, title: path.startswith("/tivi") and path.count("/") >= 2,
        "sell_sel": [".box-price-present", ".bs_price strong", ".price-one .box-price-present"],
        "mrp_sel": [".box-price-old", ".bs_price em", ".price-old"],
    },
    "DMCL": {
        "title": "DMCL",
        "list_url": "https://dienmaycholon.com/tivi-lg",
        "link_sel": "a.product-card, a:has(h3)",
        "accept": lambda path, title: len(path.strip("/")) > 5 and "tivi" in path.lower(),
        "sell_sel": [".info_pro_price .price_sale", ".box_price_layout_cost strong", ".price_sale",
                     ".product-price-new"],
        "mrp_sel": [".price_market", ".product-price-old"],
    },
    "NK": {
        "title": "NguyenKim",
        "list_url": "https://www.nguyenkim.com/tivi.c?property=thuong-hieu-brand:LG",
        "link_sel": "a[href]",
        "accept": lambda path, title: (path.count("/") == 1 and "lg" in path.lower()
                                       and ("tivi" in path.lower() or "tv" in path.lower())
                                       and re.search(r"\d", path.split("-")[-1]) is not None),
        "sell_sel": [],
        "mrp_sel": [],
    },
}

DEBUG_DIR = None


def now_str():
    return datetime.now(TZ_VN).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(msg, flush=True)


def clean_text(s):
    return " ".join((s or "").split())


def to_int(s):
    m = MONEY_RE.search(s or "")
    return int(re.sub(r"\D", "", m.group(0))) if m else 0


def extract_model_code(name, url=""):
    combined = f"{name} {url}".upper()
    m = re.search(r"\b(OLED\d{2}[A-Z0-9]{3,8}|\d{2}[A-Z]{2,5}\d{2,4}[A-Z0-9]{2,6})\b", combined)
    return m.group(1) if m else "N/A"


def slugify(url):
    return re.sub(r"[^a-z0-9]+", "-", urlparse(url).path.lower()).strip("-")[-60:] or "page"


def dump(page, site, tag):
    if not DEBUG_DIR:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    base = os.path.join(DEBUG_DIR, f"{site}_{tag}")
    try:
        page.screenshot(path=base + ".png", full_page=True)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        log(f"    [debug] khong luu duoc {tag}: {e}")


def goto(page, url, timeout=60000):
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


def new_context(browser):
    ctx = browser.new_context(user_agent=UA, locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh",
                              viewport={"width": 1366, "height": 900}, ignore_https_errors=True)
    ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font")
              else r.continue_())
    return ctx


# ------------------------------------------------------------------------------
# 1. LISTING: bam "Xem them" den het, gom link san pham
# ------------------------------------------------------------------------------
def collect_links(page, cfg, found):
    rows = page.eval_on_selector_all(
        cfg["link_sel"],
        "els => els.map(e => ({href: e.href, title: e.getAttribute('title') || e.innerText || ''}))")
    for r in rows:
        href = (r.get("href") or "").split("?")[0].split("#")[0]
        if not href.startswith("http"):
            continue
        path = urlparse(href).path
        title = clean_text(r.get("title"))
        if cfg["accept"](path, title) and href not in found:
            found[href] = title


def find_more_button(page):
    loc = page.get_by_text(MORE_RE)
    for i in range(min(loc.count(), 6)):
        el = loc.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            pass
    return None


def expand_listing(page, cfg, site, max_clicks=80):
    found = {}
    clicks = stagnant = 0
    while clicks < max_clicks and stagnant < 3:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        before = len(found)
        collect_links(page, cfg, found)
        btn = find_more_button(page)
        if not btn:
            if len(found) == before:
                stagnant += 1
            continue
        url_before = page.url
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=5000)
        except Exception:
            try:
                btn.evaluate("e => e.click()")
            except Exception:
                stagnant += 1
                continue
        clicks += 1
        page.wait_for_timeout(1800)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        collect_links(page, cfg, found)
        if len(found) == before and page.url == url_before:
            stagnant += 1
        else:
            stagnant = 0
        log(f"  [{site}] bam 'Xem them' lan {clicks}: tong {len(found)} link")
    collect_links(page, cfg, found)
    return found


# ------------------------------------------------------------------------------
# 2. TRANG CHI TIET: gia niem yet / gia ban / voucher hien thi tren trang
# ------------------------------------------------------------------------------
PAGE_JS = """() => {
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => { try { return JSON.parse(s.textContent) } catch (e) { return null } }).filter(Boolean);
  const nd = document.getElementById('__NEXT_DATA__');
  return {ld, next: nd ? nd.textContent : null,
          h1: (document.querySelector('h1') || {}).innerText || '',
          text: document.body.innerText.slice(0, 80000)};
}"""


def flatten_ld(ld):
    out = []
    for d in ld:
        if isinstance(d, list):
            out += flatten_ld(d)
        elif isinstance(d, dict):
            if "@graph" in d:
                out += flatten_ld(d["@graph"])
            out.append(d)
    return out


def first_text(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                t = clean_text(loc.first.inner_text(timeout=2000))
                if to_int(t):
                    return to_int(t)
        except Exception:
            pass
    return 0


def voucher_lines(text, limit=12):
    seen, out = set(), []
    for line in (clean_text(x) for x in text.splitlines()):
        if 6 <= len(line) <= 220 and VOUCHER_LINE_RE.search(line) and line not in seen:
            seen.add(line)
            out.append(line)
            if len(out) >= limit:
                break
    return out


def parse_detail(page, cfg, url):
    d = page.evaluate(PAGE_JS)
    name, sell, mrp = clean_text(d["h1"]), 0, 0

    if d["next"]:  # Nguyen Kim (Next.js)
        try:
            p = json.loads(d["next"])["props"]["pageProps"]["pageDetail"]["data"]
            n = p.get("name")
            name = (n.get("vi") or n.get("en")) if isinstance(n, dict) else (n or name)
            sell = int(p.get("finalPrice") or 0)
            mrp = int(p.get("price") or 0)
        except Exception:
            pass

    if not sell:
        sell = first_text(page, cfg["sell_sel"])
        mrp = first_text(page, cfg["mrp_sel"])

    if not sell:  # JSON-LD
        for node in flatten_ld(d["ld"]):
            if node.get("@type") == "Product":
                name = name or node.get("name", "")
                offers = node.get("offers") or {}
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    sell = int(float(offers.get("price") or offers.get("lowPrice") or 0))
                break

    if not sell:  # heuristic cuoi: so tien >= 1 trieu dau tien trong text
        nums = [int(re.sub(r"\D", "", m)) for m in MONEY_RE.findall(d["text"])]
        nums = [n for n in nums if n >= 1_000_000]
        if nums:
            sell = nums[0]
            mrp = nums[1] if len(nums) > 1 and nums[1] > sell else 0

    mrp = mrp or sell
    status = "Đang kinh doanh"
    m = STOCK_RE.search(d["text"][:20000])
    if m:
        status = m.group(0).lower()
    return {"name": name or "N/A", "sell": sell, "mrp": mrp, "status": status,
            "vouchers": voucher_lines(d["text"])}


# ------------------------------------------------------------------------------
# 3. GIO HANG / CONG THANH TOAN: gia cuoi sau khi ap voucher
# ------------------------------------------------------------------------------
def click_first(page, pattern, roles=("button", "link")):
    for role in roles:
        loc = page.get_by_role(role, name=pattern)
        for i in range(min(loc.count(), 6)):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled():
                    el.click(timeout=5000)
                    return True
            except Exception:
                continue
    return False


def read_cart_total(text):
    for key, label in CART_PATTERNS:
        hits = re.findall(label + r"[^\d]{0,40}(\d{1,3}(?:[.,]\d{3})+)", text, re.I)
        if hits:
            return key, int(re.sub(r"\D", "", hits[-1]))
    return None, 0


def open_voucher_panel(page):
    loc = page.get_by_text(VOUCHER_OPEN_RE)
    for i in range(min(loc.count(), 8)):
        el = loc.nth(i)
        try:
            if el.is_visible() and len(clean_text(el.inner_text(timeout=1500))) < 60:
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def checkout_price(browser, url, site):
    """Them vao gio -> doc tong tien -> mo o voucher -> ap tung voucher -> doc lai tong tien."""
    res = {"final": 0, "min": 0, "steps": "", "vouchers": []}
    ctx = new_context(browser)
    try:
        page = ctx.new_page()
        goto(page, url)
        if not click_first(page, BUY_RE) and not click_first(page, BUY_RE, ("link", "button", "generic")):
            res["steps"] = "khong tim thay nut Mua ngay/Them vao gio"
            dump(page, site, slugify(url) + "_nobuy")
            return res
        page.wait_for_timeout(3500)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        dump(page, site, slugify(url) + "_cart")

        key, total = read_cart_total(page.inner_text("body"))
        steps = [f"truoc voucher: {total:,}" if total else "khong doc duoc tong tien"]
        totals = [total] if total else []

        if open_voucher_panel(page):
            dump(page, site, slugify(url) + "_voucher")
            res["vouchers"] = voucher_lines(page.inner_text("body"), 20)
            applied = 0
            for _ in range(8):  # moi vong ap 1 voucher con "Ap dung"
                if not click_first(page, VOUCHER_APPLY_RE, ("button", "link")):
                    break
                applied += 1
                page.wait_for_timeout(2000)
                _, t = read_cart_total(page.inner_text("body"))
                if t:
                    totals.append(t)
                    steps.append(f"sau voucher #{applied}: {t:,}")
                if not open_voucher_panel(page):
                    break
        else:
            steps.append("khong mo duoc o voucher o cong thanh toan")
            res["vouchers"] = voucher_lines(page.inner_text("body"), 12)

        dump(page, site, slugify(url) + "_final")
        if totals:
            res["final"], res["min"] = totals[-1], min(totals)
        res["steps"] = " | ".join(steps)
    except Exception as e:
        res["steps"] = f"loi checkout: {e}"
    finally:
        ctx.close()
    return res


# ------------------------------------------------------------------------------
# DIEU PHOI
# ------------------------------------------------------------------------------
COLUMNS = ["Giờ quét", "Page Title", "Mã Model", "Tên Model", "Status", "direct product link",
           "MRP price", "Selling price", "Giá cuối (cổng thanh toán)", "Giá thấp nhất sau voucher",
           "Chi tiết thanh toán", "Voucher / khuyến mãi"]


def scrape_site(browser, key, limit, checkout, writer, fh):
    cfg = SITES[key]
    log(f"\n=== {key}: {cfg['list_url']} ===")
    ctx = new_context(browser)
    page = ctx.new_page()
    try:
        goto(page, cfg["list_url"])
        links = expand_listing(page, cfg, key)
        dump(page, key, "listing")
    finally:
        ctx.close()
    items = [(u, t) for u, t in links.items() if not t or "lg" in t.lower() or key == "NK"]
    log(f"[{key}] tim thay {len(items)} san pham" + (f" (gioi han {limit})" if limit else ""))
    if limit:
        items = items[:limit]

    rows = []
    for i, (url, _) in enumerate(items, 1):
        log(f"[{key} {i}/{len(items)}] {url}")
        ctx = new_context(browser)
        try:
            page = ctx.new_page()
            goto(page, url)
            d = parse_detail(page, cfg, url)
            dump(page, key, slugify(url) + "_detail")
        except Exception as e:
            log(f"    [!] loi trang chi tiet: {e}")
            continue
        finally:
            ctx.close()

        co = {"final": 0, "min": 0, "steps": "", "vouchers": []}
        if checkout and d["sell"] and d["status"] == "Đang kinh doanh":
            co = checkout_price(browser, url, key)
        vouchers = co["vouchers"] or d["vouchers"]
        row = [now_str(), cfg["title"], extract_model_code(d["name"], url), d["name"], d["status"], url,
               d["mrp"], d["sell"], co["final"] or "", co["min"] or "", co["steps"],
               " | ".join(vouchers)]
        log(f"    -> {row[2]} | niem yet {d['sell']:,} | cuoi {co['final']:,} | min {co['min']:,}")
        rows.append(row)
        writer.writerow(row)
        fh.flush()
        time.sleep(1.2)
    return rows


def push_sheet(rows):
    import gspread
    url = os.environ.get("SPREADSHEET_URL")
    if not url:
        log("[!] Thieu SPREADSHEET_URL, bo qua ghi Sheet")
        return
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds:
        gc = gspread.service_account_from_dict(json.loads(creds))
    else:
        from google.colab import auth
        from google.auth import default
        auth.authenticate_user()
        gc = gspread.authorize(default()[0])
    sh = gc.open_by_url(url)
    name = os.environ.get("TV_SHEET_NAME", "TV")
    try:
        ws = sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=2000, cols=len(COLUMNS))
    if not ws.get_all_values():
        ws.append_row(COLUMNS)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    log(f"[OK] da ghi {len(rows)} dong vao tab '{name}'")


def main():
    global DEBUG_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default="DMX,DMCL,NK")
    ap.add_argument("--limit", type=int, default=0, help="so san pham toi da moi site (0 = het)")
    ap.add_argument("--no-checkout", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--debug", action="store_true", help="luu anh + HTML moi buoc vao ./debug_tv")
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--out", default=f"lg_tv_{datetime.now(TZ_VN):%Y%m%d_%H%M}.csv")
    args = ap.parse_args()
    if args.debug:
        DEBUG_DIR = "debug_tv"

    launch = {"headless": not args.headed}
    if os.environ.get("PROXY_SERVER"):
        launch["proxy"] = {"server": os.environ["PROXY_SERVER"]}

    all_rows = []
    with sync_playwright() as pw, open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        browser = pw.chromium.launch(**launch)
        try:
            for key in [s.strip().upper() for s in args.sites.split(",") if s.strip()]:
                if key not in SITES:
                    log(f"[!] Site khong hop le: {key}")
                    continue
                try:
                    all_rows += scrape_site(browser, key, args.limit, not args.no_checkout, writer, fh)
                except Exception as e:
                    log(f"[!] {key} that bai: {e}")
        finally:
            browser.close()

    log(f"\n[OK] {len(all_rows)} san pham -> {args.out}")
    if args.sheet and all_rows:
        push_sheet(all_rows)


if __name__ == "__main__":
    sys.exit(main())
