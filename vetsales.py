"""
Vet Sales Consulting — veterinary practice listings scraper.

Texas-focused veterinary brokerage. Index at
vetsalesconsulting.com/businesses-for-sale/ renders .listing-box cards with
clean /listing/{code-slug}/ detail URLs (codes like tx134, tx132). Detail
pages carry price + revenue in .listing-price / body text.

Source: https://vetsalesconsulting.com/businesses-for-sale/
Output: output/vetsales_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import (get_session, polite_delay, parse_price, clean_text,
                   parse_location, state_from_code, STATE_ABBRS)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vetsales")

BASE_URL = "https://vetsalesconsulting.com"
LISTINGS_URL = "{}/businesses-for-sale/".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "vetsales_raw.csv")

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]


def infer_practice_type(text: str) -> str:
    t = text.lower()
    if "equine" in t or "horse" in t:
        return "Equine"
    if "mixed" in t:
        return "Mixed Animal"
    if "emergency" in t or "specialty" in t:
        return "Emergency/Specialty"
    if "small animal" in t or "companion" in t:
        return "Small Animal"
    if "large animal" in t or "bovine" in t:
        return "Large Animal"
    if "reproduct" in t or "relocation" in t or "boarding" in t:
        return "Small Animal"
    return "Small Animal"


def collect_detail_urls(session) -> List[str]:
    try:
        resp = session.get(LISTINGS_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Index fetch failed: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/listing/" not in href:
            continue
        if not href.startswith("http"):
            href = BASE_URL + href
        href = href.split("#")[0].rstrip("/") + "/"
        # skip index/category pseudopages
        if href.rstrip("/").endswith("/listing") or href in seen:
            continue
        seen.add(href)
        urls.append(href)
    return urls


def parse_detail(session, url: str) -> Optional[Dict]:
    polite_delay(1.5, 3.0)
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            return None
    except Exception as e:
        logger.warning("Detail failed %s: %s", url, e)
        return None
    soup = BeautifulSoup(resp.text, "lxml")

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    # listing code = leading token like tx134 / tx-134
    cm = re.match(r"^([a-z]{2})-?(\d{2,4})", slug, re.I)
    listing_code = (cm.group(1) + cm.group(2)).upper() if cm else slug[:16]

    h1 = soup.find("h1")
    title = clean_text(h1.get_text()) if h1 else ""
    if not title:
        t = soup.find("title")
        title = clean_text(t.get_text()).split(" - ")[0].split("|")[0] if t else ""
    if not title:
        title = slug.replace("-", " ").title()
    if re.search(r"\bsold\b|under\s+contract", title, re.I):
        return None

    full_text = soup.get_text(" ", strip=True)

    # State: from code prefix, then title
    state = state_from_code(listing_code)
    city = ""
    if not state:
        city, state = parse_location(title)

    # description
    description = ""
    for p in soup.find_all(["p", "li"]):
        t = clean_text(p.get_text(" ", strip=True))
        if len(t) > 60 and ("$" in t or re.search(r"practice|clinic|hospital|revenue|animal", t, re.I)):
            description = t[:600]
            break

    asking_price = None
    pe = soup.select_one(".listing-price")
    if pe:
        asking_price = parse_price(pe.get_text())
    if not asking_price:
        m = re.search(r"(?:asking|price|offered\s+at)[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
        if m:
            v = parse_price(m.group(1))
            asking_price = v if (v and v >= 50_000) else None

    annual_revenue = None
    m = re.search(r"(?:gross(?:\s+revenue)?|revenue|collections?|production)\s*(?:were|of|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m:
        v = parse_price(m.group(1))
        annual_revenue = v if (v and v >= 100_000) else None

    exam_rooms = None
    m = re.search(r"(\d+)\s*exam\s*rooms?", full_text, re.I)
    if m:
        exam_rooms = int(m.group(1))

    return {
        "source_id": "vsc-{}".format(listing_code.lower()),
        "title": title,
        "city": city,
        "state": state,
        "asking_price": asking_price,
        "annual_revenue": annual_revenue,
        "practice_type": infer_practice_type(title + " " + description),
        "description": description,
        "broker_name": "Vet Sales Consulting",
        "listing_url": url,
        "exam_rooms": exam_rooms,
        "sqft": None,
        "listing_code": listing_code,
    }


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    urls = collect_detail_urls(session)
    logger.info("Found %d Vet Sales Consulting listing URLs", len(urls))
    all_listings = []
    seen = set()
    for i, url in enumerate(urls, 1):
        row = parse_detail(session, url)
        if row and row["source_id"] not in seen:
            seen.add(row["source_id"])
            all_listings.append(row)
            logger.info("  [%d/%d] %s — %s — $%s", i, len(urls),
                        row["listing_code"], row["state"] or "?",
                        row.get("asking_price") or "N/A")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_listings)
    logger.info("Wrote %d listings to %s", len(all_listings), OUTPUT_FILE)
    return all_listings


if __name__ == "__main__":
    results = run()
    print("Done. {} listings saved to {}".format(len(results), OUTPUT_FILE))
