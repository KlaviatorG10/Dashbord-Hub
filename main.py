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
            midi_note = event.get('midi_note', event['note'])
            cmd = f"S:{event['hand'][0]}:{midi_note}:{event['vel']}:{max(0, int(event['timestamp']))}:{int(duration)}\n"
            if event['vel'] > 0:
                # Lagre for RTT måling og note_off planlegging
                if midi_note not in self.sent_notes:
                    self.sent_notes[midi_note] = []
                self.sent_notes[midi_note].append({
                    "time": time.perf_counter(),
                    "velocity": event['vel'],
                    "duration": duration  # Lagre duration for note_off
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
        
        print("[HARDWARE] Sender SYNC...")
        self.sent_notes = {}
        self.send_queue.put("SYNC\n")
        
        # Send STOP også for ekstra sikkerhet
        self.send_queue.put("STOP\n")

    def _try_connect_serial(self):
        if self.is_connected: return
        
        # Finn alle tilgjengelige porter på systemet
        ports = serial.tools.list_ports.comports()
        
        # Start med de hardkodede portene hvis de finnes, eller bruk alle funnet porter
        potential_ports = []
        
        # Prioriteringslogikk: JLink/SEGGER først, deretter alle andre
        for p in ports:
            if "JLink" in p.description or "SEGGER" in p.description:
                potential_ports.insert(0, p.device)
            else:
                potential_ports.append(p.device)

        # Sikre at COM3 og COM4 er med som fallback hvis de ikke ble detektert
        for fallback in ["COM3", "COM4"]:
            if fallback not in potential_ports:
                potential_ports.append(fallback)

        print(f"[HARDWARE] Skanner porter: {potential_ports}")

        for target_port in potential_ports:
            try:
                print(f"[HARDWARE] Prøver å koble til {target_port}...")
                ser = serial.Serial(port=target_port, baudrate=1000000, rtscts=False, timeout=0)
                self.serial_ports.append(ser)
                self.is_connected = True
                try:
                    self.loop = asyncio.get_running_loop()
                except RuntimeError:
                    self.loop = asyncio.get_event_loop()
                threading.Thread(target=self._serial_worker_thread, daemon=True).start()
                print(f"[HARDWARE] Tilkoblet {target_port} uten Hardware Flow Control")
                return # Koblet til én port, det holder for nå
            except Exception as e:
                # Logg feilen hvis det ikke bare er at porten er opptatt eller ikke finnes
                pass
        
        if not self.is_connected:
            print(f"[HARDWARE] Fant ingen aktive porter blant: {potential_ports}")

    def _serial_worker_thread(self):
        rx_buffers = {ser.port: "" for ser in self.serial_ports}
        commands_sent = 0
        while self.is_connected:
            try:
                # 1. Send data
                while not self.send_queue.empty():
                    cmd = self.send_queue.get_nowait()
                    for ser in self.serial_ports:
                        ser.write(cmd.encode('utf-8'))
                        
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
                                            duration_ms = 100  # Default note_off forsinkelse
                                            queue_empty = True
                                            # Finn den spesifikke noten i køen uavhengig av polyfoni-dybde
                                            if note_id in self.sent_notes and self.sent_notes[note_id]:
                                                note_data = self.sent_notes[note_id].pop(0)
                                                rtt = (time.perf_counter() - note_data["time"]) * 1000
                                                velocity = note_data["velocity"]
                                                duration_ms = note_data.get("duration", 100)
                                                queue_empty = False
                                                asyncio.run_coroutine_threadsafe(broadcast_event("rtt_update", {"latency": round(rtt, 2)}), self.loop)
                                            
                                            # DIAGNOSE: logg duration og om køen var tom
                                            print(f"[HIT] note={note_id} vel={velocity} dur={duration_ms}ms queue_empty={queue_empty}")
                                            
                                            # Send note_on (MCU-bekreftet)
                                            asyncio.run_coroutine_threadsafe(broadcast_event("note_on", {"note": note_id, "velocity": velocity}), self.loop)
                                            
                                            # Planlegg note_off basert på duration (Alternativ C)
                                            async def schedule_note_off(n, d):
                                                await asyncio.sleep(d / 1000.0)
                                                await broadcast_event("note_off", {"note": n, "velocity": 0})
                                            asyncio.run_coroutine_threadsafe(schedule_note_off(note_id, duration_ms), self.loop)
                                    except Exception as e:
                                        print(f"[SERIAL] HIT parse feil: {e}")
                                elif "REL:" in line:
                                    try:
                                        note_id = int(line.split("REL:")[1].strip().split()[0])
                                        if self.loop:
                                            asyncio.run_coroutine_threadsafe(broadcast_event("note_off", {"note": note_id, "velocity": 0}), self.loop)
                                    except: pass
                                
                                # Kritiske feilmeldinger sendes alltid til dashbordet
                                elif "ERROR:BUFFER_FULL" in line:
                                    print(f"[MCU ERROR] {line}")
                                    if self.loop:
                                        asyncio.run_coroutine_threadsafe(broadcast_event("buffer_full", {"message": line}), self.loop)
                                        asyncio.run_coroutine_threadsafe(broadcast_event("serial_log", {"type": "receive", "message": line}), self.loop)
                                elif "[HOMED] Klar" in line:
                                    if self.loop:
                                        asyncio.run_coroutine_threadsafe(
                                            broadcast_event("homed", {"status": True}),
                                            self.loop
                                        )
                                # Logg kun hvis det ikke er støy (MOTOR-meldinger er støy, PCA9685 er nyttig info)
                                elif not any(x in line for x in ["MOTOR"]):
                                    if self.loop:
                                        asyncio.run_coroutine_threadsafe(broadcast_event("serial_log", {"type": "receive", "message": line}), self.loop)
                                        # Gjenopprett grønt lys i Dashbordet
                                        asyncio.run_coroutine_threadsafe(broadcast_event("hw_status", {"status": "connected", "port": ser.port}), self.loop)
                time.sleep(0.0001)
            except: break

hardware = HardwareController()
current_playback_task = None
is_paused = False
current_window_start = 21

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

@app.get("/api/home")
async def home():
    hardware.send_queue.put("HOME\n")
    return {"status": "homing"}

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
    global current_window_start
    # Send SYNC+STOP for å tømme MCU-bufferen
    hardware.send_sync()
    await asyncio.sleep(0.1)  # Kort ventetid

    # Sjekk om noten dekkes av nåværende hvit/sort vindu
    if not hardware.scheduler._notes_fit_in_window([note], current_window_start):
        current_window_start = hardware.scheduler._window_start_for_note(note)
        pos_mm = hardware.scheduler._note_to_mm(current_window_start)
        hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': pos_mm, 'timestamp': 100})
        print(f"[API] MOVE → {pos_mm:.2f}mm (vindu starter på note {current_window_start})")
    else:
        print(f"[API] Note {note} er i nåværende vindu (start={current_window_start}). Skipper MOVE.")

    # Send STRIKE med MIDI-note — firmware kaller note_to_solenoid() internt
    hardware.send_kdaa_event({
        'type': 'STRIKE',
        'hand': 'LEFT',
        'note': note,
        'vel': 100,
        'timestamp': 500,
        'duration': 700
    })

    return {"status": f"Testet note {note} i vindu f.o.m {current_window_start}"}
@app.get("/api/solenoid_test")
async def run_solenoid_test():
    """Spiller alle 16 solenoider en etter en med 500ms mellomrom.
    CH0-7 hvite (C2-C3), CH8-15 svarte (C#2, D#2, F#2, G#2, A#2, C#3, D#3, F#3)."""
    hardware.send_sync()
    await asyncio.sleep(0.5)
    # Alle 16 noter i rekkefølge: hvite først, deretter svarte
    all_notes = [36, 38, 40, 41, 43, 45, 47, 48,   # hvite CH0-7
                 37, 39, 42, 44, 46, 49, 51, 54]    # svarte CH8-15
    for i, note in enumerate(all_notes):
        t_ms = 500 + i * 500
        hardware.send_kdaa_event({'type': 'STRIKE', 'hand': 'LEFT', 'note': note, 'vel': 127, 'timestamp': t_ms, 'duration': 700})
    return {"status": "solenoid_test_started", "notes": all_notes}

@app.get("/api/motor_test")
async def run_motor_test():
    """Tester lineær aktuator: beveger seg frem og tilbake langs 600mm bane."""
    hardware.send_sync()
    await asyncio.sleep(0.5)
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 0,   'timestamp': 500})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 150, 'timestamp': 2000})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 300, 'timestamp': 3500})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 450, 'timestamp': 5000})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 580, 'timestamp': 6500})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 0,   'timestamp': 8000})
    return {"status": "motor_test_started", "positions_mm": [0, 150, 300, 450, 580, 0]}

