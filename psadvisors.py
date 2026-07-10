"""
Practice Sales Advisors — veterinary practice listings scraper.

Wix-hosted site. The /listings index page renders detail links at
/listings/{state}{num} (e.g. fl0005, sc0008). Detail pages render real data
(title, gross revenue, asking figures) in server HTML despite the Wix stack.
Heavier per-page fetch (Wix pages are large), so we rate-limit generously.

Source: https://www.practicesalesadvisors.com/listings
Output: output/psadvisors_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import (get_session, polite_delay, parse_price, clean_text,
                   parse_location, state_from_code)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("psadvisors")

BASE_URL = "https://www.practicesalesadvisors.com"
LISTINGS_URL = "{}/listings".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "psadvisors_raw.csv")

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]

# detail slugs look like /listings/fl0005  (2-letter state + digits)
DETAIL_RE = re.compile(r"/listings/([a-z]{2}\d{3,5})$", re.I)


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


def collect_detail_urls(session) -> List[str]:
    try:
        resp = session.get(LISTINGS_URL, timeout=35)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Index fetch failed: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        m = DETAIL_RE.search(href)
        if not m:
            continue
        if not href.startswith("http"):
            href = BASE_URL + ("" if href.startswith("/") else "/") + href
        code = m.group(1).lower()
        if code in seen:
            continue
        seen.add(code)
        urls.append(href)
    return urls


def parse_detail(session, url: str) -> Optional[Dict]:
    polite_delay(2.0, 3.5)
    try:
        resp = session.get(url, timeout=35)
        if resp.status_code != 200:
            return None
    except Exception as e:
        logger.warning("Detail failed %s: %s", url, e)
        return None
    soup = BeautifulSoup(resp.text, "lxml")

    m = DETAIL_RE.search(url)
    code = m.group(1).upper() if m else url.rstrip("/").rsplit("/", 1)[-1].upper()
    state = state_from_code(code)

    t = soup.find("title")
    title = clean_text(t.get_text()).split("|")[0].split(" - ")[0] if t else ""
    h1 = soup.find("h1")
    if h1 and clean_text(h1.get_text()):
        title = clean_text(h1.get_text())
    if not title:
        title = "Veterinary Practice {}".format(code)
    if re.search(r"\bsold\b|under\s+contract", title, re.I):
        return None

    full_text = soup.get_text(" ", strip=True)
    city = ""
    if not state:
        city, state = parse_location(title)

    annual_revenue = None
    m2 = re.search(r"(?:gross(?:\s+revenue)?|revenue|collections?|production)\s*(?:were|of|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m2:
        v = parse_price(m2.group(1))
        annual_revenue = v if (v and v >= 100_000) else None

    asking_price = None
    m2 = re.search(r"(?:asking(?:\s+price)?|list\s+price|offered\s+at)[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m2:
        v = parse_price(m2.group(1))
        asking_price = v if (v and v >= 50_000) else None

    description = ""
    for p in soup.find_all(["p", "li"]):
        tx = clean_text(p.get_text(" ", strip=True))
        if len(tx) > 60 and re.search(r"practice|clinic|hospital|revenue|animal|\$", tx, re.I):
            description = tx[:600]
            break

    exam_rooms = None
    m2 = re.search(r"(\d+)\s*exam\s*rooms?", full_text, re.I)
    if m2:
        exam_rooms = int(m2.group(1))

    return {
        "source_id": "psa-{}".format(code.lower()),
        "title": title,
        "city": city,
        "state": state,
        "asking_price": asking_price,
        "annual_revenue": annual_revenue,
        "practice_type": infer_practice_type(title + " " + description),
        "description": description,
        "broker_name": "Practice Sales Advisors",
        "listing_url": url,
        "exam_rooms": exam_rooms,
        "sqft": None,
        "listing_code": code,
    }


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    urls = collect_detail_urls(session)
    logger.info("Found %d Practice Sales Advisors listing URLs", len(urls))
    all_listings, seen = [], set()
    for i, url in enumerate(urls, 1):
        row = parse_detail(session, url)
        if row and row["source_id"] not in seen:
            seen.add(row["source_id"])
            all_listings.append(row)
            logger.info("  [%d/%d] %s — %s — rev $%s", i, len(urls),
                        row["listing_code"], row["state"] or "?",
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
