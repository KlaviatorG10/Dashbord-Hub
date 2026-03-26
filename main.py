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
import threading
import queue

app = FastAPI(title="Klaviator Dashboard")

# Finn mappen der main.py ligger
current_dir = os.path.dirname(os.path.abspath(__file__))

# --- MODULÆR ARKITEKTUR: HJERNEN (Mapping Logic & Hardware) ---
class HardwareController:
    def __init__(self):
        self.look_ahead_ms = 50
        self.serial_port = None
        self.is_connected = False
        self.loop = None
        self.send_queue = queue.Queue()

    def _try_connect_serial(self):
        """Forsøker å finne og koble til nRF54L15 over UART"""
        if self.is_connected:
            return

        try:
            ports = list(serial.tools.list_ports.comports())
            target_port = None
            
            jlink_ports = [p.device for p in ports if "usbmodem" in p.device.lower()]
            if jlink_ports:
                jlink_ports.sort(reverse=True) 
                target_port = jlink_ports[0]
            
            if target_port:
                # Vi bruker en blokkerende serieport i en egen tråd
                self.serial_port = serial.Serial(target_port, 115200, timeout=1)
                self.is_connected = True
                print(f"[HARDWARE] Tilkoblet mikrokontroller på {target_port}")
                
                # Lagre referanse til event-loopen for å kunne sende WebSockets fra tråden
                self.loop = asyncio.get_running_loop()
                
                # Start lytter- og sender-tråden
                thread = threading.Thread(target=self._serial_worker_thread, daemon=True)
                thread.start()
            else:
                self.is_connected = False
        except Exception as e:
            self.is_connected = False
            print(f"[HARDWARE] Feil under tilkobling: {e}")

    def _serial_worker_thread(self):
        """Kjører i en egen tråd: Håndterer all kommunikasjon uten å blokkere Hjernen"""
        print("[HARDWARE] Nervesystem-tråd startet.")
        while self.is_connected and self.serial_port:
            try:
                # 1. LES FRA BRIKKEN (RX)
                if self.serial_port.in_waiting > 0:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        print(f"[BRIKKE SVARER] {line}")
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(
                                broadcast_event("serial_log", {"type": "receive", "message": line}), 
                                self.loop
                            )
                
                # 2. SEND TIL BRIKKEN (TX)
                try:
                    cmd = self.send_queue.get_nowait()
                    self.serial_port.write(cmd.encode('utf-8'))
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(
                            broadcast_event("serial_log", {"type": "send", "message": cmd.strip()}), 
                            self.loop
                        )
                except queue.Empty:
                    pass
                
                time.sleep(0.001) # Unngå 100% CPU
                
            except Exception:
                break
        self.is_connected = False

    def set_look_ahead(self, ms: int):
        self.look_ahead_ms = ms
        print(f"[HJERNE] Look-ahead oppdatert til {ms}ms")

    async def actuate(self, note: int, velocity: int, is_on: bool):
        """Legger kommando i køen (lynraskt)"""
        if not self.is_connected:
            self._try_connect_serial()
        
        state = "ON" if is_on else "OFF"
        cmd = f"{state}:{note}:{velocity}\n"
        self.send_queue.put(cmd)

# Initierer maskinvarekontrolleren (Hjernen)
hardware = HardwareController()

@app.on_event("startup")
async def startup_event():
    hardware._try_connect_serial()

# --- GLOBALE VARIABLER (Dashboard State) ---
connected_clients = []
current_playback_task = None
is_paused = False

MIDI_LIB_PATH = os.path.join(current_dir, "midi_library")

@app.get("/api/library")
async def get_midi_library():
    files = [f for f in os.listdir(MIDI_LIB_PATH) if f.endswith(('.mid', '.midi'))]
    return {"files": files}

@app.get("/api/play_library/{filename}")
async def play_from_library(filename: str):
    global current_playback_task, is_paused
    is_paused = False
    file_path = os.path.join(MIDI_LIB_PATH, filename)
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
    try:
        while True:
            data = await websocket.receive_text()
            parsed = json.loads(data)
            if parsed.get("action") == "set_offset":
                hardware.set_look_ahead(int(parsed.get("value", 50)))
            elif parsed.get("action") == "play_file":
                filename = parsed.get("value")
                asyncio.create_task(play_from_library(filename))
    except WebSocketDisconnect:
        connected_clients.remove(websocket)

async def broadcast_event(event_type: str, data: dict):
    message = json.dumps({"event": event_type, "data": data})
    for client in connected_clients:
        try:
            await client.send_text(message)
        except Exception:
            pass

async def process_note_event(event_type: str, note: int, velocity: int):
    is_on = (event_type == "note_on")
    await hardware.actuate(note, velocity, is_on)
    
    async def delayed_broadcast():
        current_offset = hardware.look_ahead_ms
        if current_offset > 0:
            await asyncio.sleep(current_offset / 1000.0)
        await broadcast_event(event_type, {"note": note, "velocity": velocity, "timestamp": int(time.time() * 1000)})
    
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
            if msg.type == 'note_on':
                asyncio.create_task(process_note_event("note_on", msg.note, msg.velocity)) if msg.velocity > 0 else asyncio.create_task(process_note_event("note_off", msg.note, 0))
            elif msg.type == 'note_off':
                asyncio.create_task(process_note_event("note_off", msg.note, 0))
        await broadcast_event("playback_finish", {})
    except asyncio.CancelledError:
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
