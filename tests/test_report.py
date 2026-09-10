"""Berichte: PDF, Excel, Mailversand, Empfaenger.

Aufgeteilt aus test_core.py: 197 Tests in einer Datei von 6.000 Zeilen
liessen sich nicht mehr ueberblicken. Fixtures stehen in conftest.py.
"""
import datetime as dt   # noqa: F401

import pytest   # noqa: F401



def test_report_ctas_and_source_label():
    """Report links are per-platform CTAs (Meta + Google), and the header source
    label credits Google only when Google ads are actually present."""
    from adwatch.report import _ads_cta, _google_transparency_url, _source_label
    assert "ads/library" in _ads_cta({"page_id": "111", "country": "DE"})
    assert "Google-Anzeigen" not in _ads_cta({"page_id": "111"})
    assert "adstransparency.google.com/advertiser/AR9" in _ads_cta({"google_id": "AR9", "country": "DE"})
    assert _ads_cta({"page_id": "111", "google_id": "AR9"}).count("<a ") == 2   # both platforms
    assert _ads_cta({}) == ""
    assert _source_label([{"meta_active_ads": 5, "google_active_ads": 0}]) == "Meta Ad Library"
    assert "Google" in _source_label([{"google_active_ads": 2}])
    assert _google_transparency_url(None) is None

def test_report_def_crud_and_run(temp_db, monkeypatch):
    """Saved report definitions: create/list/update/delete + validation, and
    run_definition builds over the saved filter and emails only the ACTIVE saved
    recipients (build + email mocked so no PDF/network in the test)."""
    from adwatch import report_defs
    import adwatch.report as report_mod
    import adwatch.emailer as emailer_mod
    from adwatch.models import ReportRecipient

    s = temp_db.SessionLocal()
    r1 = ReportRecipient(name="BD One", email="one@x.de", active=True)
    r2 = ReportRecipient(name="BD Two", email="two@x.de", active=False)   # inactive -> skipped
    s.add_all([r1, r2]); s.commit()
    rid1, rid2 = r1.id, r2.id
    s.close()

    d = report_defs.create_definition(
        name="Google Winback DE", filters={"ad_activity": "active", "ad_source": "google"},
        report_type="full", recipient_ids=[rid1, rid2],
        schedule_enabled=True, schedule_day=0, schedule_time="07:30")
    assert d["id"] and d["schedule_enabled"] is True
    assert len(report_defs.list_definitions()) == 1

    with pytest.raises(ValueError):
        report_defs.create_definition(name="  ", filters={})          # empty name
    with pytest.raises(ValueError):
        report_defs.create_definition(name="x", filters={}, schedule_time="99:99")  # bad time

    sent = {}
    monkeypatch.setattr(report_mod, "build_report",
                        lambda filters=None: "output/adwatch_report_KW30_2026.pdf")
    monkeypatch.setattr(report_mod, "write_report_meta", lambda *a, **k: None)
    monkeypatch.setattr(emailer_mod, "send_report_email",
                        lambda path, recipient=None, subject=None, **k: sent.update(
                            path=path, recipient=recipient, subject=subject,
                            source=k.get("source")))
    res = report_defs.run_definition(d["id"], send=True)
    assert res["sent"] is True
    assert sent["recipient"] == ["one@x.de"]                          # inactive recipient excluded
    assert "sent to 1" in report_defs.get_definition(d["id"])["last_status"]

    report_defs.update_definition(d["id"], schedule_enabled=False)
    assert report_defs.get_definition(d["id"])["schedule_enabled"] is False
    report_defs.delete_definition(d["id"])
    assert report_defs.list_definitions() == []

