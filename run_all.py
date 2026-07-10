#!/usr/bin/env python3
"""
Master veterinary scraper runner — mirrors the dental TDPM run_all.py.

Usage:
    python run_all.py               # Run all scrapers + normalize
    python run_all.py --only omnivet
    python run_all.py --normalize   # Re-normalize existing CSVs (no scraping)

Sources (all public, no-login, polite-fetch — same discipline as dental):
    omnivet    Omni Practice Group (Veterinary)   ~48
    simmons    Simmons & Associates               ~10
    vetsales   Vet Sales Consulting               ~9  (TX-focused)
    tpsg       Total Practice Solutions Group      ~16
    psbroker   PS Broker                          ~32
    psadvisors Practice Sales Advisors            (Wix; per-listing)

BLOCKED (never scraped — same blocklist as dental): BizBuySell, BizQuest,
LoopNet, DealStream, BusinessBroker.net, PracticeOrbit, Provide/TUSK.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_all")

# (display_name, module_name)
SCRAPERS = [
    ("Omni Practice Group (Veterinary)", "omnivet"),
    ("Simmons & Associates", "simmons"),
    ("Vet Sales Consulting", "vetsales"),
    ("Total Practice Solutions Group", "tpsg"),
    ("PS Broker", "psbroker"),
    ("Practice Sales Advisors", "psadvisors"),
]


def run_scraper(name, module_name):
    logger.info("=" * 60)
    logger.info("STARTING: %s", name)
    logger.info("=" * 60)
    try:
        mod = importlib.import_module(module_name)
        results = mod.run()
        count = len(results) if results else 0
        logger.info("%s: %d listings", name, count)
        return count
    except Exception as e:
        logger.error("%s failed: %s", name, e)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Run veterinary listing scrapers")
    parser.add_argument("--only", type=str, help="Run one scraper by module name")
    parser.add_argument("--normalize", action="store_true", help="Only normalize existing CSVs")
    args = parser.parse_args()

    start = time.time()
    results = {}

    if not args.normalize:
        if args.only:
            matched = False
            for name, module_name in SCRAPERS:
                if module_name == args.only:
                    results[name] = run_scraper(name, module_name)
                    matched = True
                    break
            if not matched:
                logger.error("Unknown scraper: %s", args.only)
                logger.info("Available: %s", ", ".join(m for _, m in SCRAPERS))
                return 1
        else:
            for name, module_name in SCRAPERS:
                results[name] = run_scraper(name, module_name)

    logger.info("=" * 60)
    logger.info("STARTING: Normalizer")
    logger.info("=" * 60)
    try:
        import normalizer
        merged = normalizer.run()
        results["normalized"] = len(merged) if merged else 0
    except Exception as e:
        logger.error("Normalizer failed: %s", e)
        results["normalized"] = 0

    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info("VET SCRAPER RUN COMPLETE — %.1fs", elapsed)
    logger.info("=" * 60)
    for source, count in results.items():
        logger.info("  %-34s %d", source, count)

    total = results.get("normalized", 0)
    print("\nDone. {} total vet listings in listings.json ({:.1f}s)".format(total, elapsed))
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
