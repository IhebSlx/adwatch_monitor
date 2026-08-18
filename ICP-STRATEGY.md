# ICP-Strategie — was messbar geht, was nicht, und warum

Stand 2026-08-13. Alle Zahlen sind gemessen und mit dem Code in
`scratchpad/a1..a5` bzw. `adwatch/insights/` reproduzierbar. Wo eine Zahl
geschätzt ist, steht es dabei.

---

## 0. Die Kurzfassung für die Geschäftsführung

Es gibt **nicht ein ICP, sondern vier Entscheidungen** mit sehr
unterschiedlicher Vorhersagbarkeit. Sie in einen Topf zu werfen war der
eigentliche Fehler der bisherigen Versuche.

| Entscheidung | Frage | Güte (AUC) | Nutzbar? |
|---|---|---|---|
| **Projekt-Profil (IPP)** | Welches offene Objekt gewinnen wir? | Lift **13,7×** | **Ja, ausgeliefert** |
| **Funnel-Triage** | Wer im Trichter wird Kunde? | **0,753** | **Ja, sofort** |
| **Kunden-Fortsetzung** | Wer kauft weiter / bricht ab? | **0,797** | **Ja, sofort** |
| **Kalt-Akquise-ICP** | Welche fremde Firma ist ein guter Partner? | **0,59–0,60** | Nur als Vorsortierung |

Der rote Faden: **Verhaltensdaten schlagen Stammdaten um +0,14 bis +0,16 AUC** —
und zwar in jedem einzelnen Fall. Die Kalt-Akquise ist genau der Fall, in dem
wir keine Verhaltensdaten haben. Das ist kein Modellproblem und kein
Datenmengenproblem, sondern ein Merkmalsproblem — mit einem konkreten,
billigen Ausweg (§6).

---

## 1. Erste Korrektur: die Basisrate war falsch

Jede frühere ICP-Analyse startete mit „87 % der Händler kaufen ohnehin, es gibt
kaum Negativbeispiele". Diese Zahl stammt aus dem alten 4.618-Zeilen-Export —
also aus einer Population, die **selbst schon nach Kundenstatus gefiltert war**
(sie enthielt die Firmen im Anzeigen-Monitoring, und das waren die aktiven
Partner). Auf der vollständigen Basis:

| Segment | n | Käufer | Basisrate |
|---|---:|---:|---:|
| Verarbeiter | 7.615 | 2.683 | **35,2 %** |
| Handel | 7.848 | 2.333 | **29,7 %** |
| Baudienstleister | 5.020 | 463 | 9,2 % |
| Architekten | 20.722 | 94 | **0,5 %** |

Händler + Verarbeiter zusammen: **32,4 %** (5.016 von 15.463), Deutschland
36,8 %. Das ist eine gesunde Basisrate — bei 32 % trägt eine Ja/Nein-Frage rund
0,9 Bit Information; bei 87 % wären es 0,55. **Die Annahme „zu wenig
Negativbeispiele" war schlicht falsch.** Die Frage ist damit statistisch
wohlgestellt, und ein schwaches Ergebnis muss eine andere Ursache haben.

Nebenbei bestätigt: Architekten kaufen praktisch nie (0,5 %). Sie gehören nicht
in ein Kunden-ICP, sondern in das Projekt-Profil — als Beteiligte, nicht als
Käufer.

## 2. Zweite Korrektur: die Merkmale waren vergiftet

Ein Merkmal ist nur brauchbar, wenn es für die **gesamte** Population existiert.
Sonst lernt das Modell „hat Merkmal → kauft", was in Wahrheit „wir haben die
angereichert, an die wir ohnehin verkaufen" bedeutet. Gemessen als
*Verfügbarkeitsquotient* = Füllgrad(Käufer) / Füllgrad(Nicht-Käufer),
Deutschland, Händler + Verarbeiter:

| Merkmal | Füllgrad gesamt | Quotient | Urteil |
|---|---:|---:|---|
| `kv` (zuständiger Solarlux-Mitarbeiter) | 16,9 % | **150×** | unbrauchbar |
| `products`, `description`, `service_area` … | 6–9 % | **200–220×** | unbrauchbar |
| `founded_year`, `legal_form`, `employee_hint` | 1–6 % | **145–216×** | unbrauchbar |
| `quote_count > 0` | 32,0 % | 3,4× | nur zeitlich gefiltert |
| Segment, Untersegment, PLZ, Ort, Land | 98–100 % | **1,00** | brauchbar |
| Website / E-Mail / Telefon vorhanden | 48–98 % | 1,02–1,09 | brauchbar |

