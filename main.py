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
playback_tempo_percent = 100


def analyze_midi_tempo(path: str, tempo_percent=None):
    """Returner BPM-info for dashboardet. MIDI uten tempo bruker standard 120 BPM."""
    percent = tempo_percent if tempo_percent is not None else playback_tempo_percent
    tempos = []
    try:
        midi = mido.MidiFile(path)
        for track in midi.tracks:
            for msg in track:
                if msg.type == "set_tempo":
                    tempos.append(round(mido.tempo2bpm(msg.tempo), 2))
    except Exception as e:
        return {
            "base_bpm": None,
            "effective_bpm": None,
            "display": "--",
            "source": "unknown",
            "tempo_percent": percent,
            "error": str(e),
        }

    if not tempos:
        base_bpm = 120.0
        source = "default"
    elif len(set(tempos)) == 1:
        base_bpm = tempos[0]
        source = "file"
    else:
        base_bpm = tempos[0]
        source = "variable"

    effective_bpm = round(base_bpm * percent / 100, 1)
    display = str(int(effective_bpm)) if float(effective_bpm).is_integer() else str(effective_bpm)
    return {
        "base_bpm": base_bpm,
        "effective_bpm": effective_bpm,
        "display": display,
        "source": source,
        "tempo_percent": percent,
        "tempo_count": len(tempos),
        "tempo_min": min(tempos) if tempos else base_bpm,
        "tempo_max": max(tempos) if tempos else base_bpm,
    }

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
        global current_window_start
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
                                    current_window_start = 48
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
current_window_start = 48  # C3 — ny hjemposisjon

@app.on_event("startup")
async def startup(): hardware._try_connect_serial()

connected_clients = []
MIDI_LIB_PATH = os.path.join(current_dir, "midi_library")

@app.get("/api/estop")
async def estop():
    global current_playback_task, is_paused
    if current_playback_task: current_playback_task.cancel()
    is_paused = False
    hardware.send_queue.put("STOP\n")
    await broadcast_event("emergency_stop", {"reason": "estop"})
    await broadcast_event("playback_finish", {})
    return {"status": "stopped"}

@app.get("/api/home")
async def home():
    global current_window_start
    current_window_start = 48
    hardware.send_queue.put("HOME\n")
    return {"status": "homing"}

@app.get("/api/library")
async def get_lib(): return {"files": [f for f in os.listdir(MIDI_LIB_PATH) if f.endswith(('.mid', '.midi'))]}

@app.get("/api/playback_tempo/{percent}")
async def set_playback_tempo(percent: int):
    global playback_tempo_percent
    playback_tempo_percent = max(40, min(120, percent))
    return {"status": "success", "tempo_percent": playback_tempo_percent}

@app.get("/api/midi_info/{filename}")
async def midi_info(filename: str):
    file_path = os.path.join(MIDI_LIB_PATH, filename)
    if not os.path.exists(file_path):
        return {"status": "error", "reason": "file_not_found"}
    return {"status": "success", "tempo": analyze_midi_tempo(file_path)}

@app.get("/api/play_library/{filename}")
async def play_lib(filename: str):
    global current_playback_task, is_paused
    is_paused = False
    file_path = os.path.join(MIDI_LIB_PATH, filename)
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_midi_background(file_path))
    return {"status": "playing", "tempo": analyze_midi_tempo(file_path)}

