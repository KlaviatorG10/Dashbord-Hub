# Dashbord-Hub

Dette repoet inneholder FastAPI-huben som styrer kommunikasjonen mellom MIDI-input, Unity VR og nRF54L15-mikrokontrolleren.

# Oppsett for lokal kjøring

Følg disse stegene for å kjøre dashboardet på din egen maskin:

1. Klon repoet
Åpne terminalen og naviger til mappen der du vil ha prosjektet:
git clone https://github.com/KlaviatorG10/Klaviator-Hub.git
cd Klaviator-Hub

2. Virtuelt miljø (venv)
Det anbefales å bruke venv for å holde bibliotekene isolert:
python -m venv venv

Aktiver miljøet:
Mac/Linux: source venv/bin/activate
Windows: venv\Scripts\activate

3. Installer biblioteker
Når venv er aktiv, installer alt fra requirements.txt:
pip install -r requirements.txt

4. Start serveren
Kjør denne kommandoen for å starte FastAPI:
uvicorn main:app --reload

Dashboardet er nå tilgjengelig på: http://127.0.0.1:8000

# Filstruktur
- main.py: Selve motoren. Her ligger logikken for MIDI, WebSockets og UART-sending.
- index.html: Front-end dashboard for visualisering.
- midi_library/: Mappe med MIDI-filer for testing.
- requirements.txt: Liste over alle nødvendige Python-pakker.

# UART Protokoll
Data som sendes fra Hub til nRF54L15 følger dette formatet:
STATE:NOTE:VELOCITY\n (Eks: ON:60:127\n)

Ta kontakt med Yousef (Tech Lead) hvis du har spørsmål om oppsettet.

