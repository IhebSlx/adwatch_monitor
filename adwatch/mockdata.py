"""Deterministic sample-ad generator for MOCK mode.

Lets the dashboard be populated for ANY company name (including ones you add live
in the UI) without hand-writing fixtures. Seeded by company name so results are
stable across runs. FICTIONAL data — never treat as real ad activity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random

from .sources.base import RawAd

_ROLES = ["Monteur", "Schreiner", "Fensterbauer", "Vertriebsmitarbeiter", "Kundenberater", "Servicetechniker"]
_PRODUCTS = [
    "Fenster", "Haustüren", "Wintergärten", "Terrassendächer",
    "Schiebetüren", "Glasfaltwände", "Rollläden", "Markisen",
]


def _seeded_rng(name: str) -> random.Random:
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return random.Random(int(h[:12], 16))


def _mk(rng: random.Random, idx: int, text: str, cta: str, media: str) -> RawAd:
    has_reach = rng.random() < 0.6  # ~60% carry EU reach data -> exercises both spend methods
    start = dt.date.today() - dt.timedelta(days=rng.randint(3, 40))
    return RawAd(
        external_ad_id=f"MOCK-{idx:04d}",
        ad_text=text,
        cta=cta,
        start_date=start,
        is_active=True,
        media_type=media,
        reach=rng.randint(5000, 120000) if has_reach else None,
        country="DE",
    )


def generate_ads(name: str, country: str = "DE") -> list[RawAd]:
    # Original test case: confirmed page, zero active ads.
    low = name.lower()
    if "wild" in low and "kienle" in low:
        return []

    rng = _seeded_rng(name)
    n = rng.randint(1, 6)
    ads: list[RawAd] = []
    for i in range(n):
        roll = rng.random()
        if roll < 0.35:
            role = rng.choice(_ROLES)
            ads.append(_mk(rng, i, f"Wir suchen Verstärkung! {role} (m/w/d) gesucht. Werde Teil unseres Teams. Jetzt bewerben.", "Jetzt bewerben", "image"))
        elif roll < 0.75:
            prod = rng.choice(_PRODUCTS)
            ads.append(_mk(rng, i, f"{prod} zum Aktionspreis. Jetzt Angebot sichern und sparen. Beratung anfragen.", "Angebot sichern", "image"))
        elif roll < 0.9:
            ads.append(_mk(rng, i, "Qualität aus Deutschland seit über 30 Jahren. Lernen Sie uns und unsere Werte kennen.", "Mehr erfahren", "video"))
        else:
            ads.append(_mk(rng, i, "Besuchen Sie uns auf der Hausmesse. Live-Vorführungen und exklusive Messeangebote.", "Anmelden", "image"))
    return ads
