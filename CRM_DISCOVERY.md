# CRM / Dataverse Discovery — Fragen für den CRM-Agenten

**Zweck:** Wir bauen ein internes Lead-Generation- und ICP-Tool (AdWatch) für Solarlux.
Es soll (a) Bestandskunden verstehen, (b) daraus ein ideales Kundenprofil je Kanal
ableiten, (c) neue, ähnliche Firmen finden. Dafür brauchen wir die **Datenstruktur**
des CRM — nicht die Inhalte im Detail, sondern **Tabellen, Felder, Beziehungen,
Wertelisten und Mengen**.

**Kontext, was wir schon haben:** Ein Export der View `01. Aktive Firmen`
(Datei „Kunden Handel.xlsx", 3.619 Zeilen) mit den Spalten:
`(Nicht ändern) Firma` (= accountid GUID), `(Nicht ändern) Zeilenprüfsumme`,
`(Nicht ändern) Geändert am` (= modifiedon), SAP Nummer, Firmenname, KV,
Kundensegment, Kundenuntersegment, Vertriebsweg, Straße, Adresse 1: Postleitzahl,
Ort, Land, Telefon 1, E-Mail, Fax, Website, Umsatz aktuelles Jahr bis -4.

**Das Problem, das wir lösen wollen:** In diesem Export ist `Vertriebsweg` zu
99,7 % `Fachhandelsvertrieb` (3.611 von 3.621 DE-Zeilen). `Objektvertrieb` = 1 Zeile,
`Architektenberatung` = 0 Zeilen. Ein zweiter Export (Spanien) enthält dagegen
810 × `Architektenberatung` und 66 × `Objektvertrieb`. **Uns fehlt also die
komplette deutsche Objekt- und Architektenwelt.**

---

## Wie wir die Antworten brauchen

Bitte pro Frage möglichst:

1. **Logische Feldnamen / Schemanamen** (z. B. `sl_vertriebsweg`, `parentaccountid`),
   nicht nur die deutschen Anzeigenamen — wir brauchen sie für OData-Abfragen
   (`$select`, `$filter`).
2. **Tabellen-Logicalnames** (z. B. `account`, `opportunity`, `sl_projekt`).
3. Bei Optionsets (Auswahllisten): **alle Werte + numerische Codes + Anzahl Datensätze je Wert**.
4. **Zeilenanzahl** je Tabelle (grobe Größenordnung genügt).
5. 2–3 **anonymisierte Beispielzeilen**, wenn die Struktur unklar ist.
6. Wenn etwas **nicht existiert**, bitte ausdrücklich „existiert nicht" schreiben —
   das ist für uns genauso wertvoll wie ein Fund.

---

## BLOCK A — Kanal & Segmentierung (höchste Priorität)

Wir müssen wissen, wie die drei Vertriebskanäle (Objektvertrieb / Fachhandel /
Private Endkunden) technisch abgebildet sind.

**A1.** Welches Feld auf `account` enthält den **Vertriebsweg**? Logicalname?
Ist es ein Optionset oder eine Lookup-Tabelle? **Alle Werte + Anzahl je Wert
(gesamt und nur Land = Deutschland).**

**A2.** Es gibt einen Wert **`Weg1`** — bei uns 9 Firmen, alle mit Segment `Objekt`,
alle kaufend. Was bedeutet `Weg1`? Legacy? Testdaten? Bitte klären.

**A3.** Gleiches für **`Kundensegment`** und **`Kundenuntersegment`**:
Logicalnames, alle Optionset-Werte + Anzahl je Wert.
*Warum:* Bei uns ist `Kundenuntersegment` bei 48 % der Zeilen leer — wir müssen
wissen, ob das im CRM auch so ist oder ein Export-Artefakt.

**A4.** Kann eine Firma **mehreren Kanälen** zugeordnet sein (z. B. Fachhandelspartner,
der zusätzlich als Nachunternehmer in Objektprojekten auftritt)? Wenn ja: wie wird
das gespeichert (Mehrfachauswahl, mehrere Rollen, N:N-Tabelle)?
*Warum:* Entscheidet, ob eine Firma ein oder mehrere Profile braucht.

**A5.** Gibt es ein Feld für **Partnerstatus / Kundenstatus** (aktiver Partner,
Interessent, inaktiv, gesperrt)? Und ein Feld für **Partnerlevel** (z. B. Premium-
partner, Fachpartner)? Logicalnames + Werte.

---

## BLOCK B — Die Objekt-Hierarchie (der größte blinde Fleck)

Fachlicher Hintergrund, den wir verstanden haben: Bei einem Bauprojekt verkauft
Solarlux entweder **direkt an den Generalunternehmer** (bester Fall, beste Marge),
oder an einen **Nachunternehmer darunter**, oder an einen **Nachunternehmer noch
eine Ebene tiefer** — je nach Projektgröße. Diese Hierarchie ist für uns zentral.

**B1.** Wie wird diese **Projekt-Hierarchie** im CRM abgebildet? Bitte prüfen,
welche der Varianten zutrifft:
- (a) `account.parentaccountid` (Über-/Untergeordnete Firma)
- (b) `connection` / `connectionrole` (Verbindungen mit Rollen)
- (c) eine **eigene Projekt-Tabelle** mit Beteiligten-Zeilen (N:N)
- (d) Felder direkt auf `opportunity` (z. B. „Generalunternehmer", „Bauherr")
- (e) gar nicht strukturiert (nur Freitext/Notizen)

**B2.** Wenn es eine **Beteiligten-/Rollenstruktur** gibt: welche **Rollen** sind
definiert? (Bauherr/Investor, Architekt, Generalunternehmer, Nachunternehmer,
Fachplaner, Metallbauer …) Bitte Liste + Anzahl.
*Warum:* Wir wollen messen, auf welcher Ebene Solarlux verkauft und wie sich das
auf Marge/Gewinnwahrscheinlichkeit auswirkt.

**B3.** Ist erkennbar, **auf welcher Ebene Solarlux tatsächlich verkauft hat**
(direkt GU vs. NU vs. NU-NU)? Gibt es dafür ein Feld, oder ergibt es sich nur aus
der Rolle des Rechnungsempfängers?

**B4.** Können wir zu einem Projekt **alle beteiligten Firmen** auslesen — und
umgekehrt zu einer Firma **alle Projekte**? Über welche Tabelle/Beziehung?

---

## BLOCK C — Architekten / Architektenberatung

Fachlicher Hintergrund: Architekten **kaufen nie selbst**. Sie beraten (überwiegend
im Objektgeschäft, teils bei Privatendkunden), sie sprechen mit Solarlux, und über
sie entsteht der Kontakt zum tatsächlichen Käufer, weil mit dem Architekten schon
etwas vereinbart wurde. Sie stehen im CRM.

**C1.** Wie sind Architekten gekennzeichnet? Über `Vertriebsweg =
Architektenberatung`, über `Kundensegment = Architekten`, über eine eigene Tabelle
oder über eine Rolle? Wie viele Architekten-Datensätze gibt es (Deutschland)?

**C2.** Gibt es eine **Verknüpfung Architekt → Projekt / Opportunity / Angebot**?
Wenn ja: welches Feld oder welche Beziehung? Ist sie gepflegt (bei wie viel Prozent
der Opportunities ist ein Architekt hinterlegt)?
*Warum:* Ohne diese Kante können wir den Wert eines Architekten nicht messen. Das
ist die wichtigste Einzelfrage in diesem Block.

**C3.** Gibt es eine Verknüpfung **Architekt → tatsächlicher Käufer** (die Firma,
die dann bestellt hat)? Oder ist der Zusammenhang nur über das gemeinsame Projekt
herstellbar?

**C4.** Wird irgendwo festgehalten, ob eine **Ausschreibung/Spezifikation
Solarlux-Produkte enthält** („ausgeschrieben", „geplant mit", Leistungsverzeichnis)?
*Warum:* Das wäre die Erfolgsmetrik für Architekten — nicht Umsatz.

---

## BLOCK D — Projekte, Opportunities, Angebote

**D1.** Welche Tabellen existieren für das Projektgeschäft? Bitte Logicalnames +
Zeilenanzahl. Vermutet: `opportunity`, `quote`, evtl. eine eigene
Projekt-/Bauvorhaben-Tabelle (custom, Präfix wie `sl_…`).
*Hinweis:* Es existieren Exporte namens `projektakte_full.xlsx`,
`opportunities.csv`, `sap_quotes.csv`, `quote_dates.csv`, `won_dates.csv`,
`open_activity.csv`, `parents.csv`, `portal.csv` — falls du weißt, aus welchen
Tabellen/Views die stammen, wäre das extrem hilfreich.

**D2.** Für `opportunity` (bzw. das Äquivalent): welche Felder gibt es für
**Volumen/Wert**, **Phase/Status**, **Gewonnen/Verloren + Verlustgrund**,
**Datum (erstellt / Abschluss)**, **Projektadresse/Region**, **Projekttyp**
(Neubau/Sanierung, Wohnbau/Gewerbe/Hotel)?

**D3.** Wie hängen `opportunity` / `quote` an der Firma? Über `customerid`
(`accountid`)? Und hängen sie zusätzlich an einem Projekt?

**D4.** Gibt es **Angebotspositionen** (`quotedetail` / Opportunity Products) mit
**Produktbezug**? Wenn ja: welches Feld identifiziert die **Produktfamilie/das
System** (z. B. SL 25, SL 97, cero, Wintergarten, Terrassendach, Glasdach)?
*Warum:* Damit können wir „welche Partner kaufen die Premium-Systeme" messen —
das unterscheidet gute von durchschnittlichen Kunden.

**D5.** Gibt es **Gewinnquoten** pro Firma bzw. pro Architekt auswertbar
(Anzahl Angebote vs. Anzahl Aufträge)?

---

## BLOCK E — Umsatz & Auftragshistorie

**E1.** Woher kommen die Felder **`Umsatz aktuelles Jahr` bis `-4`** auf der Firma?
Rollup-Feld, SAP-Schnittstelle, berechnetes Feld? Logicalnames?
Umfasst der Umsatz **beide Kanäle** (Fachhandel + Objekt) oder nur einen?
*Warum:* Wir nutzen diese 5 Spalten aktuell als Erfolgssignal. Bei uns ist der
Median-Umsatz eines „kaufenden" Kunden nur **514 €**, das oberste Dezil macht
**80,7 %** des Umsatzes. Wir müssen wissen, ob das echt ist oder ein Artefakt.

**E2.** Gibt es **Auftrags-/Rechnungsdaten auf Transaktionsebene** (Datum, Betrag,
Produkt) — im CRM oder nur in SAP? Wenn im CRM: Tabelle + Felder.
*Warum:* Kauffrequenz und Wiederkaufverhalten sind viel aussagekräftiger als ein
Jahresbetrag. Unsere ICP-Definition soll „hat in ≥3 von 5 Jahren gekauft" nutzen.

**E3.** Gibt es **Margen- oder Preisgruppen-/Rabattinformationen** pro Kunde?
*Warum:* „Bester Kunde" sollte Marge berücksichtigen, nicht nur Umsatz.

---

## BLOCK F — Identität, Schlüssel, Dubletten

**F1.** Bestätigung: ist `accountid` (die GUID in `(Nicht ändern) Firma`) der
**stabile Primärschlüssel**, der sich nie ändert? (Wir wollen ihn als
`crm_id` speichern und darauf matchen.)

**F2.** Ist **SAP Nummer** ein eigenes Feld auf `account`? Logicalname?
Bei uns fehlt sie in 917 von 1.000 Zeilen eines Exports — ist sie im CRM
tatsächlich so oft leer, und wann wird sie gesetzt (erst ab erstem Auftrag)?

**F3.** Gibt es im CRM **Dublettenprüfung / Merge-Historie**? Können zwei Accounts
dieselbe Firma sein?

**F4.** Gibt es Felder für **Konzern-/Filialstruktur** (`parentaccountid`,
Filialen, Zentralregulierer)? Wie viele Accounts haben einen Parent?
*Warum:* Ein Filialverbund verhält sich anders als ein Einzelbetrieb; für
Fachhandel ist Zentralregulierung margen-relevant.

---

## BLOCK G — Views & Exporte

**G1.** Welche **gespeicherten Views** auf `account` gibt es, insbesondere:
Gibt es ein Pendant zu `01. Aktive Firmen` für **Objektvertrieb** und für
**Architekten**? Wie heißen sie, wie viele Zeilen haben sie?
*Warum:* Genau diese fehlen uns. Das ist die konkreteste, direkt nutzbare Antwort
aus diesem ganzen Dokument.

**G2.** Nach welchen Kriterien filtert `01. Aktive Firmen`? („aktiv" = statecode?
= Umsatz? = Vertriebsweg?)

---

## BLOCK H — Lesender Zugriff für die Synchronisation

Ziel: AdWatch soll Firmen **lesend** aus dem CRM holen (Vollabgleich nächtlich +
Einzelsatz-Aktualisierung auf Knopfdruck) und niemals zurückschreiben.

**H1.** Ist die **Dataverse Web API** aus dem Netz erreichbar, und wie lautet die
Org-URL (`https://<org>.crm4.dynamics.com/api/data/v9.2/`)?

**H2.** Ist **Change Tracking** auf `account` aktiviert? (Ermöglicht echte Deltas.)
Falls nein: reicht `$filter=modifiedon gt <ISO-Zeitstempel>`?

**H3.** Welche Variante ist im Tenant realistisch:
- (a) **App-Registrierung + Application User** mit Leserechten nur auf `account`
  (sauberste Lösung, braucht Admin)
- (b) **Delegierter Benutzerzugriff** (App läuft mit meinen Rechten)
- (c) **Power-Automate-Flow als Lese-Proxy** (HTTP-Trigger, „Zeilen auflisten",
  läuft unter meinem Konto — ohne Admin machbar)

Für (a): welche Sicherheitsrolle wäre nötig, und wer erteilt sie?
Für (c): gibt es Einschränkungen/Governance-Regeln für HTTP-getriggerte Flows?

**H4.** Gibt es **API-Limits** (Service Protection Limits), die wir bei ~4.000
Datensätzen beachten müssen?

---

## Was wir mit den Antworten machen

- **Block A/G** → wir holen die fehlenden Objekt- und Architekten-Firmen und bauen
  **je Kanal ein eigenes ICP** (heute existiert nur eines, gebaut auf Fachhandel).
- **Block B/C** → wir modellieren die Projekt-Hierarchie als Graph und bewerten
  Architekten nach **Spezifikationserfolg statt Umsatz** (heute stehen 810
  Architekten mit 0 € Umsatz als „schlechte Kunden" im System).
- **Block D/E** → wir ersetzen „hat irgendwas gekauft" durch eine belastbare
  Erfolgsdefinition (Wiederkauf, Marge, Premium-Produktmix).
- **Block F/H** → stabile Identität (`crm_id`) und ein lesender Sync statt
  manueller Excel-Exporte.

**Priorität, wenn die Zeit knapp ist:** G1, B1, C2, A1, E2.
