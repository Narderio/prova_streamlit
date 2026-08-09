import json
import os
import time
from datetime import datetime, timedelta
from threading import Lock

TRACKER_FILE = "gemini_requests.json"
lock = Lock()

def _load_requests():
    if not os.path.exists(TRACKER_FILE):
        return []
    try:
        with open(TRACKER_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

def _save_requests(reqs):
    try:
        with open(TRACKER_FILE, "w") as f:
            json.dump(reqs, f)
    except IOError:
        pass

def log_request():
    """Registra una nuova richiesta e pulisce quelle più vecchie di 24 ore."""
    with lock:
        reqs = _load_requests()
        now = datetime.now()
        
        # Aggiungi il timestamp corrente (in formato ISO per JSON)
        reqs.append(now.isoformat())
        
        # Tieni solo le richieste delle ultime 24 ore per non far crescere il file all'infinito
        cutoff_24h = now - timedelta(hours=24)
        cleaned_reqs = [r for r in reqs if datetime.fromisoformat(r) > cutoff_24h]
        
        _save_requests(cleaned_reqs)

def get_metrics():
    """Ritorna (richieste_ultimo_minuto, richieste_ultime_24_ore)"""
    with lock:
        reqs = _load_requests()
        now = datetime.now()
        
        cutoff_1m = now - timedelta(minutes=1)
        cutoff_24h = now - timedelta(hours=24)
        
        rpm_count = 0
        rpd_count = 0
        
        # Filtra e conta
        cleaned_reqs = []
        for r_str in reqs:
            try:
                r_time = datetime.fromisoformat(r_str)
                if r_time > cutoff_24h:
                    rpd_count += 1
                    cleaned_reqs.append(r_str)
                    if r_time > cutoff_1m:
                        rpm_count += 1
            except ValueError:
                pass
                
        # Puliamo se ci sono state richieste vecchie
        if len(cleaned_reqs) != len(reqs):
            _save_requests(cleaned_reqs)
            
        return rpm_count, rpd_count
