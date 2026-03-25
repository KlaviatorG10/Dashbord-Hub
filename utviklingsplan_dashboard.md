# Utviklingsplan: Klaviator Dashboard

**Dato:** 4. mars 2026
**Prosjekt:** KLAVIATOR (Bachelor 2026)
**Dokument:** Roadmap og oppgaveoversikt for Dashboard-utvikling

Dette dokumentet skisserer veien videre for utviklingen av Dashboardet (som fungerer som Tolken og Hjernen i A-G modellen). Målet er å bygge ut funksjonaliteten trinnvis, slik at all logikk er på plass og testet før den fysiske maskinvaren integreres.

---

## 🚀 Fase 1: Kjernefunksjonalitet (Hva vi kan bygge nå)

Disse oppgavene krever ikke fysisk maskinvare, og kan utvikles og testes umiddelbart i samarbeid med Unity-modellen.

### 1. Bygge inn MIDI-leser (Tolken - B)
*   **Hva:** Integrere Python-biblioteket `mido`.
*   **Mål:** Kunne laste opp en `.mid` fil i dashboardet, lese sporet og automatisk sende ut `note_on` og `note_off` via WebSockets i riktig tempo (BPM).
*   **Verdi:** Systemet går fra manuelle knappetrykk til autonom avspilling.

### 2. Implementere "Look-ahead Offset" (Hjernen - C)
*   **Hva:** Koble offset-slideren i UI sammen med backend.
*   **Mål:** Når et signal sendes, skal Python beregne en forsinkelse. F.eks. sende fysisk aktueringssignal ved tidspunkt $T$, men holde tilbake WebSocket-signalet til Unity til tidspunkt $T + offset$.
*   **Verdi:** Dette er selve kjerneløsningen på bacheloroppgavens problemstilling rundt perseptuell synkronisering.

### 3. Sette opp Seriell-kommunikasjon (Nervesystemet - D)
*   **Hva:** Integrere `pyserial`.
*   **Mål:** Bytte ut `[MOCK]`-printen i terminalen med reell overføring av bytes via USB-porten (UART) til valgt mikrokontroller.
*   **Verdi:** Sikrer at dashboardet er "plug and play" den dagen elektro-teamet har kontrolleren klar. Har innebygd fallback ("mock mode") hvis kabel ikke er koblet til.

---

## 🌟 Fase 2: Avanserte funksjoner og UI-forbedringer (5 nye initiativer)

Dette er funksjonalitet som vil gjøre systemet mer robust, imponerende under presentasjoner, og gi verdifull data til selve bachelorrapporten.

### 4. Visuell "Piano Roll" og Note-Tidslinje i UI
*   **Hva:** Bygge en visuell representasjon i HTML Canvas eller SVG i dashboardet.
*   **Mål:** Vise hvilke noter som spilles akkurat nå, og hvilke som kommer de neste sekundene (som et "Guitar Hero"-grensesnitt).
*   **Verdi:** Gir ekstremt god visuell kontroll under avspilling og gjør dashboardet mye mer imponerende for sensorer.

### 5. Kalibreringsverktøy for PWM og Dynamics (Velocity Mapping)
*   **Hva:** Et eget panel i dashboardet for å tune kraften på solenoidene.
*   **Mål:** MIDI `velocity` går fra 0-127. Vi trenger et UI for å "mappe" dette til PWM-verdier for mikrokontrolleren. F.eks: "MIDI 100 tilsvarer 85% PWM på aktuatoren".
*   **Verdi:** Fysiske solenoider oppfører seg ulikt. Å kunne tune dette live fra dashboardet, uten å programmere om mikrokontrolleren, sparer enormt med tid under testing.

### 6. System-Logging og "Metrics" for Rapporten
*   **Hva:** Lagre nøyaktige tidsstempler for alle operasjoner i en CSV-fil.
*   **Mål:** Måle hvor lang tid Python bruker på å parse en note og sende den videre (CPU-latency).
*   **Verdi:** Data! Til bachelorrapporten trenger vi grafer som beviser at systemet vårt er raskt. Å bygge inn automatisk datainnsamling i dashboardet gir oss "gratis" innhold til resultatkapittelet i rapporten.

### 7. "Panic Button" / Nødstopp for roboten
*   **Hva:** En stor rød knapp i dashboardet.
*   **Mål:** Å trykke på denne knappen sender umiddelbart et "ALL NOTES OFF"-signal til både Unity og mikrokontrolleren, og kutter PWM-strømmen.
*   **Verdi:** Livsviktig sikkerhetsfunksjon når man jobber med fysiske aktuatorer som kan kile seg fast eller trekke for mye strøm og brenne opp.

### 8. Live Keyboard-input (MIDI over USB)
*   **Hva:** Støtte for å koble et fysisk MIDI-keyboard (synthesizer) inn i PC-en som kjører dashboardet.
*   **Mål:** Istedenfor å spille av en forhåndsinnspilt fil, lytter dashboardet til at noen spiller på et fysisk keyboard, og videresender dette direkte til roboten og Unity.
*   **Verdi:** Gir en utrolig interaktiv "live"-demo til den avsluttende presentasjonen i mai, hvor sensor kan spille på et keyboard, og roboten og Unity kopierer det live.

---

## 📅 Oppdatert Status (Sist redigert: 4. mars)
*   **Fase 1 FULLFØRT:**
    *   MIDI-leser (Tolken) er asynkron og funksjonell.
    *   Look-ahead (Hjernen) er kodet inn via `process_note_event` og synkronisert med web-slideren.
    *   Seriell-kommunikasjon (`pyserial`) er opprettet med en egen `HardwareController`-klasse.
*   **Fase 2 PÅBEGYNT:**
    *   ✅ Visuell "Piano Roll" er ferdig og bygget med presist HTML Canvas.
    *   ✅ System-Logging og Metrics: Vi har bygget inn CSV-nedlasting for CPU-latency direkte fra nettleseren.
    *   ✅ "Panic Button" (Nødstopp) er funksjonell (Stopper asynkron avspilling umiddelbart).

## 🚀 NESTE OPPGAVER (To-Do for neste økt):
1.  **Kalibreringsverktøy (Velocity Mapping):** Bygge et GUI (Modal) for å mappe MIDI-styrke (0-127) til individuelle PWM-multiplikatorer per tangent. Dette er kritisk for å "tune" den fysiske maskinen når mekanikken er ferdig.
2.  **Integrasjonstest med Unity:** Sette seg ned med Unity-utvikleren, koble C#-klienten hennes til `ws://127.0.0.1:8000/ws/unity`, og verifisere at den digitale tvillingen reagerer feilfritt på JSON-dataene våre.
