"""Build a Meta (Facebook) vehicle catalogue feed from a CarDealer5 (CD5) stock API.

Run:  CD5_API_KEY=... python3 build_feed.py dealers/chestnut-wav-centre.json feeds/chestnut-wav-centre.csv

The column set and value conventions copy the AutoHub feed Meta accepted for this
catalogue before the dealer moved to CD5. Safety rules:
  * the feed is written only after a COMPLETE read (rows == meta.total_records);
  * a run that would drop below half of the previous feed's vehicles is refused
    (override with ALLOW_DROP=1 on a deliberate manual run);
  * a refused or failed run exits non-zero and leaves the previous feed untouched;
  * one bad vehicle is skipped and logged, it never stops the feed;
  * links must point at the dealer's own site and photos at CD5's image hosts;
  * purchase price and any other internal field are never written.
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.cd5.uk/api/v1"
UA = "Vopple-CD5-Integration/1.0 (+https://vopple.com)"
COLUMNS = [
    "fb_page_id", "vehicle_id", "title", "description", "url", "make", "model", "year",
    "mileage.value", "mileage.unit", "image", "transmission", "body_style", "vin", "price",
    "exterior_color", "state_of_vehicle", "fuel_type", "availability", "vehicle_type", "trim",
    "address", "latitude", "longitude", "dealer_id", "dealer_name", "dealer_phone",
    "dealer_communication_channel", "dealer_privacy_policy_url",
]
# Words kept exactly as written when they arrive in capitals.
DISPLAY = {"WAV": "WAV", "MPV": "MPV", "SWB": "SWB", "MWB": "MWB", "LWB": "LWB", "XL": "XL", "XLWB": "XLWB",
           "TDI": "TDI", "HDI": "HDi", "BMW": "BMW", "VW": "VW", "UK": "UK", "AA": "AA", "LDV": "LDV",
           "DS": "DS", "MG": "MG", "LEVC": "LEVC", "SEAT": "SEAT", "PSV": "PSV", "SPACETOURER": "SpaceTourer",
           "IONIQ": "IONIQ", "EVITO": "eVito", "E-NV200": "e-NV200", "E-BERLINGO": "e-Berlingo", "AMG": "AMG"}
MAX_IMAGES = 20
IMAGE_HOST = re.compile(r"^img-uk\d+\.cd5\.uk$")
MOJIBAKE = {"â€“": "–", "â€”": "—", "â€™": "’", "â€˜": "‘", "â€œ": "“", "â€\x9d": "”", "â€¦": "…",
            "Â£": "£", "Â ": " ", "ï¿½": "–"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow a redirect with the key attached
        return None


_api_opener = urllib.request.build_opener(_NoRedirect)


def api_json(url, key):
    assert url.startswith(API + "/"), "the key is only ever sent to the CD5 API"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    for attempt in range(3):
        try:
            with _api_opener.open(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # message never contains the key
            if attempt == 2:
                raise SystemExit(f"CD5 API unavailable ({type(e).__name__}: {getattr(e, 'code', '')}) — previous feed kept")
            time.sleep(5 * (attempt + 1))


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fix_text(s):
    """Repair text that was double-encoded upstream, then collapse whitespace."""
    s = str(s or "")
    if re.search(r"[ÂÃâï][\x80-\xbf€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ¿½]", s):
        try:
            s = s.encode("cp1252").decode("utf-8")
        except UnicodeError:
            for bad, good in MOJIBAKE.items():
                s = s.replace(bad, good)
    s = html.unescape(s)
    return " ".join(s.split())


def read_stock(key, statuses):
    rows, page, meta = [], 1, {}
    while True:
        body = api_json(f"{API}/stock?status={','.join(statuses)}&page={page}&page_size=100", key)
        if not body.get("success"):
            raise SystemExit(f"CD5 refused the stock read: {(body.get('error') or {}).get('code')} — previous feed kept")
        rows.extend(body["data"])
        meta = body.get("meta") or {}
        if page >= int(meta.get("total_pages") or 1):
            break
        page += 1
        time.sleep(1.1)
    total = int(meta.get("total_records", -1))
    ids = {str(v.get("id")) for v in rows}
    if total != len(rows) or len(ids) != len(rows):
        raise SystemExit(f"Incomplete stock read: {len(rows)} rows, {len(ids)} unique, {total} expected — previous feed kept")
    return rows


def site_urls(dealer):
    """Map CD5 stock id -> the vehicle's own page, from the dealer's sitemap. Never fatal."""
    host = urllib.parse.urlparse(dealer["site"]).netloc
    for attempt in range(3):
        try:
            xml = fetch_text(dealer["sitemap"])
            break
        except Exception as e:
            if attempt == 2:
                print(f"WARNING: sitemap unavailable ({type(e).__name__}); links will be built from the stock number")
                return {}
            time.sleep(5)
    out = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
        loc = html.unescape(loc.strip())
        p = urllib.parse.urlparse(loc)
        m = re.search(r"/(\d{5,})/?$", p.path)
        if m and p.scheme == "https" and p.netloc == host and re.match(r"^/(cars|vans)/", p.path):
            out[m.group(1)] = loc
    return out


def nice_case(text):
    """Customer-ready casing: never ALL CAPS makes or models (grammar law)."""
    words = []
    for w in fix_text(text).split():
        if w.upper() in DISPLAY:
            words.append(DISPLAY[w.upper()])
        elif w != w.upper() or any(c.isdigit() for c in w):
            words.append(w)  # already mixed case, or a code like L2H2
        else:
            words.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(words)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "vehicle"


def clean_edition(edition, make, model, year):
    """The dealer's edition text without a repeated make/model or a stray year."""
    words = fix_text(edition).split()
    known = {w.lower() for w in (make + " " + model).split()} | {model.replace(" ", "").lower()}
    while words and (words[0].lower() in known or
                     (len(words) > 1 and (words[0] + words[1]).lower() in known)):
        words = words[2:] if len(words) > 1 and (words[0] + words[1]).lower() in known else words[1:]
    # "Automatic Master WAV…" on a Renault Master: drop a model word repeated just after the start
    words = [w for i, w in enumerate(words) if not (i < 3 and w.lower() in known)]
    if words and re.fullmatch(r"(19|20)\d\d", words[-1]):
        words = words[:-1]
    return " ".join(words)


