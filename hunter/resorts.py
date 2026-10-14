"""Canonical DVC resort names and which ones can book Aulani with resale points.

Resale contracts at the 14 'legacy' resorts can book any legacy resort (incl. Aulani).
Resale contracts at newer resorts (Riviera, Villas at Disneyland Hotel, Polynesian
Island Tower, Cabins at Fort Wilderness) can only book their own home resort, and
legacy resale points cannot book them. So only legacy contracts matter here.
"""
from __future__ import annotations

import re

RESORTS = {
    # key: (display name, legacy?)
    "aulani": ("Aulani", True),
    "akv": ("Animal Kingdom Villas", True),
    "blt": ("Bay Lake Tower", True),
    "bcv": ("Beach Club Villas", True),
    "bwv": ("BoardWalk Villas", True),
    "brv": ("Boulder Ridge Villas", True),
    "ccv": ("Copper Creek Villas", True),
    "gfv": ("Grand Floridian Villas", True),
    "okw": ("Old Key West", True),
    "pvb": ("Polynesian Villas & Bungalows", True),
    "ssr": ("Saratoga Springs", True),
    "vgc": ("Grand Californian Villas", True),
    "vb": ("Vero Beach", True),
    "hh": ("Hilton Head", True),
    # restricted resale (cannot book Aulani)
    "rr": ("Riviera", False),
    "vdh": ("Villas at Disneyland Hotel", False),
    "pit": ("Polynesian Island Tower", False),
    "cfw": ("Cabins at Fort Wilderness", False),
}

# Order matters: more specific patterns first.
_PATTERNS = [
    (r"island\s*tower", "pit"),
    (r"cabins", "cfw"),
    (r"disneyland\s*hotel|villas at disneyland", "vdh"),
    (r"riviera", "rr"),
    (r"aulani|ko\s*olina", "aulani"),
    (r"animal\s*kingdom|jambo|kidani|\bakv\b", "akv"),
    (r"bay\s*lake|contemporary|\bblt\b", "blt"),
    (r"beach\s*club|\bbcv\b", "bcv"),
    (r"boardwalk|board\s*walk|\bbwv\b", "bwv"),
    (r"boulder\s*ridge|\bbrv\b", "brv"),
    (r"copper\s*creek|\bccv\b", "ccv"),
    (r"grand\s*floridian|\bgfv\b", "gfv"),
    (r"old\s*key\s*west|\bokw\b", "okw"),
    (r"polynesian|\bpvb\b", "pvb"),
    (r"saratoga|\bssr\b", "ssr"),
    (r"grand\s*californian|\bvgc\b", "vgc"),
    (r"vero\s*beach", "vb"),
    (r"hilton\s*head", "hh"),
]


def normalize_resort(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for pat, key in _PATTERNS:
        if re.search(pat, t):
            return key
    return None


def display(key: str) -> str:
    return RESORTS.get(key, (key, False))[0]


def is_legacy(key: str | None) -> bool:
    return bool(key) and RESORTS.get(key, ("", False))[1]


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def month_num(text: str | None) -> int | None:
    """'Aug', 'August', 'AUG', '8' -> 8"""
    if not text:
        return None
    t = str(text).strip().lower()
    if t.isdigit() and 1 <= int(t) <= 12:
        return int(t)
    for i, m in enumerate(MONTHS, 1):
        if t.startswith(m[:3].lower()):
            return i
    return None