@app.get("/api/test_note/{note}")
async def trigger_test_note(note: int):
    global current_window_start
    # Send SYNC+STOP for å tømme MCU-bufferen
    hardware.send_sync()
    await asyncio.sleep(0.1)  # Kort ventetid

    # Sjekk om noten dekkes av nåværende hvit/sort vindu
    if not hardware.scheduler._notes_fit_in_window([note], current_window_start):
        next_window_start = hardware.scheduler._window_start_for_note(note)
        if next_window_start is None:
            return {"status": "error", "reason": "note_not_available_in_current_4_state_test", "note": note}
        current_window_start = next_window_start
        pos_mm = hardware.scheduler._note_to_mm(current_window_start)
        hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': pos_mm, 'timestamp': 100})
        print(f"[API] MOVE -> {pos_mm:.2f}mm (vindu starter på note {current_window_start})")
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
@app.get("/api/solenoid_test_old")
async def run_solenoid_test():
    """Spiller alle 16 solenoider en etter en med 500ms mellomrom.
    CH0-7 hvite (C3-C4), CH8-15 svarte (C#3, D#3, F#3, G#3, A#3, C#4, D#4, F#4)."""
    hardware.send_sync()
    await asyncio.sleep(0.5)
    # Alle 16 noter i rekkefølge: hvite først, deretter svarte — basert på C3 hjemposisjon
    all_notes = [48, 50, 52, 53, 55, 57, 59, 60,   # hvite CH0-7: C3-C4
                 49, 51, 54, 56, 58, 61, 63, 66]    # svarte CH8-15: C#3-F#4
    for i, note in enumerate(all_notes):
        t_ms = 500 + i * 500
        hardware.send_kdaa_event({'type': 'STRIKE', 'hand': 'LEFT', 'note': note, 'vel': 127, 'timestamp': t_ms, 'duration': 700})
    return {"status": "solenoid_test_started", "notes": all_notes}

@app.get("/api/solenoid_test")
async def run_four_state_solenoid_test():
    """Tester 4-state modell: CH0-7 hvite og CH8-15 svarte tangenter."""
    hardware.send_sync()
    await asyncio.sleep(0.5)

    states = [
        {"state": 1, "pos": 0,  "notes": [48, 50, 52, 53, 55, 57, 59, 60, 49, 51, 54, 56, 58, 61]},
        {"state": 2, "pos": 19, "notes": [62, 64, 65, 67, 69, 71, 72, 74, 63, 66, 68, 70, 73, 75]},
        {"state": 3, "pos": 37, "notes": [76, 77, 79, 81, 83, 84, 86, 88, 78, 80, 82, 85, 87]},
        {"state": 4, "pos": 56, "notes": [89, 91, 93, 95, 96, 98, 100, 101, 90, 92, 94, 97, 99, 102]},
    ]

    t_ms = 500
    planned = []
    for state in states:
        hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': state["pos"], 'timestamp': t_ms})
        t_ms += 800
        for note in state["notes"]:
            hardware.send_kdaa_event({'type': 'STRIKE', 'hand': 'LEFT', 'note': note, 'vel': 127, 'timestamp': t_ms, 'duration': 500})
            planned.append({"state": state["state"], "pos": state["pos"], "note": note})
            t_ms += 500
        t_ms += 500

    return {"status": "four_state_solenoid_test_started", "planned": planned}

@app.get("/api/stationary_solenoid_test")
async def run_stationary_solenoid_test():
    """Tester stasjonær modul på egen PCA: C1-D#2, ingen motorbevegelse."""
    hardware.send_sync()
    await asyncio.sleep(0.5)

    notes = [24, 26, 28, 29, 31, 33, 35, 36, 38, 25, 27, 30, 32, 34, 37, 39]
    t_ms = 500
    planned = []
    for note in notes:
        hardware.send_kdaa_event({
            'type': 'STRIKE',
            'hand': 'LEFT',
            'note': note,
            'vel': 110,
            'timestamp': t_ms,
            'duration': 450
        })
        planned.append({"note": note})
        t_ms += 380

    return {"status": "stationary_solenoid_test_started", "planned": planned}

@app.get("/api/stationary_note/{note}")
async def trigger_stationary_note(note: int):
    """Slår én note på stasjonær modul uten å flytte motor."""
    if note < 24 or note > 39:
        return {"status": "error", "reason": "note_not_in_stationary_window", "note": note}

    hardware.send_sync()
    await asyncio.sleep(0.1)
    hardware.send_kdaa_event({
        'type': 'STRIKE',
        'hand': 'LEFT',
        'note': note,
        'vel': 110,
        'timestamp': 500,
        'duration': 650
    })
    return {"status": "stationary_note_sent", "note": note}

