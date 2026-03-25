#!/bin/bash
# Script for å starte Klaviator-Hub på Mac/Linux

echo "🚀 Starter Klaviator Dashboard Hub..."

# Opprett venv hvis den ikke finnes
if [ ! -d "venv" ]; then
    echo "📦 Oppretter nytt virtuelt miljø (venv)..."
    python3 -m venv venv
fi

# Aktiver venv
source venv/bin/activate

# Installer/Oppdater biblioteker
echo "📥 Installerer biblioteker..."
pip install -r requirements.txt

# Start FastAPI
echo "✅ Serveren starter nå på http://127.0.0.1:8000"
uvicorn main:app --reload
