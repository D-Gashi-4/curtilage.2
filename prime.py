"""
Prime postcode layer.

PRIME_SEED is a hand-curated starting list so the tracker runs today.
Replace it with derive_from_ppd() output as soon as you have the Land
Registry file — that version is evidence-based and self-updating.
"""

import collections
import csv
import json
import re
import statistics

# --------------------------------------------------------------------------
# Seed list. Postcode districts (outward code), not sectors.
# Two tiers so the digest can distinguish super-prime from prime.
# --------------------------------------------------------------------------

SUPER_PRIME = {
    # Prime central London
    "W1J", "W1K", "W1S", "W1G", "W1H", "W1U",
    "SW1A", "SW1X", "SW1W", "SW1P", "SW1H", "SW1V",
    "SW3", "SW7", "SW10", "W8", "W11", "W2",
    "NW8", "NW3", "NW1",
    # Surrey / Berks golden triangle
    "KT11", "KT13", "KT10", "KT22", "GU25", "SL5",
    # Bucks
    "HP9", "SL9",
}

PRIME = {
    # Wider prime London
    "SW4", "SW5", "SW6", "SW11", "SW13", "SW14", "SW15", "SW18", "SW19", "SW20",
    "W4", "W6", "W9", "W10", "W14", "NW5", "NW6", "NW11",
    "N1", "N2", "N6", "N10", "N20",
    "SE1", "SE3", "SE21", "SE22", "SE24",
    "TW1", "TW9", "TW10", "TW11", "TW12",
    "EC1V", "E1W", "E14",
    # Surrey
    "KT1", "KT2", "KT7", "KT12", "KT20", "KT21", "KT23", "KT24",
    "GU1", "GU5", "GU8", "GU22", "GU23", "RH8",
    # Berkshire / Bucks
    "SL4", "SL6", "SL0", "HP7", "HP8", "RG9", "RG10",
    # Hertfordshire
    "WD3", "WD7", "WD17", "WD23", "AL5", "AL9", "EN6", "EN2", "HP1", "SG14",
    # Essex
    "CM13", "CM14", "CM15", "CM16", "CM3", "CM4",
    "IG9", "IG10", "RM14",
    # Kent
    "TN13", "TN14", "TN15", "BR6", "BR7", "DA3",
}


def district(postcode):
    """'CM15 8QA' -> 'CM15'. Returns None if unparseable."""
    if not postcode:
        return None
    m = re.match(r"^\s*([A-Z]{1,2}\d{1,2}[A-Z]?)\s*\d[A-Z]{2}\s*$",
                 postcode.strip().upper())
    return m.group(1) if m else None


def tier(postcode, super_prime=None, prime=None):
    """Return 'super_prime', 'prime' or None."""
    super_prime = super_prime if super_prime is not None else SUPER_PRIME
    prime = prime if prime is not None else PRIME
    d = district(postcode)
    if not d:
        return None
    if d in super_prime:
        return "super_prime"
    if d in prime:
        return "prime"
    return None


def load(path="prime_postcodes.json"):
    """Load a derived list if present, else fall back to the seed."""
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data["super_prime"]), set(data["prime"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return SUPER_PRIME, PRIME


# --------------------------------------------------------------------------
# Derivation from Land Registry Price Paid Data
# --------------------------------------------------------------------------

def derive_from_ppd(ppd_csv, out="prime_postcodes.json",
                    min_sales=30, super_pct=95, prime_pct=85,
                    restrict_to=None):
    """
    Build the prime lists from Land Registry Price Paid Data.

    Download the last two years from:
      https://www.gov.uk/guidance/about-the-price-paid-data
    The file has no header. Columns used: 1 = price, 3 = postcode.
    Detached/semi/terraced only; new-build and non-standard transfers dropped,
    since those distort the median.

    min_sales   districts with fewer completed sales are ignored as noise
    super_pct   percentile of district medians for the super-prime cut
    prime_pct   percentile for the prime cut
    restrict_to optional set of area prefixes, e.g. {"SW","W","KT"}
    """
    by_district = collections.defaultdict(list)

    with open(ppd_csv, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 16:
                continue
            try:
                price = int(row[1])
            except ValueError:
                continue
            d = district(row[3])
            if not d:
                continue
            if row[4] not in ("D", "S", "T"):   # exclude flats and 'Other'
                continue
            if row[5] == "Y":                    # exclude new build
                continue
            if row[14] != "A":                   # standard price paid only
                continue
            if restrict_to:
                area = re.match(r"^[A-Z]{1,2}", d).group(0)
                if area not in restrict_to:
                    continue
            by_district[d].append(price)

    medians = {d: statistics.median(v)
               for d, v in by_district.items() if len(v) >= min_sales}
    if not medians:
        raise SystemExit("No districts met min_sales. Check the input file.")

    values = sorted(medians.values())

    def pct(p):
        idx = min(int(len(values) * p / 100), len(values) - 1)
        return values[idx]

    super_cut, prime_cut = pct(super_pct), pct(prime_pct)

    super_set = sorted(d for d, m in medians.items() if m >= super_cut)
    prime_set = sorted(d for d, m in medians.items()
                       if prime_cut <= m < super_cut)

    with open(out, "w") as f:
        json.dump({
            "super_prime": super_set,
            "prime": prime_set,
            "super_prime_cut": super_cut,
            "prime_cut": prime_cut,
            "districts_scored": len(medians),
        }, f, indent=2)

    print(f"{len(medians)} districts scored")
    print(f"super-prime >= £{super_cut:,} : {len(super_set)} districts")
    print(f"prime       >= £{prime_cut:,} : {len(prime_set)} districts")
    print(f"written to {out}")
    return super_set, prime_set


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python prime.py <price_paid_data.csv>")
        sys.exit(1)
    derive_from_ppd(sys.argv[1])
