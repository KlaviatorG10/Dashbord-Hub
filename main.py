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
from kdaa_logic import KDAAScheduler

app = FastAPI(title="Klaviator Dashboard")

current_dir = os.path.dirname(os.path.abspath(__file__))

class HardwareController:
    def __init__(self):
        self.look_ahead_ms = 50
        self.serial_ports = []
        self.is_connected = False
        self.loop = None
        self.send_queue = queue.Queue()
        self.scheduler = KDAAScheduler()
        self.sent_timestamps = {} # For RTT måling

    def send_kdaa_event(self, event):
        if event['type'] == 'MOVE':
            cmd = f"M:{event['hand'][0]}:{int(event['pos'])}:{int(event['timestamp'])}\n"
        else:
            cmd = f"S:{event['hand'][0]}:{event['note']}:{event['vel']}:{int(event['timestamp'])}\n"
        self.send_queue.put(cmd)

    def send_sync(self):
        self.send_queue.put("SYNC:0\n")

    def _try_connect_serial(self):
        if self.is_connected:
            return
        try:
            target_port = "/dev/tty.usbmodem0010518293143"
            print(f"\n[HARDWARE] Kobler til nRF54LM20 på {target_port}...")

            try:
                # Åpner porten med 1M Baud og Hardware Flow Control (RTS/CTS)
                ser = serial.Serial(
                    port=target_port, 
                    baudrate=1000000, 
                    rtscts=True, 
                    timeout=0
                )
                self.serial_ports.append(ser)
                print(f"  - TILKOBLET: {target_port} @ 1M Baud med RTS/CTS")
                self.is_connected = True
                self.loop = asyncio.get_running_loop()
                thread = threading.Thread(target=self._serial_worker_thread, daemon=True)
                thread.start()
            except Exception as e:
                print(f"  - KUNNE IKKE ÅPNE {target_port}: {e}")
                self.is_connected = False

        except Exception as e:
            self.is_connected = False
            print(f"[HARDWARE] Kritisk feil under tilkobling: {e}")

    def _serial_worker_thread(self):
        print(f"[HARDWARE] Nervesystem-tråd startet. Lytter på {len(self.serial_ports)} porter.")
        rx_buffers = {ser.port: "" for ser in self.serial_ports}
        while self.is_connected and self.serial_ports:
            try:
                # 1. SEND LOGIKK (med tidsstempling)
                while not self.send_queue.empty():
                    try:
                        cmd = self.send_queue.get_nowait()
                        encoded_cmd = cmd.encode('utf-8')

                        # Lagre tidsstempel for RTT-måling (kun for kommandoer som starter med M: eller S:)
                        if cmd.startswith(('M:', 'S:')):
                            self.sent_timestamps[cmd.strip()] = time.perf_counter()

                        for ser in self.serial_ports:
                            ser.write(encoded_cmd)

                        if self.loop:
                            asyncio.run_coroutine_threadsafe(
                                broadcast_event("serial_log", {"type": "send", "message": cmd.strip()}),
                                self.loop
                            )
                    except queue.Empty:
                        break

                # 2. MOTTAK LOGIKK (med RTT-beregning)
                for ser in self.serial_ports:
                    if ser.in_waiting > 0:
                        data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                        rx_buffers[ser.port] += data
                        if "\n" in rx_buffers[ser.port]:
                            lines = rx_buffers[ser.port].split("\n")
                            rx_buffers[ser.port] = lines.pop()
                            for line in lines:
                                line = line.strip()
                                if line:
                                    # Sjekk om dette er et ekko for en sendt kommando
                                    if line in self.sent_timestamps:
                                        latency_ms = (time.perf_counter() - self.sent_timestamps[line]) * 1000
                                        del self.sent_timestamps[line] # Rydd opp
                                        if self.loop:
                                            asyncio.run_coroutine_threadsafe(
                                                broadcast_event("rtt_update", {"latency": round(latency_ms, 2)}),
                                                self.loop
                                            )

                                    if self.loop:
                                        asyncio.run_coroutine_threadsafe(
                                            broadcast_event("serial_log", {"type": "receive", "message": line}),
                                            self.loop
                                        )
                                        asyncio.run_coroutine_threadsafe(
                                            broadcast_event("hw_status", {"status": "connected", "port": ser.port}),
                                            self.loop
                                        )
                time.sleep(0.0001)
            except Exception as e:
                print(f"[HARDWARE] Tråd-feil: {e}")
                break
        self.is_connected = False
    def set_look_ahead(self, ms: int):
        self.look_ahead_ms = ms

hardware = HardwareController()

