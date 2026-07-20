"""PART 3b — 0-100 activity score per company per week.

All knobs live in config/score_config.yaml (see the formula there). The score
is computed at collection time and PERSISTED on the weekly metric row, so
historical scores reflect the assumptions of their week."""
from __future__ import annotations

import yaml

from .. import config

_cfg_cache: dict | None = None


def load_config() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        path = config.CONFIG_DIR / "score_config.yaml"
        with open(path, encoding="utf-8") as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def company_score(total_ads: int, prev_total: int | None,
                  new_ads: int, categories_active: int) -> float:
    """See score_config.yaml for the formula. Returns 0.0-100.0."""
    # No ads = no activity = 0. Without this guard the neutral first-week
    # momentum term (0.5) alone gives a company that runs ZERO ads a nonzero
    # activity score (~12.5), which reads as mild activity where there is none.
    if total_ads <= 0:
        return 0.0
    cfg = load_config()
    w = cfg["weights"]
    norm = max(int(cfg.get("volume_norm_ads", 15)), 1)

    volume = min(total_ads / norm, 1.0)

    if prev_total is None:
        momentum = 0.5                      # first week: neutral
    else:
        delta_ratio = (total_ads - prev_total) / max(prev_total, 1)
        momentum = 0.5 + max(-1.0, min(1.0, delta_ratio)) / 2

    freshness = (new_ads / total_ads) if total_ads else 0.0
    diversity = categories_active / 4.0

    score = 100.0 * (w["volume"] * volume + w["momentum"] * momentum
                     + w["freshness"] * freshness + w["diversity"] * diversity)
    return round(score, 1)
