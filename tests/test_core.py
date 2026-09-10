"""Reine Funktionen ohne Modulbindung -- Zahlenformate, Sentinels, Quelltextregeln.

Aufgeteilt aus test_core.py: 197 Tests in einer Datei von 6.000 Zeilen
liessen sich nicht mehr ueberblicken. Fixtures stehen in conftest.py.
"""
import datetime as dt   # noqa: F401

import pytest   # noqa: F401



def test_company_score_zero_ads_is_zero():
    """A company running zero ads must score 0 — not ~12.5 off the neutral
    first-week momentum term (the phantom-ad fix exposed this)."""
    from adwatch.insights.score import company_score
    assert company_score(0, None, 0, 0) == 0.0
    assert company_score(0, 5, 0, 0) == 0.0     # even with prior-week ads
    assert company_score(3, None, 3, 2) > 0     # real activity still scores

def test_crm_showroom_ingest_joins_on_crm_id(temp_db):
    """CRM showroom rows join to companies through the Dataverse GUID. A row whose
    dealer GUID is unknown locally must still be STORED (company_id NULL, counted
    as unmatched) — dropping it would hide that the company master is incomplete.
    Re-ingesting the same pull must update, never duplicate."""
    from adwatch import crm_sync
    from adwatch.models import Company, CrmShowroom
    from sqlalchemy import select

    GUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
    s = temp_db.SessionLocal()
    s.add(Company(name="Schaurraum Partner", country="DE", crm_id=GUID_A))
    s.commit(); s.close()

    recs = [
        {"crm_id": "e1", "dealer_crm_id": GUID_A, "product_family": "Glas-Faltwand",
         "product": "SL 25", "installed_on": "2024-05-01"},
        {"crm_id": "e2", "dealer_crm_id": GUID_A, "product_family": "Wintergarten",
         "product": "SDL Atrium plus", "installed_on": None},
        {"crm_id": "e3", "dealer_crm_id": "unknown-guid", "product_family": "cero"},
        {"crm_id": "", "dealer_crm_id": GUID_A, "product_family": "Ignoriert"},   # no key
    ]
    r = crm_sync.upsert_showrooms(recs)
    assert r == {"received": 4, "inserted": 3, "updated": 0,
                 "matched": 2, "unmatched": 1, "skipped": 1}

    s = temp_db.SessionLocal()
    rows = {x.crm_id: x for x in s.scalars(select(CrmShowroom))}
    cid = s.scalar(select(Company.id))
    assert rows["e1"].company_id == cid and rows["e1"].product == "SL 25"
    assert rows["e1"].installed_on.isoformat() == "2024-05-01"
    assert rows["e3"].company_id is None            # kept, but unresolved
    s.close()

    # idempotent: same pull again updates in place
    r2 = crm_sync.upsert_showrooms(recs)
    assert r2["inserted"] == 0 and r2["updated"] == 3
    s = temp_db.SessionLocal()
    assert len(list(s.scalars(select(CrmShowroom)))) == 3
    s.close()

    o = crm_sync.showroom_overview()
    assert o["rows"] == 3 and o["matched_dealers"] == 1
    assert dict(o["families"])["Glas-Faltwand"] == 1
    assert o["per_company"][cid] == ["Glas-Faltwand", "Wintergarten"]

