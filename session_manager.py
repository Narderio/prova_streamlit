import os
import json
import uuid
import time
import datetime
import re
from pathlib import Path
import streamlit as st

CACHE_DIR = Path(__file__).resolve().parent / ".session_cache"
SESSION_PARAM_KEY = "session"

PERSIST_KEYS = [
    "notes_versions",
    "current_version_index",
    "appunti_generati",
    "_last_valid_appunti",
    "testo_estratto",
    "latex_generato",
    "presentation_html",
    "canvas_chat_history",
    "show_canvas_chat",
    "saved_vimeo_url",
    "vimeo_url_input",
    "current_notion_page_id",
    "notion_page_url",
    "notion_status",
    "_last_saved_notion_notes",
    "_last_saved_version_index",
    "selected_course",
    "selected_course_page_id",
    "_last_selected_course",
    "_active_loaded_lesson_id",
    "canvas_view_radio",
    "canvas_ratio_mode",
    "canvas_width_pct",
    "feature_index",
    "formatted_date_str",
]

DATE_KEYS = [
    "saved_lesson_date",
    "lesson_date_input",
]


def _sanitize_session_id(session_id: str) -> str | None:
    if not session_id or not isinstance(session_id, str):
        return None
    session_id = session_id.strip()
    if re.match(r"^[a-zA-Z0-9_\-]{6,64}$", session_id):
        return session_id
    return None


def get_current_session_id() -> str:
    """Recupera l'ID sessione dai query parameters dell'URL o ne genera uno nuovo per la sessione corrente."""
    try:
        raw_param = st.query_params.get(SESSION_PARAM_KEY)
    except Exception:
        raw_param = None

    valid_id = _sanitize_session_id(raw_param)
    if not valid_id:
        valid_id = uuid.uuid4().hex[:12]
        try:
            st.query_params[SESSION_PARAM_KEY] = valid_id
        except Exception:
            pass

    return valid_id


def inject_client_session_sync():
    """Sincronizza l'ID sessione nel LocalStorage del browser del client in modo puramente multi-utente.
    
    - Se l'utente apre l'app all'indirizzo base (/), il suo browser legge il proprio LocalStorage e
      reindirizza istantaneamente a ?session=<suo_id>.
    - Se è un utente nuovo, genera un nuovo ID nel suo LocalStorage.
    - Nessun file globale viene condiviso sul server: ogni utente è isolato.
    """
    current_param = ""
    try:
        current_param = st.query_params.get(SESSION_PARAM_KEY, "") or ""
    except Exception:
        current_param = ""

    sync_js = f"""
    <script>
    (function() {{
        try {{
            const pDoc = window.parent.document;
            const pWin = pDoc.defaultView || window.parent;
            const storageKey = 'appunti_user_session_id';
            const currentParam = '{current_param}';
            
            const searchParams = new URLSearchParams(pWin.location.search);
            const urlSession = searchParams.get('session');

            if (!urlSession) {{
                // L'utente ha aperto l'URL base senza parametri: cerca nel proprio LocalStorage client
                let clientSavedId = pWin.localStorage.getItem(storageKey);
                if (!clientSavedId || !/^[a-zA-Z0-9_\\-]{{6,64}}$/.test(clientSavedId)) {{
                    clientSavedId = currentParam || (Math.random().toString(36).substring(2, 10) + Math.random().toString(36).substring(2, 6));
                    pWin.localStorage.setItem(storageKey, clientSavedId);
                }}
                searchParams.set('session', clientSavedId);
                const newUrl = pWin.location.pathname + '?' + searchParams.toString() + pWin.location.hash;
                pWin.location.replace(newUrl);
            }} else {{
                // L'URL ha già il parametro: memorizzalo nel LocalStorage di questo client
                if (urlSession && /^[a-zA-Z0-9_\\-]{{6,64}}$/.test(urlSession)) {{
                    pWin.localStorage.setItem(storageKey, urlSession);
                }}
            }}
        }} catch(e) {{
            console.warn("Storage session sync warning:", e);
        }}
    }})();
    </script>
    """
    st.iframe(sync_js, height=1)


