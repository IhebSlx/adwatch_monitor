# Roadmap — what AdWatch should become, and what it deliberately is not yet

Status of this document: 2026-08-18. Written when it became clear that writing
back into Dynamics is wanted but not yet permitted. Its job is to record the
intent precisely enough that nobody later mistakes a *deliberate* limitation for
an oversight — and to make sure the app never quietly grows a dependency on a
capability we do not have.

---

## 0. The rule for today: everything stays in the app

Every decision AdWatch produces — list membership, experiment arm, who was
contacted, what came of it, identity verdicts, enrichment, discovered companies
— is written to **local tables only**. Nothing is ever written to Dataverse.

This is verified rather than assumed. The app talks to Dynamics through exactly
one mechanism (`adwatch/flows.py`), which has three call sites in total:

| Call site | Role | Direction |
|---|---|---|
| `crm_accounts.py:262` | `crm_query` | read |
| `crm_emails.py:86` | `crm_query` | read |
| `emailer.py:71` | `report_email` | sends a PDF, touches no record |

There is no `PATCH`, no `PUT`, and no write role in `FLOW_ROLES`. A Dataverse
write is not disabled by a setting someone could flip by accident — the code
path does not exist.

---

## 0a. Entscheidung 2026-09-07: die Arbeitslisten sind entfernt

Iheb hat das Listen-Feature gestrichen. Sein Grund, wörtlich: *„I am in business
development not in marketing"* — die Listen verlangten, dass jemand Firmen
anspricht und das Ergebnis eintippt, und dafür ist er nicht zuständig. Nachdem
ich bestätigt hatte, dass es genau das verlangt, hat er auf Nachfrage die
vollständige Entfernung gewählt, Daten eingeschlossen.

Entfernt: `adwatch/outcomes.py`, der Listen-Tab, die sechs `/api/lists`-Routen,
die Knöpfe „Als Liste anlegen" in den Profilen, die Modelle `TargetList` /
`TargetListEntry` und die beiden Tabellen samt 3 Listen, 366 Zeilen und 73
Kontrollgruppen-Zuweisungen vom 18.08.2026. (Die Zeilen liegen noch in den 7
rotierenden Backups, bis diese durchgelaufen sind.)

**Was das kostet, damit es später niemand für ein Versehen hält.** AdWatch kann
ab jetzt sagen, wo man ansetzen sollte, aber nicht mehr, ob es gestimmt hat.
Konkret fällt damit:

* **§1 und §2 dieses Dokuments** — es gibt keine Ergebnisse mehr, die man ins
  CRM zurückschreiben könnte, und keine „einzige Kopie", die zu sichern wäre.
  Beide Abschnitte stehen weiter unten nur noch als Protokoll.
* **#16** („beweisen, dass entdeckte Leads konvertieren") — nicht mehr baubar.
* **#12** (Zwillingssuche) hing an #16. Sie lässt sich weiter bauen, aber ohne
  jede Möglichkeit zu prüfen, ob die gefundenen Firmen je etwas kaufen.

Der Weg zurück, falls die Frage doch aufkommt: eine Kontrollgruppe lässt sich
nicht rückwirkend ziehen. Eine zweite Ziehung ist eine andere Gruppe. Was
gemessen werden soll, muss vor der Ansprache markiert werden — sonst gar nicht.

Eine Alternative wurde geprüft und verworfen: die Ergebnisse aus dem CRM
selbst ablesen (taucht eine entdeckte Firma später als Lead oder Konto auf, ist
das die Antwort). Alle 304 entdeckten Firmen tragen eine Domain, aber nur 11 %
der 236.710 Leads führen eine Website, und exakter Namensabgleich findet heute
genau **einen** Treffer. Zu dünn, um darauf ein Feature zu stützen.

---

## 1. The feedback loop — write outcomes back into Dynamics

> **Hinfällig seit 2026-09-07 (§0a).** Es gibt keine Ergebnisse mehr, die
> zurückgeschrieben werden könnten. Der Abschnitt bleibt als Protokoll stehen,
> weil er die Regeln festhält, die für JEDEN künftigen Dataverse-Schreibzugriff
> gelten sollen.

**What we want:** the loop closes inside CRM. Sales works a list in AdWatch,
records the result, and that result becomes visible in Dynamics where the rest of
their day happens — instead of living in a second tool they have to remember.

**Blocked on:** Dataverse write permissions. Not available as of 2026-08-18;
being requested.