@app.on_event("startup")
async def startup_event():
    hardware._try_connect_serial()

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
    if not connected_clients: return
    message = json.dumps({"event": event_type, "data": data})
    for client in connected_clients:
        try:
            await client.send_text(message)
        except Exception:
            pass

async def play_midi_background(filepath: str, delete_after: bool = True):
    global is_paused
    try:
        # 1. Planlegg hele sangen (Lynraskt i Python)
        schedule = hardware.scheduler.generate_schedule(filepath)
        total_notes = sum(1 for e in schedule if e['type'] == 'STRIKE' and e['vel'] > 0)
        
        await broadcast_event("playback_start", {
            "file": os.path.basename(filepath), 
            "total_notes": total_notes, 
            "mode": "KDAA Hybrid Streaming"
        })

        # 2. HYBRID STREAMING: Send de første 2 sekundene med data umiddelbart
        initial_buffer_ms = 2000
        buffered_count = 0
        for event in schedule:
            if event['timestamp'] < initial_buffer_ms:
                hardware.send_kdaa_event(event)
                buffered_count += 1
            else:
                break
        
        # 3. Start avspilling på brikken umiddelbart etter initial buffer
        hardware.send_sync()
        start_time = time.perf_counter()
        
        # 4. Hovedløkke: Håndterer både Drip-feeding og Unity-synkronisering
        event_idx = 0
        schedule_len = len(schedule)
        
        while event_idx < schedule_len:
            event = schedule[event_idx]
            
            # DRIP-FEEDING: Pass på at brikken alltid har de neste 500ms med data
            # Vi bruker 1000ms (1 sek) som vindu for å være helt trygge mot OS-jitter
            now_ms = (time.perf_counter() - start_time) * 1000
            if event['timestamp'] < now_ms + 1000:
                # Hvis eventet ikke allerede er sendt i initial buffer, send det nå
                if event_idx >= buffered_count:
                    hardware.send_kdaa_event(event)
                
                # Hvis dette er et STRIKE-event, håndter Unity-visualisering asynkront
                if event['type'] == 'STRIKE':
                    asyncio.create_task(handle_visual_sync(event, start_time))
                
                event_idx += 1
            else:
                # Vent litt før vi sjekker neste event for å ikke kvele CPU
                await asyncio.sleep(0.005)

        # Vent til siste note er ferdig før vi avslutter
        await asyncio.sleep(2.0)
        await broadcast_event("playback_finish", {})
        
    except asyncio.CancelledError:
        hardware.send_queue.put("STOP\n") 
        await broadcast_event("system_halt", {})
    except Exception as e:
        print(f"[AVSPILLING] KDAA Kritisk feil: {e}")
    finally:
        if delete_after and os.path.exists(filepath): os.remove(filepath)

async def handle_visual_sync(event, start_time):
    """Håndterer synkronisering mot Unity i en egen asynkron oppgave."""
    global is_paused
    target_t = event['visual_target_t'] / 1000.0
    while True:
        if is_paused:
            # Ved pause må vi bare vente, start_time justeres i hovedloopen hvis nødvendig
            # (Pause-logikk bør forbedres for hybrid-streaming hvis aktuelt)
            await asyncio.sleep(0.05)
            continue
            
        now = time.perf_counter()
        offset = hardware.look_ahead_ms / 1000.0
        remaining = (target_t + offset) - (now - start_time)
        
        if remaining <= 0: break
        if remaining > 0.05: await asyncio.sleep(0.01)
        else: await asyncio.sleep(0.001)
        
    is_on = event['vel'] > 0
    await broadcast_event("note_on" if is_on else "note_off", {"note": event['note'], "velocity": event['vel']})

@app.get("/api/test_note/{note_id}")
async def trigger_test_note(note_id: int):
    if not hardware.is_connected:
        hardware._try_connect_serial()
    hardware.send_sync()
    on_event = {'type': 'STRIKE', 'hand': 'LEFT', 'note': note_id, 'vel': 100, 'timestamp': 100}
    hardware.send_kdaa_event(on_event)
    off_event = {'type': 'STRIKE', 'hand': 'LEFT', 'note': note_id, 'vel': 0, 'timestamp': 600}
    hardware.send_kdaa_event(off_event)
    await broadcast_event("note_on", {"note": note_id, "velocity": 100})
    await asyncio.sleep(0.5)
    await broadcast_event("note_off", {"note": note_id, "velocity": 0})
    return {"status": f"KDAA Test sendt for note {note_id}"}

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
        content = await file.read()
        file_object.write(content)
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(file_location))
    return {"status": "success", "filename": file.filename}

@app.get("/api/estop")
async def emergency_stop():
    global current_playback_task
    hardware.send_queue.put("STOP\n")
    if current_playback_task and not current_playback_task.done():
        current_playback_task.cancel()
    return {"status": "halted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
