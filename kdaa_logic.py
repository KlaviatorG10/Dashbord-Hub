"""
KDAA - Klaviator Deterministic Actuation Algorithm
Modul: Pre-Flight Analyzer & Scheduler (v2.0 - Trajectory Planning)
Utviklet av: Yousef (Tech Lead)

Dette er hjernen i systemet. Algoritmen transformerer rå MIDI-data til en 
kollisjonssikker og tidsstemplet bevegelsesplan (Global Schedule).
"""

import mido
import os

class KDAAScheduler:
    def __init__(self, v_max=1000, hand_margin=100, solenoid_delay_ms=20, test_mode=False):
        """
        HVORFOR: Vi må kjenne de fysiske begrensningene for å planlegge en trygg rute.
        :param v_max: Maksimal hastighet på beltet (mm/s).
        :param hand_margin: Sikkerhetsavstand mellom hendene (mm) for å unngå kollisjon.
        :param solenoid_delay_ms: Forsinkelsen fra strøm PÅ til fysisk anslag (ms).
        :param test_mode: Hvis True, ignorer fysiske begrensninger og send bare rå MIDI.
        """
        self.v_max = v_max
        self.hand_margin = hand_margin
        self.solenoid_delay_ms = solenoid_delay_ms
        self.test_mode = test_mode
        self.piano_min_note = 21
        self.piano_max_note = 108
        self.note_to_mm_factor = 13.5
        
    def _note_to_pos(self, midi_note):
        """Oversetter MIDI-note (21-108) til fysisk posisjon i mm."""
        return (midi_note - self.piano_min_note) * self.note_to_mm_factor

    def generate_schedule(self, midi_path):
        mid = mido.MidiFile(midi_path)
        
        # Parse MIDI med NOTE ON/OFF pairing for å beregne duration
        note_events = []  # List of {time, note, velocity, duration}
        current_time_ms = 0
        sustain_active = False
        active_notes = {}  # {note: {start_time, velocity}}
        pending_releases = {}  # Noter som venter på sustain release
        
        for msg in mid:
            current_time_ms += msg.time * 1000
            
            # Håndter sustain pedal (CC 64)
            if msg.type == 'control_change' and msg.control == 64:
                if msg.value >= 64:
                    sustain_active = True
                else:
                    # Sustain slippes - release alle ventende noter
                    sustain_active = False
                    for note in pending_releases:
                        if note in active_notes:
                            note_data = active_notes[note]
                            duration = current_time_ms - note_data['start_time']
                            note_events.append({
                                'time': note_data['start_time'],
                                'note': note,
                                'velocity': note_data['velocity'],
                                'duration': duration,
                                'pos': self._note_to_pos(note)
                            })
                            del active_notes[note]
                    pending_releases = {}
                continue
            
            # NOTE ON
            if msg.type == 'note_on' and msg.velocity > 0:
                # Hvis noten allerede er aktiv, avslutt forrige først (overlapping)
                if msg.note in active_notes:
                    note_data = active_notes[msg.note]
                    duration = current_time_ms - note_data['start_time']
                    note_events.append({
                        'time': note_data['start_time'],
                        'note': msg.note,
                        'velocity': note_data['velocity'],
                        'duration': duration,
                        'pos': self._note_to_pos(msg.note)
                    })
                
                # Start ny note
                active_notes[msg.note] = {
                    'start_time': current_time_ms,
                    'velocity': msg.velocity
                }
            
            # NOTE OFF
            elif (msg.type == 'note_off') or (msg.type == 'note_on' and msg.velocity == 0):
                if sustain_active:
                    # Marker for release når sustain slippes
                    pending_releases[msg.note] = True
                else:
                    # Release nå
                    if msg.note in active_notes:
                        note_data = active_notes[msg.note]
                        duration = current_time_ms - note_data['start_time']
                        note_events.append({
                            'time': note_data['start_time'],
                            'note': msg.note,
                            'velocity': note_data['velocity'],
                            'duration': max(50, duration),  # Minimum 50ms
                            'pos': self._note_to_pos(msg.note)
                        })
                        del active_notes[msg.note]
        
        # Avslutt eventuelle noter som aldri fikk note OFF (rare MIDI filer)
        for note, note_data in active_notes.items():
            note_events.append({
                'time': note_data['start_time'],
                'note': note,
                'velocity': note_data['velocity'],
                'duration': 200,  # Default duration
                'pos': self._note_to_pos(note)
            })

        final_schedule = []
        
        # TEST MODE: Send rå MIDI - KUN fjern eksakte multi-track duplicates
        if self.test_mode:
            # MINIMAL PROCESSING: Behold MIDI timing, fjern kun tekniske duplicates
            processed_events = []
            duplicates_removed = 0
            
            for event in note_events:
                strike_t = max(0, event['time'] - self.solenoid_delay_ms)
                
                # Sjekk kun for EKSAKT SAMME timestamp (multi-track duplicate)
                is_duplicate = False
                for existing in processed_events:
                    # Må være eksakt samme note OG eksakt samme tid (< 1ms)
                    if (existing['note'] == event['note'] and
                        abs(existing['timestamp'] - strike_t) < 1.0):
                        # Multi-track duplicate - behold høyeste velocity og lengste duration
                        # MEN ALDRI ENDRE TIMESTAMP!
                        if event['velocity'] > existing['vel']:
                            existing['vel'] = event['velocity']
                        if event['duration'] > existing['duration']:
                            existing['duration'] = event['duration']
                        is_duplicate = True
                        duplicates_removed += 1
                        break
                
                if not is_duplicate:
                    # Legg til som ny note - EKSAKT som den er i MIDI
                    processed_events.append({
                        'type': 'STRIKE',
                        'hand': 'LEFT',
                        'note': event['note'],
                        'vel': event['velocity'],
                        'duration': event['duration'],
                        'timestamp': strike_t
                    })
            
            final_schedule = processed_events
            final_schedule.sort(key=lambda x: x['timestamp'])
            
            print(f"[KDAA] TEST MODE: {len(note_events)} parsed → {len(final_schedule)} events ({duplicates_removed} multi-track duplicates removed)")
            print(f"[KDAA] Respekterer MIDI timing 100% - ingen quantization eller rounding")
            return final_schedule
        
        # PRODUCTION MODE: Full KDAA scheduling
        # Pre-posisjonering: Start hendene nær første noter
        first_notes = note_events[:10] if note_events else []
        if first_notes:
            left_start = min(e['pos'] for e in first_notes)
            right_start = max(e['pos'] for e in first_notes)
            # Hvis alle noter er tett sammen, spre hendene litt
            if abs(right_start - left_start) < 100:
                mid = (left_start + right_start) / 2
                left_start = mid - 50
                right_start = mid + 50
        else:
            left_start = self._note_to_pos(48)
            right_start = self._note_to_pos(72)
            
        hands = {
            "LEFT": {"pos": left_start, "busy_until": 0},
            "RIGHT": {"pos": right_start, "busy_until": 0}
        }
        
        # Grupper akkorder (noter innenfor 15ms)
        i = 0
        while i < len(note_events):
            chord = [note_events[i]]
            chord_time = note_events[i]['time']
            j = i + 1
            # Bruk 15ms vindu for akkorder
            while j < len(note_events) and abs(note_events[j]['time'] - chord_time) < 15:
                chord.append(note_events[j])
                j += 1
            
            # Sorter akkord etter posisjon for bedre håndfordeling
            chord.sort(key=lambda e: e['pos'])
            
            for idx, event in enumerate(chord):
                target_t = event['time']
                target_pos = event['pos']
                
                # Håndfordeling
                if len(chord) == 1:
                    # Enkelt note - velg nærmeste hånd
                    dist_l = abs(target_pos - hands["LEFT"]["pos"])
                    dist_r = abs(target_pos - hands["RIGHT"]["pos"])
                    
                    # Beregn reisetid for hver hånd
                    travel_time_l = (dist_l / self.v_max) * 1000
                    travel_time_r = (dist_r / self.v_max) * 1000
                    
                    left_ready_at = hands["LEFT"]["busy_until"] + travel_time_l
                    right_ready_at = hands["RIGHT"]["busy_until"] + travel_time_r
                    
                    # Velg hånd som kan være klar først
                    if left_ready_at <= target_t and right_ready_at <= target_t:
                        assigned_hand = "LEFT" if dist_l < dist_r else "RIGHT"
                    elif left_ready_at <= target_t:
                        assigned_hand = "LEFT"
                    elif right_ready_at <= target_t:
                        assigned_hand = "RIGHT"
                    else:
                        # Begge er for sent ute - velg den raskeste
                        assigned_hand = "LEFT" if left_ready_at < right_ready_at else "RIGHT"
                else:
                    # Akkord - split basert på posisjon
                    mid_idx = len(chord) // 2
                    assigned_hand = "LEFT" if idx < mid_idx else "RIGHT"

                # Beregn bevegelse
                travel_time = (abs(target_pos - hands[assigned_hand]["pos"]) / self.v_max) * 1000
                earliest_start = hands[assigned_hand]["busy_until"]
                ideal_start = target_t - travel_time - self.solenoid_delay_ms - 10  # 10ms ekstra margin
                
                start_move_t = max(earliest_start, ideal_start)
                actual_strike_t = start_move_t + travel_time + self.solenoid_delay_ms
                
                # Kun advare hvis forsinkelsen er signifikant (>50ms)
                if actual_strike_t > target_t + 50:
                    print(f"⚠️ CONFLICT: Note {event['note']} @ {target_t}ms -> {actual_strike_t}ms")

                # Legg til MOVE kommando hvis nødvendig
                if abs(target_pos - hands[assigned_hand]["pos"]) > 1.0:
                    final_schedule.append({
                        'type': 'MOVE',
                        'hand': assigned_hand,
                        'pos': target_pos,
                        'timestamp': max(0, start_move_t)
                    })
                
                # Legg til STRIKE kommando med duration
                final_schedule.append({
                    'type': 'STRIKE',
                    'hand': assigned_hand,
                    'note': event['note'],
                    'vel': event['velocity'],
                    'duration': event['duration'],
                    'timestamp': max(0, start_move_t + travel_time)
                })
                
                # Oppdater hånd-status
                hands[assigned_hand]["pos"] = target_pos
                # Hånden er opptatt til anslaget er ferdig + litt recovery tid
                hands[assigned_hand]["busy_until"] = actual_strike_t + 20

            i = j

        final_schedule.sort(key=lambda x: x['timestamp'])
        return final_schedule

    def get_summary(self, schedule):
        """Produserer en teknisk oppsummering av planen for feilsøking."""
        moves = len([e for e in schedule if e['type'] == 'MOVE'])
        strikes = len([e for e in schedule if e['type'] == 'STRIKE'])
        return f"Plan ferdigstilt: {moves} bevegelser og {strikes} anslag planlagt."

if __name__ == "__main__":
    scheduler = KDAAScheduler()
    print("KDAA Trajectory Planner v2.0 Initialized.")
