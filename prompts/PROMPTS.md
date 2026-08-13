# Prompt-Versionierung (Fonio + ElevenLabs)

Single source of truth für alle Assistant-Prompts. Die Dashboards (Fonio,
ElevenLabs) sind reine Deploy-Ziele — jede Änderung passiert ZUERST hier,
dann wird gepastet und der Paste unten protokolliert.

## Struktur

```
prompts/
  PROMPTS.md                         diese Datei: Konvention + Deployment-Log
  fonio/
    master_prompt_builder_v4.md      Fonios generisches Builder-Template (Referenz)
    medel_assistant_v5.md            LIVE Fonio-Assistant-Prompt
  elevenlabs/
    medel_agent_v1.md                ElevenLabs-Agent-Prompt (Sieben-Block-Struktur)
```

## Konvention

1. **Dateiname trägt die Major-Version** (`_v5`). Eine neue Major-Version ist
   eine NEUE Datei; die alte bleibt liegen und wird im Header als
   `superseded by vN` markiert. (Gelebte Praxis seit v4→v5 — hier nur
   festgeschrieben.)
2. **Header-Block** am Dateianfang jedes Prompts: Version, Datum, Status
   (`draft` / `live im Dashboard seit <Datum>` / `superseded by vN`),
   Zielplattform + Dashboard-Ort, danach ein **Änderungen-Abschnitt** für
   Edits innerhalb der Version (nummeriert, mit Datum und Anlass — wie in
   `medel_assistant_v5.md`).
3. **Deployment-Log** (unten): eine Zeile pro Dashboard-Paste. Das schließt
   die dokumentierte Diagnose-Lücke „ist der Paste überhaupt live?" —
   bei „Prompt-Änderung wirkt nicht" IMMER zuerst hier nachsehen und dann
   die Satzskelette des Agenten mit dem Prompttext vergleichen (Teil-Echos
   neuer Formulierungen beweisen einen live-Paste; Lektion 2026-08-11).
4. **Re-Audit-Pflicht:** Nach JEDER Server-Contract-Änderung (neue/geänderte
   Endpoints, Felder, reason_codes, Variablennamen) werden BEIDE
   Plattform-Prompts gegen den neuen Contract auditiert — Prompt und Server
   driften sonst lautlos auseinander (Lektion 2026-07-29: der Prompt
   verlangte noch Name+Geburtsdatum, obwohl Kundennummer-alone seit Wochen
   vollwertig war).
5. Kein Policy-Fork zwischen Plattformen: Geschäftsregeln (Verifikation
   §10.1, Bestellkontrollen, Notfall-Skripte) müssen inhaltlich identisch
   sein; nur Struktur und Plattform-Mechanik (Variablen-Seeding,
   Assignments, TTS-Normalisierung) dürfen abweichen. Bei Policy-Änderungen
   beide Dateien im selben Commit anfassen.

## Deployment-Log

| Datum | Plattform | Stand | Bemerkung |
|---|---|---|---|
| 2026-08-11 | Fonio | v5 bis Edit 12 | Live-Paste durch Satzskelett-Echos im Mettmann-Testanruf bestätigt |
| — | Fonio | v5 Edits 13–14 | Paste im Dashboard NICHT bestätigt — vor dem nächsten Test prüfen |
| — | ElevenLabs | v1 (draft) | Noch nie gepastet; Dashboard-Einrichtung per `elevenlabs_wiring_guide.md` |
