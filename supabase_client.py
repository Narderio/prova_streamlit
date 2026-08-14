import os
import datetime
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

def format_iso_date(date_val):
    """
    Formatta qualsiasi valore di data (datetime, date, o stringa 'DD/MM/YYYY') nel formato ISO 'YYYY-MM-DD' accettato da PostgreSQL.
    """
    if not date_val:
        return datetime.date.today().isoformat()
    if isinstance(date_val, (datetime.date, datetime.datetime)):
        return date_val.strftime("%Y-%m-%d")
    date_str = str(date_val).strip()
    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            # DD/MM/YYYY -> YYYY-MM-DD
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return date_str

def get_supabase_client() -> Client | None:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        print(f"Errore connessione Supabase: {e}")
        return None

def is_video_processed(video_id: str):
    """
    Verifica se il video_id è già stato registrato su Supabase.
    Restituisce (True, record) se trovato, altrimenti (False, None).
    """
    client = get_supabase_client()
    if not client:
        return False, None
    try:
        res = client.table("processed_lessons").select("*").eq("video_id", str(video_id)).execute()
        if res.data and len(res.data) > 0:
            return True, res.data[0]
        return False, None
    except Exception as e:
        print(f"Errore durante il controllo duplicati su Supabase: {e}")
        return False, None

def save_processed_lesson(video_id: str, url: str, course: str, lesson_date: str, notion_page_id: str = None):
    """
    Salva la lezione elaborata nella tabella processed_lessons di Supabase.
    Se viene fornito un nuovo notion_page_id, lo aggiorna; altrimenti preserva quello esistente.
    """
    client = get_supabase_client()
    if not client:
        return False, "Credenziali Supabase mancanti."
    try:
        iso_date = format_iso_date(lesson_date)
        
        existing_page_id = None
        try:
            existing_res = client.table("processed_lessons").select("notion_page_id").eq("video_id", str(video_id)).execute()
            if existing_res.data and len(existing_res.data) > 0:
                existing_page_id = existing_res.data[0].get("notion_page_id")
        except Exception:
            pass

        # Usa il nuovo notion_page_id se fornito, altrimenti mantieni quello esistente
        final_page_id = notion_page_id if (notion_page_id and str(notion_page_id).strip()) else existing_page_id

        data = {
            "video_id": str(video_id),
            "url": str(url),
            "course": str(course),
            "lesson_date": iso_date,
            "notion_page_id": final_page_id
        }
        res = client.table("processed_lessons").upsert(data, on_conflict="video_id").execute()
        return True, res.data
    except Exception as e:
        print(f"Errore salvataggio Supabase: {e}")
        return False, f"Errore durante il salvataggio su Supabase: {e}"

def get_all_lesson_videos(video_id: str = None, course: str = None, lesson_date: str = None, notion_page_id: str = None) -> list:
    """
    Recupera l'elenco di tutti i video (record) associati alla medesima lezione/giornata.
    Cerca per notion_page_id e per (course, lesson_date), evitando duplicati.
    Restituisce una lista di record ordinati per created_at (cronologico).
    """
    client = get_supabase_client()
    if not client:
        return []
    
    try:
        target_page_id = notion_page_id
        target_course = course
        target_date = lesson_date

        if video_id:
            res_v = client.table("processed_lessons").select("*").eq("video_id", str(video_id)).execute()
            if res_v.data and len(res_v.data) > 0:
                rec = res_v.data[0]
                if not target_page_id:
                    target_page_id = rec.get("notion_page_id")
                if not target_course:
                    target_course = rec.get("course")
                if not target_date:
                    target_date = rec.get("lesson_date")

        results = []
        seen_ids = set()

        # 1. Cerca per notion_page_id se disponibile
        if target_page_id and str(target_page_id).strip():
            res_p = client.table("processed_lessons").select("*").eq("notion_page_id", str(target_page_id)).order("created_at", desc=False).execute()
            if res_p.data:
                for r in res_p.data:
                    vid = r.get("video_id")
                    if vid and vid not in seen_ids:
                        results.append(r)
                        seen_ids.add(vid)

        # 2. Cerca per (course, lesson_date) per includere eventuali video della stessa giornata
        if target_course and target_date:
            iso_date = format_iso_date(target_date)
            res_cd = client.table("processed_lessons").select("*").eq("course", str(target_course)).eq("lesson_date", iso_date).order("created_at", desc=False).execute()
            if res_cd.data:
                for r in res_cd.data:
                    vid = r.get("video_id")
                    if vid and vid not in seen_ids:
                        results.append(r)
                        seen_ids.add(vid)

        return results
    except Exception as e:
        print(f"Errore recupero video lezioni correlate su Supabase: {e}")
        return []

def get_saved_prompts():
    """
    Recupera l'elenco dei prompt salvati nella tabella prompts su Supabase.
    """
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = client.table("prompts").select("*").order("created_at", desc=True).execute()
        return res.data or []
    except Exception as e:
        print(f"Errore durante la lettura dei prompt su Supabase: {e}")
        return []

def save_prompt(title: str, prompt_text: str, is_default: bool = False):
    """
    Salva un nuovo prompt su Supabase.
    """
    client = get_supabase_client()
    if not client:
        return False, "Credenziali Supabase mancanti."
    try:
        data = {
            "title": title,
            "prompt_text": prompt_text,
            "is_default": is_default
        }
        res = client.table("prompts").insert(data).execute()
        return True, res.data
    except Exception as e:
        return False, f"Errore salvataggio prompt su Supabase: {e}"