def test_markets_are_data_not_code():
    """Adding a market used to need three code edits (country aliases, legal-page
    term, search language) in two modules. Missing one failed SILENTLY — that is
    how 982 Spanish companies were imported as DE. Now one YAML file drives all
    three, and a successor adds a market without Python."""
    from adwatch import markets

    markets.reload()
    codes = markets.known_codes()
    assert {"DE", "ES", "PT", "AT", "CH", "FR"} <= set(codes)

    # the Spain bug, pinned: every spelling a source might use resolves
    for spelling in ("Spanien", "españa", "espana", "SPAIN", "es", "ES"):
        assert markets.code_for(spelling) == "ES", spelling
    # umlaut and its transliteration both work
    assert markets.code_for("österreich") == markets.code_for("Oesterreich") == "AT"
    # NO must survive YAML's boolean coercion of a bare NO key
    assert "NO" in codes and markets.code_for("Norwegen") == "NO"

    # an unknown name returns None so the caller can KEEP the old value rather
    # than silently defaulting to DE
    assert markets.code_for("Slowenien") is None
    assert markets.code_for("") is None
    # but an unlisted 2-letter code passes through instead of being dropped
    assert markets.code_for("si") == "SI"

    # the per-market behaviour that used to be hardcoded elsewhere
    assert markets.search_lang("ES") == "es"
    assert markets.legal_page_term("ES") == "aviso legal"      # not "Impressum"
    assert markets.legal_page_term("PT") == "contactos"
    assert markets.legal_page_term("DE") == "Impressum"
    # unknown market degrades to a usable default rather than raising
    assert markets.search_lang("XX") and markets.legal_page_term("XX")

    # every market must be complete, or a new entry could half-work
    for code, spec in markets.all_markets().items():
        assert len(code) == 2 and code.isalpha() and code.isupper(), code
        assert spec["aliases"] and spec["search_lang"] and spec["legal_page"], code

def test_flow_registry_is_configurable_and_backward_compatible(temp_db, monkeypatch):
    """Flows are addressed by ROLE, so a second integration point (the CRM query
    proxy) needs no new constant, timeout or error convention. And an install that
    still has only the old POWER_AUTOMATE_WEBHOOK_URL must keep working."""
    from adwatch import config, flows

    # unknown role fails loudly rather than silently doing nothing
    with pytest.raises(ValueError):
        flows.url_for("does_not_exist")

    # nothing configured -> a message that says WHERE to fix it
    monkeypatch.setattr(config, "resolve_setting", lambda k: "")
    assert not flows.is_configured("report_email")
    msg = flows.missing_message("crm_query")
    assert "FLOW_URL_CRM_QUERY" in msg and "Einstellungen" in msg
    with pytest.raises(RuntimeError):
        flows.post("crm_query", {})

    # legacy key alone still drives the email role (upgrade path)
    monkeypatch.setattr(config, "resolve_setting",
                        lambda k: "https://legacy.example/flow"
                        if k == "POWER_AUTOMATE_WEBHOOK_URL" else "")
    assert flows.is_configured("report_email")
    assert flows.url_for("report_email") == "https://legacy.example/flow"
    # ...but the legacy key must NOT leak into other roles
    assert not flows.is_configured("crm_query")

    # the new key wins when both are set
    monkeypatch.setattr(config, "resolve_setting",
                        lambda k: {"FLOW_URL_REPORT_EMAIL": "https://new.example/f",
                                   "POWER_AUTOMATE_WEBHOOK_URL": "https://legacy.example/f"}.get(k, ""))
    assert flows.url_for("report_email") == "https://new.example/f"

    # status() reports the integration points for Settings / diagnostics
    st = {s["role"]: s for s in flows.status()}
    assert st["report_email"]["configured"] is True
    assert st["crm_query"]["configured"] is False
    assert st["crm_query"]["key"] == "FLOW_URL_CRM_QUERY"

    # every role must have a settings entry, or Settings could never configure it
    for role, (key, _, _) in flows.FLOW_ROLES.items():
        assert key in config._SPEC_BY_KEY, f"{role} has no SETTINGS_SPEC entry"
        assert config._SPEC_BY_KEY[key]["secret"] is True, f"{key} must be masked"

