# ElevenLabs — Dashboard-Wiring-Guide (Stand 2026-08-13)

Schritt-für-Schritt-Einrichtung des MED-EL-Agenten auf der ElevenLabs
Agents Platform gegen diesen Server. Server-Seite ist fertig gebaut:
`POST /elevenlabs/call-init`, `POST /elevenlabs/post-call` (Adapter),
`POST /verify-caller` und `POST /create-request` (unverändert geteilt mit
Fonio). Prompt-Quelle: `prompts/elevenlabs/medel_agent_v1.md`.

## 0. Voraussetzungen

- **Server erreichbar**: öffentliche HTTPS-URL (ngrok static domain oder
  Deployment). `<BASE>` steht unten für diese URL.
- **`.env`**: `ELEVENLABS_POST_CALL_HMAC_SECRET` und
  `ELEVENLABS_TEST_AGENT_IDS` sind aktuell mit **DEV-Platzhaltern** belegt
  (2026-08-13, für lokale Tests) — beide werden in Schritt 6/7 durch die
  echten Werte ersetzt. Env wird beim Import gelesen → **nach jeder
  Änderung Server neu starten**.
- **Zammad einmalig**: Tag `elevenlabs` unter Admin → Tags anlegen. Der
  Service-Account darf keine NEUEN Tags erzeugen (verifiziert 2026-08-13:
  beim Ticket-Create still verworfen, `tags/add` → 403) — existiert der
  Tag nicht, tragen ElevenLabs-Tickets nur `test`; das Artikel-Subject
  „Eingehender Anruf (ElevenLabs AI)" bleibt aber immer der verlässliche
  Plattform-Marker.
- **Workspace-Secret** in ElevenLabs anlegen: Name z. B. `medel-webhook`,
  Wert = `WEBHOOK_SECRET` aus `.env`. Wird für call-init UND beide Tools
  als Header benutzt.

## 1. Agent anlegen

- Sprache: **Deutsch**. First message (aus dem Fonio-Setup übernehmen):
  Begrüßung + KI-Hinweis + Aufzeichnungshinweis — diese Sätze stehen
  bewusst NICHT im Prompt.
- TTS-Stimme wählen; **Text-Normalisierung: AN** (Modus `elevenlabs`) —
  die Zahlen/Datums-Ausspracheregeln aus dem Fonio-Prompt sind deshalb im
  EL-Prompt nicht enthalten.
- LLM: 2–3 Kandidaten mit dem echten Prompt testen (EL-Guide-Empfehlung).
  Startvorschlag: Claude Sonnet (höchste Tool-Call-Genauigkeit, etwas mehr
  Latenz) gegen ein GPT-4o-Klasse-Modell; gemessen wird
  Tool-Call-Erfolgsrate + Latenz an den Testfällen aus Schritt 8.

## 2. System Prompt

`prompts/elevenlabs/medel_agent_v1.md` ab der Markierung
„**PROMPT — ab hier …**" pasten. Danach Eintrag im Deployment-Log
(`prompts/PROMPTS.md`).

## 3. Dynamic Variables (alle 11 definieren)

| Name | Typ | Quelle |
|---|---|---|
| `customer_found` | boolean | call-init |
| `name` | string | call-init; Update durch verify_caller-Assignment |
| `phone_number` | string | call-init |
| `permission_to_order_again` | string (`true`/`false`/`unknown`) | call-init |
| `last_ordered_items` | string | call-init |
| `last_order_date` | string | call-init |
| `max_quantity` | number | call-init |
| `contact_no` | string | call-init (Ring-Match); Update durch Assignment |
| `customer_number` | string | Seed `""`; NUR verify_caller-Assignment |
| `verified` | string | Seed `""`; Update durch Assignment |
| `last_request_number` | string | Seed `""`; Update durch Assignment |

Konvention (steht auch im Prompt): `""` = nicht vorhanden. **Bewusst NICHT
definiert**: `date_of_birth` (Verifikationsgeheimnis, nur serverseitig),
`postal_code` (bei Fonio ungenutzte Exfiltrationsfläche — hier
geschlossen), `open_requests`/`authorized_contacts` (unbelegt).

