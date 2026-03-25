@echo off
REM Script for å starte Klaviator-Hub på Windows

echo 🚀 Starter Klaviator Dashboard Hub...

REM Opprett venv hvis den ikke finnes
if not exist venv (
    echo 📦 Oppretter nytt virtuelt miljø (venv)...
    python -m venv venv
)

REM Aktiver venv
call venv\Scripts\activate

REM Installer/Oppdater biblioteker
echo 📥 Installerer biblioteker...
pip install -r requirements.txt

REM Start FastAPI
echo ✅ Serveren starter nå på http://127.0.0.1:8000
uvicorn main:app --reload
pause