def test_report_events_record_creation_and_delivery(temp_db, monkeypatch, tmp_path):
    """Creating and sending a report must leave an audit row, so 'did the mail go
    out?' is answerable after a crash — the question that had no answer before."""
    from adwatch import report_log
    from adwatch.report import write_report_meta

    write_report_meta(str(tmp_path / "adwatch_top5_KW31_2026.pdf"),
                      filters={"country": ["ES"]}, source="manual")
    ev = report_log.history()
    assert len(ev) == 1
    assert ev[0]["kind"] == "created" and ev[0]["report_type"] == "top5"
    assert ev[0]["source"] == "manual" and "ES" in (ev[0]["scope"] or "")

    # a failing send is recorded too, with the real error, and still raises
    import adwatch.emailer as emailer_mod
    from adwatch import config
    monkeypatch.setattr(config, "POWER_AUTOMATE_WEBHOOK_URL", "https://example.invalid/f")
    pdf = tmp_path / "adwatch_top5_KW31_2026.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")

    def _boom(*a, **k):
        raise OSError("network is down")
    # Gepatcht wird der Transport, den der Mailer WIRKLICH benutzt: flows.post
    # ruft requests.post. Vorher stand hier `emailer_mod.requests` -- das traf
    # zufaellig dasselbe Modulobjekt und funktionierte deshalb, haengte den Test
    # aber an einen Import, den emailer.py seit dem Umstieg auf flows.post gar
    # nicht mehr braucht. Ein Test, der ueber eine fremde Namensraum-Referenz
    # patcht, haelt einen toten Import am Leben und sagt niemandem warum.
    from adwatch import flows as flows_mod
    monkeypatch.setattr(flows_mod.requests, "post", _boom)
    with pytest.raises(RuntimeError):
        emailer_mod.send_report_email(str(pdf), recipient=["a@x.de", "b@x.de"],
                                      subject="Bericht", source="pipeline")

    ev = report_log.history()
    fail = ev[0]
    assert fail["kind"] == "send_failed"
    assert fail["recipients"] == ["a@x.de", "b@x.de"]
    assert fail["source"] == "pipeline" and "network is down" in fail["detail"]

    # recording must never be what breaks a send: a broken audit write is swallowed
    monkeypatch.setattr(report_log, "SessionLocal", lambda: (_ for _ in ()).throw(OSError("db gone")))
    report_log.record("created", "x.pdf")          # must not raise

def test_default_step_order_puts_the_free_domain_pass_first():
    """The default order is load-bearing, so it gets its own test.

    Identity's only free AND authoritative tier crawls Company.website_domain.
    Full enrichment fills that column but costs money (Serper + Haiku), while
    deriving a domain from the company's own email is free. So the default must
    be: free domains -> identity -> paid enrichment."""
    from adwatch.jobs import DOMAIN_PREPASS, resolve_step_order

    both = {"enrich": True, "identity": True, "ads": ["meta"], "report": "full",
            "send_to": [1]}
    assert resolve_step_order(both) == [DOMAIN_PREPASS, "identity", "enrich",
                                        "ads", "report", "send"]

    # the pre-pass only earns its place when it can actually feed the identity
    # check — enrichment alone already does Tier 0 internally
    assert resolve_step_order({"enrich": True}) == ["enrich"]
    assert resolve_step_order({"identity": True}) == ["identity"]

    # an explicit order wins, and anything selected but omitted is appended
    # rather than silently dropped
    assert resolve_step_order({"enrich": True, "identity": True,
                               "order": ["enrich", "identity"]}) == ["enrich", "identity"]
    assert resolve_step_order({"enrich": True, "identity": True, "report": "full",
                               "order": ["enrich"]}) == ["enrich", "identity", "report"]

    # 'send' lives under send_to, not a boolean
    assert resolve_step_order({"report": "full", "send_to": [1]}) == ["report", "send"]
    assert "send" not in resolve_step_order({"report": "full"})

def test_top5_report_shows_profiles_and_an_honest_count(temp_db, tmp_path):
    """A 'Top 5' that shows 2 companies must say why, and each company must carry
    its enriched profile — otherwise the report is just ad counts with no context."""
    import datetime as dt
    from adwatch.models import Company, CompanyEnrichment, WeeklyCompanyMetric
    from adwatch.report import build_top5_report

    s = temp_db.SessionLocal()
    wk = dt.date(2026, 7, 27)
    ids = []
    for i in range(2):
        c = Company(name=f"Cerramientos {i} SL", country="ES", segment="Handel",
                    resolution_status="confirmed", website_domain=f"cerr{i}.es",
                    enrichment_status="enriched")
        s.add(c); s.flush()
        s.add(WeeklyCompanyMetric(company_id=c.id, source="meta", week_start=wk,
                                  total_active_ads=8 - i,
                                  products=["cerramiento", "porch closure"]))
        s.add(CompanyEnrichment(company_id=c.id, status="enriched", fields={
            "description_de": "Anbieter von Glasabschlüssen für Veranden und Terrassen.",
            "assessment_de": "Dürfte ein Kleinbetrieb mit regionalem Fokus sein.",
            "products": ["Terrassenverglasung"], "employee_hint": "10 Mitarbeiter",
            "competitor_brands": ["Sunflex"]}))
        ids.append(c.id)
    # 3 more companies in scope that never advertised — they are why it isn't 5
    for i in range(3):
        s.add(Company(name=f"Stille Firma {i}", country="ES", segment="Handel",
                      resolution_status="confirmed"))
    s.commit(); s.close()

    out = str(tmp_path / "top5.pdf")
    build_top5_report(path=out, filters={})

    from pypdf import PdfReader
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages)
    # collapse whitespace: the PDF line-wraps mid-sentence, which would otherwise
    # make these assertions depend on where the text happens to break
    text = " ".join(raw.split())
    # the headline states the real number instead of promising five
    assert "Top 5" not in text
    assert "Werbetreibende mit aktiven Anzeigen (2)" in text
    assert "die Liste ist nicht gekürzt" in text
    assert "nur 2 von 5 Firmen" in text
    # the enrichment reaches this report type too, fact and inference kept apart
    assert "Beschreibung:" in text and "Glasabschl" in text
    assert "Einschätzung (KI, unbestätigt):" in text and "Kleinbetrieb" in text
    assert "keine belegte Angabe" in text
    assert "10 Mitarbeiter" in text and "Sunflex" in text
    # ad-derived products arrive in German, not as 'cerramiento'/'porch closure'
    assert "cerramiento" not in text and "porch closure" not in text
    assert "Terrassenverglasung" in text

