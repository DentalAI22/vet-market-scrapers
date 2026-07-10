#!/usr/bin/env python3
"""
Veterinary listings normalizer.

Ported from the dental TDPM normalizer pattern. Reads every
output/<source>_raw.csv, maps each row to the site's Listing schema
(mirrors ~/market-network/veterinary/src/lib/types.ts), assigns a persistent
TVM-XXXXX siteId from site_id_registry.json (never renumbers, never collides
with dental's TDPM-), dedupes within + across sources, and writes:
  - listings.json               (canonical, this dir)
  - ../veterinary/public/data/listings.json         (site consumer)
  - ../veterinarypractice/public/data/listings.json (sibling site consumer)

Schema (per Listing interface):
  id, source, source_url, type, state, city, asking_price, annual_revenue,
  annual_collections, key_metric_value, broker_name, broker_company,
  broker_url, description, business_name_redacted, year_established,
  reason_for_sale, transition_type, real_estate_included, staff_info,
  technology, lease_terms, scraped_date, is_new  (+ siteId, broker_ref)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("normalizer")

HERE = os.path.dirname(__file__)
OUTPUT_DIR = os.path.join(HERE, "output")
BROKER_CODES_JSON = os.path.join(HERE, "broker_codes.json")
SITE_ID_REGISTRY = os.path.join(HERE, "site_id_registry.json")
LISTINGS_JSON = os.path.join(HERE, "listings.json")

# Site consumers — the vet flagship + its SEO siblings all read the same file.
SITE_DATA_TARGETS = [
    os.path.join(HERE, "..", "veterinary", "public", "data", "listings.json"),
    os.path.join(HERE, "..", "veterinarypractice", "public", "data", "listings.json"),
]

SITE_PREFIX = "TVM"
BASE_SITE_ID = 1  # TVM-00001 is the first

_codes = None


def load_codes() -> Dict:
    global _codes
    if _codes is None:
        with open(BROKER_CODES_JSON) as f:
            _codes = json.load(f)
    return _codes


def to_int(v) -> Optional[int]:
    if v in (None, "", "None"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


# --- siteId registry (persistent, stable, never renumber) -------------------

def load_registry():
    if os.path.exists(SITE_ID_REGISTRY):
        with open(SITE_ID_REGISTRY) as f:
            d = json.load(f)
        return d.get("next_id", BASE_SITE_ID), d.get("map", {})
    return BASE_SITE_ID, {}


def save_registry(next_id: int, id_map: Dict) -> None:
    with open(SITE_ID_REGISTRY, "w") as f:
        json.dump({"prefix": SITE_PREFIX, "base": BASE_SITE_ID,
                   "next_id": next_id, "map": id_map}, f, indent=2)


def assign_site_ids(listings: List[Dict]) -> None:
    """Assign stable TVM-XXXXX siteIds keyed by source_id (registry-backed)."""
    next_id, id_map = load_registry()
    used = set(id_map.values())
    for l in listings:
        key = l["source_id"]
        if key in id_map:
            num = id_map[key]
        else:
            while next_id in used:
                next_id += 1
            num = next_id
            id_map[key] = num
            used.add(num)
            next_id += 1
        l["siteId"] = "{}-{:05d}".format(SITE_PREFIX, num)
    save_registry(next_id, id_map)


# --- normalization ----------------------------------------------------------

def broker_ref(source_key: str, listing_code: str) -> str:
    codes = load_codes()
    meta = codes.get("sources", {}).get(source_key, {})
    prefix = meta.get("ref_prefix", source_key.upper())
    code = (listing_code or "").strip()
    # Some sources (Simmons, TPSG) only expose a long descriptive slug as their
    # identifier. If the code isn't a short broker code (letters+digits), keep
    # the ref clean by showing just the broker prefix.
    if code and not re.fullmatch(r"[A-Za-z]{1,4}\d{1,5}[A-Za-z]?", code):
        return prefix
    return "{} #{}".format(prefix, code) if code else prefix


def redacted_name(practice_type: str) -> str:
    """Never store real practice names. Emit a generic descriptor."""
    pt = (practice_type or "").strip()
    if pt and pt.lower() not in ("small animal",):
        return "{} Veterinary Practice".format(pt)
    return "Veterinary Practice"


def normalize_row(source_key: str, row: Dict, today: str, recent_cutoff: str) -> Optional[Dict]:
    codes = load_codes()
    meta = codes.get("sources", {}).get(source_key, {})

    title = (row.get("title") or "").strip()
    state = (row.get("state") or "").strip().upper()
    if not title:
        return None

    scraped = row.get("scraped_date") or today
    is_new = scraped >= recent_cutoff

    exam_rooms = to_int(row.get("exam_rooms"))

    return {
        "source_id": row.get("source_id") or "",  # internal key (dropped before write)
        "id": row.get("source_id") or "",
        "siteId": "",  # filled by assign_site_ids
        "source": source_key,
        "source_url": row.get("listing_url") or meta.get("broker_url", ""),
        "type": row.get("practice_type") or "Small Animal",
        "state": state,
        "city": (row.get("city") or "").strip(),
        "asking_price": to_int(row.get("asking_price")),
        "annual_revenue": to_int(row.get("annual_revenue")),
        "annual_collections": None,
        "key_metric_value": exam_rooms,  # site keyMetric field = exam_rooms
        "exam_rooms": exam_rooms,
        "broker_name": row.get("broker_name") or meta.get("broker_name", ""),
        "broker_company": row.get("broker_name") or meta.get("broker_name", ""),
        "broker_url": meta.get("broker_url", ""),
        "broker_ref": broker_ref(source_key, row.get("listing_code", "")),
        "description": (row.get("description") or "").strip(),
        "business_name_redacted": redacted_name(row.get("practice_type", "")),
        "scraped_date": scraped,
        "is_new": is_new,
    }


def dedupe(listings: List[Dict]) -> List[Dict]:
    """Cross-source dedupe. Same source_id, or same (state, exam_rooms, revenue)
    signature with a very similar title, collapses to one (keep the richer)."""
    by_key: Dict[str, Dict] = {}
    order: List[str] = []
    for l in listings:
        sig_bits = [l.get("state", ""), str(l.get("annual_revenue") or ""),
                    str(l.get("exam_rooms") or "")]
        title_norm = re.sub(r"[^a-z0-9]", "", (l.get("title") or "").lower())[:24]
        strong = (l.get("annual_revenue") or l.get("exam_rooms"))
        key = l["source_id"]
        if strong and title_norm:
            key = "|".join(sig_bits + [title_norm])
        if key in by_key:
            # keep the one with more populated fields
            def score(x):
                return sum(1 for k in ("asking_price", "annual_revenue",
                                       "exam_rooms", "city", "description")
                           if x.get(k))
            if score(l) > score(by_key[key]):
                by_key[key] = l
        else:
            by_key[key] = l
            order.append(key)
    return [by_key[k] for k in order]


def run() -> List[Dict]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    recent_cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    codes = load_codes()
    known = set(codes.get("sources", {}).keys())
    # map raw filename stem -> source_key
    stem_to_source = {
        "omnivet": "omnivet", "simmons": "simmons", "vetsales": "vetsales",
        "tpsg": "tpsg", "psbroker": "psbroker", "psadvisors": "psadvisors",
    }

    all_norm: List[Dict] = []
    if os.path.isdir(OUTPUT_DIR):
        for fname in sorted(os.listdir(OUTPUT_DIR)):
            if not fname.endswith("_raw.csv"):
                continue
            stem = fname[:-len("_raw.csv")]
            source_key = stem_to_source.get(stem, stem)
            if source_key not in known:
                logger.warning("Skipping unknown source file: %s", fname)
                continue
            path = os.path.join(OUTPUT_DIR, fname)
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            n = 0
            for r in rows:
                nr = normalize_row(source_key, r, today, recent_cutoff)
                if nr:
                    all_norm.append(nr)
                    n += 1
            logger.info("%-12s %d rows -> %d normalized", source_key, len(rows), n)

    before = len(all_norm)
    all_norm = dedupe(all_norm)
    logger.info("Deduped %d -> %d", before, len(all_norm))

    assign_site_ids(all_norm)

    # sort: new first, then by state
    all_norm.sort(key=lambda x: (not x.get("is_new"), x.get("state", "")))

    # strip the internal source_id before writing the public file
    public = []
    for l in all_norm:
        d = dict(l)
        d.pop("source_id", None)
        public.append(d)

    with open(LISTINGS_JSON, "w") as f:
        json.dump(public, f, indent=2)
    # Sibling-site convenience writes for LOCAL dev only. In CI (GitHub Actions)
    # the ../veterinary and ../veterinarypractice checkouts don't exist, and the
    # sites pull listings.json from this repo's public raw URL at build time
    # (see scrape-veterinary.yml). Only write a sibling if its site dir already
    # exists so CI never creates stray ../veterinary* junk dirs.
    for target in SITE_DATA_TARGETS:
        site_root = os.path.dirname(os.path.dirname(os.path.dirname(target)))
        if not os.path.isdir(site_root):
            logger.info("Skipping sibling write (not present): %s",
                        os.path.relpath(target, HERE))
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            json.dump(public, f, indent=2)
        logger.info("Wrote %d listings -> %s", len(public), os.path.relpath(target, HERE))

    logger.info("Wrote %d listings -> listings.json", len(public))
    return public


if __name__ == "__main__":
    out = run()
    print("Done. {} listings normalized.".format(len(out)))
