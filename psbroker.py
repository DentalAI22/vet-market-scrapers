"""
PS Broker — veterinary practice listings scraper.

Index at psbroker.com/veterinary-practices-for-sale/ links to detail pages at
/property/{county-state-code}/ (e.g. bexar-county-texas-tx10). The trailing
token (tx10) encodes state. Detail pages carry gross revenue / practice facts;
asking price is often gated (buyer registration).

Source: https://psbroker.com/veterinary-practices-for-sale/
Output: output/psbroker_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import (get_session, polite_delay, parse_price, clean_text,
                   parse_location, state_from_code, STATE_ABBRS,
                   STATE_NAME_TO_ABBR)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("psbroker")

BASE_URL = "https://psbroker.com"
LISTINGS_URL = "{}/veterinary-practices-for-sale/".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "psbroker_raw.csv")

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]

DETAIL_RE = re.compile(r"/property/[a-z0-9-]+/?$", re.I)


def infer_practice_type(text: str) -> str:
    t = text.lower()
    if "equine" in t:
        return "Equine"
    if "mixed" in t:
        return "Mixed Animal"
    if "emergency" in t or "specialty" in t:
        return "Emergency/Specialty"
    if "large animal" in t:
        return "Large Animal"
    return "Small Animal"


def state_from_slug(slug: str) -> str:
    # trailing code like 'tx10' or 'ca36'
    m = re.search(r"([a-z]{2})\d{1,3}$", slug, re.I)
    if m and m.group(1).upper() in STATE_ABBRS:
        return m.group(1).upper()
    # full state name embedded in slug: 'bexar-county-texas-tx10'
    for name, abbr in STATE_NAME_TO_ABBR.items():
        if name.replace(" ", "-") in slug.lower():
            return abbr
    return ""


def county_from_slug(slug: str) -> str:
    m = re.match(r"^([a-z-]+?)-county", slug, re.I)
    if m:
        return m.group(1).replace("-", " ").title() + " County"
    return ""


def collect_detail_urls(session) -> List[str]:
    try:
        resp = session.get(LISTINGS_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Index fetch failed: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not DETAIL_RE.search(href):
            continue
        if not href.startswith("http"):
            href = BASE_URL + href
        href = href.split("#")[0].rstrip("/") + "/"
        if href in seen:
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
    state = state_from_slug(slug)
    city = county_from_slug(slug)

    h1 = soup.find("h1")
    title = clean_text(h1.get_text()) if h1 else ""
    if not title:
        t = soup.find("title")
        title = clean_text(t.get_text()).split("|")[0].split(" - ")[0] if t else ""
    if not title:
        # build a readable title from the county/state
        title = "Veterinary Practice — {}".format(
            (city + ", " + state).strip(", ") or slug.replace("-", " ").title())
    if re.search(r"\bsold\b|under\s+contract", title, re.I):
        return None

    full_text = soup.get_text(" ", strip=True)
    if not state:
        _, state = parse_location(title)

    annual_revenue = None
    m = re.search(r"(?:gross(?:\s+revenue)?|revenue|collections?|production)\s*(?:were|of|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m:
        v = parse_price(m.group(1))
        annual_revenue = v if (v and v >= 100_000) else None

    asking_price = None
    m = re.search(r"(?:asking\s+price|list\s+price)[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m:
        v = parse_price(m.group(1))
        asking_price = v if (v and v >= 50_000) else None

    description = ""
    for p in soup.find_all(["p", "li"]):
        t = clean_text(p.get_text(" ", strip=True))
        if len(t) > 60 and re.search(r"practice|clinic|hospital|revenue|animal|\$", t, re.I):
            description = t[:600]
            break

    exam_rooms = None
    m = re.search(r"(\d+)\s*exam\s*rooms?", full_text, re.I)
    if m:
        exam_rooms = int(m.group(1))

    return {
        "source_id": "psb-{}".format(slug[:24]),
        "title": title,
        "city": city,
        "state": state,
        "asking_price": asking_price,
        "annual_revenue": annual_revenue,
        "practice_type": infer_practice_type(title + " " + description),
        "description": description,
        "broker_name": "PS Broker",
        "listing_url": url,
        "exam_rooms": exam_rooms,
        "sqft": None,
        "listing_code": slug[:24],
    }


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    urls = collect_detail_urls(session)
    logger.info("Found %d PS Broker property URLs", len(urls))
    all_listings, seen = [], set()
    for i, url in enumerate(urls, 1):
        row = parse_detail(session, url)
        if row and row["source_id"] not in seen:
            seen.add(row["source_id"])
            all_listings.append(row)
            logger.info("  [%d/%d] %s — %s — rev $%s", i, len(urls),
                        row["listing_code"][:20], row["state"] or "?",
                        row.get("annual_revenue") or "N/A")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_listings)
    logger.info("Wrote %d listings to %s", len(all_listings), OUTPUT_FILE)
    return all_listings


if __name__ == "__main__":
    results = run()
    print("Done. {} listings saved to {}".format(len(results), OUTPUT_FILE))
