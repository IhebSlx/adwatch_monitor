"""Aggregate a company's classified ads for one run into metric values."""
from __future__ import annotations

from collections import Counter

from .classify import CATEGORIES
from .spend import estimate_spend


def aggregate(classified_ads: list[dict]) -> dict:
    """`classified_ads` items: {raw: RawAd, category, product}."""
    by_cat = Counter({c: 0 for c in CATEGORIES})
    products: list[str] = []
    raws = []

    for item in classified_ads:
        by_cat[item["category"]] += 1
        raws.append(item["raw"])
        if item.get("product"):
            for p in str(item["product"]).split(","):
                p = p.strip()
                if p and p not in products:
                    products.append(p)

    est = estimate_spend(raws)
    real_spend = sum((getattr(r, "real_spend", None) or 0) for r in raws) or None

    return {
        "total_active_ads": len(classified_ads),
        "ads_by_category": dict(by_cat),
        "products": products,
        "estimated_spend_low": est.low,
        "estimated_spend_high": est.high,
        "spend_method": est.method,
        "spend_model_version": est.model_version,
        "real_spend_regulated": real_spend,
    }
