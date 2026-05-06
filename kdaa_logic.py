"""
KDAA - Klaviator Deterministic Actuation Algorithm
Modul: Pre-Flight Analyzer & Scheduler (v3.0 - Single Hand, Sliding Window)
Utviklet av: Yousef (Tech Lead)

Én hånd med 16 solenoider på en belte-aktuator.
CH0-7:  hvite tangent-solenoider (8 stk)
CH8-15: sorte tangent-solenoider (8 stk)

Vinduet er definert av 8 konsekutive HVITE tangenter.
Sorte solenoider starter på første sorte tangent etter vindu-start.
"""

import mido
import os

# Fysisk posisjon i aktuator-mm for hver MIDI-note
NOTE_POSITIONS_MM = {
    21: 0.000,   # A0
    22: 1.854,   # A#0
    23: 3.708,   # B0
    24: 5.562,   # C1
    25: 7.416,   # C#1
    26: 9.270,   # D1
    27: 11.124,  # D#1
    28: 12.978,  # E1
    29: 14.832,  # F1
    30: 16.686,  # F#1
    31: 18.540,  # G1
    32: 20.394,  # G#1
    33: 22.248,  # A1
    34: 24.102,  # A#1
    35: 25.956,  # B1
    36: 27.810,  # C2
    37: 29.664,  # C#2
    38: 31.518,  # D2
    39: 33.372,  # D#2
    40: 35.226,  # E2
    41: 37.080,  # F2
    42: 38.934,  # F#2
    43: 40.788,  # G2
    44: 42.642,  # G#2
    45: 44.496,  # A2
    46: 46.350,  # A#2
    47: 48.204,  # B2
    48: 50.058,  # C3
    49: 51.912,  # C#3
    50: 53.766,  # D3
    51: 55.620,  # D#3
    52: 57.474,  # E3
    53: 59.328,  # F3
    54: 61.182,  # F#3
    55: 63.036,  # G3
    56: 64.890,  # G#3
    57: 66.744,  # A3
    58: 68.598,  # A#3
    59: 70.452,  # B3
    60: 72.306,  # C4
    61: 74.160,  # C#4
    62: 76.014,  # D4
    63: 77.868,  # D#4
    64: 79.722,  # E4
    65: 81.576,  # F4
    66: 83.430,  # F#4
    67: 85.284,  # G4
    68: 87.138,  # G#4
    69: 88.992,  # A4
    70: 90.846,  # A#4
    71: 92.700,  # B4
    72: 94.554,  # C5
    73: 96.408,  # C#5
    74: 98.262,  # D5
    75: 100.116, # D#5
    76: 101.970, # E5
    77: 103.824, # F5
}

# Sorte tangenter: C#, D#, F#, G#, A# (semitone % 12 i {1, 3, 6, 8, 10})
_BLACK_SEMITONES = {1, 3, 6, 8, 10}

WHITE_KEYS = [n for n in range(21, 78) if (n % 12) not in _BLACK_SEMITONES]
BLACK_KEYS = [n for n in range(21, 78) if (n % 12) in _BLACK_SEMITONES]

# Kalibrert hvit-tangent-bredde i aktuator-mm.
# 3.17319932498 / 1.01 korrigerer ~1 % overskytning (50mm → 50.5mm målt).
WHITE_KEY_SPACING_MM = 3.14178

# Maks kommandert posisjon slik at fysisk grense ≤ 60 mm (60 / 1.01)
MAX_POS_MM = 59.4

# Maks vindu-startindeks slik at 8 hvite alltid er tilgjengelig
_MAX_WHITE_IDX = len(WHITE_KEYS) - 8   # = 26  → WHITE_KEYS[26] = F4 (65)
_MAX_BLACK_IDX = len(BLACK_KEYS) - 8   # = 15  → BLACK_KEYS[15] = A#3 (58)


def _is_black(note):
    return (note % 12) in _BLACK_SEMITONES


