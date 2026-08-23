import json
import os
import time
from datetime import datetime, timedelta
from threading import Lock

TRACKER_FILE = "gemini_requests.json"
lock = Lock()

# Soglie di sicurezza richieste
MAX_RPM_LIMIT = 13   # Limite di sicurezza su 15 RPM
MAX_RPD_LIMIT = 495  # Limite di sicurezza su 500 RPD
RPM_WINDOW_SECONDS = 60
RPD_WINDOW_HOURS = 24

class GeminiRateLimitError(Exception):
    """Sollevata quando viene raggiunto il limite giornaliero RPD o quando una richiesta non può essere elaborata."""
    pass

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

def _clean_and_get_timestamps(now=None):
    """Carica e pulisce le richieste più vecchie di 24 ore, ritornando lista di datetime ordinata."""
    if now is None:
        now = datetime.now()
    reqs_str = _load_requests()
    cutoff_24h = now - timedelta(hours=RPD_WINDOW_HOURS)
    
    valid_dts = []
    for r_str in reqs_str:
        try:
            dt = datetime.fromisoformat(r_str)
            if dt > cutoff_24h:
                valid_dts.append(dt)
        except (ValueError, TypeError):
            pass
            
    valid_dts.sort()
    return valid_dts, now

def get_metrics():
    """
    Ritorna (richieste_ultimo_minuto, richieste_ultime_24_ore) e pulisce le richieste obsolete.
    """
    with lock:
        now = datetime.now()
        valid_dts, now = _clean_and_get_timestamps(now)
        _save_requests([dt.isoformat() for dt in valid_dts])
        
        cutoff_1m = now - timedelta(seconds=RPM_WINDOW_SECONDS)
        rpm_count = sum(1 for dt in valid_dts if dt > cutoff_1m)
        rpd_count = len(valid_dts)
        return rpm_count, rpd_count

def check_rate_limits():
    """
    Ritorna lo stato dettagliato dei limiti di frequenza:
    (allowed_bool, rpm_count, rpd_count, wait_seconds_rpm, wait_seconds_rpd, reason_msg)
    """
    with lock:
        now = datetime.now()
        valid_dts, now = _clean_and_get_timestamps(now)
        cutoff_1m = now - timedelta(seconds=RPM_WINDOW_SECONDS)
        reqs_1m = [dt for dt in valid_dts if dt > cutoff_1m]
        
        rpm_count = len(reqs_1m)
        rpd_count = len(valid_dts)
        
        wait_seconds_rpd = 0.0
        if rpd_count >= MAX_RPD_LIMIT:
            target_dt = valid_dts[len(valid_dts) - MAX_RPD_LIMIT]
            wait_seconds_rpd = max(0.0, (target_dt + timedelta(hours=RPD_WINDOW_HOURS) - now).total_seconds()) + 1.0
            hours = int(wait_seconds_rpd // 3600)
            minutes = int((wait_seconds_rpd % 3600) // 60)
            time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes} min"
            return False, rpm_count, rpd_count, 0.0, wait_seconds_rpd, f"Limite RPD ({rpd_count}/500) raggiunto. Reset stimato tra ~{time_str}."

        wait_seconds_rpm = 0.0
        if rpm_count >= MAX_RPM_LIMIT:
            target_dt = reqs_1m[len(reqs_1m) - MAX_RPM_LIMIT]
            wait_seconds_rpm = max(0.1, (target_dt + timedelta(seconds=RPM_WINDOW_SECONDS) - now).total_seconds()) + 0.5
            return False, rpm_count, rpd_count, wait_seconds_rpm, 0.0, f"Limite RPM ({rpm_count}/15) raggiunto. In attesa di reset tra {wait_seconds_rpm:.1f}s."

        return True, rpm_count, rpd_count, 0.0, 0.0, ""

def wait_and_log_request():
    """
    Controlla e applica i vincoli di frequenza:
    1. Se RPD >= 495: blocca e lancia GeminiRateLimitError fino al reset del giorno.
    2. Se RPM >= 13: attende automaticamente il tempo necessario affinché scada la richiesta più vecchia nel minuto,
       quindi registra il timestamp e procede.
    """
    while True:
        with lock:
            now = datetime.now()
            valid_dts, now = _clean_and_get_timestamps(now)
            
            cutoff_1m = now - timedelta(seconds=RPM_WINDOW_SECONDS)
            reqs_1m = [dt for dt in valid_dts if dt > cutoff_1m]
            
            rpm_count = len(reqs_1m)
            rpd_count = len(valid_dts)
            
            # 1. Controllo limite giornaliero RPD (495 / 500)
            if rpd_count >= MAX_RPD_LIMIT:
                target_dt = valid_dts[len(valid_dts) - MAX_RPD_LIMIT]
                wait_sec_rpd = max(0.0, (target_dt + timedelta(hours=RPD_WINDOW_HOURS) - now).total_seconds()) + 1.0
                hours = int(wait_sec_rpd // 3600)
                minutes = int((wait_sec_rpd % 3600) // 60)
                time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes} min"
                raise GeminiRateLimitError(
                    f"⛔ Limite giornaliero di sicurezza raggiunto ({rpd_count}/500 RPD). "
                    f"Richieste bloccate per proteggere la quota giornaliera fino al reset (stimato: ~{time_str})."
                )
                
            # 2. Controllo limite al minuto RPM (13 / 15)
            if rpm_count >= MAX_RPM_LIMIT:
                target_dt = reqs_1m[len(reqs_1m) - MAX_RPM_LIMIT]
                wait_sec_rpm = max(0.1, (target_dt + timedelta(seconds=RPM_WINDOW_SECONDS) - now).total_seconds()) + 0.5
            else:
                # Sia RPM che RPD sono sotto la soglia: registra la richiesta e procedi
                valid_dts.append(now)
                _save_requests([dt.isoformat() for dt in valid_dts])
                return True
                
        # Attesa del reset del minuto fuori dal lock per non bloccare altre letture
        time.sleep(min(wait_sec_rpm, 65.0))

def log_request():
    """Alias di wait_and_log_request() per compatibilità con il codice esistente."""
    return wait_and_log_request()
