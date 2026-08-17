# Methodik — wie AdWatch zu seinen Zahlen kommt

Für Kolleginnen und Kollegen, die wissen wollen, **was hier eigentlich gerechnet
wird und warum man den Ergebnissen trauen kann** — oder an welchen Stellen
ausdrücklich nicht.

Ergänzt `ICP-STRATEGY.md` (die Befunde) um das Handwerk dahinter. Stand
2026-08-17.

---

## 1. Was hier überhaupt vorhergesagt wird

Alle Modelle beantworten **eine** Sorte Frage:

> Hier sind zwei Firmen — welche wird eher das tun, was wir wollen?

Nicht „kauft Firma X" (das kann niemand beantworten), sondern **Reihenfolge**.
Daraus folgt alles Weitere, insbesondere wie gemessen wird.

Es gibt **vier** solcher Fragen, nicht eine — das war die wichtigste Einsicht
des Projekts:

| Frage | Modell | Güte |
|---|---|---|
| Welches offene Objekt gewinnen wir? | Projekt-Profil (IPP) | Lift **13,7×** |
| Wer im Trichter wird Kunde? | Funnel-Triage | AUC **0,753** |
| Wer im Bestand bricht ab? | Kunden-Fortsetzung | AUC **0,797** |
| Ist diese fremde Firma ein guter Partner? | Kalt-Akquise | AUC **0,63** |

## 2. AUC — die Kennzahl, in einem Satz

Man ziehe **zufällig** eine Firma, die gekauft hat, und eine, die nicht gekauft
hat. **AUC ist die Wahrscheinlichkeit, dass das Modell der kaufenden die höhere
Punktzahl gibt.**

| AUC | Bedeutung |
|---|---|
| 0,50 | Münzwurf — das Modell weiß nichts |
| 0,63 | in 63 von 100 Paaren richtig — real, aber schwach |
| 0,80 | in 80 von 100 richtig — brauchbar |
| 1,00 | nie falsch |

**Lift** ist dasselbe in Vertriebssprache: kaufen 11,3 % aller Händler, im
obersten Zehntel des Modells aber 18,1 %, dann ist der Lift **1,61** — im
obersten Zehntel findet man 1,6-mal so viele Käufer wie beim zufälligen Anrufen.

## 3. Die vier Fallen — und was dagegen getan wird

### 3.1 Nachträgliches Wissen („Leakage")

Das Merkmal `kv` (zuständiger Solarlux-Mitarbeiter) sagte den Kauf fast perfekt
voraus. Natürlich: **eine Firma bekommt einen Betreuer, WEIL sie schon Kunde
ist.** Das Modell hat nicht die Zukunft vorhergesagt, sondern die Antwort
abgelesen.

**Test:** für jedes Feld messen wir *Füllgrad bei Käufern ÷ Füllgrad bei
Nicht-Käufern*.

| Merkmal | Quotient | Konsequenz |
|---|---:|---|
| `kv` | **150×** | entfernt |
| Anreicherungsfelder (DE) | **200–220×** | entfernt |
| Segment, PLZ, Ort, Land | 1,00 | behalten |

### 3.2 Nachträglich gesetzte WERTE — die schwerere Falle

Ein Feld kann für alle gefüllt sein, sein *Wert* aber erst nachträglich gesetzt
werden. Zwei Fälle, beide zunächst als starke Prädiktoren aufgetreten:

* **`Vertriebsweg = Direktvertrieb`** — 55 Firmen, 54,5 % Kaufquote gegen 13,5 %.
  Das beschreibt **unsere Beziehung**, nicht die Firma. Entfernt.
* **`Untersegment = leer`** — 209 Firmen, 42,6 % Kaufquote. Das ist
  Import-Herkunft. Und es ist die gefährliche Richtung: eine im Internet neu
  gefundene Firma hat **ebenfalls** kein Untersegment und bekäme aus einem nicht
  übertragbaren Grund eine hohe Punktzahl. Entfernt.

Preis der Ehrlichkeit: **0,025 AUC weniger.** Genau das war der Zweck.