**Ein Kundenverantwortlicher wird zugeteilt, WEIL jemand Kunde ist.** Ein
Modell, das `kv` benutzt, sagt die Vergangenheit voraus und keine Zukunft.
Dasselbe gilt für jedes Anreicherungsfeld in Deutschland: angereichert wurde
bisher fast nur, wer schon kauft.

Damit bleibt für eine fremde Firma genau das übrig, was auch ein Fremder über
sie wüsste: **Branche (Untersegment), Region (PLZ), Land, Erreichbarkeit.**

## 3. Zwei versteckte Fallen, gefunden und entfernt

Die Verfügbarkeitsprüfung findet fehlende Werte. Sie findet **nicht**, wenn ein
*vorhandener Wert* nachträglich gesetzt wird. Zwei Fälle, beide zunächst als
starke Prädiktoren aufgetaucht:

**`Vertriebsweg = Direktvertrieb`** — n = 55, Kaufrate 54,5 % gegen 13,5 %
Basis. Das ist keine Eigenschaft der Firma, sondern eine Beschreibung unserer
Beziehung zu ihr („wir beliefern sie direkt"). Merkmal entfernt.

**`Untersegment = leer`** — n = 209, Kaufrate **42,6 %** gegen 13,5 %. 189 davon
sind Handel. Das ist Import-Herkunft, nicht Betriebswirklichkeit — und es ist die
gefährliche Richtung: eine im Internet neu gefundene Firma hat **ebenfalls** kein
Untersegment und bekäme aus einem nicht übertragbaren Grund eine hohe Punktzahl.
Genau das würde die spätere Zwillingssuche vergiften. Zeilen entfernt.

Der Preis der Ehrlichkeit: AUC fällt von 0,623 auf **0,598**. Diese 0,025 waren
Artefakt.

## 4. Was der Kalt-ICP wirklich kann

Aufbau: Stichtag 2023-01-01, Population = Händler/Verarbeiter **ohne jede
Bestellung 2019–2022**, Zielgröße = mindestens eine Bestellung 2023-01-01 bis
2026-08-05. Nur die Merkmale aus §2. 5-fache Kreuzvalidierung, plus ein
**geografischer Holdout** (GroupKFold über PLZ-Zonen: das Modell sieht die
Testregion nie, kann also keine Region auswendig lernen).

| Population | n | Positive | AUC | Geo-Holdout | Top-Dezil |
|---|---:|---:|---:|---:|---:|
| Deutschland | 7.822 | 995 | **0,598** | 0,588 | 1,40× |
| Alle Länder | 11.319 | 1.369 | **0,635** | 0,620 | 2,00× |
| Replikation, Stichtag 2024 | 10.045 | 877 | **0,590** | — | — |

Die Replikation an einem unabhängigen Stichtag liefert praktisch dieselbe Zahl —
die Schätzung ist stabil, nicht zufällig.

**Was das Modell inhaltlich sagt** (beobachtete Kaufraten, Deutschland):

| Untersegment | n | Kaufrate |
|---|---:|---:|
| Fensterbau | 648 | 15,4 % |
| Glaser | 296 | 14,9 % |
| Tischler / Schreiner / Zimmerer | 1.517 | 13,9 % |
| Metallbau / Schlosser | 1.447 | 13,6 % |
| Bauelementehandel | 3.037 | 10,8 % |
| Baustoffhandel | 159 | **6,3 %** |

Das ist plausibel und brauchbar — ein Fensterbauer ist 2,4-mal so
wahrscheinlich Kunde wie ein Baustoffhändler. Aber die gesamte Spannweite der
verfügbaren Merkmale beträgt eben nur 6 % bis 15 %. **Mehr Information steckt
nicht in Branche und Postleitzahl.**

## 5. Warum es nicht an der Datenmenge liegt (der entscheidende Test)

Zwei unabhängige Belege:

**Lernkurve** — AUC gegen Trainingsgröße, Test auf festgehaltenen 25 %:

| Trainingszeilen | 848 | 1.697 | 2.971 | 4.244 | 5.942 | 7.215 | 8.489 |
|---|---:|---:|---:|---:|---:|---:|---:|
| AUC | 0,565 | 0,577 | 0,583 | 0,584 | 0,592 | 0,600 | **0,605** |

Zehnfache Datenmenge bringt +0,04; der letzte Zuwachs beträgt +0,005. Die Kurve
ist praktisch flach. **Zehntausend weitere Firmen würden das Ergebnis nicht
verändern.**

**Zielgrößen-Variation** — dieselben Merkmale, drei verschiedene Zielgrößen:

| Zielgröße | Positive | AUC |
|---|---:|---:|
| irgendeine Bestellung | 1.369 | 0,608 |
| materieller Auftrag (≥ 2.000 €) | 731 | **0,624** |
| oberstes Umsatzquartil | 319 | 0,598 |

Alle drei liegen innerhalb von 0,03. **Auch eine andere Fragestellung hebt die
Decke nicht.** Decke = Merkmalsinformation.

## 6. Der Gegenbeweis: wo Verhaltensdaten vorliegen, funktioniert es

Dieselben Firmen, dieselbe Methode — nur mit Verhaltensdaten:

**Funnel-Triage** (Stichtag 2024, Firmen ohne Bestellung, aber mit mindestens
einer Verkaufschance 2023; n = 916, Basis 14,6 %):

| Merkmalssatz | AUC | Top-Dezil |
|---|---:|---:|
| nur Stammdaten | 0,606 | 1,20× |
| **+ Trichter-Historie** | **0,753** | **3,83×** |
| + Trichter **ohne** „gewonnene VC" | **0,753** | 3,83× |

Die letzte Zeile ist der Integritätstest: das Signal stammt **nicht** aus der
Buchhaltungs-Tatsache „hat schon eine VC gewonnen" (das wäre fast mechanisch),
sondern aus der Intensität des Kontakts — Anzahl, Werte, Rollen. Ohne dieses
Merkmal ist das Modell sogar minimal besser.

**Kunden-Fortsetzung** (bestehende Kunden, n = 3.506, Basis 51,1 %):

| Merkmalssatz | AUC |
|---|---:|
| nur Stammdaten | 0,636 |
| **+ eigene Kaufhistorie (RFM)** | **0,797** |

Dezile der Kaufwahrscheinlichkeit: 18 % · 26 % · 29 % · 34 % · 37 % · 50 % ·
56 % · 79 % · 86 % · **98 %**. Das unterste Fünftel ist die Abbruch-Liste.

**Projekt-Profil (IPP)** — Lift oberstes/unterstes Dezil **13,67×**, Gewinnquote
3 % → 39 %, 8 von 9 Stufen monoton (`adwatch/insights/ipp.py`).

Drei Fälle, drei Male derselbe Befund: **Verhalten schlägt Stammdaten um
+0,14 bis +0,16 AUC.**

## 7. Die Konsequenz — und der Versuch, der sie prüft

Für eine fremde Firma gibt es genau eine Quelle beobachtbaren Verhaltens: **ihre
eigene Website und ihre Werbung.** Genau das erzeugt die Anreicherungs-Pipeline
— sie ist gebaut, geprüft und in Spanien im Populationsmaßstab gelaufen
(1.601 von 1.758 Firmen, 6.629 Feldwerte, rund 1,43 € für 358 Firmen ≈
**0,004 € pro Firma**).

In Deutschland ist sie bisher fast nur auf Kunden gelaufen — daher der
Quotient von 200 aus §2. Läuft sie auf **allen** Händlern, verschwindet die
Verzerrung per Konstruktion: angereichert wird dann jeder, Käufer wie
Nicht-Käufer.

**Die Frage, wie viel das bringt, lässt sich mit den heutigen Daten nicht
beantworten** — in Deutschland sind ganze ~7 angereicherte Nicht-Käufer
vorhanden, in Spanien gibt es nur 20 Händler-Käufer. Beides zu wenig. Sie muss
also gemessen werden, und zwar vorher:

> **Pilotversuch.** Zufallsstichprobe von 600 deutschen Händlern/Verarbeitern,
> geschichtet nach Ausgang (300 Käufer / 300 Nicht-Käufer), **alle** anreichern.
> Dann AUC(Stammdaten) gegen AUC(Stammdaten + Anreicherung) auf denselben
> Zeilen. Kosten ≈ **2,40 €**, Laufzeit ≈ 3–4 Stunden.
>
> Trennschärfe (Hanley-McNeil, gepaartes Design): 600 Zeilen erkennen einen
> AUC-Zuwachs von **0,05** — genau die Schwelle, ab der sich die Entscheidung
> ändert. 200 Zeilen könnten nur 0,09 erkennen und wären zu grob.
>
> Entscheidungsregel, **vorab** festgelegt: ΔAUC ≥ 0,05 → gesamte deutsche
> Händlerbasis anreichern (10.985 Firmen ≈ 44 €). ΔAUC < 0,05 → Kalt-ICP bleibt
> dauerhaft eine Vorsortierung, und die Akquise stützt sich auf Funnel und
> Projekte.

Der Pilot ist der einzige ehrliche Weg. Die gesamte Basis sofort anzureichern
wäre zwar auch billig, würde aber die Frage nie beantworten, weil danach kein
unangereicherter Vergleich mehr existiert.

## 8. Was heute ausgeliefert werden sollte

1. **IPP** (fertig) — Triage der 10.349 offenen Objekte.
2. **Funnel-Triage** — Top-Dezil 3,83×. Die Liste, die der Innendienst
   tatsächlich abtelefonieren kann.
3. **Kunden-Fortsetzung** — unterstes Fünftel = Abbruchgefahr, oberstes = sicher.
4. **Kalt-ICP** als *Priorität*, nicht als Rangliste: ein Fensterbauer in einer
   guten Region ist ein besserer Erstkontakt als ein Baustoffhändler. Mit
   ausgewiesener Güte (AUC 0,60) und dem ausdrücklichen Hinweis, dass die
   Reihenfolge innerhalb der Liste wenig bedeutet.

Was **nicht** ausgeliefert werden sollte: ein Kalt-ICP, der so tut, als sei
Rang 3 besser als Rang 30. Bei AUC 0,60 ist er das nicht.

## 9. Offene Fragen an den Vertrieb

1. **Ist „mindestens eine Bestellung" der richtige Erfolg?** Alternativ:
   Deckungsbeitrag, Wiederkaufrate, oder „wird Stammpartner" (≥ 3 Jahre aktiv).
   Die Zielgröße ist eine Geschäftsentscheidung, keine mathematische.
2. **Was kostet welche Ansprache?** Siehe §10 — die Frage lautet nicht „lohnt
   sich der Kontakt" (er lohnt sich fast immer), sondern „wer bekommt den
   teuren Kontakt".
3. ~~Gibt es eine Liste kontaktierter, aber nicht gewonnener Firmen?~~
   **Beantwortet, siehe §11 — es gibt sie, teilweise.**

## 10. Die Wirtschaftlichkeit — warum AUC 0,60 trotzdem Geld wert ist

Wert einer neu gewonnenen Händlerbeziehung, gemessen an den 1.486 Firmen, die
seit 2023 erstmals gekauft haben (Fenster 2023-01 bis 2026-08):

| Kennzahl | Wert |
|---|---:|
| Median-Umsatz | 2.752 € |
| **Mittelwert** | **18.357 €** |
| 90. Perzentil | 41.138 € |
| 95. Perzentil | 71.880 € |
| Gesamtumsatz der Kohorte | 27,3 Mio. € |

Rechenbeispiel über 100 Anrufe bei deutschen Händlern:

| Auswahl | Trefferquote | erwartete Kunden | erwarteter Umsatz |
|---|---:|---:|---:|
| zufällig | 12,7 % | ~13 | ~239.000 € |
| oberstes Modell-Dezil | 18,0 % | ~18 | ~330.000 € |

Die Rangfolge ist damit rund **900 € zusätzlich erwarteter Umsatz je getätigtem
Anruf** wert — auch bei „schwacher" AUC 0,60, weil der Gewinn je Treffer groß
ist.

**Die betriebswirtschaftliche Folgerung ist wichtiger als die Zahl:** bei
18.357 € Durchschnittswert trägt sich fast jede Kontaktform. Es ist also
falsch, die Liste hart abzuschneiden. Die Aufgabe der Rangfolge ist nicht zu
entscheiden, **ob** man jemanden anspricht, sondern **wer die teure Ansprache
bekommt** — Außendienstbesuch und Messeeinladung für das oberste Dezil,
Serienmail für den Rest. Dafür wird der Preis je Kontaktform gebraucht, nicht
eine Wertgrenze.

**Verfeinerung, die aus der Schiefe folgt:** Mittelwert 18.357 € gegen Median
2.752 € heißt, dass wenige große Partner die gesamte Kohorte tragen. Die Liste
sollte daher nach **Erwartungswert** (Wahrscheinlichkeit × prognostizierte
Größe) sortieren, nicht nach Wahrscheinlichkeit allein.

## 7a. ERGEBNIS des Anreicherungs-Experiments (2026-08-17)

Durchgeführt wie in §7 vorregistriert: 600 deutsche Händler, 300/300 gezogen mit
festem Startwert, **alle** angereichert (Job 58, 600/600, 39 Fehlversuche =
nicht erreichbare Websites). Etiketten nach der bereinigten Regel neu berechnet
(mind. ein Ereignis > 0 €) → 272 Käufer / 328 Nicht-Käufer. 428 der 600 Firmen
lieferten Anreicherungsfelder, 41 hatten keine auffindbare Website.

Gepaarter Vergleich auf **denselben Zeilen und denselben Folds**:

| Merkmalssatz | GBM | Logit |
|---|---:|---:|
| A Stammdaten | 0,569 | 0,588 |
| B* + Anreicherung, **streng** (ohne Solarlux-Bezug) | 0,569 | **0,619** |
| B + Anreicherung, alle Felder | 0,569 | 0,619 |

**ΔAUC gegenüber Stammdaten, gepaarter Bootstrap (4.000 Ziehungen):**

| Vergleich | Δ | 95-%-Intervall |
|---|---:|---|
| B* streng, GBM | +0,001 | −0,045 … +0,048 |
| B* streng, Logit | **+0,032** | **−0,005 … +0,068** |

**Urteil nach der vorab festgelegten Regel: ΔAUC = +0,031 < +0,050 →
die gesamte deutsche Händlerbasis wird NICHT angereichert.**

Drei Dinge sind an diesem Ergebnis wichtiger als die Zahl selbst:

1. **Das Intervall enthält die Null.** Es gibt keinen Beleg für einen Gewinn —
   und ebenso wenig einen Beleg, dass der Gewinn null ist. Die Stichprobe war
   auf 0,05 ausgelegt; ein echter Effekt von 0,03 wäre hier nicht sicher
   nachweisbar gewesen und läge ohnehin unter der Entscheidungsschwelle.
2. **Die befürchtete Rückwärtswissen-Falle war empirisch keine.** Streng und
   vollständig liefern identisch 0,619 — `mentions_solarlux` trug nichts bei.
   Die methodische Sorge war berechtigt, das Problem existierte nicht.
3. **Das Verfahren zählt, nicht das Ergebnis.** Wäre die Regel erst nach dem
   Blick auf die Zahlen formuliert worden, hätte man aus +0,032 eine
   Erfolgsmeldung machen können. Genau deshalb stand sie vorher fest.

**Inhaltlich interessant, aber nur ein Hinweis** (die Gesamtwirkung ist nicht
belegt, also sind es auch die Einzelkoeffizienten nicht):

| Merkmal | Koeffizient |
|---|---:|
| **montiert selbst** | **+0,371** |
| **eigene Fertigung** | **−0,210** |
| gegründet (Alter) | +0,147 |
| Website vorhanden | +0,098 |

Das ist betriebswirtschaftlich stimmig: **ein Betrieb, der montiert, muss Systeme
einkaufen; ein Betrieb mit eigener Fertigung baut sie selbst.** Wenn sich diese
Richtung an einer größeren Stichprobe bestätigt, ist „montiert, fertigt aber
nicht selbst" das schärfste Einzelkriterium, das wir für Kaltakquise haben.

### Was daraus folgt

Der Befund engt die Empfehlung ein, er kippt sie nicht: **beschreibende Merkmale
(was eine Firma IST) helfen kaum; verhaltensbezogene (was zwischen uns und ihr
geschehen ist) helfen stark** — Trichter +0,147, Kaufhistorie +0,161, gegen
+0,03 für die Anreicherung. Das ist derselbe Befund wie in §6, jetzt auch von der
anderen Seite bestätigt.

**Die Anreicherung bleibt trotzdem wertvoll — nur nicht für diese Frage.** Sie
trägt den spanischen Bericht, die Identitätsprüfung, die Qualifizierung („was
macht diese Firma eigentlich") und ist die einzig mögliche Datenquelle für die
Zwillingssuche. Nicht gerechtfertigt ist allein, sie zu bezahlen, *um die
Kalt-Rangfolge zu verbessern*.

**Nächster Hebel ist damit nicht mehr die Anreicherung, sondern der
Dataverse-Abzug** (§11): Leads und Aktivitäten sind VERHALTENSdaten — wen haben
wir angesprochen, wer hat abgelehnt — und Verhalten ist gemessen das Einzige,
was diese Modelle wirklich bewegt.

## 10a. Partnerstufen (Gold-Partner) — niemals Merkmal, vermutlich Zielgröße

**Als Merkmal verboten.** Eine Firma ist Gold-Partner, WEIL sie viel verkauft.
Ein Modell, das die Stufe als Eingabe bekommt, sagt die Vergangenheit voraus —
derselbe Fehler wie `kv` (Quotient 150×). Das gilt für alles daraus Abgeleitete:
Rabattstufe, Vertragsart, Showroom-Zuschuss.

**Als Zielgröße wahrscheinlich besser.** Was aus den 1.369 seit 2023 neu
gewonnenen Händlern wirklich wurde:

| Ergebnis | n | Anteil |
|---|---:|---:|
| **Strohfeuer** — ein Bestelljahr, < 2.000 € | **556** | 41 % |
| ≥ 2 Bestelljahre | 409 | 30 % |
| ≥ 2 Jahre und ≥ 10.000 € — echte Partner | 279 | 20 % |
| ≥ 3 Jahre und ≥ 25.000 € — „Gold"-artig | **85** | 6 % |

**Zwei von fünf „Gewinnen" sind Strohfeuer.** Das heutige ICP behandelt eine
einmalige 101-€-Bestellung wie eine 72.000-€-Partnerschaft. Das ist unabhängig
von jeder AUC die falsche Steuergröße.

Die Umsatzkonzentration verschärft das: die besten **1 %** der Händler (34
Firmen) tragen **36,1 %**, die besten 5 % (173 Firmen) **72,6 %** des
Händlerumsatzes. Das Spiel besteht darin, die wenigen Großen zu finden.

Gemessene Vorhersagbarkeit:

| Zielgröße | Positive | AUC (95 %) | Geo-Holdout | Lift ob. Dezil |
|---|---:|---|---|---:|
| irgendeine Bestellung | 1.369 | 0,608 [0,593–0,624] | 0,600 | 1,47× |
| **Gold-artig (3 J / 25 k)** | 85 | **0,686 [0,633–0,736]** | 0,648 [0,595–0,697] | **2,71×** |

Die kreuzvalidierten Intervalle trennen sich knapp (0,624 gegen 0,633), die
Geo-Holdout-Intervalle **überlappen** jedoch. Bei 85 Positiven ist das
**ein Hinweis, kein Beleg** — und mehr wird hier nicht behauptet.

Entscheidend: diese 85 sind ein Behelfsmaß, gebildet nur aus NEU gewonnenen
Firmen, die eine harte Schwelle in 3,6 Jahren erreichen. Die **echte
Gold-Liste enthält auch langjährige Partner** — vermutlich einige hundert
Positive, womit die Frage sauber entscheidbar wäre.

**Für Bestandspartner ist die Stufe die natürliche Schichtung:** „welcher
Silber-Partner hat Gold-Potenzial" ist eine andere, gut gestellte Frage — dort
gehört die Stufe als Segmentierung hin, nicht als Prädiktor ihrer selbst.

**Offene Frage an den Vertrieb:** Wird Gold rein nach Umsatz vergeben oder auch
nach qualitativen Kriterien (Schulungen, Showroom, Zertifizierung,
Exklusivität)? Rein umsatzabgeleitet wäre es nur eine umbenannte Umsatzstufe
ohne Zusatzinformation. Kodiert es Bindung und Fähigkeit, ist es die bessere
Zielgröße — und die qualitativen Kriterien selbst wären hervorragende
Anreicherungsmerkmale, nach denen sich auf der Website eines Interessenten
suchen ließe.

Das Feld liegt **nicht** im Spiegel (`partner_of` ist angereichert und meint
Wettbewerbsmarken) und gehört zum Dataverse-Abzug (§11, Aufgabe 8).

## 10b. Ein Modell je Filter? Gemessen: nein

Naheliegende Idee: eigene ICPs je Land, je Produktfamilie, je Land × Produkt.
Empirisch geprüft — gepoolt (Land als **Merkmal**) gegen getrennt (ein Modell
**je** Land), identische Folds:

| Land | n | Käufer | gepoolt | getrennt | Urteil |
|---|---:|---:|---:|---:|---|
| DE | 7.822 | 995 | 0,602 | 0,601 | gleich |
| FR | 1.495 | 155 | 0,532 | 0,557 | getrennt +0,025 |
| AT | 945 | 154 | 0,541 | 0,510 | **gepoolt +0,031** |
| NL/ES/SE/DK/IT | 101–294 | 5–17 | — | — | zu wenige Positive |
| **gesamt** | 11.319 | 1.369 | **0,611** | **0,607** | **gepoolt gewinnt** |

**Die Regel dahinter:** Trennen lohnt nur, wenn sich die ZUSAMMENHÄNGE
unterscheiden, nicht wenn sich die Basisraten unterscheiden. Dass Spanien
weniger kauft als Deutschland, ist ein Unterschied in der Basisrate — dafür
genügt EIN Parameter (das Merkmal `land`), während alle anderen Muster
weiterhin aus allen 11.319 Zeilen lernen. Ein eigenes Modell zahlt sich erst
aus, wenn etwa Fensterbauer in Deutschland gute, in Frankreich schlechte
Interessenten wären. Das ist gemessen nicht der Fall.

**Der Preis der Trennung ist nichtlinear:** rund 50 wirksame Parameter
(One-Hot-Kategorien) und die übliche Regel von ≥ 10 Ereignissen je Parameter
ergeben **~500 Positive je Zelle**. Deutschland hat 995, Frankreich 155,
Österreich 154, Spanien 5. Land × Produktfamilie landet überall im
einstelligen Bereich.

**Die Produktdimension hat zusätzlich ein strukturelles Problem:** bei einem
Interessenten wissen wir nicht, was er kaufen WÜRDE — die Produktfamilie ist
erst nach dem Kauf beobachtbar. Deshalb:

| Frage | machbar? |
|---|---|
| ICP je Produkt für **Interessenten** | nein — außer die Anreicherung liest es von der Website |
| Produktfamilien im **Projekt-Profil** | **ja, bereits umgesetzt** (Schiebe-Dreh 1,60×, Wintergarten 0,60×) |
| Cross-Selling im **Bestand** („wer kauft als Nächstes cero?") | ja, gut besetzt |

**Empfehlung:** ein gepooltes Modell, Filter als Merkmale, und der Filter wirkt
erst bei der ANZEIGE. „Spanien, Wintergarten" filtert die Liste; bewertet hat
ein Modell, das auf allem trainiert wurde.

Die Laplace-Glättung in `profiles.py` und `ipp.py`
(`p = (won + 5·base) / (total + 5)`) ist genau die statistisch richtige
Zwischenform: **partielles Pooling**. Eine kleine Zelle wird in dem Maß zur
Gesamtrate hingezogen, in dem ihr Beleg fehlt — ein spanisches Untersegment mit
12 Firmen darf nicht so laut sprechen wie ein deutsches mit 3.037.

## 11. Was das CRM über verlorene Ansprachen wirklich weiß

| | Anzahl |
|---|---:|
| Händler mit ≥ 1 Verkaufschance, aber **nie** einer Bestellung | **2.421** |
| davon mit mindestens einer ausdrücklich verlorenen VC | 1.943 |
| Händler **ohne jede kommerzielle Spur** | 8.026 |

Die 2.421 sind echte Negativbeispiele und werden bereits genutzt — sie sind die
Grundgesamtheit der Funnel-Triage (§6), und genau deshalb erreicht die 0,753
statt 0,60.

**Aber die Verlustgründe verschieben die Bedeutung.** Für Händler, die nie
Kunde wurden:

| Grund | Anzahl |
|---|---:|
| Keine Baugenehmigung | 1.123 |
| Kein Feedback vom Kunden | 881 |
| Zu teuer | 555 |
| Kunde hat den Auftrag nicht erhalten | 522 |
| Wettbewerb | 211 |
| Kein Interesse mehr | 193 |

Das sind überwiegend **gestorbene Projekte, keine abgelehnten
Partnerschaften**. Ein Bauvorhaben ohne Genehmigung sagt nichts darüber, ob
diese Tischlerei ein guter Solarlux-Partner wäre. Nur „Zu teuer",
„Wettbewerb" und „Kein Interesse mehr" (zusammen ~960) sind echte
kommerzielle Absagen.

Für die **8.026 Händler ohne jede Spur** lässt sich „nie angesprochen" nicht
von „angesprochen und abgelehnt" unterscheiden. Auflösbar ist das: Dataverse
führt `leads`, `phonecalls`, `tasks` und `appointments` mit jeweils ≥ 5.000
Datensätzen — **keine davon ist bisher abgezogen**. Das ist der eigentliche
Wert des Leads-Abzugs, und er ist größer als gedacht: er verwandelt einen Teil
der 8.026 „unbekannt" in echte Negativbeispiele.

---

### Methodische Festlegungen

* Ausgeschlossen: Private Endkunden, Wettbewerber-Standorte, Intercompany
  (`adwatch/scope.py`) — vor jeder Zählung, nicht danach.
* Alle Merkmale sind strikt vor dem Stichtag berechnet; Zielgrößen strikt danach.
* Jede AUC mit 95-%-Bootstrap-Intervall (2.000 Ziehungen).
* Jedes Modell gegen eine Grundlinie geprüft (Segment allein: AUC 0,515).
* Geografischer Holdout, wo Regionen als Merkmal auftreten.
* Bekannte, nicht behebbare Einschränkung: Der Datenspiegel enthält **kein
  Anlagedatum der Firmenstammsätze**. Firmen, die erst 2024 ins CRM kamen,
  erscheinen als „hat 2019–2022 nicht gekauft". Das erzeugt Rauschen in der
  Negativklasse und drückt die gemessene Basisrate — es erzeugt **kein**
  falsches Signal. Behebbar durch einen `createdon`-Abzug aus Dataverse.

## 12. Beschaffung — gibt es überhaupt Firmen, die wir nicht kennen?

Die Zwillingssuche scheitert oder gelingt nicht am Bewerten, sondern am FINDEN.
Gemessen 2026-08-18: **Website-Merkmale allein erreichen AUC 0,595** und damit
etwas mehr als die CRM-Stammdaten (0,583) — einen Fremden zu bewerten ist also
möglich. Die offene Frage war, ob sich Fremde überhaupt beschaffen lassen.

### Warum Suchmaschine und nicht Wettbewerber-Verzeichnis

Geprüft wurden drei Quellen, bevor eine gewählt wurde:

| Quelle | Befund |
|---|---|
| **Cortizo** | robots.txt verbietet `/instaladores/desplegar/` ausdrücklich — Verzeichnis tabu |
| **Schüco** | Partnersuche ist ein JavaScript-Formular hinter undokumentierter Schnittstelle |
| **Suchmaschine (Serper)** | bereits bezahlt, skaliert über Gewerke und Regionen |

Dazu ein grundsätzliches Argument: ein Wettbewerber-Verzeichnis liefert nur,
was der Wettbewerber gerade veröffentlicht, und kann morgen weg sein.

### Pilot (12 Abfragen, 4 Städte × 3 Gewerke)

| | |
|---|---:|
| gefundene Firmen | 77 |
| über Domain als bekannt erkannt | 14 |
| über Name+Ort als bekannt erkannt | 2 |
| **zunächst als neu gezählt** | **61 (79 %)** |

**Diese 79 % waren zu optimistisch, und zwar aus einem messbaren Grund:** von
10.998 deutschen Händlern haben nur **5.463 (49 %)** überhaupt eine Domain
hinterlegt. Über die Domain ist also nur die Hälfte des Bestands auffindbar.

Gegenprobe mit unscharfem Namensvergleich gegen alle 48.239 Firmennamen: 22 der
61 „neuen" haben einen möglichen Treffer. Ein Teil davon sind allerdings
Fehlalarme des Vergleichs (`braun-fensterbau.de` gegen eine Firma namens
„Braun"; `glaserei.org` gegen „Tischlerei Glaserei Carsten M."), weil kurze
oder gattungsartige Namenskerne bei Token-Vergleichen leicht 100 % erreichen.

**Ehrliches Ergebnis: zwischen 50 % und 79 % wirklich neu.** Die Untergrenze
stammt aus dem übervorsichtigen Namensvergleich, die Obergrenze aus dem
Domain-Abgleich allein. Genauer geht es ohne Handprüfung nicht.

**Auch die Untergrenze trägt die Entscheidung:** die Hälfte dessen, was eine
einfache Suche findet, steht nicht im CRM. Der Kanal existiert.

### Was daraus folgt

Der nächste Schritt ist NICHT, mehr zu suchen, sondern die Gefundenen durch die
vorhandene Kette zu schicken — Identität prüfen, anreichern, gegen das
Gewinner-Profil bewerten — und die Besten als **Arbeitsliste mit
Kontrollgruppe** auszuspielen (§ `adwatch/outcomes.py`). Erst dann ist
messbar, ob ein aus dem Internet beschaffter Kontakt tatsächlich Kunde wird.

Offen und ausdrücklich unbewiesen bleibt: ob diese Firmen GUTE Interessenten
sind. Gefunden ≠ geeignet. Das entscheidet erst die Kontrollgruppe.
