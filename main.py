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
        # TEST MODE: True = ingen fysisk simulering, False = full KDAA scheduling
        self.scheduler = KDAAScheduler(test_mode=True)
        self.sent_notes = {} # For RTT måling

    def send_kdaa_event(self, event):
        if event['type'] == 'MOVE':
            cmd = f"M:{event['hand'][0]}:{int(event['pos'])}:{max(0, int(event['timestamp']))}\n"
        else:
            # MCU format: S:H:N:V:T:D (Hand, Note, Velocity, Timestamp, Duration)
            duration = event.get('duration', 50)  # Default 50ms hvis ikke spesifisert
            cmd = f"S:{event['hand'][0]}:{event['note']}:{event['vel']}:{max(0, int(event['timestamp']))}:{int(duration)}\n"
            if event['vel'] > 0:
                # Lagre for RTT måling
                if event['note'] not in self.sent_notes:
                    self.sent_notes[event['note']] = []
                self.sent_notes[event['note']].append({
                    "time": time.perf_counter(),
                    "velocity": event['vel']
                })
        self.send_queue.put(cmd)

    def send_sync(self):
        # CRITICAL: Tøm HELE køen før SYNC for å unngå gamle events
        print("[HARDWARE] Tømmer event kø...")
        while not self.send_queue.empty():
            try:
                self.send_queue.get_nowait()
            except:
                break
        
        print("[HARDWARE] Sender SYNC:0...")
        self.sent_notes = {}
        self.send_queue.put("SYNC:0\n")
        
        # Send STOP også for ekstra sikkerhet
        self.send_queue.put("STOP\n")

    def _try_connect_serial(self):
        if self.is_connected: return
        target_port = "/dev/tty.usbmodem0010518293143"
        try:
            # Roocode krav 1: rtscts=True er KRITISK ved 1M Baud for stabilitet
            ser = serial.Serial(port=target_port, baudrate=1000000, rtscts=True, timeout=0)
            self.serial_ports.append(ser)
            self.is_connected = True
            self.loop = asyncio.get_running_loop()
            threading.Thread(target=self._serial_worker_thread, daemon=True).start()
            print(f"[HARDWARE] Tilkoblet {target_port} med aktiv Hardware Flow Control (RTS/CTS)")
        except Exception as e:
            print(f"[HARDWARE] Tilkoblingsfeil: {e}")

    def _serial_worker_thread(self):
        rx_buffers = {ser.port: "" for ser in self.serial_ports}
        commands_sent = 0
        while self.is_connected:
            try:
                # 1. Send data med ØKET pacing for å unngå buffer overflow på MCU
                while not self.send_queue.empty():
                    cmd = self.send_queue.get_nowait()
                    for ser in self.serial_ports:
                        ser.write(cmd.encode('utf-8'))
                        # ØKT PAUSE: 2ms mellom hver kommando for å la MCU prosessere
                        time.sleep(0.002)
                        
                        commands_sent += 1
                        # Debug log for første 50 kommandoer
                        if commands_sent <= 50:
                            print(f"[TX #{commands_sent}] {cmd.strip()}")
                        
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(
                                broadcast_event("serial_log", {"type": "send", "message": cmd.strip()}),
                                self.loop
                            )
                
                # 2. Motta data (HIT: parsing)
                for ser in self.serial_ports:
                    if ser.in_waiting > 0:
                        data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                        rx_buffers[ser.port] += data
                        if "\n" in rx_buffers[ser.port]:
                            lines = rx_buffers[ser.port].split("\n")
                            rx_buffers[ser.port] = lines.pop()
                            for line in lines:
                                line = line.strip()
                                if not line: continue
                                
                                # Roocode krav 3: Håndter HIT:<midi_note>
                                if "HIT:" in line:
                                    try:
                                        note_id = int(line.split("HIT:")[1].strip().split()[0])
                                        if self.loop:
                                            velocity = 100 
                                            # Finn den spesifikke noten i køen uavhengig av polyfoni-dybde
                                            if note_id in self.sent_notes and self.sent_notes[note_id]:
                                                note_data = self.sent_notes[note_id].pop(0)
                                                rtt = (time.perf_counter() - note_data["time"]) * 1000
                                                velocity = note_data["velocity"]
                                                asyncio.run_coroutine_threadsafe(broadcast_event("rtt_update", {"latency": round(rtt, 2)}), self.loop)
                                            
                                            # Send note_on - Dashboardet håndterer nå at flere noter er på samtidig
                                            asyncio.run_coroutine_threadsafe(broadcast_event("note_on", {"note": note_id, "velocity": velocity}), self.loop)
                                    except Exception as e:
                                        print(f"[SERIAL] HIT parse feil: {e}")
                                elif "REL:" in line:
                                    try:
                                        note_id = int(line.split("REL:")[1].strip().split()[0])
                                        if self.loop:
                                            asyncio.run_coroutine_threadsafe(broadcast_event("note_off", {"note": note_id, "velocity": 0}), self.loop)
                                    except: pass
                                
                                # Logg kun hvis det ikke er støy
                                elif not any(x in line for x in ["PCA9685", "ERROR", "MOTOR"]):
                                    if self.loop: 
                                        asyncio.run_coroutine_threadsafe(broadcast_event("serial_log", {"type": "receive", "message": line}), self.loop)
                                        # Gjenopprett grønt lys i Dashbordet
                                        asyncio.run_coroutine_threadsafe(broadcast_event("hw_status", {"status": "connected", "port": ser.port}), self.loop)
                time.sleep(0.0001)
            except: break