def short_trim(text, limit=50):
    if len(text) <= limit:
        return text
    return text[: limit + 1].rsplit(" ", 1)[0].rstrip(" -,/")


FOOTER = re.compile(r"(call or message us today|whats ?app contact|visit our website|we also offer taxi finance)", re.I)


def clean_description(raw, fallback):
    text = fix_text(raw)
    m = FOOTER.search(text)
    if m:
        text = text[: m.start()]
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"(\+44\s?|\b0)\d[\d\s]{8,12}\d", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -–|,")
    return (text or fallback)[:5000]


def fuel_of(v):
    s = (v.get("fuel") or "").lower()
    if "hybrid" in s or ("electric" in s and "petrol" in s):
        return "HYBRID"
    if "electric" in s:
        return "ELECTRIC"
    if "diesel" in s:
        return "DIESEL"
    if "petrol" in s:
        return "GASOLINE"
    return "OTHER"


def transmission_of(v):
    s = (v.get("transmission") or "").lower()
    if any(k in s for k in ("auto", "semi", "cvt", "dsg", "tiptronic", "dct", "powershift")):
        return "AUTOMATIC"
    if s.startswith("man"):
        return "MANUAL"
    return ""


def body_and_type(v):
    series = (v.get("model_series") or "").lower()
    if re.search(r"\b(panel van|van|chassis|luton|tipper|pick ?up)\b", series):
        return "VAN", "COMMERCIAL"
    return "MINIVAN", "CAR_TRUCK"


def as_int(x):
    return int(float(str(x).replace(",", "").replace("£", "").strip()))


