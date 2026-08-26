# Flow bauen: „Personen suchen (Office 365)"

Damit Berichts-Empfänger **ausgewählt statt abgetippt** werden. Fünf Minuten,
keine Azure-App-Registrierung, keine Administrator-Zustimmung — der Flow
handelt unter deiner eigenen Identität, genau wie der Dataverse-Flow.

Die App funktioniert auch **ohne** diesen Flow: das Empfängerfeld bleibt dann
ein normales Eingabefeld. Nichts bricht, wenn du es nie einrichtest.

---

## 1. Flow anlegen

1. [make.powerautomate.com](https://make.powerautomate.com) öffnen
2. **Erstellen → Sofortiger Cloud-Flow**
3. Name: `AdWatch — Personen suchen`
4. Trigger wählen: **Wenn eine HTTP-Anforderung empfangen wird**
5. **Erstellen**

## 2. Trigger einrichten

Den Trigger aufklappen, bei **Anforderungstext-JSON-Schema** genau das eintragen:

```json
{
  "type": "object",
  "properties": {
    "suche": { "type": "string" },
    "top": { "type": "integer" }
  }
}
```

*(Die URL erscheint erst nach dem ersten Speichern — deshalb kommt sie in
Schritt 5.)*

## 3. Aktion: im Verzeichnis suchen

1. **+ Neuer Schritt**
2. Nach **Office 365 Users** suchen
3. Aktion: **Search for users (V2)** (deutsch: *Nach Benutzern suchen (V2)*)
4. Felder ausfüllen:

| Feld | Wert |
|---|---|
| **Search term** | dynamischer Wert `suche` aus dem Trigger |
| **Top** | dynamischer Wert `top` aus dem Trigger |

> Findet Power Automate `top` nicht in der Liste der dynamischen Werte, trag
> stattdessen den Ausdruck ein:
> `coalesce(triggerBody()?['top'], 8)`
> Das setzt 8 als Vorgabe, falls das Feld einmal fehlt.

## 4. Aktion: Antwort zurückgeben

1. **+ Neuer Schritt**
2. Nach **Antwort** (englisch *Response*) suchen — die Aktion heißt
   **Antwort** und gehört zu „Anforderung/Request"
3. Felder:

| Feld | Wert |
|---|---|
| **Statuscode** | `200` |
| **Text** (Body) | die Ausgabe von *Search for users (V2)* — im dynamischen Inhalt als **value** angeboten |

> Die App nimmt **beide** Formen an: eine nackte Liste ebenso wie
> `{"value": [...]}`. Du kannst also `value` einsetzen oder den ganzen
> Body — beides funktioniert.

## 5. Speichern und URL kopieren

1. **Speichern**
2. Den Trigger wieder aufklappen — jetzt steht dort die
   **HTTP-POST-URL**
3. Auf das Kopiersymbol klicken

## 6. In AdWatch eintragen

1. AdWatch → **Einstellungen** → Gruppe *Power Automate flows*
2. Feld **„Flow: Personen suchen (Office 365)"**
3. URL einfügen → **Speichern** → **Test**

Fertig. Unter **Berichte → Empfänger** genügen jetzt zwei Buchstaben im
Adressfeld, und die Vorschläge kommen aus dem Verzeichnis.

---

## Prüfen, ob es tut

Im Flow-Editor auf **Testen → Manuell**, dann diesen Text als Anforderungstext:

```json
{ "suche": "Marouani", "top": 5 }
```

Die Ausführung muss grün sein und im Antwort-Schritt Personen enthalten.

## Wenn etwas klemmt

| Symptom | Ursache |
|---|---|
| HTTP 502 „NoResponse" sofort | Ein Pflichtfeld der Suchaktion ist leer. Power Automate lässt den Flow trotzdem speichern; die AKTION scheitert dann, und der Aufrufer sieht nur einen allgemeinen 502. Dieselbe Falle wie beim leeren `$filter` im Dataverse-Flow. |
| Läuft grün, aber AdWatch zeigt nichts | Der Antwort-Schritt gibt nicht das Suchergebnis zurück, sondern etwas anderes (oder ist leer). |
| „Unerwarteter Host" beim Test | Es ist keine Power-Automate-URL. Sie muss auf `powerplatform.com` oder `logic.azure.com` enden. |
| Namen kommen, aber ohne Adresse | Der Connector liefert `userPrincipalName` statt `mail` — die App versteht beides; wenn wirklich keine Adresse dabei ist, fehlt sie im Verzeichnis. |

## Was gespeichert wird

Nur was zum Versenden nötig ist: **Name und Adresse**, und erst dann, wenn du
jemanden bewusst als Empfänger aufnimmst. Suchergebnisse landen nicht in der
Datenbank, Fotos werden gar nicht erst geholt. Es ist ein Empfängerfeld, kein
Personenverzeichnis.

## Teams-Chat

Braucht **gar nichts** — kein Flow, keine Berechtigung. Neben jedem Empfänger
steht ein **💬 Teams**-Knopf, der einen Chat mit dieser Person öffnet.

Teams selbst lässt sich **nicht** in die App einbetten: Microsoft verbietet das
per `frame-ancestors`, ein eingebetteter Rahmen bliebe leer. Der Deep Link tut,
was gemeint ist.