def test_backup_verify_catches_the_snapshots_that_bit_us(temp_db, tmp_path, monkeypatch):
    """13 of 14 retained snapshots were once 4 KB empty files written by the test
    suite, with the only good copy one rotation from deletion. verify_latest()
    exists so a useless snapshot is FOUND rather than trusted."""
    import sqlite3
    from adwatch import backup, config as cfg

    bdir = tmp_path / "b"; bdir.mkdir()
    monkeypatch.setattr(cfg, "BACKUP_DIR", bdir, raising=False)
    monkeypatch.setattr(backup.config, "BACKUP_DIR", bdir, raising=False)

    # no backup at all
    assert backup.verify_latest()["ok"] is False

    # a tiny/empty snapshot must be rejected, not reported as fine
    tiny = bdir / "adwatch_20260101_000000_x.db"
    con = sqlite3.connect(tiny); con.execute("CREATE TABLE companies (id INTEGER)")
    con.commit(); con.close()
    r = backup.verify_latest()
    assert r["ok"] is False and "small" in (r.get("error") or "")

def test_scan_and_model_brands_are_merged_not_replaced():
    """The scan guarantees completeness over the page; the model catches loose
    phrasing a literal match misses. Storing either one alone loses companies."""
    from adwatch.enrich.service import _merge_brands
    assert _merge_brands(["Schüco"], ["Cortizo", "Schüco"]) == ["Schüco", "Cortizo"]
    assert _merge_brands(None, None) == []
    assert _merge_brands(["Sunflex"], ["sunflex"]) == ["Sunflex"]   # case-insensitive dedupe

def test_an_empty_dataverse_filter_never_leaves_the_app(temp_db):
    """Gemessen 2026-08-18: drei Testabfragen ohne Filter, drei fehlgeschlagene
    Flow-Läufe, HTTP 502 NoResponse beim Aufrufer. Der Konnektor lehnt einen
    leeren $filter ab — und weil die Aktion scheitert, erreicht der Lauf die
    Response nie, sodass der Fehler wie ein Netzwerkproblem aussieht statt wie
    ein Eingabefehler.

    Aufgefallen war es jahrelang nicht, weil jeder echte Aufruf einen Filter
    trug (`modifiedon gt ...`). Die erste ungefilterte Abfrage stolperte darüber.
    Die Vorgabe wird deshalb HIER gesetzt, an der einzigen Stelle, durch die
    jeder Flow-Aufruf läuft."""
    from adwatch import flows

    # leer, fehlend, nur Leerzeichen -> alle bekommen die Vorgabe
    for payload in ({"entity": "leads", "select": "leadid", "filter": ""},
                    {"entity": "leads", "select": "leadid"},
                    {"entity": "leads", "select": "leadid", "filter": "   "}):
        out = flows._guard_payload("crm_query", payload)
        assert out["filter"] == flows._DEFAULT_DATAVERSE_FILTER
        assert out["entity"] == "leads", "der Rest bleibt unangetastet"

    # ein echter Filter wird NIE überschrieben
    real = {"entity": "accounts", "select": "accountid",
            "filter": "modifiedon gt 2026-01-01T00:00:00Z"}
    assert flows._guard_payload("crm_query", real)["filter"] == real["filter"]

    # andere Rollen bleiben unberührt — die Vorgabe ist Dataverse-spezifisch
    mail = {"recipient": "x@y.de"}
    assert flows._guard_payload("report_email", mail) == mail