@app.get("/api/live_note/{note}")
async def trigger_live_note(note: int, velocity: int = 100, duration: int = 180):
    """Lav-latency VR-input uten SYNC/STOP. Ruter automatisk til stationary eller moving."""
    global current_window_start

    velocity = max(1, min(127, velocity))
    duration = max(40, min(1200, duration))

    if 24 <= note <= 39:
        hardware.send_kdaa_event({
            'type': 'STRIKE',
            'hand': 'LEFT',
            'note': note,
            'vel': velocity,
            'timestamp': 0,
            'duration': duration
        })
        return {"status": "live_stationary_note_sent", "note": note, "velocity": velocity, "duration": duration}

    if note < hardware.scheduler.piano_min_note or note > hardware.scheduler.piano_max_note:
        return {"status": "error", "reason": "note_not_mapped", "note": note}

    if not hardware.scheduler._notes_fit_in_window([note], current_window_start):
        next_window_start = hardware.scheduler._window_start_for_note(note)
        if next_window_start is None:
            return {"status": "error", "reason": "note_not_available_in_moving_states", "note": note}

        old_pos = hardware.scheduler._note_to_mm(current_window_start)
        new_pos = hardware.scheduler._note_to_mm(next_window_start)
        travel_ms = int((abs(new_pos - old_pos) / max(1, hardware.scheduler.v_max)) * 1000) + 40

        current_window_start = next_window_start
        hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': new_pos, 'timestamp': 0})
        strike_timestamp = max(60, travel_ms)
    else:
        strike_timestamp = 0

    hardware.send_kdaa_event({
        'type': 'STRIKE',
        'hand': 'LEFT',
        'note': note,
        'vel': velocity,
        'timestamp': strike_timestamp,
        'duration': duration
    })
    return {
        "status": "live_moving_note_sent",
        "note": note,
        "velocity": velocity,
        "duration": duration,
        "window_start": current_window_start,
        "strike_timestamp": strike_timestamp
    }

@app.get("/api/motor_test")
async def run_motor_test():
    """Tester lineær aktuator: beveger seg frem og tilbake langs 600mm bane."""
    hardware.send_sync()
    await asyncio.sleep(0.5)
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos':  0, 'timestamp':  500})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 19, 'timestamp': 2000})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 37, 'timestamp': 3500})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos': 56, 'timestamp': 5000})
    hardware.send_kdaa_event({'type': 'MOVE', 'hand': 'LEFT', 'pos':  0, 'timestamp': 6500})
    return {"status": "motor_test_started", "positions_mm": [0, 19, 37, 56, 0]}

@app.get("/api/motor_move/{position_mm}")
async def motor_move(position_mm: int):
    """Beveg aktuatoren til en spesifikk posisjon (0-60 input)."""
    pos = max(0, min(60, position_mm))  # Klem innenfor sikre grenser
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
    return {"status": "success", "filename": file.filename, "tempo": analyze_midi_tempo(file_path)}

@app.get("/api/playback/{action}")
async def control_playback(action: str):
    global is_paused, current_playback_task
    if action == "pause": is_paused = True
    elif action == "resume": is_paused = False
    elif action == "stop":
        if current_playback_task: current_playback_task.cancel()
        is_paused = False
        hardware.send_queue.put("STOP\n")
        await broadcast_event("emergency_stop", {"reason": "playback_stop"})
        await broadcast_event("playback_finish", {})
    return {"status": action}

@app.get("/api/test_sequence")
async def test_sequence():
    global current_playback_task, is_paused
    is_paused = False
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_test_sequence())
    return {"status": "playing"}

@app.get("/api/four_state_showcase")
async def four_state_showcase():
    global current_playback_task, is_paused
    is_paused = False
    if current_playback_task: current_playback_task.cancel()
    current_playback_task = asyncio.create_task(play_four_state_showcase())
    return {"status": "playing"}

