from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import time
import json
import asyncio
import mido
import os
import serial
import serial.tools.list_ports

app = FastAPI(title="Klaviator Dashboard")

# Finn mappen der main.py ligger
current_dir = os.path.dirname(os.path.abspath(__file__))

# --- MODULÆR ARKITEKTUR: HJERNEN (Mapping Logic & Hardware) ---
class HardwareController:
    def __init__(self):
        self.look_ahead_ms = 50
        self.serial_port = None
        self.is_connected = False
        self._try_connect_serial()

    def _try_connect_serial(self):
        """Forsøker å finne og koble til nRF54L15 over UART"""
        try:
            ports = list(serial.tools.list_ports.comports())
            target_port = None
            
            for port in ports:
                description = port.description.lower()
                device = port.device.lower()
                # Se etter 'usbmodem' eller 'usb serial' som spesifisert
                if "usbmodem" in device or "usb serial" in description:
                    target_port = port.device
                    break
            
            if target_port:
                self.serial_port = serial.Serial(target_port, 115200, timeout=1)
                self.is_connected = True
                print(f"[HARDWARE] Tilkoblet mikrokontroller på {target_port}")
            else:
                self.is_connected = False
                print("[HARDWARE] Ingen passende USB-enhet funnet (usbmodem/usb serial).")
        except Exception as e:
            self.is_connected = False
            print(f"[HARDWARE] Feil under tilkobling: {e}. Kjører i MOCK-modus.")

    def set_look_ahead(self, ms: int):
        self.look_ahead_ms = ms
        print(f"[HJERNE] Look-ahead oppdatert til {ms}ms")

    async def actuate(self, note: int, velocity: int, is_on: bool):
        """Sender kommando til robot over UART: STATE:NOTE:VELOCITY\n"""
        if not self.is_connected or not self.serial_port:
            # Forsøk periodisk re-tilkobling hvis vi ikke er tilkoblet
            self._try_connect_serial()

        if self.is_connected and self.serial_port:
            try:
                state = "ON" if is_on else "OFF"
                cmd = f"{state}:{note}:{velocity}\n"
                self.serial_port.write(cmd.encode('utf-8'))
            except Exception as e:
                print(f"[HARDWARE] Mistet kobling: {e}")
                self.is_connected = False
                if self.serial_port:
                    try:
                        self.serial_port.close()
                    except:
                        pass
                    self.serial_port = None
        else:
            # Mock-modus eller ikke tilkoblet
            pass

# Initierer maskinvarekontrolleren (Hjernen)
hardware = HardwareController()

# --- GLOBALE VARIABLER (Dashboard State) ---
connected_clients = []
current_playback_task = None
is_paused = False

MIDI_LIB_PATH = os.path.join(current_dir, "midi_library")
if not os.path.exists(MIDI_LIB_PATH):
    os.makedirs(MIDI_LIB_PATH)

@app.get("/api/library")
async def get_midi_library():
    files = [f for f in os.listdir(MIDI_LIB_PATH) if f.endswith(('.mid', '.midi'))]
    return {"files": files}

@app.get("/api/play_library/{filename}")
async def play_from_library(filename: str):
    global current_playback_task, is_paused
    is_paused = False
    file_path = os.path.join(MIDI_LIB_PATH, filename)
    if not os.path.exists(file_path):
        return {"error": "Filen finnes ikke"}
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(file_path, delete_after=False))
    return {"status": "playing", "file": filename}

@app.get("/")
async def get():
    index_path = os.path.join(current_dir, "index.html")
    with open(index_path, "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.get("/logo.png")
async def get_logo():
    logo_path = os.path.join(current_dir, "Klaviator Logo Gold.png")
    return FileResponse(logo_path)

@app.websocket("/ws/unity")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"[WS] Klient koblet til! Totalt: {len(connected_clients)}")
    files = [f for f in os.listdir(MIDI_LIB_PATH) if f.endswith(('.mid', '.midi'))]
    await websocket.send_text(json.dumps({"event": "library_update", "data": {"files": files}}))
    try:
        while True:
            data = await websocket.receive_text()
            try:
                parsed = json.loads(data)
                if parsed.get("action") == "set_offset":
                    hardware.set_look_ahead(int(parsed.get("value", 50)))
                elif parsed.get("action") == "play_file":
                    filename = parsed.get("value")
                    asyncio.create_task(play_from_library(filename))
            except Exception as e:
                print(f"[WS ERROR] {e}")
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print("[WS] Klient koblet fra.")