@app.get("/api/motor_move/{position_mm}")
async def motor_move(position_mm: int):
    """Beveg aktuatoren til en spesifikk posisjon (0-590mm)."""
    pos = max(0, min(590, position_mm))  # Klem innenfor sikre grenser
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': pos, 'timestamp': 500})
    return {"status": "move_sent", "position_mm": pos}

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

@app.get("/api/test_sequence")
async def test_sequence():
    global current_playback_task, is_paused
    is_paused = False
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_test_sequence())
    return {"status": "playing"}

async def play_test_sequence():
    notes = [
        {'note': 21, 'vel': 80, 'duration': 400},   # A0
        {'note': 28, 'vel': 80, 'duration': 400},   # E1
        {'note': 33, 'vel': 80, 'duration': 400},   # A1
        {'note': 37, 'vel': 80, 'duration': 400},   # C#2
        {'note': 40, 'vel': 80, 'duration': 400},   # E2
        {'note': 45, 'vel': 80, 'duration': 400},   # A2
        {'note': 49, 'vel': 80, 'duration': 400},   # C#3
        {'note': 52, 'vel': 80, 'duration': 400},   # E3
        {'note': 57, 'vel': 80, 'duration': 400},   # A3
        {'note': 64, 'vel': 80, 'duration': 400},   # E4
        {'note': 69, 'vel': 80, 'duration': 400},   # A4
        {'note': 77, 'vel': 80, 'duration': 400},   # F5
    ]
    
    sequence = []
    t = 1000
    for n in notes:
        pos_mm = (n['note'] - 21) * 1.854
        sequence.append({
            'type': 'MOVE',
            'hand': 'LEFT',
            'pos': pos_mm,
            'timestamp': t
        })
        t += 300
        sequence.append({
            'type': 'STRIKE',
            'hand': 'LEFT',
            'note': n['note'],
            'vel': n['vel'],
            'duration': n['duration'],
            'timestamp': t
        })
        t += 600

    await broadcast_event("playback_start", {"file": "A-dur testsekvens", "total_notes": len(sequence)})
    hardware.send_sync()
    await asyncio.sleep(1.0)
    start_t = time.perf_counter()
    for event in sequence:
        current_time_ms = (time.perf_counter() - start_t) * 1000
        time_until = event['timestamp'] - current_time_ms
        if time_until > 0:
            await asyncio.sleep(time_until / 1000)
        hardware.send_kdaa_event(event)
    await asyncio.sleep(2.0)
    await broadcast_event("playback_finish", {})

@app.get("/")
async def get_index():
    with open(os.path.join(current_dir, "index.html"), "r", encoding="utf-8") as f: return HTMLResponse(f.read())

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
                # Lyd og piano-roll drives utelukkende av HIT:<note> fra MCU (Alternativ A)
            
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
