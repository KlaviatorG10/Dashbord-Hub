"""
KDAA - Klaviator Deterministic Actuation Algorithm
Modul: Pre-Flight Analyzer & Scheduler (v4.0 - Fixed 4-State Test)
Utviklet av: Yousef (Tech Lead)

Testmodell:
Modulen flytter seg bare mellom fire faste tilstander. Hver tilstand har
8 hvite tangenter paa solenoid CH0-7 og inntil 8 sorte tangenter paa CH8-15.

Tilstand 1: motor  0mm, S0 = C3
Tilstand 2: motor 19 input, S0 = D4
Tilstand 3: motor 37 input, S0 = E5
Tilstand 4: motor 56 input, S0 = F6

Fysisk tilstandslengde paa pianoet er 165.78mm, men firmware/motor bruker
Dashboard-input skaleres fysisk med ca. input * 7.4625.
Derfor sendes motorposisjonene 0, 19, 37 og 56 i input-skalaen.
"""

import mido

_BLACK_SEMITONES = {1, 3, 6, 8, 10}

WHITE_KEYS = [n for n in range(48, 107) if (n % 12) not in _BLACK_SEMITONES]
BLACK_KEYS = [n for n in range(48, 107) if (n % 12) in _BLACK_SEMITONES]

STATE_LENGTH_PHYSICAL_MM = 165.78
ACTUAL_MM_PER_INPUT = 7.4625

# Calibration after physical test:
# - State 2 at 20 input was about 5mm actual too far:
#   5 / 7.4625 = 0.67, so the nearest integer input is 19.
# - State 3 at 38 input was a couple of millimeters too far right. Since the
#   current firmware protocol uses integer positions, the stable test point is 37.
# - State 4 at 57 input was about 5-6mm actual too far. 5.5 / 7.4625 = 0.74,
#   so it is pulled back one input step to 56.
STATE_MOTOR_POSITIONS_MM = [0, 19, 37, 56]
STATE_BASE_NOTES = [48, 62, 76, 89]  # C3, D4, E5, F6
STATE_BASE_WHITE_INDEXES = [WHITE_KEYS.index(n) for n in STATE_BASE_NOTES]
STATE_BLACK_MAPPINGS = [
    {8: 49, 9: 51, 11: 54, 12: 56, 13: 58, 15: 61},
    {8: 63, 10: 66, 11: 68, 12: 70, 14: 73, 15: 75},
    {9: 78, 10: 80, 11: 82, 13: 85, 14: 87},
    {8: 90, 9: 92, 10: 94, 12: 97, 13: 99, 15: 102},
]

STATIONARY_MIN_NOTE = 24  # C1
STATIONARY_MAX_NOTE = 39  # D#2

# Keep the old moving-only MIDI scheduler available for fast rollback.
# Set this to False if a test MIDI exposes a regression.
USE_DUAL_MODULE_MIDI = True


def _is_black(note):
    return (note % 12) in _BLACK_SEMITONES