### 3.3 Die Zukunft mit der Zukunft vorhersagen

Wer Daten zufällig in Training und Test teilt, lässt das Modell aus 2025 lernen,
um 2023 „vorherzusagen". So arbeitet die Wirklichkeit nie.

Deshalb wird **nach Zeit geschnitten**: trainiert wird auf allem vor einem
Stichtag, geprüft nur auf danach. Zusätzlich an **zwei** Stichtagen
wiederholt (2023 und 2024) — Ergebnisse 0,598 und 0,590, also stabil und kein
Zufallstreffer eines Datums.

**Geografischer Holdout:** ganze Postleitzahl-Regionen werden beim Training
versteckt und nur dort getestet. Das prüft, ob das Modell *Struktur* gelernt hat
(„Fensterbauer kaufen") oder *auswendig* („Region DE81 kauft"). Der Abfall war
gering (0,598 → 0,588) — also Struktur.

### 3.4 Rauschen für einen Befund halten

Eine einzelne Zahl ist kein Ergebnis. Jede AUC wird **gebootstrapt**: dieselbe
Messung auf 2.000 Zufallsstichproben der Daten, um zu sehen, wie stark sie
schwankt.

Das hat eine Überinterpretation verhindert: „Gold-artige" Partner erreichten
**0,686** gegen 0,608 — sieht nach einem klaren Gewinn aus. Das Schwankungsband
lag aber bei 0,633–0,736 und überlappte im strengeren geografischen Test
vollständig mit dem schwächeren Modell. Bei 85 Positiven ist das **ein Hinweis,
kein Beleg** — und steht so im Bericht.

## 4. Warum „mehr Daten" nicht hilft — zwei unabhängige Belege

**Lernkurve.** AUC gegen Trainingsgröße:

| Zeilen | 848 | 1.697 | 2.971 | 4.244 | 5.942 | 7.215 | 8.489 |
|---|---:|---:|---:|---:|---:|---:|---:|
| AUC | 0,565 | 0,577 | 0,583 | 0,584 | 0,592 | 0,600 | **0,605** |

Zehnfache Datenmenge bringt +0,04; der letzte Zuwachs +0,005. Die Kurve ist flach.

**Andere Zielgröße.** Drei Varianten (irgendeine Bestellung / materieller Auftrag
/ oberstes Umsatzquartil) liegen alle innerhalb von 0,03.

Hilft weder mehr Datenmenge noch eine andere Frage, bleibt nur eins: **in den
Spalten steckt nicht mehr Information.** Der Gegenbeweis von der anderen Seite:
wo **Verhaltensdaten** vorliegen, bringen sie **+0,14 bis +0,16** — zehn- bis
dreißigmal mehr als alles andere.

## 5. Warum kein „richtiges" Machine Learning ausgeliefert wird

Gemessen wurde **mit** ML (Gradient Boosting, logistische Regression,
Kreuzvalidierung, Bootstrap). Ausgeliefert werden **Lift-Punktetabellen**. Gründe,
in dieser Reihenfolge:

1. **Es gewinnt nicht.** GBM gegen logistische Regression über alle Versuche:
   0,618/0,623 · 0,635/0,630 · 0,604/0,598 — Unterschied ±0,01, also Rauschen.
2. **Es gibt strukturell nichts zu finden.** Gradient Boosting lebt von
   *Wechselwirkungen* zwischen vielen Merkmalen. Hier gibt es **vier
   kategoriale Spalten**. Ein Baumverfahren, eine Regression und eine
   Nachschlagetabelle schätzen darauf dieselben Wahrscheinlichkeiten.
3. **Eine Punktetabelle kann man bestreiten.** „Fensterbau 1,21× weil 15,4 % von
   648 gekauft haben" ist nachrechenbar. „Das Modell sagt 0,73" nicht. Ein
   Modell, dem niemand widersprechen kann, wird nicht benutzt.

