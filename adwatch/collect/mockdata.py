"""Deterministic sample-ad generator for MOCK mode.

Lets the dashboard be populated for ANY company name (including ones you add live
in the UI) without hand-writing fixtures. Seeded by company name so results are
stable across runs. FICTIONAL data — never treat as real ad activity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random

from .base import RawAd

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


def generate_hub_items() -> list[dict]:
    """Fake partner-hub sweep results (raw Apify-shaped dicts) for MOCK mode.

    Simulates a dedicated 'Solarlux Quality Partner' page running ads whose
    landing URLs point at solarlux.com pages naming a monitored company
    (Nagelschmidt — in the default seed list), plus one page advertising an
    UNKNOWN partner, so the linker's no-match path is exercised too."""
    def item(page_id: str, page_name: str, ad_id: str, body: str, link_url: str, utm: str) -> dict:
        return {
            "ad_archive_id": ad_id,
            "page_id": page_id,
            "page_name": page_name,
            "is_active": True,
            "start_date": (dt.date.today() - dt.timedelta(days=4)).isoformat(),
            "snapshot": {
                "page_id": page_id,
                "page_name": page_name,
                "body": {"text": body},
                "cta_text": "Mehr erfahren",
                "display_format": "IMAGE",
                "link_url": f"{link_url}?utm_source=Meta&utm_medium=Image&utm_campaign={utm}",
                "page_categories": ["Product/Service"],
                "page_profile_uri": f"https://www.facebook.com/{page_id}/",
                "cards": [],
            },
        }

    return [
        item("MOCKHUB-1", "Solarlux Quality Partner Westfalen", "HUB-0001",
             "Ihr Wintergarten vom zertifizierten Partner. Jetzt Beratungstermin sichern.",
             "https://solarlux.com/de-de/landing/wintergarten-nagelschmidt/",
             "DE%20Nagelschmidt%20Online%20Kampagnen"),
        item("MOCKHUB-1", "Solarlux Quality Partner Westfalen", "HUB-0002",
             "Glas-Faltwände für Ihr Zuhause — Aktionswochen beim Quality Partner.",
             "https://solarlux.com/de-de/landing/glasfaltwand-nagelschmidt/",
             "DE%20Nagelschmidt%20GFW"),
        item("MOCKHUB-2", "Solarlux Premium Partner Bayern", "HUB-0003",
             "Terrassendächer nach Maß — Ihr Premium Partner berät Sie gern.",
             "https://solarlux.com/de-de/landing/terrassendach-sonnenbau-muenchen/",
             "DE%20Sonnenbau%20TD"),
    ]


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
