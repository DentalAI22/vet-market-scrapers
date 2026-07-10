"""
Simmons & Associates — veterinary practice listings scraper.

Simmons is the oldest/largest dedicated veterinary practice brokerage. Their
listings live at simmonsinc.com/practice-listings/ (WordPress + JetEngine grid),
paginated /practice-listings/page/N/. The index links to detail pages at
/practice-listings/{slug}/. Detail pages carry a title, location, and a
description body with gross-revenue / doctor-count hints.

Source: https://simmonsinc.com/practice-listings/
Output: output/simmons_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import (get_session, polite_delay, parse_price, clean_text,
                   parse_location, STATE_ABBRS, STATE_NAME_TO_ABBR)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("simmons")

BASE_URL = "https://simmonsinc.com"
LISTINGS_URL = "{}/practice-listings/".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "simmons_raw.csv")
MAX_PAGES = 6

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]

DETAIL_RE = re.compile(r"^https://simmonsinc\.com/practice-listings/[a-z0-9-]+/?$", re.I)
SKIP_SLUGS = {"feed", "page"}


def infer_practice_type(text: str) -> str:
    t = text.lower()
    if "equine" in t or "horse" in t:
        return "Equine"
    if "mixed" in t:
        return "Mixed Animal"
    if "emergency" in t or "specialty" in t:
        return "Emergency/Specialty"
    if "companion" in t or "small animal" in t:
        return "Small Animal"
    if "large animal" in t or "food animal" in t or "bovine" in t:
        return "Large Animal"
    if "exotic" in t or "avian" in t:
        return "Exotic/Avian"
    return "Small Animal"


def collect_detail_urls(session) -> List[str]:
    urls = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        url = LISTINGS_URL if page == 1 else "{}page/{}/".format(LISTINGS_URL, page)
        polite_delay(1.5, 3.0)
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200:
                logger.info("Page %d -> HTTP %d, stopping.", page, resp.status_code)
                break
        except Exception as e:
            logger.warning("Page %d failed: %s", page, e)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        page_urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0].rstrip("/") + "/"
            if not DETAIL_RE.match(href):
                continue
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if slug in SKIP_SLUGS or href in seen:
                continue
            seen.add(href)
            page_urls.append(href)
        logger.info("Page %d: %d detail links", page, len(page_urls))
        if not page_urls:
            break
        urls.extend(page_urls)
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
    h1 = soup.find("h1")
    title = clean_text(h1.get_text()) if h1 else ""
    if not title:
        t = soup.find("title")
        title = clean_text(t.get_text()).split(" - ")[0] if t else ""
    if not title:
        return None

    slug = url.rstrip("/").rsplit("/", 1)[-1]

    # Skip already-sold listings (not for sale)
    if re.search(r"\bsold\b", title, re.I):
        return None

    # Pull the main body text (skip global nav which repeats known phrases)
    NAV_NOISE = ("Appraisals Veterinary Practice Valuations", "Resources Learning Center")
    body_paras = []
    for p in soup.find_all(["p", "li"]):
        t = clean_text(p.get_text(" ", strip=True))
        if len(t) < 40:
            continue
        if any(n in t for n in NAV_NOISE):
            continue
        body_paras.append(t)
    description = ""
    for t in body_paras:
        if "$" in t or re.search(r"\b(practice|revenue|dvm|doctor|hospital|clinic)\b", t, re.I):
            description = t[:600]
            break
    full_text = soup.get_text(" ", strip=True)

    # Location: title has priority. Handle "Washington DC" before the parser
    # can mistake DC for WA. Then fall back to slug code, then body.
    city, state = "", ""
    if re.search(r"washington\s*,?\s*d\.?c\.?", title, re.I):
        state = "DC"
    else:
        city, state = parse_location(title)
    if not state:
        # bare full state name in the title (e.g. "Southern Oregon")
        for name, abbr in STATE_NAME_TO_ABBR.items():
            if re.search(r"\b" + re.escape(name) + r"\b", title, re.I):
                state = abbr
                break
    if not state:
        # slug often ends in a broker code like 'fl70c' — leading 2 letters
        m = re.search(r"([a-z]{2})\d{1,3}[a-z]?$", slug, re.I)
        if m and m.group(1).upper() in STATE_ABBRS:
            state = m.group(1).upper()
    if not state:
        m = re.search(r"location[:\s]+([A-Za-z .,'-]+)", full_text, re.I)
        if m:
            _, state = parse_location(m.group(1))

    practice_type = infer_practice_type(title + " " + description)

    annual_revenue = None
    m = re.search(r"(?:gross(?:\s+revenue)?|revenue|collections?|production)\s+(?:over|of|were|:)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)", full_text, re.I)
    if m:
        v = parse_price(m.group(1))
        if v and v >= 100_000:
            annual_revenue = v

    exam_rooms = None
    m = re.search(r"(\d+)\s*exam\s*rooms?", full_text, re.I)
    if m:
        exam_rooms = int(m.group(1))

    return {
        "source_id": "sim-{}".format(slug),
        "title": title,
        "city": city,
        "state": state,
        "asking_price": None,  # Simmons does not publish asking prices
        "annual_revenue": annual_revenue,
        "practice_type": practice_type,
        "description": description,
        "broker_name": "Simmons & Associates",
        "listing_url": url,
        "exam_rooms": exam_rooms,
        "sqft": None,
        "listing_code": slug[:24],
    }


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    logger.info("Collecting Simmons detail URLs...")
    urls = collect_detail_urls(session)
    logger.info("Found %d unique listing URLs; parsing details...", len(urls))

    all_listings = []
    seen = set()
    for i, url in enumerate(urls, 1):
        row = parse_detail(session, url)
        if row and row["source_id"] not in seen:
            seen.add(row["source_id"])
            all_listings.append(row)
            logger.info("  [%d/%d] %s — %s, %s", i, len(urls),
                        row["listing_code"], row["city"] or "?", row["state"] or "?")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_listings)
    logger.info("Wrote %d listings to %s", len(all_listings), OUTPUT_FILE)
    return all_listings


if __name__ == "__main__":
    results = run()
    print("Done. {} listings saved to {}".format(len(results), OUTPUT_FILE))