**Kein SHAP** — aus demselben Grund: bei einem additiven Modell **ist** der
Log-Lift jedes Merkmals bereits sein exakter Beitrag; genau das steht in der
Spalte „Warum". SHAP wäre der teurere Weg zur selben Zahl, und es gilt zudem bei
korrelierten Merkmalen als unzuverlässig (Kumar u. a. 2020) — Region und Branche
hängen hier stark zusammen. **Sobald die Anreicherung 20+ Merkmale liefert, wird
SHAP das richtige Werkzeug.**

## 6. Zwei kleinere Verfahren, die trotzdem wichtig sind

**Laplace-Glättung.** Eine Branche mit 3 Firmen, von denen alle 3 gekauft haben,
ist keine 100-%-Branche — bei 3 Beobachtungen weiß man fast nichts. Die Formel
`(Käufer + 5 × Durchschnitt) / (Gesamt + 5)` zieht kleine Gruppen zum Mittel,
und zwar in dem Maß, in dem ihnen Beleg fehlt.

**Poolen statt Trennen.** Ein eigenes Modell je Land klingt richtig, starb aber
am Test: **gepoolt 0,611, getrennt 0,607** — und Österreich wurde mit eigenem
Modell schlechter (0,541 → 0,510). Begründung: dass Spanien weniger kauft, ist
ein Unterschied in der **Basisrate** — dafür genügt ein Parameter. Trennen lohnt
nur bei unterschiedlichen **Zusammenhängen**. Faustregel für künftige
Trennungswünsche: **~500 Positive je Zelle**; nur Deutschland erfüllt das.

## 7. Datenqualität: was bereinigt wurde

**0-Euro-Bewegungen sind keine Käufe.** 14.049 der 91.992 Bestellereignisse
stehen auf 0 EUR (Garantie, Muster, Ersatz); **486 Firmen haben ausschließlich
solche** und galten damit als Kunden. Wer eine Garantiegutschrift als Erfolg
zählt, trainiert das Modell darauf, Reklamationen vorherzusagen.

Die Zielgröße verlangt jetzt mindestens **ein Ereignis mit Betrag > 0**:

| | alt | neu |
|---|---:|---:|
| „Käufer" unter 11.319 Händlern | 1.369 | 1.274 |
| Basisrate | 12,09 % | 11,26 % |
| **AUC** | 0,617 | **0,629** |
| Lift oberstes Dezil | 1,45× | **1,61×** |

Die Rangfolge der Branchen bleibt unverändert; alle verschieben sich leicht nach
unten. Durch einen Test abgesichert.

## 8. Wie gecrawlt wird (und warum so)

Zwei Stufen, bewusst getrennt nach Kosten:

1. **Einfacher Abruf** (`requests`) — schnell, kostenlos, reicht für ~95 % der
   Seiten.
2. **Echter Browser** (Playwright/Chromium) — **nur** wenn Stufe 1 weniger als
   **400 Zeichen** sichtbaren Text liefert. Genau das passiert bei
   JavaScript-Seiten (Single-Page-Apps): der Abruf gelingt, liefert HTTP 200 und
   **null Text**, weil der Inhalt erst im Browser entsteht. Ohne Stufe 2 fiele
   jede solche Firma stillschweigend aus allen Listen.

Stufe 2 repariert außerdem die **Linkerkennung**: die Navigation einer
JavaScript-Seite existiert im Rohtext nicht, also gingen ohne Rendering nicht nur
die Startseite, sondern alle Unterseiten verloren.

Weitere Festlegungen: `robots.txt` wird respektiert · SSRF-Schutz (nur öffentlich
erreichbare Adressen) · harte Kappung 1,5 MB / 25 s je Seite · Pause zwischen
Seiten derselben Website · **Impressum/Kontakt wird gezielt mitgelesen**, weil
dort in Deutschland Telefon und Anschrift stehen (das hob die harte
Telefon-Prüfung von 7/18 auf 12/18).

## 9. Bekannte Grenzen — ausdrücklich, nicht versteckt