hardware = HardwareController()
current_playback_task = None
is_paused = False

@app.on_event("startup")
async def startup(): hardware._try_connect_serial()

connected_clients = []
MIDI_LIB_PATH = os.path.join(current_dir, "midi_library")

@app.get("/api/estop")
async def estop():
    global current_playback_task
    if current_playback_task: current_playback_task.cancel()
    hardware.send_queue.put("STOP\n")
    return {"status": "stopped"}

@app.get("/api/library")
async def get_lib(): return {"files": [f for f in os.listdir(MIDI_LIB_PATH) if f.endswith(('.mid', '.midi'))]}

@app.get("/api/play_library/{filename}")
async def play_lib(filename: str):
    global current_playback_task, is_paused
    is_paused = False
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(os.path.join(MIDI_LIB_PATH, filename)))
    return {"status": "playing"}

@app.get("/api/test_note/{note}")
async def trigger_test_note(note: int):
    # Enkel test-funksjon for de virtuelle tangentene
    hardware.send_kdaa_event({'type': 'STRIKE', 'hand': 'LEFT', 'note': note, 'vel': 100, 'timestamp': 0})
    await asyncio.sleep(0.5)
    hardware.send_kdaa_event({'type': 'STRIKE', 'hand': 'LEFT', 'note': note, 'vel': 0, 'timestamp': 0})
    return {"status": f"Testet note {note}"}

@app.get("/api/toggle_test_mode")
async def toggle_test_mode():
    hardware.scheduler.test_mode = not hardware.scheduler.test_mode
    mode = "TEST" if hardware.scheduler.test_mode else "PRODUCTION"
    return {"status": "success", "mode": mode, "test_mode": hardware.scheduler.test_mode}

@app.get("/api/get_mode")
async def get_mode():
    return {"test_mode": hardware.scheduler.test_mode}

@app.post("/api/upload_midi")
async def upload_midi(file: UploadFile = File(...)):
    global current_playback_task, is_paused
    is_paused = False
    file_path = os.path.join(MIDI_LIB_PATH, file.filename)
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(file_path))
    return {"status": "success", "filename": file.filename}

@app.get("/api/playback/{action}")
async def control_playback(action: str):
    global is_paused, current_playback_task
    if action == "pause": is_paused = True
    elif action == "resume": is_paused = False
    elif action == "stop":
        if current_playback_task: current_playback_task.cancel()
        hardware.send_queue.put("STOP\n")
    return {"status": action}

@app.get("/")
async def get_index():
    with open(os.path.join(current_dir, "index.html"), "r") as f: return HTMLResponse(f.read())