def test_konversion_behauptet_nur_was_das_intervall_traegt(temp_db, monkeypatch):
    """Angebot -> Auftrag je Gruppe, mit Konfidenzintervall statt nackter Quote.

    Der Punkt dieses Moduls ist nicht die Quote, sondern ihre Unsicherheit:
    37,5 % auf 120 Faellen und 21,6 % auf 13.453 sehen als Zahl gleich sicher
    aus. Deshalb wird eine Gruppe erst als ueber/unter der Grundlinie markiert,
    wenn ihr 95-%-Intervall die Grundlinie NICHT mehr enthaelt.
    """
    from adwatch.insights import konversion
    from adwatch.models import Company, CrmOpportunity

    # Wilson zuerst gegen bekannte Werte: 50 von 100 -> rund 40,4 % bis 59,6 %
    lo, hi = konversion.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    # Wenige Faelle -> breites Intervall; viele Faelle -> schmales
    assert (konversion.wilson(5, 10)[1] - konversion.wilson(5, 10)[0]) > \
           (konversion.wilson(500, 1000)[1] - konversion.wilson(500, 1000)[0])
    # Randfall: kein Versuch heisst "wir wissen nichts", nicht "0 %"
    assert konversion.wilson(0, 0) == (0.0, 1.0)

    s = temp_db.SessionLocal()
    s.add_all([
        Company(crm_id="F-GUT", name="Gut GmbH", segment="Wohnungswirtschaft"),
        Company(crm_id="F-MIT", name="Mittel GmbH", segment="Handel"),
        Company(crm_id="F-PRIV", name="Privat", segment="Private Endkunden"),
    ])
    vcs = []
    # Wohnungswirtschaft: 90 von 100 gewonnen -> klar ueber der Grundlinie
    for i in range(100):
        vcs.append(CrmOpportunity(
            crm_id=f"W{i}", parent_account_crm_id="F-GUT", quoted_value=1000.0,
            invoiced_value=(800.0 if i < 90 else 0.0),
            state=("gewonnen" if i < 90 else "verloren")))
    # Handel: 10 von 100 gewonnen -> klar darunter
    for i in range(100):
        vcs.append(CrmOpportunity(
            crm_id=f"H{i}", parent_account_crm_id="F-MIT", quoted_value=1000.0,
            invoiced_value=(500.0 if i < 10 else 0.0),
            state=("gewonnen" if i < 10 else "verloren")))
    # Private Endkunden duerfen in keiner Auswertung auftauchen
    vcs.append(CrmOpportunity(crm_id="P1", parent_account_crm_id="F-PRIV",
                              quoted_value=99999.0, state="gewonnen"))
    s.add_all(vcs); s.commit(); s.close()
    monkeypatch.setattr(konversion, "SessionLocal", temp_db.SessionLocal)

    d = konversion.nach("segment", min_entschieden=10)
    gruppen = {z["gruppe"]: z for z in d["zeilen"]}
    assert "Private Endkunden" not in gruppen, "Privatkunden gehoeren in keine Auswertung"
    assert d["basis_gewinnrate"] == 0.5, "90 + 10 von 200 entschiedenen"

    gut, mit = gruppen["Wohnungswirtschaft"], gruppen["Handel"]
    assert gut["gewinnrate"] == 0.9 and mit["gewinnrate"] == 0.1
    assert gut["ueber_basis"] is True and gut["unter_basis"] is False
    assert mit["unter_basis"] is True and mit["ueber_basis"] is False
    # Euro-Quote: 90 x 800 von 100.000 angeboten
    assert gut["euro_quote"] == 0.72 and mit["euro_quote"] == 0.05
    # Belegdeckung sagt, wie gross der blinde Fleck ist
    assert gut["beleg_deckung"] == 0.9 and mit["beleg_deckung"] == 0.1
    # Die Rangfolge folgt der Gewinnrate
    assert [z["gruppe"] for z in d["zeilen"]] == ["Wohnungswirtschaft", "Handel"]

    # Eine Gruppe knapp an der Grundlinie darf NICHT markiert werden.
    # 50 von 100 bei einer Grundlinie von 50 % -> das Intervall enthaelt sie.
    assert not konversion.wilson(50, 100)[0] > 0.5