| Grenze | Wirkung | behebbar? |
|---|---|---|
| **Kein Anlagedatum** der Firmenstammsätze | 2024 angelegte Firmen sehen aus wie „hat 2019–2022 nicht gekauft" | ja, `createdon` aus Dataverse |
| **Januar 2023 = CRM-Start** | Verkaufschancen erst ab 2023-01-02; ältere Bestellungen stammen aus einem anderen System | nein |
| **Auswahl im Bestand selbst** | die 46.485 Firmen sind die, die jemand ins CRM eingetragen hat — keine Zufallsstichprobe des Marktes | nein; begrenzt die Übertragbarkeit auf die Zwillingssuche |
| **Rechtszensierung** | wer 2026 gewonnen wurde, hatte weniger Zeit, Wert zu zeigen | strukturell |
| **Feldbedeutungen erschlossen**, nicht dokumentiert | z. B. `Vertriebsweg` aus 55 Zeilen gedeutet | ja, durch Rückfrage im Vertrieb |
| **Viele Analysen an einem Tag** | manches könnte Zufall sein | teilweise: das Hauptexperiment wurde **vorab** registriert |

**Und eine Einschränkung des laufenden Experiments, die wir selbst gefunden
haben:** angereichert wird **heute** (2026), vorhergesagt werden Käufe aus
2023–2026. Eine Firma, die 2023 Partner wurde, schreibt heute womöglich
„Solarlux-Partner" auf ihre Website — Felder wie `mentions_solarlux` wären dann
**direktes Rückwärtswissen**. Die Auswertung erfolgt deshalb **zweimal**: mit und
ohne alle Solarlux-bezogenen Felder. Nur die strenge Zahl darf die
44-Euro-Entscheidung tragen. (Für den Produktivbetrieb spielt das keine Rolle:
bei einem neuen Interessenten ist die heutige Website genau die richtige Quelle.)

## 10. Power Automate — muss der Flow geändert werden?

Kurz: **für neue FELDER nein, für neue TABELLEN ja — eine Zeile.**

Der Flow ist ein generischer Dataverse-Proxy:
`{entity, select, filter, top} → {value: [Zeilen]}`. In der App sind
`select`, `filter` und `top` dynamisch verdrahtet.

**Aber der Tabellenname steht im Flow fest auf `accounts`.** Deshalb:

| Vorhaben | Flow-Änderung nötig? |
|---|---|
| Neue Felder auf Firmen (`createdon`, Gold-Stufe, `numberofemployees`) | **nein** — nur die `select`-Liste in `crm_accounts.select_fields()` |
| Neue Tabellen (`leads`, `phonecalls`, `tasks`, `appointments`) | **ja** — eine Änderung |

Die Änderung: in der Aktion *„Zeilen aus der ausgewählten Umgebung auflisten"*
den **Tabellennamen** von fest `accounts` auf den Ausdruck
`@{triggerBody()?['entity']}` umstellen. Die App sendet `entity` bereits mit —
**am Code ist nichts zu tun.**

Nicht vergessen (kostete beim ersten Mal Stunden): Umgebung muss
**`Solarlux Prod`** sein, nicht die Standardumgebung (deren Datenbank ist leer
und liefert 0 Zeilen **ohne Fehler**); *Sortieren nach* muss leer bleiben;
`select` muss eine **komma-getrennte Zeichenkette** sein, kein JSON-Array.

**Zugriff bleibt lesend. Es wird nie nach Dataverse geschrieben.**

## 11. Wo das alles steht

| Was | Wo |
|---|---|
| Befunde und Empfehlungen | `ICP-STRATEGY.md` |
| Diese Methodik | `METHODIK.md` |
| Projekt-Profil (IPP) | `adwatch/insights/ipp.py` |
| Die drei Firmen-Profile | `adwatch/insights/profiles.py` |
| Datenqualitäts-Reparaturen | `adwatch/dataquality.py` |
| Feindlicher Erstbefund | `AUDIT.md`, `adwatch/audit.py` |
| Anreicherung + Crawler | `adwatch/enrich/` |
| Regeln, die überall gelten | `adwatch/scope.py` |

Jeder Befund in diesem Dokument ist durch einen Test abgesichert
(`tests/test_core.py`, aktuell 143) oder durch ein Skript reproduzierbar.
