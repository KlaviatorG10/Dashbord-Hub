# 🎹 Klaviator: Unity Integration Guide (PRO)

Denne guiden forklarer hvordan Unity kobles til **Klaviator Dashboard** (Python/FastAPI) på din maskin.

---

## 1. Oppsett i Unity
1.  **Installer WebSocket:**
    *   Gå til **Window** -> **Package Manager** -> **+** -> **Add package from git URL...**.
    *   Lim inn: `https://github.com/endel/NativeWebSocket.git#upm`
2.  **Lag Script:**
    *   Lag et nytt C#-script kalt `KlaviatorReceiver.cs`.
    *   Lim inn koden fra **Punkt 3** nedenfor.
3.  **Koble sammen:**
    *   Legg `KlaviatorReceiver`-scriptet på et objekt i scenen din.
    *   Dra `PianoKeyController`-objektet ditt inn i feltet "Piano Controller" i Inspector.

---

## 2. Tilkoblingsdetaljer
Serveren kjører på din IP. Unity må bruke denne URL-en:

*   **URL:** `ws://10.36.180.240:8000/ws/unity` (Oppdater denne hvis din IP endrer seg!)

---

## 3. C# Script (Klar for Bibliotek & Synkronisering)

```csharp
using NativeWebSocket;
using UnityEngine;
using System;

[Serializable]
public class KlaviatorEvent {
    public string @event; 
    public KlaviatorData data;
}

[Serializable]
public class KlaviatorData {
    public int note;
    public int velocity;
    public string[] files; // Her havner sangnavnene fra biblioteket!
}

public class KlaviatorReceiver : MonoBehaviour {
    public PianoKeyController pianoController; 
    private WebSocket websocket;

    async void Start() {
        websocket = new WebSocket("ws://10.36.180.240:8000/ws/unity");

        websocket.OnOpen += () => Debug.Log("<color=green>Tilkoblet Yousefs Dashboard!</color>");
        websocket.OnMessage += (bytes) => {
            string message = System.Text.Encoding.UTF8.GetString(bytes);
            ProcessEvent(message);
        };

        await websocket.Connect();
    }

    void Update() {
        #if !UNITY_WEBGL || UNITY_EDITOR
            websocket.DispatchMessageQueue();
        #endif
    }

    private void ProcessEvent(string json) {
        var evt = JsonUtility.FromJson<KlaviatorEvent>(json);
        
        if (evt.@event == "library_update") {
            Debug.Log("Sanger mottatt! Antall: " + evt.data.files.Length);
            // FYLL DIN UI-LISTE HER med evt.data.files
        } else if (evt.@event == "note_on") {
            pianoController.NoteOn(evt.data.note, evt.data.velocity);
        } else if (evt.@event == "note_off") {
            pianoController.NoteOff(evt.data.note);
        }
    }

    // Kall denne for å starte en sang fra Unity
    public void RequestPlay(string filename) {
        string msg = "{\"action\": \"play_file\", \"value\": \"" + filename + "\"}";
        websocket.SendText(msg);
    }

    private async void OnApplicationQuit() {
        if (websocket != null) await websocket.Close();
    }
}
```

---

## 4. Hvordan synkroniseringen fungerer (Look-Ahead)
For å løse problemet med fysisk treghet i roboten, bruker dashboardet en **Look-Ahead**-verdi (ms).

1.  **Dashboardet** sender signal til roboten (Solenoid) *umiddelbart*.
2.  **Dashboardet** venter i f.eks. **50ms** (basert på slideren).
3.  **Unity** mottar WebSocket-meldingen *etter* disse 50ms.

**Kalibrering:** Hvis du ser at animasjonen i Unity skjer *før* roboten lager lyd, må dashboard-operatøren **øke** Look-ahead verdien. Da blir Unity-signalet forsinket akkurat nok til at det treffer lyden perfekt.

---

## 5. JSON Format (For Referanse)
Hver gang en note spilles, sender Python-serveren dette formatet:

```json
{
  "event": "note_on", 
  "data": {
    "note": 60, 
    "velocity": 100, 
    "timestamp": 1741168000000
  }
}
```
når biblioteket oppdateres sendes dette:
```json
{
  "event": "library_update", 
  "data": {
    "files": ["bach.mid", "beethoven.mid"]
  }
}
```