def to_row(v, dealer, urls):
    vid = str(v["id"])
    make, model = nice_case(v.get("make")), nice_case(v.get("model"))
    year, mileage, price = as_int(v["year"]), as_int(v["mileage"]), as_int(v["sale_price"])
    colour = fix_text(v.get("colour"))
    if not (make and model and colour) or year < 1990 or price <= 0:
        raise ValueError("missing make, model, colour, year or price")
    url = urls.get(vid) or f"{dealer['site']}/cars/{slug(make)}/{slug(model)}/{slug(v.get('edition'))}/{vid}/"
    if not url.startswith(dealer["site"] + "/"):
        raise ValueError("link is not on the dealer's site")
    images = []
    for i in sorted(v.get("images") or [], key=lambda i: int(i.get("position") or 999)):
        p = urllib.parse.urlparse(i.get("url") or "")
        if p.scheme == "https" and IMAGE_HOST.match(p.netloc):
            images.append({"url": i["url"]})
    if not images:
        raise ValueError("no usable photo")
    edition = clean_edition(v.get("edition"), make, model, year)
    title = " ".join(f"{year} {make} {model} {edition}".split())[:200]
    raw_desc = v.get("description") or ""
    if re.search(r"vat will be added", raw_desc, re.I):
        title = (title + " - No VAT")[:200]
    fallback = f"{year} {make} {model} {edition}. {mileage:,} miles. Wheelchair accessible vehicle."
    body, vtype = body_and_type(v)
    return {
        "fb_page_id": dealer["fb_page_id"],
        "vehicle_id": vid,
        "title": title,
        "description": clean_description(raw_desc, fallback),
        "url": url,
        "make": make,
        "model": model,
        "year": year,
        "mileage.value": mileage,
        "mileage.unit": "MI",
        "image": json.dumps(images[:MAX_IMAGES], separators=(",", ":")),
        "transmission": transmission_of(v),
        "body_style": body,
        "vin": fix_text(v.get("vin")),
        "price": f"{price} GBP",
        "exterior_color": colour,
        "state_of_vehicle": "Used",
        "fuel_type": fuel_of(v),
        "availability": "available",
        "vehicle_type": vtype,
        "trim": short_trim(edition),
        "address": json.dumps(dealer["address"], separators=(",", ":")),
        "latitude": dealer["latitude"],
        "longitude": dealer["longitude"],
        "dealer_id": dealer["dealer_id"],
        "dealer_name": dealer["dealer_name"],
        "dealer_phone": dealer["dealer_phone"],
        "dealer_communication_channel": dealer["dealer_communication_channel"],
        "dealer_privacy_policy_url": dealer["dealer_privacy_policy_url"],
    }


def previous_count(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def main():
    dealer_path, out_path = sys.argv[1], sys.argv[2]
    dealer = json.load(open(dealer_path))
    key = os.environ.get("CD5_API_KEY", "").strip()
    if not key:
        raise SystemExit("CD5_API_KEY is not set")
    stock = read_stock(key, dealer["statuses"])
    urls = site_urls(dealer)
    rows, skipped = [], []
    for v in stock:
        if v.get("website_visible") is False:
            skipped.append((str(v.get("id")), "hidden from website"))
            continue
        try:
            rows.append(to_row(v, dealer, urls))
        except (ValueError, KeyError, TypeError) as e:
            skipped.append((str(v.get("id")), str(e) or type(e).__name__))
    rows.sort(key=lambda r: int(r["vehicle_id"]))

    previous = previous_count(out_path)
    if not rows:
        raise SystemExit("No advertisable vehicles — previous feed kept")
    if previous and len(rows) < previous * 0.5 and os.environ.get("ALLOW_DROP") != "1":
        raise SystemExit(f"Refused: {len(rows)} vehicles vs {previous} last time (over half gone) — previous feed kept")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, out_path)

    fed = {r["vehicle_id"] for r in rows}
    on_site_not_fed = sorted(set(urls) - fed)
    built_links = sum(1 for r in rows if r["vehicle_id"] not in urls)
    print(f"Wrote {len(rows)} vehicles (previous {previous}).")
    print(f"Skipped {len(skipped)}: " + ("; ".join(f"{i} ({why})" for i, why in skipped) or "none"))
    print(f"Links built without the sitemap: {built_links}. On the website but not in the feed "
          f"(usually reserved): {len(on_site_not_fed)} {' '.join(on_site_not_fed)}")


if __name__ == "__main__":
    main()