**Classification: an optimisation, never a dependency.** The models do not need
it. This distinction matters enough to state plainly, because it is easy to
oversell:

> Behavioural features are worth +0,14 to +0,16 AUC; descriptive features are
> worth +0,03 (measured four independent times — see `ICP-STRATEGY.md` §5, §6,
> §7a, §13). Outcomes are behavioural data, which is why recording them is the
> most valuable thing the app does. But that value is realised the moment they
> are recorded **in AdWatch**. Writing them onward to Dynamics adds **zero**
> predictive power.

What it does buy is real, just different: **adoption** (sales sees its own work
reflected in the system it trusts) and **durability** (the record outlives
AdWatch). Both are worth having. Neither is worth a wrong number.

### What the write-back may and may not do

1. **Only fields AdWatch owns.** Score at list creation, list membership, arm,
   contact date, outcome code. Never overwrite a human-entered CRM field — if a
   colleague typed it, AdWatch does not get a vote.
2. **Never expose the control arm as a target.** The 15 % holdout exists only as
   long as nobody calls it. A write-back that pushes control-arm companies into
   CRM as a working list destroys the one measurement that makes the whole
   discovery programme falsifiable. Either the arm is written as an explicit
   *do-not-contact* marker, or it is not written at all.
3. **Idempotent, keyed on `crm_id` + list id.** A second run updates; it never
   duplicates. Same discipline as `crm_emails.sync()` on `activity_id`.
4. **One new flow role, `crm_write`.** One `FLOW_ROLES` entry plus one
   `SETTINGS_SPEC` line — the registry was built for exactly this. It arrives
   with masking and a test button for free.
5. **Absent configuration means no writes.** Read-only stays the default
   posture. The write URL must be configured deliberately, per install.

---

## 2. The consequence of staying local, which needs a decision

Because outcomes exist **only** in AdWatch, the local database now holds the
single copy of the most expensive data in the system.

The asymmetry is worth being explicit about. Emails, accounts and opportunities
are a *mirror* of CRM — if the database burned down tomorrow, they are re-pullable
in a few hours. Paid enrichment, verified identities, and human sales decisions
are not re-pullable at any price. Backups are 7 rotated daily copies
(`config.BACKUP_KEEP`), sized deliberately against a database heading for ~1,9 GB.

**Correction, measured 2026-09-07: it was not seven days.** `BACKUP_KEEP`
counts *files*, and until this date a snapshot was written on **every** start.
Six of the seven retained slots held startup copies and one held the nightly
backup — the rotation window was set by how often the app was restarted, not by
time. Seven restarts in one afternoon would have left nothing older than that
afternoon. `backup_now(hoechstens_alle_h=12)` now throttles the startup
snapshot; the nightly job is unchanged, so the seven slots hold roughly seven
days again. (Same change removed 24,5 s from every start — see §3b.)

Seven days of rollback is defensible for a mirror. It is thin for the only copy
of a running experiment. **Proposed near-term mitigation: a plain CSV/Excel
export of lists, arms and outcomes** — small to build, readable without AdWatch,
and it removes the single-point-of-failure without needing any Dataverse
permission at all. Not built yet; flagged here as a decision, not a plan.

---

## 3. The rest of the plan, in order

| # | Item | Gated on |
|---|---|---|
| 10 | Ads-vs-enrichment consistency check — the second identity scrutiny | Apify credits |
| 12 | Twin search prototype — lookalike leads from the internet | nothing mehr — siehe §0a |
| 7 | Split `app.js` into ES modules | nothing — housekeeping |
| ~~16~~ | ~~Prove discovered leads convert~~ | **gestrichen 2026-09-07 (§0a)** |
| ~~1~~ | ~~Dataverse write-back~~ | **hinfällig 2026-09-07 (§0a)** |

**#12 ist damit nicht mehr gesperrt, aber auch nicht mehr überprüfbar.** Die
Reihenfolge stand ursprünglich mit Absicht so: erst beweisen, dass die
Beschaffung etwas bringt, dann mehr davon bauen. Dieser Beweis ist jetzt
gestrichen — wer #12 baut, baut es auf Vertrauen.

**Erledigt am 2026-09-02:** #17 (Konversion Angebot → Auftrag, eigener Tab, mit
Wilson-Intervall je Zeile — Wohnungswirtschaft 37,5 % und Gebäudebetreiber
30,9 % über einer Grundlinie von 21,3 %, Architekten 5,9 % darunter) ·
#2 (Export von Listen, Armen und Ergebnissen als Excel, Kontrollgruppe markiert
und rot hinterlegt).