def note_name(note):
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[note % 12]}{note // 12 - 1}"


class KDAAScheduler:
    def __init__(self, v_max=600, solenoid_delay_ms=20, test_mode=False):
        self.v_max = v_max
        self.solenoid_delay_ms = solenoid_delay_ms
        self.test_mode = test_mode
        self.piano_min_note = 48
        self.piano_max_note = 102  # F#7
        self.num_white = 8

    def state_mapping(self, state_index):
        """Returnerer mapping for state_index 0-3: solenoid -> MIDI-note."""
        if state_index < 0 or state_index >= len(STATE_BASE_WHITE_INDEXES):
            return {}

        white_base_idx = STATE_BASE_WHITE_INDEXES[state_index]

        mapping = {
            sol: WHITE_KEYS[white_base_idx + sol]
            for sol in range(self.num_white)
            if white_base_idx + sol < len(WHITE_KEYS)
            and WHITE_KEYS[white_base_idx + sol] <= self.piano_max_note
        }
        mapping.update(STATE_BLACK_MAPPINGS[state_index])
        return mapping

    def _state_for_window_start(self, window_start):
        for idx, base in enumerate(STATE_BASE_NOTES):
            if window_start == base:
                return idx
        return 0

    def _note_to_mm(self, window_start):
        """Kommandert motorposisjon for en state base-note."""
        if window_start is None:
            return None
        state = self._state_for_window_start(window_start)
        return STATE_MOTOR_POSITIONS_MM[state]

    def _state_for_note(self, note):
        """Velger state som dekker noten i moving-modulens fire tilstander."""
        for state in range(len(STATE_BASE_WHITE_INDEXES)):
            if note in self.state_mapping(state).values():
                return state
        return None

    def _window_start_for_note(self, note):
        state = self._state_for_note(note)
        if state is None:
            return None
        return STATE_BASE_NOTES[state]

    def _solenoid_index(self, note, window_start):
        state = self._state_for_window_start(window_start)
        for sol, mapped_note in self.state_mapping(state).items():
            if mapped_note == note:
                return sol
        return -1

    def _notes_fit_in_window(self, notes, window_start):
        state = self._state_for_window_start(window_start)
        covered = set(self.state_mapping(state).values())
        return all(n in covered for n in notes)

    def _is_stationary_note(self, note):
        return STATIONARY_MIN_NOTE <= note <= STATIONARY_MAX_NOTE

    def _event_sort_key(self, event):
        # MCU queue is FIFO, so same-timestamp ordering matters:
        # MOVE first, stationary strikes before moving strikes.
        if event["type"] == "MOVE":
            priority = 0
        elif event.get("module") == "stationary":
            priority = 1
        else:
            priority = 2
        return (event["timestamp"], priority)

    def _best_window_for_chord(self, chord_notes, current_window_start=None):
        """Velg state som dekker flest noter, med kortest reise som tie-break."""
        current_state = self._state_for_window_start(current_window_start) if current_window_start else 0
        best_state = 0
        best_score = None

        for state in range(len(STATE_BASE_NOTES)):
            covered = set(self.state_mapping(state).values())
            hits = sum(1 for note in chord_notes if note in covered)
            travel = abs(state - current_state)
            score = (-hits, travel)
            if best_score is None or score < best_score:
                best_score = score
                best_state = state

        return STATE_BASE_NOTES[best_state]

    def debug_mapping(self, note):
        state = self._state_for_note(note)
        if state is None:
            return {
                "ok": False,
                "note": note,
                "name": note_name(note) if 0 <= note <= 127 else str(note),
                "reason": "NOTE_NOT_AVAILABLE_IN_4_STATE_TEST",
            }

        window_start = STATE_BASE_NOTES[state]
        sol = self._solenoid_index(note, window_start)
        return {
            "ok": True,
            "note": note,
            "name": note_name(note),
            "state": state + 1,
            "window_start": window_start,
            "position_mm": STATE_MOTOR_POSITIONS_MM[state],
            "solenoid": sol,
            "state_mapping": self.state_mapping(state),
        }

    def _parse_midi(self, midi_path):
        mid = mido.MidiFile(midi_path)
        note_events = []
        current_time_ms = 0
        sustain_active = False
        active_notes = {}
        pending_releases = {}

        for msg in mid:
            current_time_ms += msg.time * 1000

            if msg.type == "control_change" and msg.control == 64:
                if msg.value >= 64:
                    sustain_active = True
                else:
                    sustain_active = False
                    for note in list(pending_releases.keys()):
                        if note in active_notes:
                            note_data = active_notes[note]
                            note_events.append({
                                "time": note_data["start_time"],
                                "note": note,
                                "velocity": note_data["velocity"],
                                "duration": max(50, current_time_ms - note_data["start_time"]),
                            })
                            del active_notes[note]
                    pending_releases = {}
                continue

            if msg.type == "note_on" and msg.velocity > 0:
                if msg.note in active_notes:
                    note_data = active_notes[msg.note]
                    note_events.append({
                        "time": note_data["start_time"],
                        "note": msg.note,
                        "velocity": note_data["velocity"],
                        "duration": max(50, current_time_ms - note_data["start_time"]),
                    })
                active_notes[msg.note] = {
                    "start_time": current_time_ms,
                    "velocity": msg.velocity,
                }

            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                if sustain_active:
                    pending_releases[msg.note] = True
                elif msg.note in active_notes:
                    note_data = active_notes[msg.note]
                    note_events.append({
                        "time": note_data["start_time"],
                        "note": msg.note,
                        "velocity": note_data["velocity"],
                        "duration": max(50, current_time_ms - note_data["start_time"]),
                    })
                    del active_notes[msg.note]

        for note, note_data in active_notes.items():
            note_events.append({
                "time": note_data["start_time"],
                "note": note,
                "velocity": note_data["velocity"],
                "duration": 200,
            })

        note_events.sort(key=lambda e: e["time"])
        return note_events

    def generate_schedule(self, midi_path, force_production=False):
        note_events = self._parse_midi(midi_path)

        if self.test_mode and not force_production:
            final_schedule = []
            seen = {}
            for event in note_events:
                strike_t = max(0, event["time"] - self.solenoid_delay_ms)
                key = (event["note"], round(strike_t))
                if key in seen:
                    existing = seen[key]
                    existing["vel"] = max(existing["vel"], event["velocity"])
                    existing["duration"] = max(existing["duration"], event["duration"])
                    continue
                item = {
                    "type": "STRIKE",
                    "hand": "LEFT",
                    "note": event["note"],
                    "vel": event["velocity"],
                    "duration": event["duration"],
                    "timestamp": strike_t,
                }
                seen[key] = item
                final_schedule.append(item)
            final_schedule.sort(key=lambda x: x["timestamp"])
            print(f"[KDAA] TEST MODE: {len(note_events)} parsed -> {len(final_schedule)} events")
            return final_schedule

        if not USE_DUAL_MODULE_MIDI:
            return self._generate_moving_only_schedule(note_events)

        return self._generate_dual_module_schedule(note_events)

    def _generate_moving_only_schedule(self, note_events):
        """Original moving-module-only MIDI scheduler kept intact for rollback."""
        final_schedule = []
        window_start = STATE_BASE_NOTES[0]
        current_pos_mm = self._note_to_mm(window_start)
        hand_busy_until = 0

        i = 0
        while i < len(note_events):
            chord = [note_events[i]]
            chord_time = note_events[i]["time"]
            j = i + 1
            while j < len(note_events) and abs(note_events[j]["time"] - chord_time) < 15:
                chord.append(note_events[j])
                j += 1

            chord_notes = [e["note"] for e in chord]
            new_window_start = self._best_window_for_chord(chord_notes, window_start)
            new_pos_mm = self._note_to_mm(new_window_start)
            travel_mm = abs(new_pos_mm - current_pos_mm)
            travel_time_ms = (travel_mm / self.v_max) * 1000

            target_t = chord_time
            ideal_move_start = target_t - travel_time_ms - self.solenoid_delay_ms - 10
            actual_move_start = max(hand_busy_until, ideal_move_start)
            actual_arrive_t = actual_move_start + travel_time_ms

            if new_window_start != window_start:
                final_schedule.append({
                    "type": "MOVE",
                    "hand": "LEFT",
                    "pos": new_pos_mm,
                    "timestamp": max(0, actual_move_start),
                })
                window_start = new_window_start
                current_pos_mm = new_pos_mm

            for event in chord:
                sol = self._solenoid_index(event["note"], window_start)
                if sol < 0:
                    print(f"[KDAA] SKIP {event['note']} ({note_name(event['note'])}) - not in 4-state test")
                    continue

                final_schedule.append({
                    "type": "STRIKE",
                    "hand": "LEFT",
                    "note": event["note"],
                    "solenoid_index": sol,
                    "vel": event["velocity"],
                    "duration": event["duration"],
                    "timestamp": max(0, actual_arrive_t),
                })

            hand_busy_until = actual_arrive_t + 20
            i = j

        final_schedule.sort(key=self._event_sort_key)

        moves = len([e for e in final_schedule if e["type"] == "MOVE"])
        strikes = len([e for e in final_schedule if e["type"] == "STRIKE"])
        print(f"[KDAA] 4-STATE TEST: {moves} MOVE + {strikes} STRIKE = {len(final_schedule)} events")
        return final_schedule

    def _generate_dual_module_schedule(self, note_events):
        """Schedule stationary C1-D#2 directly, and keep moving states unchanged."""
        final_schedule = []
        window_start = STATE_BASE_NOTES[0]
        current_pos_mm = self._note_to_mm(window_start)
        hand_busy_until = 0

        i = 0
        while i < len(note_events):
            chord = [note_events[i]]
            chord_time = note_events[i]["time"]
            j = i + 1
            while j < len(note_events) and abs(note_events[j]["time"] - chord_time) < 15:
                chord.append(note_events[j])
                j += 1

            stationary_chord = []
            moving_chord = []
            unsupported_chord = []

            for event in chord:
                note = event["note"]
                if self._is_stationary_note(note):
                    stationary_chord.append(event)
                elif self._state_for_note(note) is not None:
                    moving_chord.append(event)
                else:
                    unsupported_chord.append(event)

            stationary_t = max(0, chord_time - self.solenoid_delay_ms - 10)

            if moving_chord:
                chord_notes = [e["note"] for e in moving_chord]
                new_window_start = self._best_window_for_chord(chord_notes, window_start)
                new_pos_mm = self._note_to_mm(new_window_start)
                travel_mm = abs(new_pos_mm - current_pos_mm)
                travel_time_ms = (travel_mm / self.v_max) * 1000

                target_t = chord_time
                ideal_move_start = target_t - travel_time_ms - self.solenoid_delay_ms - 10
                actual_move_start = max(hand_busy_until, ideal_move_start)
                actual_arrive_t = actual_move_start + travel_time_ms
                # OLD coupled behavior:
                # stationary_t = max(0, actual_arrive_t)
                # The stationary module does not move, so it keeps MIDI timing
                # while the moving module travels and delays only its own strike.

                if new_window_start != window_start:
                    final_schedule.append({
                        "type": "MOVE",
                        "hand": "LEFT",
                        "pos": new_pos_mm,
                        "timestamp": max(0, actual_move_start),
                    })
                    window_start = new_window_start
                    current_pos_mm = new_pos_mm

                for event in moving_chord:
                    sol = self._solenoid_index(event["note"], window_start)
                    if sol < 0:
                        print(f"[KDAA] SKIP {event['note']} ({note_name(event['note'])}) - not in moving 4-state test")
                        continue

                    final_schedule.append({
                        "type": "STRIKE",
                        "hand": "LEFT",
                        "note": event["note"],
                        "module": "moving",
                        "solenoid_index": sol,
                        "vel": event["velocity"],
                        "duration": event["duration"],
                        "timestamp": max(0, actual_arrive_t),
                    })

                hand_busy_until = actual_arrive_t + 20

            for event in stationary_chord:
                final_schedule.append({
                    "type": "STRIKE",
                    "hand": "LEFT",
                    "note": event["note"],
                    "module": "stationary",
                    "vel": event["velocity"],
                    "duration": event["duration"],
                    "timestamp": stationary_t,
                })

            for event in unsupported_chord:
                print(f"[KDAA] SKIP {event['note']} ({note_name(event['note'])}) - not mapped to stationary or moving module")

            i = j

        final_schedule.sort(key=self._event_sort_key)

        moves = len([e for e in final_schedule if e["type"] == "MOVE"])
        stationary = len([e for e in final_schedule if e.get("module") == "stationary"])
        moving = len([e for e in final_schedule if e.get("module") == "moving"])
        strikes = stationary + moving
        print(
            f"[KDAA] DUAL MODULE: {moves} MOVE + {stationary} stationary STRIKE + "
            f"{moving} moving STRIKE = {len(final_schedule)} events"
        )
        return final_schedule

    def get_summary(self, schedule):
        moves = len([e for e in schedule if e["type"] == "MOVE"])
        strikes = len([e for e in schedule if e["type"] == "STRIKE"])
        return f"Plan ferdigstilt: {moves} bevegelser og {strikes} anslag planlagt."


if __name__ == "__main__":
    scheduler = KDAAScheduler()
    print("KDAA Trajectory Planner v4.0 - Fixed 4-State Test")
    for i, base in enumerate(STATE_BASE_NOTES):
        print(f"State {i + 1}: motor={STATE_MOTOR_POSITIONS_MM[i]}mm S0={note_name(base)} mapping={scheduler.state_mapping(i)}")