async def play_four_state_showcase():
    """Rask showcase for de fire kalibrerte state-vinduene."""
    states = [
        {"name": "State 1 C3-C4", "pos": 0,  "notes": [48, 52, 55, 60, 58, 55, 54, 52, 49, 48]},
        {"name": "State 2 D4-D5", "pos": 19, "notes": [62, 65, 68, 69, 73, 74, 75, 74, 70, 69, 65, 62]},
        {"name": "State 3 E5-E6", "pos": 37, "notes": [76, 78, 79, 82, 83, 87, 88, 87, 85, 83, 80, 79, 76]},
        {"name": "State 4 F6-F7", "pos": 56, "notes": [89, 90, 93, 94, 96, 99, 102, 101, 99, 96, 94, 93, 90, 89]},
        {"name": "State 3 return", "pos": 37, "notes": [88, 87, 83, 82, 79, 78, 76]},
        {"name": "State 2 return", "pos": 19, "notes": [74, 73, 69, 68, 65, 63, 62]},
        {"name": "State 1 finale", "pos": 0, "notes": [48, 49, 52, 54, 55, 58, 60, 55, 52, 48]},
    ]

    sequence = []
    t = 700
    move_settle_ms = 720
    note_gap_ms = 170
    note_duration_ms = 230

    for state in states:
        sequence.append({
            'type': 'MOVE',
            'hand': 'LEFT',
            'pos': state["pos"],
            'timestamp': t
        })
        t += move_settle_ms

        for note in state["notes"]:
            sequence.append({
                'type': 'STRIKE',
                'hand': 'LEFT',
                'note': note,
                'vel': 110,
                'duration': note_duration_ms,
                'timestamp': t
            })
            t += note_gap_ms

        t += 220

    sequence.append({'type': 'MOVE', 'hand': 'LEFT', 'pos': 0, 'timestamp': t + 300})

    await broadcast_event("playback_start", {"file": "Four-state showcase", "total_notes": len(sequence)})
    hardware.send_sync()
    await asyncio.sleep(0.6)
    start_t = time.perf_counter()

    for event in sequence:
        current_time_ms = (time.perf_counter() - start_t) * 1000
        time_until = event['timestamp'] - current_time_ms
        if time_until > 0:
            await asyncio.sleep(time_until / 1000)
        hardware.send_kdaa_event(event)

    await asyncio.sleep(2.0)
    await broadcast_event("playback_finish", {})

async def play_test_sequence():
    notes = [
        {'note': 48, 'vel': 80, 'duration': 400},   # C3
        {'note': 52, 'vel': 80, 'duration': 400},   # E3
        {'note': 55, 'vel': 80, 'duration': 400},   # G3
        {'note': 60, 'vel': 80, 'duration': 400},   # C4
        {'note': 64, 'vel': 80, 'duration': 400},   # E4
        {'note': 67, 'vel': 80, 'duration': 400},   # G4
        {'note': 72, 'vel': 80, 'duration': 400},   # C5
        {'note': 76, 'vel': 80, 'duration': 400},   # E5
        {'note': 79, 'vel': 80, 'duration': 400},   # G5
        {'note': 84, 'vel': 80, 'duration': 400},   # C6
        {'note': 88, 'vel': 80, 'duration': 400},   # E6
        {'note': 91, 'vel': 80, 'duration': 400},   # G6
    ]
    
    sequence = []
    t = 1000
    for n in notes:
        window_start = hardware.scheduler._window_start_for_note(n['note'])
        if window_start is None:
            print(f"[TEST_SEQUENCE] note {n['note']} not available")
            continue
        pos_mm = hardware.scheduler._note_to_mm(window_start)
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
        schedule = hardware.scheduler.generate_schedule(path, force_production=True)
        tempo_info = analyze_midi_tempo(path)
        tempo_scale = 100 / max(1, playback_tempo_percent)

        if tempo_scale != 1:
            for event in schedule:
                event['timestamp'] = int(event['timestamp'] * tempo_scale)
                if event.get('duration') is not None:
                    event['duration'] = max(20, int(event['duration'] * tempo_scale))
        
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
        
        await broadcast_event("playback_start", {
            "file": os.path.basename(path),
            "total_notes": len(schedule),
            "mode": "MCU-Master",
            "tempo": tempo_info,
        })
        
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