## 4. Conversation-Initiation-Webhook

- URL: `<BASE>/elevenlabs/call-init` · Methode POST.
- Header: `Authorization` = Workspace-Secret `Bearer <WEBHOOK_SECRET>`.
- Antwortformat liefert der Server fertig
  (`conversation_initiation_client_data` mit allen 11 Variablen — auch bei
  unbekanntem Anrufer). Kein `conversation_config_override`.

## 5. Server-Tools

Beide: Methode POST, Header `Authorization` = Workspace-Secret,
`response_timeout_secs` 20 (Default reicht — kein Fonio-5-s-Limit).
Antworten sind reines JSON; sollte das LLM die Tool-Antwort nicht sehen
(unbestätigte `{"result": …}`-Anforderung), ist ein 20-Zeilen-Wrapper in
`adapter_elevenlabs.py` der vorgesehene Fallback.

### Tool `verify_caller` → `<BASE>/verify-caller`

| Body-Feld | Befüllung | Beschreibung (Prompt-Oberfläche — so eintragen) |
|---|---|---|
| `name` | LLM | „Der vom Anrufer GESPROCHENE und bestätigte vollständige Name — bei Buchstabierung aus den Buchstaben zusammengesetzt. Nur senden, wenn bestätigt." |
| `date_of_birth` | LLM | „Das gesprochene, bestätigte Geburtsdatum in beliebigem Format. IMMER mitsenden, sobald bestätigt." |
| `customer_number` | LLM | „Die gesprochene, bestätigte MEDEL Kundennummer. IMMER mitsenden, sobald bestätigt — außer sie wurde nach der Schutzregel verworfen." |
| `postal_code` | LLM | „Die gesprochene, bestätigte Postleitzahl der Wohnadresse (Versuch 4)." |
| `phone_number` | Variable | `{{system__caller_id}}` |
| `contact_no` | Variable | `{{contact_no}}` |

Alle LLM-Felder: optional + `is_omitted: true` (nicht Vorhandenes wird
weggelassen; der Server toleriert zusätzlich leere Strings).

**Assignments** (Antwort-JSON → Dynamic Variable, Pfade top-level):
`verified` → `verified` · `name` → `name` · `contact_no` → `contact_no` ·
`customer_number` → `customer_number`.