async def broadcast_event(event_type: str, data: dict):
    message = json.dumps({"event": event_type, "data": data})
    for client in connected_clients:
        try:
            await client.send_text(message)
        except Exception:
            pass

async def process_note_event(event_type: str, note: int, velocity: int):
    """
    Synkroniseringslogikk:
    1. Send til Hardware UMIDDELBART.
    2. Start en bakgrunnsoppgave som venter [look_ahead] før den sender til Unity.
    Dette sikrer at MIDI-loopen ikke blir forsinket.
    """
    is_on = (event_type == "note_on")
    
    # 1. FYSIKK: Aktiver maskinvaren NÅ
    await hardware.actuate(note, velocity, is_on)
    
    # 2. VISUELT: Start en uavhengig timer for Unity
    async def delayed_broadcast():
        current_offset = hardware.look_ahead_ms
        if current_offset > 0:
            await asyncio.sleep(current_offset / 1000.0)
        
        # print(f"[DEBUG] Sender til Unity med offset: {current_offset}ms")
        
        data = {
            "note": note,
            "velocity": velocity,
            "timestamp": int(time.time() * 1000)
        }
        await broadcast_event(event_type, data)
    
    # Kjør timeren i bakgrunnen uten å vente (await) på den her
    asyncio.create_task(delayed_broadcast())

@app.get("/api/test_note/{note_id}")
async def trigger_test_note(note_id: int):
    asyncio.create_task(process_note_event("note_on", note_id, 100))
    await asyncio.sleep(0.5)
    asyncio.create_task(process_note_event("note_off", note_id, 0))
    return {"status": f"Spilte note {note_id}"}

async def play_midi_background(filepath: str, delete_after: bool = True):
    global is_paused
    try:
        mid = mido.MidiFile(filepath)
        print(f"[PLAYBACK] Starter: {filepath}")
        total_notes = sum(1 for msg in mid if msg.type == 'note_on' and msg.velocity > 0)
        await broadcast_event("playback_start", {"file": os.path.basename(filepath), "total_notes": total_notes})
        for msg in mid:
            while is_paused: await asyncio.sleep(0.1)
            if msg.time > 0:
                st = msg.time
                while st > 0:
                    if is_paused: await asyncio.sleep(0.1)
                    else:
                        chunk = min(st, 0.02)
                        await asyncio.sleep(chunk)
                        st -= chunk
            if msg.type == 'set_tempo':
                bpm = mido.tempo2bpm(msg.tempo)
                await broadcast_event("bpm_change", {"bpm": round(bpm)})
            elif msg.type == 'note_on':
                asyncio.create_task(process_note_event("note_on", msg.note, msg.velocity)) if msg.velocity > 0 else asyncio.create_task(process_note_event("note_off", msg.note, 0))
            elif msg.type == 'note_off':
                asyncio.create_task(process_note_event("note_off", msg.note, 0))
        print("[PLAYBACK] Ferdig.")
        await broadcast_event("playback_finish", {})
    except asyncio.CancelledError:
        print("[PLAYBACK] Avbrutt.")
        await broadcast_event("system_halt", {})
    finally:
        if delete_after and os.path.exists(filepath): os.remove(filepath)

@app.get("/api/playback/pause")
async def pause_playback():
    global is_paused
    is_paused = True
    return {"status": "paused"}

@app.get("/api/playback/resume")
async def resume_playback():
    global is_paused
    is_paused = False
    return {"status": "resumed"}

@app.get("/api/playback/stop")
async def stop_playback():
    global current_playback_task, is_paused
    is_paused = False
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    await broadcast_event("system_halt", {})
    return {"status": "stopped"}

@app.post("/api/upload_midi")
async def upload_midi(file: UploadFile = File(...)):
    global current_playback_task, is_paused
    is_paused = False
    file_location = os.path.join(current_dir, f"temp_{file.filename}")
    with open(file_location, "wb+") as file_object:
        file_object.write(await file.read())
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(file_location))
    return {"status": "success", "filename": file.filename}

@app.get("/api/estop")
async def emergency_stop():
    global current_playback_task
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    return {"status": "halted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
