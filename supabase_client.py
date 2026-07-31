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
    Preserva SEMPRE il notion_page_id della versione originale (la prima creata) per evitare che rielaborazioni future lo sovrascrivano.
    """
    client = get_supabase_client()
    if not client:
        return False, "Credenziali Supabase mancanti."
    try:
        iso_date = format_iso_date(lesson_date)
        
        # Controlla se la lezione esiste già per non sovrascrivere l'ID della prima versione originale
        existing_page_id = None
        try:
            existing_res = client.table("processed_lessons").select("notion_page_id").eq("video_id", str(video_id)).execute()
            if existing_res.data and len(existing_res.data) > 0:
                existing_page_id = existing_res.data[0].get("notion_page_id")
        except Exception:
            pass

        # Se esiste già un notion_page_id per la prima versione, MANTIENILO!
        final_page_id = existing_page_id if (existing_page_id and str(existing_page_id).strip()) else notion_page_id

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