def test_profiles_are_cut_at_the_bottom_not_at_the_alphabet(temp_db):
    """The profile section inherited the overview's ordering: active advertisers
    first, then by NAME. Spain has nine advertisers and ~700 enriched companies,
    so in practice the section was alphabetical and the cut at limit=80 landed
    inside the letter A — 'Aluminios y Cristaleria Hisalma' was the last profile
    in the report. Every Betrieb with Passung hoch and every architect who awards
    contracts from B to Z was named in the qualification tables and then missing
    from the profiles underneath.

    The profiles now use the same tiers the qualification section ranks by, so
    the two sections agree on who matters."""
    from adwatch import report
    from adwatch.db import SessionLocal
    from adwatch.models import Company, CompanyEnrichment

    s = SessionLocal()
    # deliberately adversarial names: the best company sorts LAST alphabetically
    seed = [("Zenit Cerramientos SL", "hoch", None, None, ["Panoramah"]),
            ("Yebra Arquitectos", None, "hoch", "vergibt Aufträge", []),
            ("Alfa Aluminios SL", "gering", None, None, []),
            ("Beta Metalicas SL", "mittel", None, None, [])]
    for name, fit, rel, role, brands in seed:
        s.add(Company(name=name, country="ES", solarlux_fit=fit,
                      solarlux_relevance=rel, decision_role=role,
                      competitor_brands=brands, description=f"{name} Beschreibung"))
    s.commit()
    data = []
    for c in s.query(Company).all():
        s.add(CompanyEnrichment(company_id=c.id, status="enriched",
                                fields={"description_de": f"{c.name} Beschreibung"}))
        data.append({"company_id": c.id, "company": c.name,
                     "total_active_ads": 0, "has_data": False})
    s.commit()
    s.close()

    from reportlab.lib.styles import getSampleStyleSheet
    story = report._profiles_story(data, None, getSampleStyleSheet(), limit=2)
    text = " ".join(getattr(p, "text", "") for p in story)

    zenit, yebra = text.find("Zenit"), text.find("Yebra")
    alfa = text.find("Alfa Aluminios")
    assert zenit != -1 and yebra != -1, "Passung hoch und Relevanz hoch muessen drin sein"
    assert zenit < yebra, "Betrieb mit Passung hoch vor dem Buero"
    assert alfa == -1 or alfa > yebra, \
        "der alphabetisch erste, aber schlechteste Treffer darf den Platz nicht besetzen"
    assert "nie am Alphabet" in text, "der Schnitt muss sich erklaeren"

def test_ein_buero_je_domain_in_der_ortsliste():
    """Ein Buero steht im CRM oft mehrfach mit derselben Website --
    'Estudio Closa-Godoy' gegen 'Esudio Closa- Godoy' (Tippfehler),
    'Herzog & de Meuron' gegen '… Basel' (Standort). In einer Ortsliste liest
    sich dieselbe Firma dreimal wie ein Fehler, also wird fuer die ANZEIGE
    ueber die Domain zusammengefasst. Die Konten bleiben unangetastet."""
    from adwatch.taetigkeit import _je_buero_einmal

    roh = [
        {"name": "Bofill Architects", "website": "bofill.com", "stufe": 1},
        {"name": "Ricardo Bofill- Taller de Arquitectura", "website": "bofill.com", "stufe": 3},
        {"name": "Ricardo Bofill- Taller", "website": "BOFILL.COM", "stufe": 0},
        {"name": "Anderes Buero", "website": "anderes.es", "stufe": 0},
        {"name": "Ohne Website", "website": "", "stufe": 2},
    ]
    aus = _je_buero_einmal(roh)
    assert len(aus) == 3, "drei bofill-Zeilen werden zu einer"
    gewaehlt = next(b for b in aus if "bofill" in (b["website"] or "").lower())
    assert gewaehlt["stufe"] == 3, "die waermste Zeile gewinnt"
    assert any(b["name"] == "Ohne Website" for b in aus), \
        "ohne Domain wird nicht zusammengefasst"

