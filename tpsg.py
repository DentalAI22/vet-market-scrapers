"""
Total Practice Solutions Group — veterinary practice listings scraper.

Index at totalpracticesolutionsgroup.com/practices-for-sale/ renders detail
links at /practice/{slug}/ with asking prices right in the index HTML
(e.g. $1,191,218). Detail pages carry fuller data.

Source: https://www.totalpracticesolutionsgroup.com/practices-for-sale/
Output: output/tpsg_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import get_session, polite_delay, parse_price, clean_text, parse_location

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("tpsg")

BASE_URL = "https://www.totalpracticesolutionsgroup.com"
LISTINGS_URL = "{}/practices-for-sale/".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "tpsg_raw.csv")

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]

DETAIL_RE = re.compile(r"/practice/[a-z0-9-]+/?$", re.I)


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
    if "large animal" in t:
        return "Large Animal"
    return "Small Animal"


def collect_cards(session):
    """Return list of (url, index_price) tuples from the index page."""
    try:
        resp = session.get(LISTINGS_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Index fetch failed: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    out = []
    seen = set()
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
        # try to find a price near this card
        price = None
        card = a
        for _ in range(4):
            card = card.parent
            if card is None:
                break
            m = re.search(r"\$[\d,]{4,}", card.get_text(" ", strip=True))
            if m:
                price = parse_price(m.group(0))
                break
        out.append((href, price))
    return out


def parse_detail(session, url: str, index_price: Optional[int]) -> Optional[Dict]:
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
    h1 = soup.find("h1")
    title = clean_text(h1.get_text()) if h1 else ""
    if not title:
        t = soup.find("title")
        title = clean_text(t.get_text()).split("|")[0].split(" - ")[0] if t else ""
    if not title:
        title = slug.replace("-", " ").title()
    if re.search(r"\bsold\b|under\s+contract", title, re.I):
        return None

    full_text = soup.get_text(" ", strip=True)
    city, state = parse_location(title)

    asking_price = index_price
    if not asking_price:
        m = re.search(r"(?:asking(?:\s+price)?|price|list(?:ed)?(?:\s+at)?)[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
        if m:
            v = parse_price(m.group(1))
            asking_price = v if (v and v >= 50_000) else None

    annual_revenue = None
    m = re.search(r"(?:gross(?:\s+revenue)?|revenue|collections?|production)\s*(?:were|of|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m:
        v = parse_price(m.group(1))
        annual_revenue = v if (v and v >= 100_000) else None

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
        "source_id": "tpsg-{}".format(slug[:24]),
        "title": title,
        "city": city,
        "state": state,
        "asking_price": asking_price,
        "annual_revenue": annual_revenue,
        "practice_type": infer_practice_type(title + " " + description),
        "description": description,
        "broker_name": "Total Practice Solutions Group",
        "listing_url": url,
        "exam_rooms": exam_rooms,
        "sqft": None,
        "listing_code": slug[:24],
    }


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    cards = collect_cards(session)
    logger.info("Found %d TPSG practice URLs", len(cards))
    all_listings = []
    seen = set()
    for i, (url, price) in enumerate(cards, 1):
        row = parse_detail(session, url, price)
        if row and row["source_id"] not in seen:
            seen.add(row["source_id"])
            all_listings.append(row)
            logger.info("  [%d/%d] %s — %s — $%s", i, len(cards),
                        row["listing_code"][:20], row["state"] or "?",
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