@app.get("/logo.png")
async def get_logo(): return FileResponse(os.path.join(current_dir, "Klaviator Logo Gold.png"))

@app.websocket("/ws/unity")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"[WS] Dashboard tilkoblet. Aktive klienter: {len(connected_clients)}")
    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            if data.get("action") == "play_file": asyncio.create_task(play_lib(data.get("value")))
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print(f"[WS] Dashboard frakoblet. Aktive klienter: {len(connected_clients)}")

async def broadcast_event(etype: str, data: dict):
    if not connected_clients: return
    msg = json.dumps({"event": etype, "data": data})
    for c in connected_clients:
        try: await c.send_text(msg)
        except: pass

async def play_midi_background(path: str):
    global is_paused
    try:
        schedule = hardware.scheduler.generate_schedule(path)
        
        # CRITICAL FIX: Legg til 1000ms offset til ALLE timestamps
        # Dette gir MCU tid til å starte etter SYNC
        for event in schedule:
            event['timestamp'] += 1000
        
        print(f"\n{'='*60}")
        print(f"[PLAY] Schedule generert: {len(schedule)} events")
        print(f"[PLAY] Første 5 events:")
        for i, ev in enumerate(schedule[:5]):
            print(f"  #{i+1}: {ev['type']} note={ev.get('note', 'N/A')} vel={ev.get('vel', 'N/A')} t={ev['timestamp']}ms dur={ev.get('duration', 'N/A')}ms")
        print(f"{'='*60}\n")
        
        await broadcast_event("playback_start", {"file": os.path.basename(path), "total_notes": len(schedule), "mode": "MCU-Master"})
        
        # CRITICAL: Send SYNC OG STOP, vent til MCU har prosessert
        hardware.send_sync()
        await asyncio.sleep(1.0)  # ØKT TIL 1 SEKUND for å sikre alle gamle events er borte
        
        print(f"[PLAY] Starter sending av {len(schedule)} events...")
        
        start_t = time.perf_counter()
        pause_accumulated = 0
        
        for idx, event in enumerate(schedule):
            # Sjekk om vi er på pause
            if is_paused:
                pause_start = time.perf_counter()
                while is_paused:
                    await asyncio.sleep(0.1)
                pause_accumulated += (time.perf_counter() - pause_start)
                
            # Beregn når vi skal sende denne event
            # current_time_ms er hvor langt vi er kommet i sangen (minus pause-tid)
            current_time_ms = (time.perf_counter() - start_t - pause_accumulated) * 1000
            
            # time_until_event er hvor lenge til denne event skal skje
            time_until_event = event['timestamp'] - current_time_ms
            
            # Vi sender events 1.5 sekunder i forkant (lookahead buffer)
            # Dette gir MCU tid til å forberede uten at vi sender ALT på en gang
            lookahead_ms = 1500
            
            # Vent til vi er innenfor lookahead-vinduet
            while time_until_event > lookahead_ms:
                await asyncio.sleep(0.005)  # Kortere sleep for bedre presisjon
                if is_paused: break
                current_time_ms = (time.perf_counter() - start_t - pause_accumulated) * 1000
                time_until_event = event['timestamp'] - current_time_ms
            
            if not is_paused:
                hardware.send_kdaa_event(event)
                # Debug logging for første 10 events
                if idx < 10:
                    print(f"[PLAY] Event {idx}: type={event['type']}, timestamp={event['timestamp']}ms, sent_at={current_time_ms:.1f}ms")
            
        # Vent til sangen er ferdig (litt lenger enn siste event)
        if schedule:
            last_event_time = schedule[-1]['timestamp']
            remaining_time = (last_event_time / 1000) - (time.perf_counter() - start_t - pause_accumulated)
            if remaining_time > 0:
                await asyncio.sleep(remaining_time + 1.0)  # +1 sekund ekstra buffer
        
        await broadcast_event("playback_finish", {})
        print(f"[PLAY] Avspilling fullført")
    except asyncio.CancelledError:
        hardware.send_queue.put("STOP\n")
        print(f"[PLAY] Avspilling avbrutt")
    except Exception as e:
        print(f"[PLAY] Feil: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
