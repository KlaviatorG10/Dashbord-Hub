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
    def __init__(self, v_max=1000, hand_margin=100, solenoid_delay_ms=20):
        """
        HVORFOR: Vi må kjenne de fysiske begrensningene for å planlegge en trygg rute.
        :param v_max: Maksimal hastighet på beltet (mm/s).
        :param hand_margin: Sikkerhetsavstand mellom hendene (mm) for å unngå kollisjon.
        :param solenoid_delay_ms: Forsinkelsen fra strøm PÅ til fysisk anslag (ms).
        """
        self.v_max = v_max
        self.hand_margin = hand_margin
        self.solenoid_delay_ms = solenoid_delay_ms
        self.piano_min_note = 21 
        self.piano_max_note = 108 
        self.note_to_mm_factor = 13.5 
        
    def _note_to_pos(self, midi_note):
        """Oversetter MIDI-note (21-108) til fysisk posisjon i mm."""
        return (midi_note - self.piano_min_note) * self.note_to_mm_factor

    def generate_schedule(self, midi_path):
        """
        HVORDAN: 
        1. Analyserer MIDI-strømmen og tildeler noter til LEFT/RIGHT hånd.
        2. Beregner nødvendig bevegelsestid (Trajectory Planning).
        3. Genererer en sekvensiell liste med MOVE og STRIKE kommandoer.
        """
        mid = mido.MidiFile(midi_path)
        raw_events = []
        current_time_ms = 0
        
        # 1. Parsing av MIDI
        for msg in mid:
            current_time_ms += msg.time * 1000
            if msg.type in ['note_on', 'note_off']:
                raw_events.append({
                    'time': current_time_ms,
                    'note': msg.note,
                    'velocity': msg.velocity if msg.type == 'note_on' else 0,
                    'pos': self._note_to_pos(msg.note)
                })

        # 2. Hand Assignment & Trajectory Planning
        final_schedule = []
        
        # Initialtilstand for hendene
        hands = {
            "LEFT": {"pos": self._note_to_pos(48), "last_event_t": 0},
            "RIGHT": {"pos": self._note_to_pos(72), "last_event_t": 0}
        }
        
        for event in raw_events:
            target_t = event['time']
            target_pos = event['pos']
            
            # --- SPATIAL LOGIC: Hvilken hånd tar noten? ---
            # Vi velger hånd basert på avstand, men med kollisjonssikring
            if target_pos < hands["RIGHT"]["pos"] - self.hand_margin:
                assigned_hand = "LEFT"
            else:
                assigned_hand = "RIGHT"

            current_hand_pos = hands[assigned_hand]["pos"]
            distance = abs(target_pos - current_hand_pos)
            
            # --- TRAJECTORY PLANNING: Hvor lang tid tar bevegelsen? ---
            # HVORFOR: Vi må vite om vi rekker frem før noten skal spilles.
            # T_travel = Distance (mm) / Speed (mm/ms)
            travel_time_ms = (distance / self.v_max) * 1000
            
            # Beregn når bevegelsen må STARTE for å være ferdig i tide
            # Vi legger til en sikkerhetsmargin på 5ms for stabilisering
            start_move_t = target_t - travel_time_ms - 5
            
            # Sjekk om dette overlapper med forrige event for denne hånden
            if start_move_t < hands[assigned_hand]["last_event_t"]:
                # FORSVARBARHET: Her oppdager algoritmen fysisk umulige bevegelser.
                # I en full versjon ville vi her senket BPM for hele sangen.
                print(f"[KDAA_WARN] Note {event['note']} er for rask for {assigned_hand} hånd!")
                start_move_t = hands[assigned_hand]["last_event_t"]

            # --- GENERER KOMMANDOER ---
            
            # A. MOVE-kommando: Flytt beltet i posisjon
            if distance > 1.0: # Kun hvis vi faktisk må flytte oss
                final_schedule.append({
                    'type': 'MOVE',
                    'hand': assigned_hand,
                    'pos': target_pos,
                    'timestamp': start_move_t
                })
            
            # B. STRIKE-kommando: Slå på tangenten
            # HVORFOR: Vi trekker fra solenoid_delay for å oppnå perseptuell synk.
            strike_t = target_t - self.solenoid_delay_ms
            
            final_schedule.append({
                'type': 'STRIKE',
                'hand': assigned_hand,
                'note': event['note'],
                'vel': event['velocity'],
                'timestamp': strike_t,
                'visual_target_t': target_t # Tidspunktet Unity skal vise anslaget
            })
            
            # Oppdater hand state
            hands[assigned_hand]["pos"] = target_pos
            hands[assigned_hand]["last_event_t"] = target_t
                
        # Sorter hele listen på tidspunkt før retur
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