def test_bueros_liste_wird_nicht_von_der_kartenkuerzung_beschnitten(temp_db, monkeypatch):
    """Gemessen 2026-09-08: die Spanien-Liste zeigte 213 statt 231 Büros.

    `orte()` kürzt die Büroliste je Kartennadel auf 40 — verständlich, die
    Sprechblase zeigt ohnehin nur die ersten Zeilen. `bueros()` las aber genau
    aus diesem gekürzten Ergebnis. Barcelona nennt 92 Büros, Madrid 83, also
    fielen 95 Listeneinträge weg, und ein Büro, dessen einzige spanische Orte
    Barcelona und Madrid sind und das dort auf Platz 41 stand, fehlte in der
    Arbeitsliste vollständig — samt Excel und PDF, die daraus entstehen.

    Eine Kürzung für die ANZEIGE darf nie die DATENMENGE beschneiden.
    """
    from adwatch import taetigkeit
    from adwatch.models import Company

    monkeypatch.setattr(taetigkeit, "SessionLocal", temp_db.SessionLocal)
    # Ein Ort, mehr Büros als in eine Nadel passen. Der Ort braucht eine
    # Koordinate, sonst landet er gar nicht auf der Karte -- "Mallorca" steht
    # in _FLAECHEN und ist damit unabhängig von der plz_geo-Tabelle.
    n = taetigkeit._LISTE_JE_NADEL + 12
    s = temp_db.SessionLocal()
    s.add_all([
        Company(name=f"Büro {i:03d}", segment="Architekten",
                sub_segment="Architekturbüro",
                website_domain=f"buero{i:03d}.example",
                active_cities={"ES": ["Mallorca"]}, relation_level=0)
        for i in range(n)
    ])
    s.commit(); s.close()

    karte = taetigkeit.orte(land="ES")
    liste = taetigkeit.bueros(land="ES")

    # Die Karte darf kürzen ...
    assert len(karte["pins"]) == 1
    assert len(karte["pins"][0]["liste"]) == taetigkeit._LISTE_JE_NADEL
    # ... die Nadel muss aber die WAHRE Zahl nennen, nicht die Länge ihrer Liste
    assert karte["pins"][0]["bueros"] == n
    # ... und die Arbeitsliste muss vollständig sein
    assert liste["bueros"] == n
    assert len(liste["rows"]) == n
    # Karte und Liste sind zwei Ansichten EINER Menge: eine Zahl, nicht zwei.
    assert karte["bueros"] == liste["bueros"]

def test_taetigkeit_bericht_nimmt_genau_die_uebergebenen_ids(temp_db, monkeypatch, tmp_path):
    """Die Kopffilter der Tätigkeitsliste leben nur im Browser — der Bericht
    bekommt deshalb die sichtbaren IDs mitgeschickt, in der sichtbaren
    Reihenfolge. Die DATEN kommen trotzdem aus der Datenbank: der Bildschirm
    bestimmt die Auswahl, nicht den Inhalt."""
    from pypdf import PdfReader

    from adwatch import taetigkeit
    from adwatch.models import Company
    from adwatch.report import build_taetigkeit_report

    monkeypatch.setattr(taetigkeit, "SessionLocal", temp_db.SessionLocal)
    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Warmes Büro", segment="Architekten",
                sub_segment="Architekturbüro", website_domain="warm.example",
                city="Lübeck", country="DE",
                active_cities={"ES": ["Mallorca"]}, relation_level=4),
        Company(name="Kaltes Büro", segment="Architekten",
                sub_segment="Architekturbüro", website_domain="kalt.example",
                city="Essen", country="DE",
                active_cities={"ES": ["Mallorca"]}, relation_level=0),
    ])
    s.commit(); s.close()

    alle = taetigkeit.bueros(land="ES")["rows"]
    assert len(alle) == 2
    warm = [z for z in alle if z["stufe"] >= 3]

    pfad = build_taetigkeit_report(
        land="ES", zeilen=warm, filters={"segment": ["Architekten"]},
        tabellenfilter="Tabellenfilter: Beziehung mindestens 3",
        path=str(tmp_path / "probe.pdf"))
    text = "\n".join(pg.extract_text() for pg in PdfReader(pfad).pages)
    assert "Warmes Büro" in text
    assert "Kaltes Büro" not in text          # gefiltert heißt gefiltert
    assert "Beziehung mindestens 3" in text     # der Filter steht im Kopf
    assert "Mallorca" in text
    assert "keine Rangfolge" in text            # der Rollen-Vorbehalt fährt mit