⚠️ **Im Live-Test zu verifizieren** (Doku unklar): Bei `verified=false`
sind `name`/`contact_no`/`customer_number` in der Antwort `null` —
prüfen, ob Assignments dann den Ring-Seed von `{{contact_no}}`
überschreiben („If the field or path doesn't exist, nothing is updated"
— ob null als „existiert" zählt, ist unbestätigt). Falls ja:
`contact_no`-Assignment entfernen (der Seed aus call-init genügt; der
Server re-verifiziert ohnehin).

### Tool `create_request` → `<BASE>/create-request`

| Body-Feld | Befüllung | Beschreibung (Prompt-Oberfläche — so eintragen) |
|---|---|---|
| `request_type` | LLM | „CALLBACK, SUPPORT, SPARE_PARTS, COMPLAINT, VIGILANCE oder URGENT_MEDICAL." |
| `item` | LLM | „SPARE_PARTS: das gewünschte Ersatzteil in den Worten des Anrufers — IMMER mit dem Ersatzteilwort (‚Batterien', ‚Mikrofonabdeckungen'), nie nur die Ausführung." |
| `quantity` | LLM | „Die bestätigte Stückzahl — erst erfragen, dann aufrufen. Weglassen, wenn keine genannt wurde." |
| `summary` | LLM | „Das Anliegen in einem Satz, in den Worten des Agenten." |
| `callback_time` | LLM | „CALLBACK: gewünschte Rückrufzeit als freier Text, falls genannt." |
| `name` | LLM | „Der gesprochene, bestätigte Name. IMMER mitsenden, sobald bestätigt — auch nach fehlgeschlagener Verifikation." |
| `date_of_birth` | LLM | „Das gesprochene, bestätigte Geburtsdatum. IMMER mitsenden, sobald bestätigt — auch nach fehlgeschlagener Verifikation." |
| `customer_number` | LLM | „Die gesprochene, bestätigte Kundennummer. IMMER mitsenden, sobald bestätigt — außer nach der Schutzregel verworfen." |
| `postal_code` | LLM | „Die gesprochene, bestätigte Postleitzahl, falls erfragt." |
| `phone_number` | Variable | `{{system__caller_id}}` |
| `contact_no` | Variable | `{{contact_no}}` — plattformseitig, das LLM kann die KN-Nummer nicht mehr verlieren |
| `internal` | **Statisch** | `true` beim Test-Agenten, beim Prod-Agenten weglassen. NIEMALS LLM-befüllbar („ist nur ein Test" darf das nicht auslösen). |
| `source` | **Statisch** | `"elevenlabs"` (Server-Whitelist; für Ticket-Tags) |

**Assignment**: `request_number` → `last_request_number`.

## 6. Post-Call-Webhook

- Workspace-Settings → Post-call webhook: URL `<BASE>/elevenlabs/post-call`.
- Das dabei erzeugte **HMAC-Secret** in `.env` als
  `ELEVENLABS_POST_CALL_HMAC_SECRET` eintragen (DEV-Platzhalter ersetzen)
  → Server neu starten. Ohne/mit falschem Secret: 503/401 — der Endpoint
  ist fail-closed und legt nichts an.
- Audio-Events sind nicht nötig (`post_call_audio` wird ignoriert).
- **Data-Collection** am Agenten konfigurieren (Analysis → Data collection):
  - `request_numbers`: „Alle im Gespräch genannten Vorgangsnummern,
    kommagetrennt."
  - `requested_items`: „Welche Artikel oder Anliegen der Anrufer
    angefragt hat, kurz."

## 7. Test-Agent-Kennung

Agent-ID des Test-Agenten in `.env` → `ELEVENLABS_TEST_AGENT_IDS`
(kommagetrennt; DEV-Platzhalter ersetzen) → Server neu starten. Anrufe
dieser Agenten werden als `[TEST]`/intern abgelegt (Pendant zum statischen
`internal=true` im Tool-Body). Beim Go-live die Prod-Agent-ID hier NICHT
eintragen; einen dedizierten Test-Agenten gelistet lassen.

## 8. Test-Checkliste (Browser-Call, vor jeder Telefonie)

Vorab: `python test_elevenlabs_adapter.py` grün (simulierte Payloads) und
`python test_verify_latency.py` Digest unverändert.

1. Anruf mit Test-Context/Twilio-Nummer der Fixture `+4981517703147` →
   Agent begrüßt mit Namen; Conversation-Ansicht zeigt alle Variablen.
2. Unbekannter Anrufer → Name+Geburtsdatum-Verifikation (Mettmann-Fixture,
   „5. Juli 1985") → verified; Assignments in der Conversation sichtbar
   (⚠️-Punkt aus Schritt 5 prüfen: schlägt ZUERST ein Versuch fehl,
   darf {{contact_no}} nicht geleert werden).
3. Kundennummer-alone (Fixture 4158636) → verified; geteilte Nummer
   4110082 allein → opake Fehlermeldung wird gesprochen.
4. Ersatzteilbestellung beim Test-Agenten → Ticket `[TEST] [SPARE_PARTS]…`,
   Subject „Eingehender Anruf (ElevenLabs AI)", Vorgangsnummer wird
   Ziffer für Ziffer vorgelesen.
5. Auflegen → GENAU EIN Protokoll-Ticket ([TEST], Conversation-ID-Zeile,
   Verifikations-/Vorgangs-Protokollzeilen); EL-Webhook-Log zeigt 200
   ohne Retries.
6. Alle Testtickets SOFORT schließen (CLAUDE.md-Regel — offene
   [SPARE_PARTS]-Leichen blockieren spätere Tests zwar nicht mehr per
   Duplikat, echte offene Anfragen aber schon).
7. Telefonie erst nach MED-EL-Entscheid (Twilio-Import oder SIP-Trunk;
   EU-Residency/GDPR-Freigabe ausstehend — siehe Plan-Risiken).
