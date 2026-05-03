"""
KDAA - Klaviator Deterministic Actuation Algorithm
Modul: Pre-Flight Analyzer & Scheduler (v3.0 - Single Hand, Sliding Window)
Utviklet av: Yousef (Tech Lead)

Én hånd med 16 solenoider på en belte-aktuator.
Solenoidene dekker et glidende 16-note vindu over klaviaturet.
MOVE-kommandoer posisjonerer belte-aktuatoren slik at riktig solenoid
er over riktig tangent før STRIKE-kommandoen sendes.
"""

import mido
import os

class KDAAScheduler:
    def __init__(self, v_max=600, solenoid_delay_ms=20, test_mode=False):
        """
        :param v_max: Maksimal hastighet på den lineære aktuatoren (mm/s).
                      Topphastighet uten last: 750mm/s. Med last ~600mm/s (80%).
        :param solenoid_delay_ms: Forsinkelsen fra strøm PÅ til fysisk anslag (ms).
        :param test_mode: Hvis True, ignorer fysiske begrensninger og send bare rå MIDI.
        """
        self.v_max = v_max
        self.solenoid_delay_ms = solenoid_delay_ms
        self.test_mode = test_mode
        self.piano_min_note = 21
        self.piano_max_note = 108

        # 23.65mm mellom solenoidene (målt fysisk)
        # Solenoidene er montert med jevn avstand uavhengig av hvit/svart tangent
        self.note_to_mm_factor = 23.65

        # Antall solenoider på hånden (16: CH0-7 hvite, CH8-15 svarte)
        self.num_solenoids = 16

    def _note_to_mm(self, midi_note):
        """Oversetter MIDI-note til fysisk posisjon i mm fra piano-start."""
        return (midi_note - self.piano_min_note) * self.note_to_mm_factor

    def _window_start_for_note(self, midi_note):
        """
        Beregner hvilken note solenoid-vinduet skal starte på for å nå midi_note.
        Solenoid-vinduet er 16 noter bredt. Vi sentrerer vinduet rundt noten
        så godt som mulig, men klemmer det innenfor piano-grensene.
        """
        ideal_start = midi_note - self.num_solenoids // 2
        clamped_start = max(self.piano_min_note,
                            min(ideal_start, self.piano_max_note - self.num_solenoids + 1))
        return clamped_start

    def _solenoid_index(self, midi_note, window_start):
        """Returnerer solenoid-indeks (0-15) for en note gitt vindu-start."""
        return midi_note - window_start

    def generate_schedule(self, midi_path):
        mid = mido.MidiFile(midi_path)

        # --- Parse MIDI med NOTE ON/OFF pairing for å beregne duration ---
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

        # Avslutt noter uten NOTE OFF
        for note, note_data in active_notes.items():
            note_events.append({
                'time': note_data['start_time'],
                'note': note,
                'velocity': note_data['velocity'],
                'duration': 200,
            })

        # Sorter etter tid
        note_events.sort(key=lambda e: e['time'])

        # --- TEST MODE: Rå MIDI med ghost-note sammenslåing ---
        if self.test_mode:
            processed_events = []
            duplicates_removed = 0
            merged_count = 0

            for event in note_events:
                strike_t = max(0, event['time'] - self.solenoid_delay_ms)

                merged = False
                for existing in processed_events:
                    # Eksakt multi-track duplicate (< 1ms)
                    if (existing['note'] == event['note'] and
                            abs(existing['timestamp'] - strike_t) < 1.0):
                        if event['velocity'] > existing['vel']:
                            existing['vel'] = event['velocity']
                        if event['duration'] > existing['duration']:
                            existing['duration'] = event['duration']
                        merged = True
                        duplicates_removed += 1
                        break

                    # Alternativ 2: Slå sammen overlappende noter på samme pitch innenfor 10ms
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
            print(f"[KDAA] note_to_mm_factor={self.note_to_mm_factor}mm/halvtone (23.5mm hvit tangent)")
            return final_schedule

        # --- PRODUCTION MODE: Én hånd, glidende solenoid-vindu ---
        final_schedule = []

        # Start-posisjon: sentrert rundt første note
        if note_events:
            first_note = note_events[0]['note']
            window_start = self._window_start_for_note(first_note)
        else:
            window_start = 60  # C4 som default

        current_pos_mm = self._note_to_mm(window_start)
        hand_busy_until = 0

        # Grupper akkorder (noter innenfor 15ms)
        i = 0
        while i < len(note_events):
            chord = [note_events[i]]
            chord_time = note_events[i]['time']
            j = i + 1
            while j < len(note_events) and abs(note_events[j]['time'] - chord_time) < 15:
                chord.append(note_events[j])
                j += 1

            # Finn optimalt vindu for akkorden (dekker flest mulig noter)
            chord_notes = [e['note'] for e in chord]
            min_note = min(chord_notes)
            max_note = max(chord_notes)

            # Prøv å finne et vindu som dekker alle noter i akkorden
            ideal_window = max(self.piano_min_note,
                               min(min_note, self.piano_max_note - self.num_solenoids + 1))
            # Sjekk at max_note er innenfor vinduet
            if max_note > ideal_window + self.num_solenoids - 1:
                # Akkorden er bredere enn 16 noter — sentrér vinduet
                mid_note = (min_note + max_note) // 2
                ideal_window = self._window_start_for_note(mid_note)

            new_window_start = ideal_window
            new_pos_mm = self._note_to_mm(new_window_start)

            # Beregn reisetid for belte-aktuatoren
            travel_mm = abs(new_pos_mm - current_pos_mm)
            travel_time_ms = (travel_mm / self.v_max) * 1000

            target_t = chord_time
            ideal_move_start = target_t - travel_time_ms - self.solenoid_delay_ms - 10
            actual_move_start = max(hand_busy_until, ideal_move_start)
            actual_arrive_t = actual_move_start + travel_time_ms

            if actual_arrive_t > target_t + 50:
                print(f"⚠️ CONFLICT: Chord @ {target_t}ms → arrive {actual_arrive_t:.0f}ms "
                      f"(notes {min_note}-{max_note})")

            # Send MOVE hvis vinduet endrer seg
            if new_window_start != window_start or travel_mm > 1.0:
                final_schedule.append({
                    'type': 'MOVE',
                    'hand': 'LEFT',
                    'pos': new_pos_mm,
                    'timestamp': max(0, actual_move_start)
                })
                window_start = new_window_start
                current_pos_mm = new_pos_mm

            # Send STRIKE for hver note i akkorden
            # Sender solenoid-indeks (0-15) som 'note' — MCU bruker dette direkte
            for event in chord:
                sol_idx = self._solenoid_index(event['note'], window_start)
                if 0 <= sol_idx < self.num_solenoids:
                    strike_t = max(0, actual_arrive_t)
                    final_schedule.append({
                        'type': 'STRIKE',
                        'hand': 'LEFT',
                        'note': sol_idx,            # Solenoid-indeks 0-15, ikke MIDI-note
                        'midi_note': event['note'], # Beholdes for visualisering
                        'vel': event['velocity'],
                        'duration': event['duration'],
                        'timestamp': strike_t
                    })
                else:
                    print(f"⚠️ Note {event['note']} utenfor solenoid-vindu "
                          f"[{window_start}-{window_start + self.num_solenoids - 1}]")

            hand_busy_until = actual_arrive_t + 20
            i = j

        final_schedule.sort(key=lambda x: x['timestamp'])

        moves = len([e for e in final_schedule if e['type'] == 'MOVE'])
        strikes = len([e for e in final_schedule if e['type'] == 'STRIKE'])
        print(f"[KDAA] PRODUCTION: {moves} MOVE + {strikes} STRIKE = {len(final_schedule)} events")
        print(f"[KDAA] note_to_mm_factor={self.note_to_mm_factor}mm/halvtone, v_max={self.v_max}mm/s")
        return final_schedule

    def get_summary(self, schedule):
        moves = len([e for e in schedule if e['type'] == 'MOVE'])
        strikes = len([e for e in schedule if e['type'] == 'STRIKE'])
        return f"Plan ferdigstilt: {moves} bevegelser og {strikes} anslag planlagt."


if __name__ == "__main__":
    scheduler = KDAAScheduler()
    print("KDAA Trajectory Planner v3.0 - Single Hand Sliding Window")