**Erledigt seit der letzten Fassung:** #8 (Dataverse-Lesen: 438.979 E-Mails über
44/44 Monate, 236.710 Leads, `createdon`, SAP-Beleg-Join auf Projektebene) ·
Projektwert = primäre Verkaufschance statt Summe · Explorer (Karte × Liste ×
Firmen × Projekte) · dunkle Haut mit hellem Rückweg · Projektkarte in der Höhe ·
Spaltenfilter über der Karte.

**Entfernt am 2026-09-08:** der Chatbot (`adwatch/fragen.py`, Tab, `/api/fragen`, fünf Tests) auf Ihebs Anweisung — vollständig, nicht ausgeblendet.

---

## 3b. Startzeit und Ladezeit — gemessen, 2026-09-07

Iheb fragte, warum AdWatch lange braucht. Gemessen an der echten Datei
(1,82 GB), nicht geschätzt:

| Wo | Vorher | Nachher | Was geändert wurde |
|---|---|---|---|
| `PRAGMA quick_check` | 6,0 s blockierend | 0 s | läuft als Daemon-Thread weiter |
| Start-Backup | 24,5 s **bei jedem Start** | 0 s | höchstens alle 12 h (§2) |
| `init_db()` gesamt | ~31 s | **1,3 s** | |
| Erste Kartenöffnung | 12,5 s | ~1,6 s | Projektcache wird beim Start vorgewärmt |
| Weitere Kartenöffnungen | 1,7 s | 1,4 s | unverändert schnell |

**gzip wurde gemessen und dann eingeschränkt.** Naheliegend war, die 6,2 MB
Pin-JSON zu komprimieren — sie schrumpfen auf 1,7 MB. Der Zeitmessung nach ist
das lokal ein Verlust:

    ohne gzip   Median 1,38 s   6.205 KB
    mit gzip    Median 2,11 s   1.699 KB

Über die Loopback-Schnittstelle kostet Übertragung praktisch nichts, also
bezahlt man das Komprimieren und bekommt nichts zurück. AdWatch bindet
standardmäßig 127.0.0.1 — der Normalfall ist genau der, in dem gzip schadet.
Die Middleware entscheidet deshalb nach der Gegenstelle: lokal roh, über das
Netz komprimiert. Der SSE-Stream (`/api/fetch/stream/{id}`) bleibt in beiden
Fällen unkomprimiert, weil ein Kompressor Bytes sammelt und der
Fortschrittsbalken eines Imports dann stockend ankäme.

**Noch offen:** die 6,2 MB selbst. Jeder der 44.000 Pins trägt eine 36-stellige
GUID; das Verschlanken der Nutzlast würde Bytes *und* Serialisierungszeit
drücken. Nicht gemacht, weil es als Einziges den Kartencode anfasst.

---

## 3a. Was auf deiner Seite liegt

Nichts davon kann die App selbst erledigen. Sie ist an allen vier Stellen
fertig und wartet.

| Was | Warum es wartet | Wenn es kommt |
|---|---|---|
| ~~Die 245 Anrufe~~ | entfällt — Listen gestrichen (§0a) | — |
| **Personen-Flow** (`FLOW_URL_GRAPH_USERS`) | fünf Minuten in Power Automate, Anleitung in `docs/FLOW-PERSONENSUCHE.md` | Empfänger werden gesucht statt abgetippt; Teams-Link je Person |
| **Apify-Guthaben** | leer | #10, und der Ad lookup als zweite Prüfung |
| **Dataverse-Schreibrecht** | nicht erteilt | §1, die Rückgabe der Ergebnisse ins CRM |

Der Personen-Flow ist der billigste davon: die Suche, der Endpunkt und die
Auswahlliste stehen bereits im Code, `verfuegbar()` meldet nur `False`, weil
die URL fehlt. Ohne ihn bleibt das Empfängerfeld ein Eingabefeld — es bricht
nichts, es ist nur Tipparbeit.

Note the ordering of #12 behind #16 on purpose. Twin search is the terminal
feature of the whole vision, and sourcing candidates already demonstrably works.
What is *not* yet known is whether companies found that way ever buy. Building
more sourcing before that reads out would be scaling something unproven.
