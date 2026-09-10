"""Testdaten, die mehr als eine Testdatei braucht.

Nicht in conftest.py: pytest traegt von dort nur FIXTURES automatisch in
jede Datei, keine gewoehnlichen Funktionen und Konstanten. Beim Aufteilen
von test_core.py sind fuenf Tests genau darueber gestolpert.
"""
import io

_MARKT_CSV = (
    "Name;Typ;Adresse;Lat;Lng;Website;Ansprechpartner;Notizen;Untertyp;"
    "Marken/Produkte;Einschaetzung;\n"
    # intact row
    "LUCOR Ventanas;potenzialkunde;Calle X 1, 14001 Cordoba, Spanien;37.8;-4.7;"
    "https://www.lucor.es/;;;;;;\n"
    # SHIFTED by one: the Spanish legal-form comma became a semicolon
    "CARPYVENT; S.L.;potenzialkunde;Av Y 2, 03001 Alicante, Spanien;38.3;-0.4;"
    "http://carpyvent.es;;;;;\n"
    # a real competitor location: the manufacturer's name IS the company name
    "Schueco Showroom Madrid;wettbewerber;Valdemoro, 28340 Madrid;40.1;-3.6;"
    "https://schueco.com/es;;Eigener Showroom;Showroom;;;\n"
    # installs a competitor's systems -> a PROSPECT, not a competitor
    "Premial;wettbewerber;Mijas Costa, 29650 Malaga;36.5;-4.8;;;"
    "Schueco-Premiumpartner;Produktion;Schueco Premium Partner;;\n"
    # duplicate of the row above with a different Typ
    "Premial;potenzialkunde;Mijas Costa, 29650 Malaga;36.5;-4.8;;;"
    "Schueco-Premiumpartner;Produktion;Schueco Premium Partner;;\n"
    # existing customer, joinable only by the Kd-Nr in free text
    "IBZ Cristal;bestandskunde;Ibilbidea 80, 20115 Astigarraga;43.2;-1.9;;;"
    "Kd-Nr. 5164611 | Lizenznehmer SL25;;;;\n"
)


def _write_markt(tmp_path):
    p = tmp_path / "markt.csv"
    p.write_text(_MARKT_CSV, encoding="utf-8-sig")
    return p
