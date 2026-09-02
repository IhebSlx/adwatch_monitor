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

## 1. The feedback loop — write outcomes back into Dynamics

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

Seven days of rollback is defensible for a mirror. It is thin for the only copy
of a running experiment. **Proposed near-term mitigation: a plain CSV/Excel
export of lists, arms and outcomes** — small to build, readable without AdWatch,
and it removes the single-point-of-failure without needing any Dataverse
permission at all. Not built yet; flagged here as a decision, not a plan.

---

## 3. The rest of the plan, in order

| # | Item | Gated on |
|---|---|---|
| 16 | **Prove discovered leads convert** — 293 in lists, **1 contacted** since 18.08. | sales working the lists |
| 10 | Ads-vs-enrichment consistency check — the second identity scrutiny | Apify credits |
| 12 | Twin search prototype — lookalike leads from the internet | #16 reading out first |
| 1 | Dataverse write-back (this document, §1) | write permissions |
| 7 | Split `app.js` into ES modules | nothing — housekeeping |

**Erledigt am 2026-09-02:** #17 (Konversion Angebot → Auftrag, eigener Tab, mit
Wilson-Intervall je Zeile — Wohnungswirtschaft 37,5 % und Gebäudebetreiber
30,9 % über einer Grundlinie von 21,3 %, Architekten 5,9 % darunter) ·
#2 (Export von Listen, Armen und Ergebnissen als Excel, Kontrollgruppe markiert
und rot hinterlegt).

**Erledigt seit der letzten Fassung:** #8 (Dataverse-Lesen: 438.979 E-Mails über
44/44 Monate, 236.710 Leads, `createdon`, SAP-Beleg-Join auf Projektebene) ·
Projektwert = primäre Verkaufschance statt Summe · Explorer (Karte × Liste ×
Firmen × Projekte) · dunkle Haut mit hellem Rückweg · Projektkarte in der Höhe ·
Spaltenfilter über der Karte · Chatbot schlägt Läufe vor.

---

## 3a. Was auf deiner Seite liegt

Nichts davon kann die App selbst erledigen. Sie ist an allen vier Stellen
fertig und wartet.

| Was | Warum es wartet | Wenn es kommt |
|---|---|---|
| **Die 245 Anrufe** | 1 von 293 seit dem 18.08. | #16 liest aus, #12 wird baubar |
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
