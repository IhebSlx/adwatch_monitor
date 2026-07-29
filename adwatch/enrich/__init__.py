"""PART 4 — Enrichment: fill in a company's missing website and learn useful
facts about it from its own public website.

Three tiers, cheapest first (see service.enrich_company):
  Tier 0  domains.py         website derived from the SAP contact email — free
  Tier 1  website_finder.py  website found via a web search (Serper) — ~$0.001
  Tier 2  extract.py         facts pulled from the site's own text (one LLM call)

The hard rule everywhere in this package: enrichment only ever FILLS BLANKS.
SAP master data and anything a human entered are authoritative and are never
overwritten — and a website is only auto-accepted when a DETERMINISTIC check
(validate.py) proves the site belongs to that company. Anything weaker is
parked as `needs_review` for a human, because a wrong website silently poisons
every downstream fact (and the win-back list) exactly the way a wrong Meta page
did.
"""