def _get_cache_file(session_id: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{session_id}.json"


def auto_save_session():
    """Salva su disco lo stato significativo della sessione corrente."""
    session_id = get_current_session_id()
    if not session_id:
        return

    # Non salvare se lo stato è completamente vuoto
    has_meaningful_data = any([
        st.session_state.get("appunti_generati"),
        st.session_state.get("notes_versions"),
        st.session_state.get("testo_estratto"),
        st.session_state.get("canvas_chat_history"),
        st.session_state.get("saved_vimeo_url"),
        st.session_state.get("_active_loaded_lesson_id"),
    ])

    cache_file = _get_cache_file(session_id)
    if not has_meaningful_data and not cache_file.exists():
        return

    payload = {
        "_updated_at": time.time(),
        "_session_id": session_id,
    }

    for k in PERSIST_KEYS:
        val = st.session_state.get(k)
        if val is not None:
            payload[k] = val

    for dk in DATE_KEYS:
        dval = st.session_state.get(dk)
        if dval is not None:
            if isinstance(dval, (datetime.date, datetime.datetime)):
                payload[dk] = dval.isoformat()
            elif isinstance(dval, str):
                payload[dk] = dval

    tmp_file = CACHE_DIR / f"{session_id}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, cache_file)
    except Exception:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass


def restore_session_if_available() -> bool:
    """Ripristina lo stato salvato se disponibile per l'ID sessione presente nell'URL."""
    if st.session_state.get("_session_restored", False):
        return False

    session_id = get_current_session_id()
    if not session_id:
        st.session_state["_session_restored"] = True
        return False

    cache_file = _get_cache_file(session_id)
    if not cache_file.exists():
        st.session_state["_session_restored"] = True
        return False

    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        st.session_state["_session_restored"] = True
        return False

    if not isinstance(data, dict):
        st.session_state["_session_restored"] = True
        return False

    # Ripristino chiavi standard
    for k in PERSIST_KEYS:
        if k in data and data[k] is not None:
            st.session_state[k] = data[k]

    # Ripristino date
    for dk in DATE_KEYS:
        if dk in data and data[dk]:
            try:
                raw_d = str(data[dk]).split("T")[0]
                parts = [int(p) for p in raw_d.split("-")]
                if len(parts) == 3:
                    st.session_state[dk] = datetime.date(parts[0], parts[1], parts[2])
            except Exception:
                pass

    # Sincronizzazione campi editor e ponti note
    restored_notes = st.session_state.get("appunti_generati")
    if restored_notes:
        st.session_state["_last_valid_appunti"] = restored_notes
        st.session_state["markdown_editor_area"] = restored_notes
        st.session_state["markdown_editor_area_canvas"] = restored_notes
        st.session_state["notes_sync_bridge_input"] = restored_notes

    # Assicura che notes_versions sia una lista coerente
    if not st.session_state.get("notes_versions") and restored_notes:
        st.session_state["notes_versions"] = [restored_notes]
        st.session_state["current_version_index"] = 0

    st.session_state["_session_restored"] = True
    return True


def reset_session():
    """Elimina i dati persistiti su disco e resetta lo stato della sessione."""
    session_id = get_current_session_id()
    if session_id:
        cache_file = _get_cache_file(session_id)
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception:
                pass

    # Reset delle chiavi di sessione
    for k in PERSIST_KEYS:
        if k in st.session_state:
            del st.session_state[k]

    for dk in DATE_KEYS:
        if dk in st.session_state:
            del st.session_state[dk]

    # Genera un nuovo ID sessione
    new_id = uuid.uuid4().hex[:12]
    try:
        st.query_params[SESSION_PARAM_KEY] = new_id
    except Exception:
        pass

    st.session_state["_session_restored"] = True


def cleanup_old_sessions(max_age_days: int = 7):
    """Rimuove periodicamente file di sessione scaduti (default 7 giorni / 1 settimana)."""
    if not CACHE_DIR.exists():
        return
    now = time.time()
    cutoff = now - (max_age_days * 86400)
    try:
        for p in CACHE_DIR.iterdir():
            if p.is_file() and (p.suffix in (".json", ".tmp")):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
    except Exception:
        pass