class KDAAScheduler:
    def __init__(self, v_max=600, solenoid_delay_ms=20, test_mode=False):
        self.v_max = v_max
        self.solenoid_delay_ms = solenoid_delay_ms
        self.test_mode = test_mode
        self.piano_min_note = 21
        self.piano_max_note = 77
        self.note_to_mm_factor = 1.854
        self.num_white = 8
        self.num_black = 8

    def _note_to_mm(self, midi_note):
        """Aktuatorposisjon for en hvit vindu-startnote. Klippes til MAX_POS_MM."""
        if midi_note in WHITE_KEYS:
            pos = WHITE_KEYS.index(midi_note) * WHITE_KEY_SPACING_MM
        else:
            pos = NOTE_POSITIONS_MM.get(midi_note, 0.0)
        return min(pos, MAX_POS_MM)

    def _black_start_idx(self, window_start):
        """Indeks inn i BLACK_KEYS for første sorte tangent etter window_start."""
        for i, b in enumerate(BLACK_KEYS):
            if b > window_start:
                return i
        return len(BLACK_KEYS)

    def _window_start_for_note(self, note):
        """
        Returnerer MIDI-note for første HVITE tangent i vinduet som dekker 'note'.
        Returnerer alltid en note fra WHITE_KEYS.
        """
        if _is_black(note):
            b_idx = BLACK_KEYS.index(note)
            ideal_b = max(0, min(b_idx - 4, _MAX_BLACK_IDX))
            target_black = BLACK_KEYS[ideal_b]
            whites_before = [w for w in WHITE_KEYS if w < target_black]
            w_start = whites_before[-1] if whites_before else WHITE_KEYS[0]
            w_idx = WHITE_KEYS.index(w_start)
        else:
            w_idx = WHITE_KEYS.index(note) - 3

        w_idx = max(0, min(w_idx, _MAX_WHITE_IDX))
        return WHITE_KEYS[w_idx]

    def _solenoid_index(self, note, window_start):
        """
        Returnerer solenoid-indeks 0-15.
        Hvite tangenter → CH0-7, sorte → CH8-15.
        Returnerer -1 hvis noten er utenfor vinduet.
        """
        try:
            if _is_black(note):
                b_start = self._black_start_idx(window_start)
                sol = BLACK_KEYS.index(note) - b_start + 8
            else:
                w_start = WHITE_KEYS.index(window_start)
                sol = WHITE_KEYS.index(note) - w_start
            return sol if 0 <= sol <= 15 else -1
        except ValueError:
            return -1

    def _notes_fit_in_window(self, notes, window_start):
        """Returnerer True hvis alle noter dekkes av nåværende vindu."""
        w_idx = WHITE_KEYS.index(window_start)
        white_covered = set(WHITE_KEYS[w_idx:w_idx + self.num_white])
        b_start = self._black_start_idx(window_start)
        black_covered = set(BLACK_KEYS[b_start:b_start + self.num_black])
        covered = white_covered | black_covered
        return all(n in covered for n in notes)

    def _best_window_for_chord(self, chord_notes):
        """Velger best mulig vindu for en akkord basert på de hvite tangentene."""
        chord_white = [n for n in chord_notes if not _is_black(n)]
        chord_black = [n for n in chord_notes if _is_black(n)]
        if chord_white:
            mid = chord_white[len(chord_white) // 2]
        else:
            mid = chord_black[len(chord_black) // 2]
        return self._window_start_for_note(mid)

    def generate_schedule(self, midi_path):
        mid = mido.MidiFile(midi_path)

        # --- Parse MIDI med NOTE ON/OFF pairing ---
        note_events = []
        current_time_ms = 0
        sustain_active = False
        active_notes = {}
        pending_releases = {}

        for msg in mid:
            current_time_ms += msg.time * 1000

            if msg.type == 'control_change' and msg.control == 64:
                if msg.value >= 64:
                    sustain_active = True
                else:
                    sustain_active = False
                    for note in list(pending_releases.keys()):
                        if note in active_notes:
                            note_data = active_notes[note]
                            duration = current_time_ms - note_data['start_time']
                            note_events.append({
                                'time': note_data['start_time'],
                                'note': note,
                                'velocity': note_data['velocity'],
                                'duration': max(50, duration),
                            })
                            del active_notes[note]
                    pending_releases = {}
                continue

            if msg.type == 'note_on' and msg.velocity > 0:
                if msg.note in active_notes:
                    note_data = active_notes[msg.note]
                    duration = current_time_ms - note_data['start_time']
                    note_events.append({
                        'time': note_data['start_time'],
                        'note': msg.note,
                        'velocity': note_data['velocity'],
                        'duration': max(50, duration),
                    })
                active_notes[msg.note] = {
                    'start_time': current_time_ms,
                    'velocity': msg.velocity
                }

            elif (msg.type == 'note_off') or (msg.type == 'note_on' and msg.velocity == 0):
                if sustain_active:
                    pending_releases[msg.note] = True
                else:
                    if msg.note in active_notes:
                        note_data = active_notes[msg.note]
                        duration = current_time_ms - note_data['start_time']
                        note_events.append({
                            'time': note_data['start_time'],
                            'note': msg.note,
                            'velocity': note_data['velocity'],
                            'duration': max(50, duration),
                        })
                        del active_notes[msg.note]

        for note, note_data in active_notes.items():
            note_events.append({
                'time': note_data['start_time'],
                'note': note,
                'velocity': note_data['velocity'],
                'duration': 200,
            })

        note_events.sort(key=lambda e: e['time'])

        # --- TEST MODE ---
        if self.test_mode:
            processed_events = []
            duplicates_removed = 0
            merged_count = 0

            for event in note_events:
                strike_t = max(0, event['time'] - self.solenoid_delay_ms)
                merged = False
                for existing in processed_events:
                    if (existing['note'] == event['note'] and
                            abs(existing['timestamp'] - strike_t) < 1.0):
                        if event['velocity'] > existing['vel']:
                            existing['vel'] = event['velocity']
                        if event['duration'] > existing['duration']:
                            existing['duration'] = event['duration']
                        merged = True
                        duplicates_removed += 1
                        break
                    if (existing['note'] == event['note'] and
                            abs(existing['timestamp'] - strike_t) < 10.0):
                        earliest_t = min(existing['timestamp'], strike_t)
                        latest_end = max(
                            existing['timestamp'] + existing['duration'],
                            strike_t + event['duration']
                        )
                        existing['timestamp'] = earliest_t
                        existing['duration'] = latest_end - earliest_t
                        if event['velocity'] > existing['vel']:
                            existing['vel'] = event['velocity']
                        merged = True
                        merged_count += 1
                        break

                if not merged:
                    processed_events.append({
                        'type': 'STRIKE',
                        'hand': 'LEFT',
                        'note': event['note'],
                        'vel': event['velocity'],
                        'duration': event['duration'],
                        'timestamp': strike_t
                    })

            final_schedule = sorted(processed_events, key=lambda x: x['timestamp'])
            print(f"[KDAA] TEST MODE: {len(note_events)} parsed → {len(final_schedule)} events "
                  f"({duplicates_removed} duplicates removed, {merged_count} ghost notes merged)")
            return final_schedule

        # --- PRODUCTION MODE: hvit/sort vindu ---
        final_schedule = []

        if note_events:
            window_start = self._window_start_for_note(note_events[0]['note'])
        else:
            window_start = WHITE_KEYS[0]

        current_pos_mm = self._note_to_mm(window_start)
        hand_busy_until = 0

        i = 0
        while i < len(note_events):
            chord = [note_events[i]]
            chord_time = note_events[i]['time']
            j = i + 1
            while j < len(note_events) and abs(note_events[j]['time'] - chord_time) < 15:
                chord.append(note_events[j])
                j += 1

            chord_notes = [e['note'] for e in chord]

            if self._notes_fit_in_window(chord_notes, window_start):
                new_window_start = window_start
            else:
                new_window_start = self._best_window_for_chord(chord_notes)

            new_pos_mm = self._note_to_mm(new_window_start)
            travel_mm = abs(new_pos_mm - current_pos_mm)
            travel_time_ms = (travel_mm / self.v_max) * 1000

            target_t = chord_time
            ideal_move_start = target_t - travel_time_ms - self.solenoid_delay_ms - 10
            actual_move_start = max(hand_busy_until, ideal_move_start)
            actual_arrive_t = actual_move_start + travel_time_ms

            if actual_arrive_t > target_t + 50:
                print(f"⚠️ CONFLICT: Chord @ {target_t}ms → arrive {actual_arrive_t:.0f}ms "
                      f"(notes {min(chord_notes)}-{max(chord_notes)})")

            if new_window_start != window_start:
                final_schedule.append({
                    'type': 'MOVE',
                    'hand': 'LEFT',
                    'pos': new_pos_mm,
                    'timestamp': max(0, actual_move_start)
                })
                window_start = new_window_start
                current_pos_mm = new_pos_mm

            for event in chord:
                if self.piano_min_note <= event['note'] <= self.piano_max_note:
                    final_schedule.append({
                        'type': 'STRIKE',
                        'hand': 'LEFT',
                        'note': event['note'],   # MIDI-note — firmware kaller note_to_solenoid()
                        'vel': event['velocity'],
                        'duration': event['duration'],
                        'timestamp': max(0, actual_arrive_t)
                    })
                else:
                    print(f"⚠️ Note {event['note']} utenfor piano-range")

            hand_busy_until = actual_arrive_t + 20
            i = j

        final_schedule.sort(key=lambda x: x['timestamp'])

        moves = len([e for e in final_schedule if e['type'] == 'MOVE'])
        strikes = len([e for e in final_schedule if e['type'] == 'STRIKE'])
        print(f"[KDAA] PRODUCTION: {moves} MOVE + {strikes} STRIKE = {len(final_schedule)} events")
        print(f"[KDAA] v_max={self.v_max}mm/s, vindu=8 hvite + 8 sorte")
        return final_schedule

    def get_summary(self, schedule):
        moves = len([e for e in schedule if e['type'] == 'MOVE'])
        strikes = len([e for e in schedule if e['type'] == 'STRIKE'])
        return f"Plan ferdigstilt: {moves} bevegelser og {strikes} anslag planlagt."


if __name__ == "__main__":
    scheduler = KDAAScheduler()
    print("KDAA Trajectory Planner v3.0 - Single Hand Sliding Window (hvit/sort)")
    print(f"WHITE_KEYS ({len(WHITE_KEYS)}): {WHITE_KEYS}")
    print(f"BLACK_KEYS ({len(BLACK_KEYS)}): {BLACK_KEYS}")