def test_taetigkeit_dateiname_kommt_durch_die_download_weiche():
    """`_REPORT_FILENAME_RE` ist nicht nur für die Historie da —
    `_safe_report_path` lässt nur durch, was dort passt. Ein Berichtstyp, der
    in der Regex fehlt, lässt sich erzeugen und dann nicht herunterladen."""
    from adwatch.report import parse_report_filename

    p = parse_report_filename("adwatch_taetigkeit_KW37_2026.pdf")
    assert p and p["report_type"] == "taetigkeit" and p["label"] == "KW37_2026"
    # die bestehenden Typen dürfen sich dabei nicht verschoben haben
    assert parse_report_filename("adwatch_report_KW37_2026.pdf")["report_type"] == "full"
    assert parse_report_filename("adwatch_top5_KW37_2026_02.pdf")["report_type"] == "top5"
    assert parse_report_filename("../../etc/passwd") is None

def test_filterbeschreibung_verschweigt_die_taetigkeitsfilter_nicht():
    """Der Umfangskasten sagte "Segment: Architekten" und ließ "tätig in: ES"
    weg — ein Bericht, dessen Kopf seinen eigenen Umfang zu WEIT angibt, ist
    schlimmer als einer ohne Kopf: der Leser hält 213 spanienaktive Büros für
    20.696 Architekten."""
    from adwatch.report import _describe_filters_de

    d = _describe_filters_de({"segment": ["Architekten"], "active_country": ["ES"],
                              "relation_min": 3, "city": "Mallorca",
                              "sap_state": "with"})
    assert "Segment: Architekten" in d
    assert "tätig in: ES" in d
    assert "Beziehung mindestens 3" in d
    assert "Mallorca" in d
    assert "SAP-Nummer" in d
    assert _describe_filters_de({}) is None

def test_excel_hat_die_bestellten_spalten():
    """Was Iheb aufgezaehlt hat, muss als Spalte existieren -- namentlich.

    Die Liste stand so in seiner Nachricht: Link und Adresse je Projekt,
    Ansprechpartner, Projekte gesamt, davon in Spanien, Hauptsitz,
    Niederlassung. Dazu Baujahr und Gebaeudeart. Ein Test darauf, weil eine
    fehlende Spalte in einer 30-spaltigen Datei niemandem auffaellt.
    """
    import inspect

    from tools import spanien_excel

    quelle = inspect.getsource(spanien_excel.bauen)
    for spalte in ("Hauptsitz Straße", "Hauptsitz PLZ", "Hauptsitz Ort",
                   "Hauptsitz Land", "Niederlassung in ES", "Projekte gesamt",
                   "Projekte in ES", "Anteil ES", "Link", "Ort", "Region",
                   "Baujahr", "Gebäudeart", "Quelle"):
        assert f'("{spalte}"' in quelle, f"Spalte fehlt: {spalte}"

def test_beleg_niederlassung_zeigt_das_signal_nicht_den_seitentitel():
    """Ein Beleg muss belegen.

    In der Spalte stand der Titel der Fundseite: "People - Nordic Office of
    Architecture - We are 400 architects...". Daneben ein "ja". Iheb hat
    zurecht gefragt, wie diese Zeilen in die Liste kommen. Jetzt steht dort,
    WAS gefunden wurde: Adresse, Telefonnummer, und erst danach die Fundstelle.
    """
    from tools.spanien_excel import _beleg_nl

    b = _beleg_nl({"plz_mit_ort": "28010 Madrid", "vorwahl_34": True,
                   "wort_spanien": True, "zeile": "Contact | Broadway Malyan"})
    assert b.startswith("Adresse 28010 Madrid, Telefonnummer +34")
    assert "Contact | Broadway Malyan" in b

    # Das Wort allein ist kein Beleg -- es steht auf jeder Seite eines Bueros,
    # das in Spanien baut.
    assert _beleg_nl({"wort_spanien": True, "zeile": "x"}) == "kein harter Beleg"
    assert _beleg_nl(None) == ""
