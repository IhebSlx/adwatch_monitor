"""What counts as "the business" — one definition, enforced in the read model.

"Private Endkunden" are consumers: private individuals who once bought a
conservatory, not companies the partner program can sell to, market with, or
acquire more of. They are 1,666 of 4,618 rows — 36% of the base — so leaving them
in the totals made every number quietly wrong: "14 of 4618 advertisers" implied a
1.5% activation rate over a population that was a third private households, none
of which will ever run a Facebook ad campaign.

They stay in the database (they are real customer history and the revenue is
real), but they are excluded from every count, score, ranking, report and export.

Enforced HERE rather than by each caller remembering a filter, because that is
exactly what failed before: the Companies tab excluded them via a frontend
default the user could switch off, the ICP engine excluded them separately in its
own constant, and the dashboard excluded them nowhere at all.

The single escape hatch is `include_consumers=True`, for the rare case of looking
a consumer record up on purpose. Nothing passes it by default.
"""
from __future__ import annotations

from sqlalchemy import or_

from .models import Company

# Segment values that are not part of the partner business.
EXCLUDED_SEGMENTS: tuple[str, ...] = ("Private Endkunden",)

# Kundenklassen, die kein Partnergeschäft sind. „07 - SL Mitarbeiter" sind
# Konten von Solarlux-Beschäftigten — dieselbe Sorte Verunreinigung wie Private
# Endkunden, nur schwerer zu sehen: Segment und Branche sehen aus wie bei einem
# echten Händler. Gemessen am 2026-08-20 lagen 2.126 solcher Konten im
# Händler-Panel, auf dem jede ICP-Zahl beruht.
#
# Verglichen wird auf ENTHALTEN statt auf Gleichheit, weil das Label in CRM den
# Zifferncode trägt („07 - SL Mitarbeiter") und eine Umbenennung der Anzeige
# den Ausschluss sonst still aushebeln würde.
EXCLUDED_CLASS_MARKER: str = "SL Mitarbeiter"


def in_scope_clause():
    """SQLAlchemy criterion for "part of the business".

    A NULL segment is KEPT: unknown is not the same as consumer, and silently
    dropping unclassified rows would hide a data-quality problem instead of
    showing it.

    Competitors' own locations are excluded too. They are imported deliberately
    (see Company.is_competitor) because the competitive footprint is useful, but a
    Schüco showroom must never surface in a count, a ranking or a call list. A
    NULL is treated as False so rows predating the column stay in scope.
    """
    from sqlalchemy import and_
    return and_(
        or_(Company.segment.is_(None),
            Company.segment.not_in(EXCLUDED_SEGMENTS)),
        or_(Company.is_competitor.is_(None), Company.is_competitor.is_(False)),
        # NULL bleibt drin: unbekannt ist nicht dasselbe wie Mitarbeiterkonto,
        # und der Wert fehlt bei jeder Firma, die nicht aus CRM stammt.
        or_(Company.sl_customer_class.is_(None),
            ~Company.sl_customer_class.contains(EXCLUDED_CLASS_MARKER)),
    )


def apply(stmt):
    """Add the scope restriction to a select() over Company."""
    return stmt.where(in_scope_clause())


def is_in_scope(segment: str | None, is_competitor: bool | None = False,
                customer_class: str | None = None) -> bool:
    """Same rule for rows already loaded in Python."""
    if is_competitor:
        return False
    if customer_class and EXCLUDED_CLASS_MARKER in customer_class:
        return False
    return segment is None or segment not in EXCLUDED_SEGMENTS


def excluded_ids(session) -> set[int]:
    """Ids to drop from anything keyed by company id (metrics, rankings)."""
    from sqlalchemy import select
    return set(session.scalars(
        select(Company.id).where(Company.segment.in_(EXCLUDED_SEGMENTS))))
