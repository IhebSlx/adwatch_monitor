# Erledigte Werkzeuge

Skripte, deren Aufgabe abgeschlossen ist. Sie liegen hier statt im Papierkorb,
weil einige davon ein **Verfahren** sind und nicht nur Code — beim nächsten Land
werden sie wieder gebraucht.

Sie werden von nichts importiert. Wer eines benutzt, ruft es von Hand auf.

| Skript | Wozu | Warum hier |
|---|---|---|
| `urteile_anwenden.py` | im Chat gelesene Urteile in die Datenbank schreiben | Spanien abgeschlossen: alle 562 offenen Seiten gelesen, `offen()` gibt 0. **Wieder nötig, sobald das API-Guthaben mitten in einem Lauf ausgeht** — das ist der Ersatzweg. |
| `offene_faelle.py` | offene Projektseiten zum Nachlesen aufbereiten | Gegenstück zu `urteile_anwenden.py`, dieselbe Lage. |
| `tiefenlauf_pruefen.py` | Stichprobe: Seite neu holen und zeigen, was der Crawler gelesen hat | Das Prüfverfahren, das aus „2 von 14 richtig" die Ortszonen-Korrektur gemacht hat. Vor jedem neuen Land einmal laufen lassen. |
| `relevanz_pruefen.py` | Relevanzregeln gegen echte Ausgänge prüfen | Einmalige Messung, Ergebnis ist eingearbeitet. |
| `spanien_export.py` | erste Fassung des Spanien-Exports | Von `tools/spanien_excel.py` abgelöst. |
| `make_icon.py` | das Desktop-Symbol erzeugen | Einmalig gelaufen. Zieht `PIL`, das nicht in `requirements.txt` steht — deshalb nicht im aktiven Ordner. |
| `laenderlauf_start.py` | Starter für den Länderlauf | Von `tools/ueber_nacht.py` abgelöst. |
| `tiefenlauf_start.py` | Starter für den Tiefenlauf | Von `tools/spanienlauf.py` abgelöst. |

## Was im aktiven Ordner bleibt

`spanienlauf.py` (Vorabtest + Tiefenlauf), `ueber_nacht.py` (die ganze Kette
unbeaufsichtigt), `spanien_excel.py`, `spanien_bueros.py`, `spanien_bericht.py`
(die drei Ergebnisdateien) und `kv_holen.py` (holt den Kundenverantwortlichen
aus dem CRM nach — läuft, sooft neue Firmen dazukommen).
