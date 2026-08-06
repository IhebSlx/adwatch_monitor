# AdWatch — Betriebsanleitung für Kollegen

*Stand: 2026-08-06. Für Nicht-Techniker geschrieben. Wenn etwas hier nicht
stimmt oder fehlt: bitte direkt in dieser Datei korrigieren.*

## Was ist das?

AdWatch beantwortet drei Fragen über unsere (potenziellen) Partner:

1. **Wer sind sie?** — 46.000+ Firmen aus dem CRM plus recherchierte Listen,
   mit geprüfter Website-Identität und automatisch angereicherten Profilen
   (Produkte, Positionierung, eigene Fertigung, Fremdmarken …).
2. **Was tun sie gerade?** — Wer schaltet aktuell Werbung auf Meta/Google?
   Ein Interessent, der Geld für Anzeigen ausgibt, ist im Markt aktiv.
3. **Wen sollten wir anrufen?** — Chancen-Tab (stille Kunden, die gegen ihren
   eigenen Bestellrhythmus überfällig sind) und ICP-Ranking.

## Starten & Stoppen

```
C:\Users\<du>\AppData\Local\miniconda3\envs\adtracker\python.exe -m uvicorn adwatch.web:app --host 127.0.0.1 --port 8000
```

Dann im Browser: **http://localhost:8000**. Stoppen: das Konsolenfenster
schließen (Strg+C). **Nie stoppen, während ein Job läuft** (Fortschrittsleiste
oben prüfen) — laufende Jobs werden sonst abgebrochen.

## Die Tabs, in Arbeitsreihenfolge

| Tab | Wozu |
|---|---|
| **Dashboard** | Überblick: aktive Werbetreibende, interessante Partner |
| **Companies** | Alle Firmen, Filter, Detail-Drawer, Identitäts-Check starten |
| **Chancen** | Stille Kunden nach eigenem Bestellrhythmus — die Rückgewinnungsliste |
| **Prüfen** | Website-Kandidaten bestätigen/ablehnen (1 Klick, Hinweis steht dabei) |
| **ICP** | Wer sieht aus wie unsere besten Kunden? (liest den Backtest — Zahl ohne Backtest nicht vortragen!) |
| **Reports** | PDF-Berichte erzeugen und per E-Mail versenden |
| **Logs** | Was wurde wann erzeugt/versendet; Job-Historie |
| **Settings** | API-Schlüssel, Power-Automate-Flows, Zeitplan |

## Was kostet Geld (und was nicht)

| Aktion | Kosten | Anbieter |
|---|---|---|
| Website-Identität prüfen (Crawl) | **kostenlos** | — |
| Website suchen (Firma ohne Domain) | ~0,1 Cent/Firma | Serper |
| Anreicherung (Beschreibung, Produkte …) | ~0,3 Cent/Firma | Anthropic (Haiku) |
| Meta-/Google-Anzeigen abrufen | ~1–3 Cent/Firma | Apify |
| CRM-Sync (Delta oder Scope-Load) | **kostenlos** | Power Automate |

Faustregel: eine komplette Pipeline über ~300 Firmen kostet **unter 10 €**.
Nichts läuft von allein außer dem eingestellten Zeitplan (Settings).

## Die eiserne Regel: Identität vor Geld

Eine Website/Facebook-Seite gilt erst als „diese Firma", wenn ein **harter
Beweis** vorliegt (eigene Telefonnummer, PLZ+Straße, PLZ+Name auf der Seite)
oder ein Mensch sie im **Prüfen**-Tab bestätigt hat. Vorher wird von ihr
weder angereichert noch werden ihr Anzeigen zugeordnet. Deshalb gibt es den
Status `conflict`: „Seite gelesen, gehört nachweislich jemand anderem" —
solche Domains niemals von Hand auf verified setzen.

## Wöchentliche Routine (15 Minuten)

1. **Prüfen**-Tab leeren (Ja/Nein-Klicks).
2. **Chancen**-Tab: Liste an Vertrieb geben — Filter „nur mit Werbung" zuerst.
3. Bericht unter **Reports** erzeugen, Empfänger anhaken, senden.
4. CRM-Delta läuft per Zeitplan; Status in **Logs** kontrollieren.

## Wenn etwas klemmt

- **Job hängt scheinbar** → Logs-Tab: steht der letzte Eintrag > 15 min still,
  Job abbrechen (Kreuz), Server neu starten, Job neu anlegen. Jobs sind
  idempotent — nichts geht doppelt verloren.
- **„database is locked"** → es läuft schon ein Job oder Skript. Warten,
  nicht parallel starten.
- **Datenbank kaputt?** → `data/backups/` enthält tägliche Snapshots.
  Server stoppen, neueste `.db` nach `data/adwatch.db` kopieren, starten.
- **Zahlen wirken falsch** → zuerst ins Feld `identity_status` bzw. die
  Provenienz im Firmen-Drawer schauen: jede automatische Entscheidung trägt
  ihre Begründung bei sich.

## Grenzen, ehrlich

- Der ICP **rankt aktuell nicht** innerhalb von Handel+Verarbeiter (Backtest
  0,63×). Segment-Ebene ja, Feinranking nein — nicht überverkaufen.
- Anzeigen-Daten decken nur Firmen mit bestätigter Seite/Domain ab.
- Beschreibungen fehlen bei JS-lastigen Websites (~Hälfte der spanischen).

## Schlüssel & Zugänge (in Settings, nie im Code)

Serper, Anthropic, Apify: eigene Konten, Schlüssel in **Settings** eintragen.
Power-Automate-Flows: je Rolle eine URL (Bericht-Mail, CRM-Abfrage) — die URL
ist ein Geheimnis, nicht weitergeben, bei Verdacht im Flow neu generieren.
