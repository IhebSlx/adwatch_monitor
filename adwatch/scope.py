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

# NICHTS wird über sl_customer_class ausgeschlossen — und das ist eine
# Korrektur, keine Auslassung.
#
# Am 2026-08-20 wurde „07 - SL Mitarbeiter" als „Konto einer Solarlux-Person"
# gelesen und ausgeschlossen. Der volle Abruf widerlegte das binnen Minuten:
# 8.204 Konten tragen die Klasse, 7.302 davon im Händler-Panel, zusammen
# 104,6 Mio EUR Angebotsvolumen — und darunter Firmen wie MADEROS Wintergärten,
# LEEB Balkone, Willab Garden AB. Das sind Händler, keine Beschäftigten.
#
# Die Klasse bedeutet offenkundig die BETREUUNGSART (direkt durch einen
# Solarlux-Mitarbeiter statt über den Fachhandelsvertrieb) — passend dazu sind
# alle anderen Werte ebenfalls Vertriebswege: Zuschuss, Fachhandelsvertrieb,
# Direktvertrieb, Objektvertrieb, Architektenberatung.
#
# Zwei Lehren, beide teuer:
#  * Der Ausschluss hätte fast die halbe Grundgesamtheit gelöscht (6.207 -> 1.858
#    allein im Kalt-Profil), inklusive der größten Konten.
#  * Als MERKMAL ist das Feld verboten: „wer betreut die Firma" beschreibt
#    unsere Beziehung, nicht die Firma — dieselbe Falle wie
#    `Vertriebsweg = Direktvertrieb` (§3), die bereits als Wert-Leckage
#    gemessen und entfernt wurde.
#
# Gespiegelt bleibt die Spalte trotzdem: sie ist als Filter und als Kontext
# nützlich, nur eben nicht als Ausschluss und nicht als Merkmal.


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
    )


def apply(stmt):
    """Add the scope restriction to a select() over Company."""
    return stmt.where(in_scope_clause())


def is_in_scope(segment: str | None, is_competitor: bool | None = False) -> bool:
    """Same rule for rows already loaded in Python."""
    if is_competitor:
        return False
    return segment is None or segment not in EXCLUDED_SEGMENTS


def excluded_ids(session) -> set[int]:
    """Ids to drop from anything keyed by company id (metrics, rankings)."""
    from sqlalchemy import select
    return set(session.scalars(
        select(Company.id).where(Company.segment.in_(EXCLUDED_SEGMENTS))))
