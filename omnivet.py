"""
Omni Practice Group — Veterinary listings scraper.

Ported from the dental TDPM omni.py. Omni runs a separate veterinary brand at
omnipg-vet.com with the SAME WordPress "listingBox" markup as its dental side:
each card has .listingContent > .listingTitle (h2 + detail <a>),
.listingInternalID (e.g. 'AZV311' — {state}V{number}, V = veterinary), and
a description block. Detail pages carry asking price + collections + ops.

Source: https://omnipg-vet.com/practices-for-sale/
Output: output/omnivet_raw.csv
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from utils import get_session, polite_delay, parse_price, clean_text, state_from_code

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("omnivet")

BASE_URL = "https://omnipg-vet.com"
LISTINGS_URL = "{}/practices-for-sale/".format(BASE_URL)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "omnivet_raw.csv")

FIELDNAMES = [
    "source_id", "title", "city", "state", "asking_price", "annual_revenue",
    "practice_type", "description", "broker_name", "listing_url",
    "exam_rooms", "sqft", "listing_code",
]

# Non-veterinary keywords — Omni's vet brand is vet-only, but guard anyway.
NON_VET = ["dental", "dentist", "optometr", "pharmacy", "chiropract"]


def extract_money(text: str, pattern: str) -> Optional[int]:
    """Run a labeled-money pattern and return a sane dollar int, or None.

    Only accepts values >= $10,000 to reject partial/garbage parses (vet
    brokers write shorthand like '$1mil', '$6.7mil EBITDA' — a wrong number
    is worse than none in an acquirer's diligence sweep)."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    val = parse_price(m.group(1))
    if val is None or val < 10_000:
        return None
    return val


def is_vet(text: str) -> bool:
    t = text.lower()
    if any(kw in t for kw in NON_VET):
        return False
    return True


def infer_practice_type(text: str) -> str:
    t = text.lower()
    if "equine" in t or "horse" in t:
        return "Equine"
    if "mixed" in t or "mixed animal" in t:
        return "Mixed Animal"
    if "emergency" in t or "specialty" in t or "24-hour" in t or "24 hour" in t:
        return "Emergency/Specialty"
    if "small animal" in t or "companion" in t or "companion animal" in t:
        return "Small Animal"
    if "exotic" in t or "avian" in t:
        return "Exotic/Avian"
    if "large animal" in t or "bovine" in t or "food animal" in t:
        return "Large Animal"
    return "Small Animal"


TITLE_PREFIXES = re.compile(
    r"^(?:Reduced\s+Price:\s*|New\s+Listing:?\s*|Must-See,?\s*|"
    r"Well-Established\s+|Stunning\s+|Beautiful\s+|Thriving\s+|Profitable\s+|"
    r"Turn-Key\s+|Recently\s+Updated\s+|High-Visibility\s+|Amazing\s+|"
    r"Prime\s+|Ideal\s+)",
    re.I,
)


def parse_location_from_title(title: str) -> tuple:
    cleaned = TITLE_PREFIXES.sub("", title).strip()
    m = re.search(r"^(.+?),\s*([A-Z]{2})\b", cleaned)
    if m:
        return m.group(1).strip().title(), m.group(2).upper()
    m = re.search(r"([A-Za-z .'-]+),\s*([A-Z]{2})\b", cleaned)
    if m:
        return m.group(1).strip().title(), m.group(2).upper()
    return "", ""


def parse_box(box) -> Optional[Dict]:
    content = box.find(class_="listingContent")
    if not content:
        return None

    title_el = content.find(class_="listingTitle")
    title = ""
    listing_url = ""
    if title_el:
        h2 = title_el.find("h2")
        title = clean_text(h2.get_text()) if h2 else clean_text(title_el.get_text())
        a = title_el.find("a", href=True)
        if a:
            listing_url = a["href"]
            if not listing_url.startswith("http"):
                listing_url = BASE_URL + listing_url
    if not title:
        return None
    if not is_vet(title):
        return None
    # skip associate-position ads
    if re.search(r"associate\s+(?:veterinarian|position|dvm)|associateship", title, re.I):
        return None

    id_el = content.find(class_="listingInternalID")
    internal_id = clean_text(id_el.get_text()) if id_el else ""
    # strip label like "Listing ID: AZV311"
    m = re.search(r"([A-Za-z]{2,4}\d{2,5})", internal_id)
    internal_id = m.group(1) if m else internal_id

    desc_el = (content.find(class_="advertising-desription")
               or content.find(class_="listingExcerpt")
               or content.find("p"))
    description = clean_text(desc_el.get_text())[:600] if desc_el else ""

    city, state = parse_location_from_title(title)
    if not state and internal_id:
        state = state_from_code(internal_id)

    practice_type = infer_practice_type(title + " " + description)

    asking_price = None
    annual_revenue = None
    exam_rooms = None
    sqft = None
    if description:
        annual_revenue = extract_money(
            description,
            r"(?:collections?|revenue|gross(?:ing)?|production|sales)\s*(?:were|of|total(?:ed)?|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)",
        )
        asking_price = extract_money(
            description,
            r"(?:asking|listed\s+at|price|offered\s+at)[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)",
        )
        em = re.search(r"(\d+)\s*(?:[-\s])?(?:exam\s*rooms?|exam\s*suites?|treatment\s*rooms?)", description, re.I)
        if em:
            exam_rooms = int(em.group(1))
        sm = re.search(r"([\d,]+)\s*(?:sq\.?\s*ft|sf|square\s*f)", description, re.I)
        if sm:
            sqft = int(sm.group(1).replace(",", ""))

    source_id = "omnv-{}".format(internal_id) if internal_id else ""
    if not source_id:
        return None

    clean_title = re.sub(r"^(?:Reduced\s+Price:|New\s+Listing:?)\s*", "", title, flags=re.I)

    return {
        "source_id": source_id,
        "title": clean_title,
        "city": city,
        "state": state,
        "asking_price": asking_price,
        "annual_revenue": annual_revenue,
        "practice_type": practice_type,
        "description": description,
        "broker_name": "Omni Practice Group (Veterinary)",
        "listing_url": listing_url,
        "exam_rooms": exam_rooms,
        "sqft": sqft,
        "listing_code": internal_id,
    }


def scrape_detail(session, listing: Dict) -> Dict:
    url = listing.get("listing_url")
    if not url:
        return listing
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            return listing
    except Exception as e:
        logger.warning("Detail failed %s: %s", listing["source_id"], e)
        return listing

    soup = BeautifulSoup(resp.text, "lxml")

    page_text = soup.get_text(" ", strip=True)
    if not listing.get("asking_price"):
        listing["asking_price"] = extract_money(
            page_text,
            r"asking\s+price[:\s]*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)",
        )
    if not listing.get("annual_revenue"):
        listing["annual_revenue"] = extract_money(
            page_text,
            r"(?:gross(?:ing)?|collections?|revenue|production|sales)\s*(?:were|of|total(?:ed)?|:|at)?\s*(\$[\d.,]+\s*(?:mil(?:lion)?|k)?)",
        )
    if not listing.get("exam_rooms"):
        m = re.search(r"(\d+)\s*(?:exam\s*rooms?|exam\s*suites?|treatment\s*rooms?)", page_text, re.I)
        if m:
            listing["exam_rooms"] = int(m.group(1))
    if not listing.get("sqft"):
        m = re.search(r"([\d,]+)\s*(?:sq\.?\s*ft|square\s*f)", page_text, re.I)
        if m:
            listing["sqft"] = int(m.group(1).replace(",", ""))
    if len(listing.get("description", "")) < 120:
        for p in soup.find_all("p"):
            t = clean_text(p.get_text())
            if len(t) > 120 and ("practice" in t.lower() or "veterinar" in t.lower() or "$" in t):
                listing["description"] = t[:600]
                break
    return listing


def run() -> List[Dict]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()
    logger.info("Fetching Omni-vet listings: %s", LISTINGS_URL)
    try:
        resp = session.get(LISTINGS_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch Omni-vet: %s", e)
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    boxes = soup.find_all("div", class_="listingBox")
    logger.info("Found %d listingBox elements", len(boxes))

    all_listings = []
    seen = set()
    for box in boxes:
        listing = parse_box(box)
        if listing and listing["source_id"] not in seen:
            seen.add(listing["source_id"])
            all_listings.append(listing)

    logger.info("Parsed %d vet listings; scraping detail pages...", len(all_listings))
    for i, listing in enumerate(all_listings, 1):
        polite_delay(1.0, 2.0)
        scrape_detail(session, listing)
        logger.info("  [%d/%d] %s — %s, %s — $%s",
                    i, len(all_listings), listing["source_id"],
                    listing["city"] or "?", listing["state"] or "?",
                    listing.get("asking_price") or "N/A")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_listings)
    logger.info("Wrote %d listings to %s", len(all_listings), OUTPUT_FILE)
    return all_listings


if __name__ == "__main__":
    results = run()
    print("Done. {} listings saved to {}".format(len(results), OUTPUT_FILE))