def test_startbackup_wird_gedrosselt(tmp_path, monkeypatch):
    """Ein Backup je Start kostete gemessen 24,5 s bei 1,82 GB -- und ass die
    Rotation auf: BACKUP_KEEP zaehlt DATEIEN, sechs von sieben Plaetzen waren
    Start-Schnappschuesse. Mit Drossel gibt es hoechstens eins je Fenster."""
    import sqlite3

    from adwatch import backup as bk

    quelle = tmp_path / "quelle.db"
    c = sqlite3.connect(str(quelle))
    c.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY)")
    c.execute("INSERT INTO companies (id) VALUES (1)")
    c.commit(); c.close()

    ziel = tmp_path / "backups"
    monkeypatch.setattr(bk.config, "DB_URL", f"sqlite:///{quelle}")
    monkeypatch.setattr(bk.config, "BACKUP_DIR", ziel)

    erstes = bk.backup_now(tag="startup", hoechstens_alle_h=12)
    assert erstes is not None, "das erste Backup muss geschrieben werden"
    zweites = bk.backup_now(tag="startup", hoechstens_alle_h=12)
    assert zweites is None, "das zweite im selben Fenster muss uebersprungen werden"
    # Ohne Drossel (der naechtliche Lauf) schreibt weiterhin jedes Mal
    assert bk.backup_now(tag="nightly") is not None
    assert len(list(ziel.glob("adwatch_*.db"))) == 2

def test_regexe_enthalten_keine_steuerzeichen():
    """Ein `\\b`, das als Backspace in der Datei landet, macht das Muster still
    wirkungslos.

    Genau das ist passiert: `_ZITAT_DAVOR` stand als
    `(\\x08In:|\\(Ed\\.\\)|\\x08Vol\\.|...)` in der Datei und traf deshalb NIE \u2014
    die Literaturregel lief ins Leere, ohne einen Fehler zu werfen. Ein Muster,
    das nichts findet, sieht aus wie ein Datenbestand ohne Treffer.

    U+FEFF steht mit in der Liste, weil dieselbe Fehlerform ein zweites Mal
    zugeschlagen hat, diesmal am Dateianfang: fuenf Dateien trugen ein BOM,
    `ast.parse` scheiterte an jeder einzelnen, und keine Pruefung sah es. Der
    Waechter gegen unsichtbare Zeichen war selbst blind fuer eins.

    Geprueft werden auch `tools/`: die Datei mit dem kaputten `\\b` lag zwar in
    `adwatch/`, aber das Skript, das sie geschrieben hat, lag daneben.
    """
    import io
    import pathlib

    schlimm = {"\x08": "\\b", "\x0c": "\\f", "\x07": "\\a", "\x0b": "\\v",
               "﻿": "BOM (U+FEFF)"}
    wurzel = pathlib.Path(__file__).resolve().parent.parent
    treffer = []
    for ordner in ("adwatch", "tools"):
        for datei in sorted((wurzel / ordner).rglob("*.py")):
            roh = io.open(datei, encoding="utf-8").read()
            for zeichen in schlimm:
                if zeichen in roh:
                    treffer.append(f"{datei.name}: {schlimm[zeichen]}")
    assert not treffer, "Steuerzeichen in Quelldateien: " + ", ".join(treffer)

def test_alle_quelldateien_sind_parsebar():
    """Jede Datei muss sich als Python lesen lassen, nicht nur ausfuehren.

    Ein BOM stoert den Interpreter nicht, `ast.parse` aber schon. Alles, was den
    Quelltext ANALYSIERT statt ihn auszufuehren -- Werkzeuge zur Suche nach
    totem Code, Linter, dieser Test -- geht an solchen Dateien vorbei und meldet
    dabei keinen Fehler, sondern einfach nichts. Fuenf Module waren so lange
    unsichtbar.
    """
    import ast
    import io
    import pathlib

    wurzel = pathlib.Path(__file__).resolve().parent.parent
    kaputt = []
    for ordner in ("adwatch", "tools", "tests"):
        for datei in sorted((wurzel / ordner).rglob("*.py")):
            try:
                ast.parse(io.open(datei, encoding="utf-8").read())
            except SyntaxError as e:
                kaputt.append(f"{datei.name}: {e.msg}")
    assert not kaputt, "nicht parsebar: " + ", ".join(kaputt)
