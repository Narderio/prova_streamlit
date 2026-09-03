import streamlit as st
import os
import time
import datetime
import threading
import importlib
import html
import re
import json
from dotenv import load_dotenv

import backend
import notion_helper
import supabase_client
import gemini_rate_tracker
from streamlit.runtime.scriptrunner import add_script_run_ctx

# Ricarica dinamica moduli per garantire che le modifiche al codice backend siano sempre applicate
importlib.reload(backend)
importlib.reload(notion_helper)
importlib.reload(supabase_client)

from backend import (
    download_and_process, fetch_aggregated_transcript, generate_notes, generate_latex,
    export_to_notion, extract_vimeo_ids, agent_edit_notes, agent_edit_notes_stream,
    parse_agent_response, DEFAULT_PROMPT,
    agent_edit_targeted_stream, parse_targeted_agent_response,
    extract_targeted_edit_request, replace_section_in_markdown
)

load_dotenv()

# --- FUNZIONE JAVASCRIPT PER IL CLIPBOARD ---
def st_copy_to_clipboard(text, label="📋 Copia"):
    escaped_text = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")
    copy_js = f"""
    <div id="copy-container">
        <button id="copy-button" style="
            display: inline-flex; align-items: center; justify-content: center;
            font-weight: 400; padding: 0.25rem 0.75rem; border-radius: 0.5rem;
            margin: 0px; line-height: 1.6; color: inherit; width: auto;
            cursor: pointer; user-select: none; background-color: rgb(255, 255, 255);
            border: 1px solid rgba(49, 51, 63, 0.2); font-family: 'Source Sans Pro', sans-serif;
            ">
            {label}
        </button>
    </div>
    <script>
    const btn = document.getElementById('copy-button');
    btn.addEventListener('click', function() {{
        const textToCopy = '{escaped_text}';
        navigator.clipboard.writeText(textToCopy).then(() => {{
            btn.innerText = '✅ Copiato!';
            btn.style.backgroundColor = '#d4edda';
            setTimeout(() => {{ btn.innerText = '{label}'; btn.style.backgroundColor = 'white'; }}, 2000);
        }}).catch(err => {{ alert('Errore copia. Usa HTTPS.'); }});
    }});
    </script>
    """
    st.iframe(copy_js, height=50)

# --- CALLBACK E HELPER PER LA GESTIONE DELLE VERSIONI DEGLI APPUNTI ---
def safe_set_session_state(key, val):
    try:
        st.session_state[key] = val
    except Exception:
        pass

def safe_sync_version_tabs(new_ver_str):
    for k in list(st.session_state.keys()):
        if k.endswith("_ver_segmented_tab"):
            safe_set_session_state(k, new_ver_str)

def add_note_version(new_notes):
    """Aggiunge una nuova versione degli appunti accodandola sempre alla fine."""
    if not new_notes:
        return
    new_notes = notion_helper.normalize_images_to_markdown(new_notes)
    if 'notes_versions' not in st.session_state or not isinstance(st.session_state.notes_versions, list):
        st.session_state.notes_versions = []
    
    st.session_state.notes_versions.append(new_notes)
    new_idx = len(st.session_state.notes_versions) - 1
    st.session_state.current_version_index = new_idx
    st.session_state.force_version_sync = new_idx
    st.session_state.appunti_generati = new_notes
    st.session_state._last_valid_appunti = new_notes
    st.session_state._version_just_switched = True
    st.session_state._version_switch_timestamp = time.time()
    
    safe_set_session_state("markdown_editor_area", new_notes)
    safe_set_session_state("markdown_editor_area_canvas", new_notes)
    safe_set_session_state("notes_sync_bridge_input", new_notes)

    new_ver_str = str(new_idx + 1)
    safe_sync_version_tabs(new_ver_str)

def switch_note_version(target_index):
    """Cambia la versione attiva degli appunti in modo atomico."""
    if 'notes_versions' in st.session_state and 0 <= target_index < len(st.session_state.notes_versions):
        st.session_state.current_version_index = target_index
        st.session_state.force_version_sync = target_index
        selected_notes = notion_helper.normalize_images_to_markdown(st.session_state.notes_versions[target_index])
        st.session_state.notes_versions[target_index] = selected_notes
        st.session_state.appunti_generati = selected_notes
        st.session_state._last_valid_appunti = selected_notes
        st.session_state._version_just_switched = True
        st.session_state._version_switch_timestamp = time.time()
        
        safe_set_session_state("markdown_editor_area", selected_notes)
        safe_set_session_state("markdown_editor_area_canvas", selected_notes)
        safe_set_session_state("notes_sync_bridge_input", selected_notes)

        new_ver_str = str(target_index + 1)
        safe_sync_version_tabs(new_ver_str)

def update_appunti_from_editor():
    if st.session_state.get("_version_just_switched", False):
        return
    if time.time() - st.session_state.get("_version_switch_timestamp", 0) < 0.8:
        return
        
    updated_val = None
    if "markdown_editor_area" in st.session_state and st.session_state.markdown_editor_area:
        updated_val = st.session_state.markdown_editor_area
    elif "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
        updated_val = st.session_state.markdown_editor_area_canvas
    
    if updated_val:
        idx = st.session_state.get('current_version_index', 0)
        versions = st.session_state.get('notes_versions', [])
        if 0 <= idx < len(versions):
            if versions[idx] != updated_val:
                st.session_state.notes_versions[idx] = updated_val
                st.session_state.appunti_generati = updated_val
                st.session_state._last_valid_appunti = updated_val

def render_version_navigation_bar(key_prefix=""):
    versions = st.session_state.get("notes_versions", [])
    if (not versions or not isinstance(versions, list)) and st.session_state.get("appunti_generati"):
        st.session_state.notes_versions = [st.session_state.appunti_generati]
        st.session_state.current_version_index = 0
        versions = st.session_state.notes_versions

    if versions and len(versions) > 0:
        current_idx = st.session_state.get("current_version_index", 0)
        options = [str(i + 1) for i in range(len(versions))]
        
        if current_idx >= len(options):
            current_idx = len(options) - 1
            st.session_state.current_version_index = current_idx

        widget_key = f"{key_prefix}_ver_segmented_tab"
        target_str = options[current_idx]
        
        # Se è stata richiesta una sincronizzazione forzata della versione dal codice (es. nuova versione creata o switch)
        forced_idx = st.session_state.get("force_version_sync")
        if forced_idx is not None and 0 <= forced_idx < len(options):
            st.session_state.current_version_index = forced_idx
            target_str = options[forced_idx]
            safe_set_session_state(widget_key, target_str)
            # Consuma la richiesta di sync forzato
            st.session_state.force_version_sync = None
        elif widget_key not in st.session_state or st.session_state[widget_key] not in options:
            safe_set_session_state(widget_key, target_str)

        def on_segmented_version_change():
            val = st.session_state.get(widget_key)
            if val and val in options:
                new_i = int(val) - 1
                if new_i != st.session_state.get("current_version_index"):
                    switch_note_version(new_i)

        selected_v = st.segmented_control(
            "Versione",
            options=options,
            selection_mode="single",
            required=True,
            label_visibility="collapsed",
            key=widget_key,
            on_change=on_segmented_version_change
        )
        
        # Consuma il flag _version_just_switched in modo che non persista nei render successivi
        if st.session_state.get("_version_just_switched", False):
            st.session_state._version_just_switched = False

        if selected_v and selected_v in options:
            new_idx = int(selected_v) - 1
            if new_idx < len(versions) and new_idx != st.session_state.get("current_version_index"):
                switch_note_version(new_idx)
                st.rerun()

# --- FUNZIONI DI CACHING PER ELIMINARE RITARDI DI RETE AD OGNI RERUN ---
@st.cache_data(ttl=600, show_spinner=False)
def cached_get_available_courses(corsi_id, token):
    return notion_helper.get_available_courses(corsi_id, token)

@st.cache_data(ttl=300, show_spinner=False)
def cached_get_course_lessons(course_page_id, course_name, token):
    return notion_helper.get_course_lessons(course_page_id, course_name=course_name, api_key=token)

@st.cache_data(ttl=600, show_spinner=False)
def cached_fetch_aggregated_transcript(video_urls_tuple):
    if not video_urls_tuple:
        return False, "Nessun video specificato."
    return backend.fetch_aggregated_transcript(list(video_urls_tuple))

@st.cache_data(ttl=300, show_spinner=False)
def cached_is_video_processed(v_id):
    return supabase_client.is_video_processed(v_id)

@st.cache_data(ttl=300, show_spinner=False)
def cached_get_all_lesson_videos(video_id=None, course=None, lesson_date=None, notion_page_id=None):
    return supabase_client.get_all_lesson_videos(video_id=video_id, course=course, lesson_date=lesson_date, notion_page_id=notion_page_id)

@st.cache_data(ttl=600, show_spinner=False)
def cached_get_notion_page_markdown(page_id, token):
    return notion_helper.get_notion_page_markdown(page_id, api_key=token)

def parse_date_safely(date_val, title_val=""):
    if date_val:
        try:
            if isinstance(date_val, datetime.date):
                return date_val
            if isinstance(date_val, datetime.datetime):
                return date_val.date()
            parts = str(date_val).strip().split("-")
            if len(parts) == 3:
                return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            pass
    if title_val:
        match = re.search(r'(\d{2})[/.-](\d{2})[/.-](\d{4})', str(title_val))
        if match:
            try:
                return datetime.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            except Exception:
                pass
    return datetime.date.today()

# --- INIZIO INTERFACCIA STREAMLIT ---
st.set_page_config(page_title="Vimeo to Notion University Notes", page_icon="🎓", layout="wide")

# --- SIDEBAR CONFIGURAZIONE ---
st.sidebar.title("⚙️ Configurazione")

# 1. Google API Key (senza mai esporre la chiave in .env nell'interfaccia)
env_key = os.getenv("GOOGLE_API_KEY")
if env_key:
    api_key_override = st.sidebar.text_input("Sovrascrivi API Key (opzionale)", type="password", help="Lascia vuoto per usare la chiave in .env")
    if api_key_override.strip():
        os.environ["GOOGLE_API_KEY"] = api_key_override.strip()
else:
    user_api_key = st.sidebar.text_input("Google API Key", type="password", help="Inserisci la tua chiave API per Gemini")
    if user_api_key.strip():
        os.environ["GOOGLE_API_KEY"] = user_api_key.strip()

rpm, rpd = gemini_rate_tracker.get_metrics()
st.sidebar.caption("Statistiche API Gemini (Locali)")
col1, col2 = st.sidebar.columns(2)
col1.metric("RPM", f"{rpm} / 15", help="Richieste nell'ultimo minuto (attesa automatica al raggiungimento di 13)")
col2.metric("RPD", f"{rpd} / 500", help="Richieste nelle ultime 24 ore (blocco di sicurezza al raggiungimento di 495)")

if rpd >= 495:
    st.sidebar.error("⛔ Limite di sicurezza (495/500 RPD) raggiunto! Richieste bloccate fino al reset del giorno.")
elif rpm >= 13:
    st.sidebar.warning("⏳ Soglia di 13/15 RPM raggiunta. Nuove richieste in attesa automatica del reset del minuto.")

st.sidebar.divider()

# Modelli Gemini configurati per l'applicazione (Default: gemini-3.5-flash-lite)
MODEL_NOTES = "gemini-3.5-flash-lite"
MODEL_GENERAL = "gemini-3.5-flash-lite"

# --- INIZIALIZZAZIONE SESSION STATE ---
if 'testo_estratto' not in st.session_state:
    st.session_state.testo_estratto = None
if 'appunti_generati' not in st.session_state:
    st.session_state.appunti_generati = None
if 'notes_versions' not in st.session_state:
    st.session_state.notes_versions = []
if 'current_version_index' not in st.session_state:
    st.session_state.current_version_index = 0
if 'latex_generato' not in st.session_state:
    st.session_state.latex_generato = None
if 'notion_status' not in st.session_state:
    st.session_state.notion_status = None
if 'notion_page_url' not in st.session_state:
    st.session_state.notion_page_url = None
if 'current_notion_page_id' not in st.session_state:
    st.session_state.current_notion_page_id = None
if 'show_canvas_chat' not in st.session_state:
    st.session_state.show_canvas_chat = False
if 'canvas_chat_history' not in st.session_state:
    st.session_state.canvas_chat_history = []
if 'pending_agent_stream' not in st.session_state:
    st.session_state.pending_agent_stream = False
if 'notion_save_thread' not in st.session_state:
    st.session_state.notion_save_thread = None
if 'saved_vimeo_url' not in st.session_state:
    st.session_state.saved_vimeo_url = ""
if 'vimeo_url_input' not in st.session_state:
    st.session_state.vimeo_url_input = ""
if 'saved_lesson_date' not in st.session_state:
    st.session_state.saved_lesson_date = datetime.date.today()
if 'lesson_date_input' not in st.session_state:
    st.session_state.lesson_date_input = datetime.date.today()
if '_last_saved_notion_notes' not in st.session_state:
    st.session_state._last_saved_notion_notes = None
if '_should_scroll_to_results' not in st.session_state:
    st.session_state._should_scroll_to_results = False

def normalize_markdown_for_comparison(text):
    if text is None:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in t.splitlines()]
    return "\n".join(lines).strip()

def check_has_unsaved_changes():
    # Se un salvataggio su Notion è già in corso in background, non mostrare l'avviso
    if is_notion_saving_active():
        return False

    curr_notes = st.session_state.get("appunti_generati")
    if not curr_notes or not str(curr_notes).strip():
        return False
    
    last_saved = st.session_state.get("_last_saved_notion_notes")
    if last_saved is None:
        return True
    
    return normalize_markdown_for_comparison(curr_notes) != normalize_markdown_for_comparison(last_saved)

# --- FUNZIONE DI VERIFICA LOCK NOTION ---
def is_notion_saving_active():
    thread = st.session_state.get("notion_save_thread")
    if thread:
        if thread.is_alive():
            return True
        else:
            st.session_state.notion_save_thread = None
            st.session_state.notion_status = "✅ Pagina aggiornata su Notion con successo!"
            st.toast("✅ Appunti salvati su Notion con successo!", icon="🎉")
            st.rerun()
            return False
    return False

# --- FUNZIONE DI VERIFICA ED ESECUZIONE IN BACKGROUND PER LATEX ---
def is_latex_regen_active():
    thread = st.session_state.get("latex_regen_thread")
    if thread:
        if thread.is_alive():
            return True
        else:
            st.session_state.latex_regen_thread = None
            if st.session_state.get("latex_regen_error"):
                err = st.session_state.latex_regen_error
                st.session_state.latex_regen_error = None
                st.toast(f"❌ Errore generazione LaTeX: {err}", icon="⚠️")
            else:
                st.toast("📄 Codice LaTeX generato con successo!", icon="✅")
            st.rerun()
            return False
    return False

def trigger_background_latex_regen(model=MODEL_GENERAL):
    if is_latex_regen_active():
        st.warning("⏳ Generazione LaTeX già in corso in background...")
        st.toast("⏳ Generazione LaTeX già in corso in background...", icon="⚠️")
        return
    notes_snap = st.session_state.get("appunti_generati")
    if not notes_snap:
        st.warning("Nessun appunto presente per generare il LaTeX.")
        st.toast("⚠️ Nessun appunto presente per generare il LaTeX.", icon="⚠️")
        return
    
    def _worker(notes, model):
        try:
            success_lat, latex_res = generate_latex(notes, model_name=model)
            if success_lat:
                st.session_state.latex_generato = latex_res
                st.session_state.latex_regen_error = None
            else:
                st.session_state.latex_regen_error = latex_res
        except Exception as e:
            st.session_state.latex_regen_error = str(e)
            
    t = threading.Thread(target=_worker, args=(notes_snap, model), daemon=True)
    add_script_run_ctx(t)
    st.session_state.latex_regen_thread = t
    t.start()
    st.toast("⚡ Rigenerazione LaTeX avviata in background!", icon="📄")

def sync_latex_reprocess_checkboxes():
    already_proc = st.session_state.get("already_processed", False)
    has_reprocess = st.session_state.get("chk_force_reprocess", False)
    is_new_proc = (not already_proc) or has_reprocess

    has_latex = st.session_state.get("chk_do_latex", False)
    has_markdown = st.session_state.get("chk_do_markdown_notion", False)

    if is_new_proc:
        if has_latex:
            st.session_state.chk_do_markdown_notion = True
            st.session_state.chk_do_transcript = True
        elif has_markdown:
            st.session_state.chk_do_transcript = True

# --- FUNZIONE DI SALVATAGGIO SU NOTION CON LOCK E SNAPSHOT ---
def save_current_notes_to_notion():
    if is_notion_saving_active():
        st.warning("⏳ Un salvataggio su Notion è già in corso. Attendi che il salvataggio precedente sia completato.")
        st.toast("⏳ Salvataggio su Notion già in corso...", icon="⚠️")
        return

    # Sincronizza lo stato più recente degli appunti
    cur_idx = st.session_state.get("current_version_index", 0)
    versions = st.session_state.get("notes_versions", [])
    bridge_val = st.session_state.get("notes_sync_bridge_input")
    if 0 <= cur_idx < len(versions):
        if bridge_val and str(bridge_val).strip() and bridge_val != versions[cur_idx]:
            clean_bridge = notion_helper.normalize_images_to_markdown(bridge_val)
            versions[cur_idx] = clean_bridge
            st.session_state.appunti_generati = clean_bridge
            st.session_state._last_valid_appunti = clean_bridge
        elif st.session_state.get("appunti_generati"):
            clean_appunti = notion_helper.normalize_images_to_markdown(st.session_state.appunti_generati)
            versions[cur_idx] = clean_appunti
            st.session_state.appunti_generati = clean_appunti

    # Priorità assoluta alla lezione esplicitamente selezionata / attiva
    target_pid = st.session_state.get("_active_loaded_lesson_id") or st.session_state.get("current_notion_page_id")
    notion_token = os.getenv("NOTION_API_KEY")
    notion_corsi_id = os.getenv("NOTION_CORSI_PAGE_ID")
    
    if not target_pid and notion_corsi_id:
        selected_course = st.session_state.get("selected_course", "Generale")
        formatted_date_str = st.session_state.get("formatted_date_str", datetime.date.today().strftime("%d/%m/%Y"))
        already_processed = st.session_state.get("already_processed", False)
        selected_course_page_id = st.session_state.get("selected_course_page_id", notion_corsi_id)
        
        db_id, _ = notion_helper.get_or_create_course_database(selected_course_page_id, selected_course, notion_token)
        target_pid, _, _ = notion_helper.get_or_create_lesson_entry(db_id, formatted_date_str, is_same_video=already_processed, api_key=notion_token)
        st.session_state.current_notion_page_id = target_pid
        st.session_state._active_loaded_lesson_id = target_pid

    if target_pid:
        markdown_snapshot = notion_helper.normalize_images_to_markdown(str(st.session_state.appunti_generati or ""))
        st.session_state.appunti_generati = markdown_snapshot
        st.session_state._last_saved_notion_notes = markdown_snapshot
        st.session_state._last_saved_version_index = cur_idx
        safe_set_session_state("notes_sync_bridge_input", markdown_snapshot)
        safe_set_session_state("markdown_editor_area", markdown_snapshot)
        safe_set_session_state("markdown_editor_area_canvas", markdown_snapshot)
        st.session_state._version_just_switched = True
        st.session_state._version_switch_timestamp = time.time()

        bg_thread = threading.Thread(
            target=notion_helper.update_notion_page_in_place,
            args=(target_pid, markdown_snapshot, notion_token),
            daemon=True
        )
        add_script_run_ctx(bg_thread)
        st.session_state.notion_save_thread = bg_thread
        bg_thread.start()

        clean_pid = notion_helper.format_notion_id(target_pid).replace("-", "")
        st.session_state.notion_page_url = f"https://www.notion.so/{clean_pid}"
        st.session_state.notion_status = "⚡ Salvataggio avviato su Notion con snapshot protetto!"
        st.session_state.canvas_view_radio = "👁️ Anteprima Formattata"
        st.session_state.standard_view_radio = "👁️ Anteprima Formattata"
        st.toast("📤 Salvataggio degli appunti su Notion avviato!", icon="🚀")
        st.rerun()
    else:
        st.error("Impossibile individuare la pagina Notion da aggiornare.")
        st.toast("❌ Errore: Impossibile individuare la pagina Notion da aggiornare.", icon="⚠️")

# --- COMPONENTI FRAGMENT PER IL PULSANTE NOTION ---
@st.fragment(run_every="2s")
def render_notion_save_button_split():
    if is_notion_saving_active():
        st.button("⏳ Salvataggio Notion...", disabled=True, key="btn_save_canvas_split_dis", use_container_width=True)
    else:
        if st.button("💾 Salva su Notion", type="primary", key="btn_save_canvas_split", use_container_width=True):
            save_current_notes_to_notion()

@st.fragment(run_every="2s")
def render_notion_save_button_tab():
    if is_notion_saving_active():
        st.button("⏳ Salvataggio Notion...", disabled=True, key="btn_save_edited_notion_dis", use_container_width=True)
    else:
        btn_type = "primary" if check_has_unsaved_changes() else "secondary"
        if st.button("📤 Salva su Notion", type=btn_type, use_container_width=True, key="btn_save_edited_notion"):
            save_current_notes_to_notion()

if 'canvas_ratio_mode' not in st.session_state:
    st.session_state.canvas_ratio_mode = "Canvas XXL"
if 'canvas_width_pct' not in st.session_state:
    st.session_state.canvas_width_pct = 55

# --- SCRIPT JAVASCRIPT UNIVERSALE PER INSERIMENTO E RIDIMENSIONAMENTO IMMAGINI (DRAG-TO-RESIZE) ---
def handle_notes_sync_bridge():
    if st.session_state.get("_version_just_switched", False):
        st.session_state._version_just_switched = False
        return
    if time.time() - st.session_state.get("_version_switch_timestamp", 0) < 0.8:
        return

    new_val = st.session_state.get("notes_sync_bridge_input", "")
    if not new_val or not new_val.strip():
        return

    cur_idx = st.session_state.get("current_version_index", 0)
    versions = st.session_state.get("notes_versions", [])
    if 0 <= cur_idx < len(versions):
        if versions[cur_idx] != new_val:
            st.session_state.notes_versions[cur_idx] = new_val
            st.session_state.appunti_generati = new_val
            st.session_state._last_valid_appunti = new_val
            if "markdown_editor_area_canvas" in st.session_state:
                st.session_state.markdown_editor_area_canvas = new_val
            if "markdown_editor_area" in st.session_state:
                st.session_state.markdown_editor_area = new_val

def render_notes_sync_bridge():
    st.markdown("""
    <style>
    div[data-testid="stTextArea"]:has(textarea[aria-label="__notes_sync_bridge__"]),
    div:has(> textarea[aria-label="__notes_sync_bridge__"]),
    div[data-testid="element-container"]:has(textarea[aria-label="__notes_sync_bridge__"]),
    textarea[aria-label="__notes_sync_bridge__"] {
        position: fixed !important;
        left: -9999px !important;
        top: -9999px !important;
        width: 1px !important;
        height: 1px !important;
        opacity: 0.01 !important;
        pointer-events: none !important;
    }
    </style>
    """, unsafe_allow_html=True)
    st.text_area(
        "__notes_sync_bridge__",
        value=st.session_state.get("appunti_generati", ""),
        key="notes_sync_bridge_input",
        on_change=handle_notes_sync_bridge,
        label_visibility="collapsed"
    )

def generate_image_paste_drop_js():
    """
    Genera lo script JavaScript per:
    1. Intercettare Ctrl+V (paste) e Drag & Drop di immagini.
    2. Gestire il ridimensionamento interattivo con maniglia di trascinamento (Drag-to-Resize) dall'anteprima formattata.
    """
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_KEY", "")
    
    if not supabase_url or not supabase_key:
        return ""
    
    return f"""
    <script>
    (function() {{
        const pDoc = window.parent.document;
        const pWin = pDoc.defaultView || window.parent;
        const SUPABASE_URL = '{supabase_url}';
        const SUPABASE_KEY = '{supabase_key}';
        const BUCKET = 'canvas-images';

        // Stili per la maniglia di trascinamento e il badge percentuale
        function ensureImageResizeStyles() {{
            if (pDoc.getElementById('image-resize-styles')) return;
            var styleEl = pDoc.createElement('style');
            styleEl.id = 'image-resize-styles';
            styleEl.textContent = `
                .resizable-img-wrapper {{
                    position: relative;
                    user-select: none;
                    display: block;
                    margin: 18px auto;
                }}
                .img-drag-handle {{
                    position: absolute;
                    bottom: 8px;
                    right: 8px;
                    width: 24px;
                    height: 24px;
                    background: rgba(15, 23, 42, 0.88);
                    border: 1.5px solid rgba(255, 255, 255, 0.45);
                    border-radius: 6px;
                    color: #ffffff;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    cursor: nwse-resize;
                    opacity: 0;
                    transition: opacity 0.2s ease, transform 0.15s ease, background-color 0.2s ease;
                    z-index: 20;
                    backdrop-filter: blur(4px);
                    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
                    touch-action: none;
                }}
                .resizable-img-wrapper:hover .img-drag-handle {{
                    opacity: 0.85;
                }}
                .img-drag-handle:hover, .img-drag-handle.active-drag {{
                    opacity: 1 !important;
                    transform: scale(1.18);
                    background: #2563eb !important;
                    border-color: #60a5fa !important;
                }}
                .img-size-badge {{
                    position: absolute;
                    bottom: 38px;
                    right: 8px;
                    background: rgba(15, 23, 42, 0.95);
                    color: #60a5fa;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 3px 8px;
                    border-radius: 6px;
                    border: 1px solid #3b82f6;
                    z-index: 25;
                    pointer-events: none;
                    font-family: system-ui, -apple-system, sans-serif;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
                }}
            `;
            pDoc.head.appendChild(styleEl);
        }}

        function uuid4() {{
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {{
                var r = Math.random() * 16 | 0;
                return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
            }});
        }}

        function compressImageToWebP(file, maxWidth, quality) {{
            maxWidth = maxWidth || 1920;
            quality = quality || 0.82;
            return new Promise(function(resolve, reject) {{
                var reader = new FileReader();
                reader.onload = function(e) {{
                    var img = new Image();
                    img.onload = function() {{
                        var canvas = pDoc.createElement('canvas');
                        var w = img.width;
                        var h = img.height;
                        if (w > maxWidth) {{
                            h = Math.round(h * maxWidth / w);
                            w = maxWidth;
                        }}
                        canvas.width = w;
                        canvas.height = h;
                        var ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, w, h);
                        canvas.toBlob(function(blob) {{
                            if (blob) {{
                                resolve(blob);
                            }} else {{
                                resolve(file);
                            }}
                        }}, 'image/webp', quality);
                    }};
                    img.onerror = function() {{ reject(new Error('Errore caricamento immagine')); }};
                    img.src = e.target.result;
                }};
                reader.onerror = function() {{ reject(new Error('Errore lettura file')); }};
                reader.readAsDataURL(file);
            }});
        }}

        function uploadToSupabase(blob, filename) {{
            var filePath = 'images/' + filename;
            var uploadUrl = SUPABASE_URL + '/storage/v1/object/' + BUCKET + '/' + filePath;

            return fetch(uploadUrl, {{
                method: 'POST',
                headers: {{
                    'Authorization': 'Bearer ' + SUPABASE_KEY,
                    'apikey': SUPABASE_KEY,
                    'Content-Type': blob.type || 'image/webp',
                    'Cache-Control': 'public, max-age=31536000, immutable'
                }},
                body: blob
            }}).then(function(res) {{
                if (!res.ok) {{
                    return res.text().then(function(t) {{ throw new Error('Upload fallito: ' + t); }});
                }}
                var publicUrl = SUPABASE_URL + '/storage/v1/object/public/' + BUCKET + '/' + filePath;
                return publicUrl;
            }});
        }}

        function showUploadIndicator(show) {{
            var indicator = pDoc.getElementById('img-upload-indicator');
            if (show) {{
                if (!indicator) {{
                    indicator = pDoc.createElement('div');
                    indicator.id = 'img-upload-indicator';
                    indicator.innerHTML = '<div style="position:fixed;bottom:80px;right:25px;z-index:999999999;background:rgba(15,23,42,0.95);backdrop-filter:blur(10px);border:1px solid #3b82f6;border-radius:12px;padding:12px 20px;box-shadow:0 10px 30px rgba(0,0,0,0.6);display:flex;align-items:center;gap:10px;animation:slide-in-notification 0.3s ease-out;"><div style="width:18px;height:18px;border:2.5px solid #3b82f6;border-top-color:transparent;border-radius:50%;animation:spin-upload 0.8s linear infinite;"></div><span style="color:#60a5fa;font-size:13px;font-weight:600;">Caricamento immagine...</span></div><style>@keyframes spin-upload{{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}</style>';
                    pDoc.body.appendChild(indicator);
                }}
                indicator.style.display = 'block';
            }} else {{
                if (indicator) indicator.remove();
            }}
        }}

        function showUploadToast(success, message) {{
            try {{
                var oldToasts = pDoc.querySelectorAll('.custom-upload-toast');
                oldToasts.forEach(function(el) {{ el.remove(); }});
            }} catch(e) {{}}

            var toast = pDoc.createElement('div');
            toast.className = 'custom-upload-toast';
            var bg = success ? 'rgba(16,185,129,0.95)' : 'rgba(239,68,68,0.95)';
            var icon = success ? '✅' : '❌';
            if (!success && message.includes("Modalità Modifica")) {{
                icon = '⚠️';
                bg = 'rgba(245,158,11,0.95)';
            }}
            toast.style.cssText = 'position:fixed;bottom:80px;right:25px;z-index:999999999;background:' + bg + ';backdrop-filter:blur(10px);border-radius:12px;padding:12px 20px;box-shadow:0 10px 30px rgba(0,0,0,0.6);color:#ffffff;font-size:13px;font-weight:600;cursor:pointer;user-select:none;transition:opacity 0.4s ease, transform 0.4s ease;animation:slide-in-notification 0.3s ease-out;';
            toast.innerHTML = icon + ' ' + message;
            
            toast.onclick = function() {{
                toast.remove();
            }};

            pDoc.body.appendChild(toast);

            pWin.setTimeout(function() {{
                try {{
                    toast.style.opacity = '0';
                    toast.style.transform = 'translateY(15px)';
                    pWin.setTimeout(function() {{
                        try {{ toast.remove(); }} catch(e) {{}}
                    }}, 400);
                }} catch(e) {{
                    try {{ toast.remove(); }} catch(e2) {{}}
                }}
            }}, 2800);
        }}

        function findTargetTextarea(eventTarget) {{
            if (eventTarget && eventTarget.tagName === 'TEXTAREA') return eventTarget;
            
            var rawEditor = pDoc.querySelector('.raw-markdown-editor');
            if (rawEditor) return rawEditor;

            var textareas = pDoc.querySelectorAll('textarea');
            for (var i = 0; i < textareas.length; i++) {{
                var label = textareas[i].getAttribute('aria-label') || '';
                
                if (label.indexOf('Assistente AI') !== -1 || label.indexOf('Chiedi') !== -1 || label.indexOf('__notes_sync_bridge__') !== -1) {{
                    continue;
                }}
                
                if (label.indexOf('Modifica direttamente il testo') !== -1 || label.indexOf('Modifica liberamente il testo') !== -1) {{
                    return textareas[i];
                }}
            }}
            return null; 
        }}

        function insertTextAtCursor(textarea, textToInsert) {{
            if (!textarea) return;
            
            try {{ textarea.focus(); }} catch(e) {{}}
            
            var start = textarea.selectionStart || 0;
            var end = textarea.selectionEnd || 0;
            var currentVal = textarea.value || '';

            var prefix = '\\n\\n';
            var suffix = '\\n\\n';

            var newValue = currentVal.substring(0, start) + prefix + textToInsert + suffix + currentVal.substring(end);

            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                pWin.HTMLTextAreaElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(textarea, newValue);

            textarea.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
            textarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
            
            var bridge = pDoc.querySelector('textarea[aria-label="__notes_sync_bridge__"]');
            if (bridge) {{
                if (nativeInputValueSetter) {{
                    nativeInputValueSetter.call(bridge, newValue);
                }} else {{
                    bridge.value = newValue;
                }}
                var tracker = bridge._valueTracker;
                if (tracker) tracker.setValue(currentVal);
                bridge.dispatchEvent(new Event('input', {{ bubbles: true }}));
                bridge.dispatchEvent(new Event('change', {{ bubbles: true }}));
                bridge.dispatchEvent(new Event('focusout', {{ bubbles: true }}));
                bridge.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }}
            
            var newCursorPos = start + prefix.length + textToInsert.length + suffix.length;
            textarea.selectionStart = newCursorPos;
            textarea.selectionEnd = newCursorPos;
            
            try {{ textarea.focus(); }} catch(e) {{}}
        }}

        function escapeRegExp(string) {{
            return string.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
        }}

        function syncImageSizeChange(rawUrl, rawAlt, newPct) {{
            var escapedUrl = escapeRegExp(rawUrl);
            var imgRegex = new RegExp('!\\[([^\\]]*)\\]\\(' + escapedUrl + '\\)', 'g');
            var cleanAlt = rawAlt.split('|')[0].trim() || 'Immagine';
            var newTag = '![' + cleanAlt + '|' + newPct + '%](' + rawUrl + ')';

            // 1. Controlla prima le textarea visibili
            var visibleTextarea = null;
            var textareas = pDoc.querySelectorAll('textarea');
            for (var i = 0; i < textareas.length; i++) {{
                var lbl = textareas[i].getAttribute('aria-label') || '';
                if (lbl.indexOf('Modifica direttamente il testo') !== -1 || lbl.indexOf('Modifica liberamente il testo') !== -1) {{
                    visibleTextarea = textareas[i];
                    break;
                }}
            }}

            if (visibleTextarea) {{
                var currentVal = visibleTextarea.value || '';
                if (imgRegex.test(currentVal)) {{
                    var updatedVal = currentVal.replace(imgRegex, newTag);
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, 'value').set;
                    nativeInputValueSetter.call(visibleTextarea, updatedVal);
                    visibleTextarea.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
                    visibleTextarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                    return;
                }}
            }}

            // 2. Se in Anteprima Formattata, usa il bridge textarea nascosto
            var bridgeTextarea = pDoc.querySelector('textarea[aria-label="__notes_sync_bridge__"]');
            if (bridgeTextarea) {{
                var currentBridgeVal = bridgeTextarea.value || '';
                if (imgRegex.test(currentBridgeVal)) {{
                    var updatedBridgeVal = currentBridgeVal.replace(imgRegex, newTag);
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, 'value').set;
                    nativeInputValueSetter.call(bridgeTextarea, updatedBridgeVal);
                    bridgeTextarea.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
                    bridgeTextarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                }}
            }}
        }}

        // --- GESTIONE DRAG-TO-RESIZE IMMAGINI NELL'ANTEPRIMA FORMATTATA ---
        function initImageResizeDrag() {{
            ensureImageResizeStyles();

            var isResizing = false;
            var currentWrapper = null;
            var currentBadge = null;
            var startX = 0;
            var startWidth = 0;
            var parentWidth = 0;
            var rawUrl = '';
            var rawAlt = '';
            var finalPct = 100;

            function onMouseDown(e) {{
                var handle = e.target.closest ? e.target.closest('.img-drag-handle') : null;
                if (!handle) return;

                e.preventDefault();
                e.stopPropagation();

                currentWrapper = handle.closest('.resizable-img-wrapper');
                if (!currentWrapper) return;

                currentBadge = currentWrapper.querySelector('.img-size-badge');
                rawUrl = currentWrapper.getAttribute('data-raw-url') || '';
                rawAlt = currentWrapper.getAttribute('data-raw-alt') || 'Immagine';

                var wrapperRect = currentWrapper.getBoundingClientRect();
                var container = currentWrapper.closest('[data-testid="stColumn"]') || 
                                currentWrapper.closest('.stTabs') || 
                                currentWrapper.parentElement;
                var containerRect = container ? container.getBoundingClientRect() : wrapperRect;

                startX = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 0);
                startWidth = wrapperRect.width;
                parentWidth = containerRect.width || startWidth;

                isResizing = true;
                handle.classList.add('active-drag');
                if (currentBadge) {{
                    currentBadge.style.display = 'block';
                    var curPct = Math.max(15, Math.min(100, Math.round(startWidth / parentWidth * 100)));
                    currentBadge.innerText = curPct + '%';
                }}

                pDoc.body.style.cursor = 'nwse-resize';
                pDoc.body.style.userSelect = 'none';

                pDoc.addEventListener('mousemove', onMouseMove, true);
                pDoc.addEventListener('mouseup', onMouseUp, true);
                pDoc.addEventListener('touchmove', onMouseMove, {{ passive: false, capture: true }});
                pDoc.addEventListener('touchend', onMouseUp, true);
            }}

            function onMouseMove(e) {{
                if (!isResizing || !currentWrapper) return;

                e.preventDefault();
                var clientX = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : startX);
                var deltaX = clientX - startX;
                var newWidthPx = Math.max(80, Math.min(parentWidth, startWidth + (deltaX * 2)));
                var pct = Math.max(15, Math.min(100, Math.round(newWidthPx / parentWidth * 100)));
                finalPct = pct;

                currentWrapper.style.width = pct + '%';
                if (currentBadge) {{
                    currentBadge.innerText = pct + '%';
                    currentBadge.style.display = 'block';
                }}
            }}

            function onMouseUp(e) {{
                if (!isResizing) return;
                isResizing = false;

                pDoc.removeEventListener('mousemove', onMouseMove, true);
                pDoc.removeEventListener('mouseup', onMouseUp, true);
                pDoc.removeEventListener('touchmove', onMouseMove, true);
                pDoc.removeEventListener('touchend', onMouseUp, true);

                pDoc.body.style.cursor = '';
                pDoc.body.style.userSelect = '';

                if (currentWrapper) {{
                    var handle = currentWrapper.querySelector('.img-drag-handle');
                    if (handle) handle.classList.remove('active-drag');
                }}

                if (currentBadge) {{
                    setTimeout(function() {{
                        if (currentBadge && !isResizing) currentBadge.style.display = 'none';
                    }}, 1200);
                }}

                if (rawUrl) {{
                    syncImageSizeChange(rawUrl, rawAlt, finalPct);
                }}

                currentWrapper = null;
                currentBadge = null;
            }}

            if (pDoc.__imgResizeMouseDown) {{
                pDoc.removeEventListener('mousedown', pDoc.__imgResizeMouseDown, true);
            }}
            if (pDoc.__imgResizeTouchStart) {{
                pDoc.removeEventListener('touchstart', pDoc.__imgResizeTouchStart, true);
            }}

            pDoc.__imgResizeMouseDown = onMouseDown;
            pDoc.__imgResizeTouchStart = onMouseDown;

            pDoc.addEventListener('mousedown', onMouseDown, true);
            pDoc.addEventListener('touchstart', onMouseDown, {{ passive: false, capture: true }});
        }}

        function processImageFile(file, targetTextarea) {{
            if (!file || !file.type.startsWith('image/')) return;
            
            if (!targetTextarea) {{
                showUploadToast(false, 'Passa in Modalità Modifica per inserire immagini!');
                return;
            }}
            
            showUploadIndicator(true);
            
            compressImageToWebP(file, 1920, 0.82).then(function(compressedBlob) {{
                var filename = uuid4().replace(/-/g, '') + '.webp';
                return uploadToSupabase(compressedBlob, filename);
            }}).then(function(publicUrl) {{
                showUploadIndicator(false);
                var markdownTag = '![Immagine](' + publicUrl + ')';
                insertTextAtCursor(targetTextarea, markdownTag);
                showUploadToast(true, 'Immagine inserita!');
            }}).catch(function(err) {{
                showUploadIndicator(false);
                console.error('Errore upload immagine:', err);
                showUploadToast(false, 'Errore caricamento: ' + err.message);
            }});
        }}

        if (pDoc.__pasteHandler) {{
            pDoc.removeEventListener('paste', pDoc.__pasteHandler, true);
        }}
        pDoc.__pasteHandler = function(e) {{
            var items = (e.clipboardData || e.originalEvent.clipboardData || {{}}).items;
            if (!items) return;
            
            for (var i = 0; i < items.length; i++) {{
                if (items[i].type.indexOf('image') !== -1) {{
                    e.preventDefault();
                    e.stopPropagation();
                    var file = items[i].getAsFile();
                    var textarea = findTargetTextarea(e.target);
                    processImageFile(file, textarea);
                    return;
                }}
            }}
        }};
        pDoc.addEventListener('paste', pDoc.__pasteHandler, true);

        function preventDefaults(e) {{
            e.preventDefault();
            e.stopPropagation();
        }}

        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {{
            pDoc.addEventListener(eventName, preventDefaults, false);
            pWin.addEventListener(eventName, preventDefaults, false);
            pDoc.body.addEventListener(eventName, preventDefaults, false);
        }});

        if (pDoc.__dropHandler) {{
            pDoc.removeEventListener('drop', pDoc.__dropHandler, true);
        }}
        pDoc.__dropHandler = function(e) {{
            preventDefaults(e);
            
            if (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0) return;
            
            var file = e.dataTransfer.files[0];
            if (!file.type.startsWith('image/')) return;

            var textarea = null;
            if (e.target && e.target.tagName === 'TEXTAREA') {{
                textarea = e.target;
                if (pDoc.caretPositionFromPoint) {{
                    var pos = pDoc.caretPositionFromPoint(e.clientX, e.clientY);
                    if (pos && pos.offsetNode === textarea) {{
                        textarea.selectionStart = pos.offset;
                        textarea.selectionEnd = pos.offset;
                    }}
                }} else if (pDoc.caretRangeFromPoint) {{
                    var range = pDoc.caretRangeFromPoint(e.clientX, e.clientY);
                    if (range) {{
                        textarea.selectionStart = range.startOffset;
                        textarea.selectionEnd = range.startOffset;
                    }}
                }}
            }} else {{
                textarea = findTargetTextarea(e.target);
            }}
            
            processImageFile(file, textarea);
        }};
        pDoc.addEventListener('drop', pDoc.__dropHandler, true);

        initImageResizeDrag();
        console.log('[ImageDragResize] Inizializzato con successo.');
    }})();
    </script>
    """

def inject_scroll_sync_mode_js():
    """
    Sincronizza le modifiche di testo dell'editor HTML verso Streamlit e mantiene con precisione la posizione di lettura tra Anteprima e Modifica.
    """
    sync_js = r"""
    <script>
    (function() {
        const pDoc = window.parent.document || document;
        const pWin = window.parent || window;

        function getCanvasPreviewContainer() {
            const anchor = pDoc.getElementById('canvas-scroll-anchor');
            if (anchor) {
                let p = anchor.parentElement;
                while (p && p !== pDoc.body) {
                    const style = pWin.getComputedStyle(p);
                    const oy = style.overflowY || style.overflow;
                    if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') && p.clientHeight >= 200) {
                        return p;
                    }
                    p = p.parentElement;
                }
            }
            const col3 = pDoc.querySelector('div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3)');
            if (col3) {
                const divs = col3.querySelectorAll('div');
                for (const d of divs) {
                    const style = pWin.getComputedStyle(d);
                    const oy = style.overflowY || style.overflow;
                    if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') && d.clientHeight >= 200 && d.clientHeight <= 1000) {
                        if (d.querySelector('h1, h2, h3, p, li')) {
                            return d;
                        }
                    }
                }
            }
            return null;
        }

        function getVisibleSnippet(container) {
            if (!container) return '';
            const cRect = container.getBoundingClientRect();
            const elements = container.querySelectorAll('h1, h2, h3, h4, h5, p, li, strong, code');
            for (const el of elements) {
                const r = el.getBoundingClientRect();
                // Primo elemento vicino alla cima visibile del contenitore
                if (r.top >= cRect.top - 15 && r.top <= cRect.top + 220) {
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt.length >= 6) return txt.substring(0, 35);
                }
            }
            return '';
        }

        // Listener di scroll per Anteprima: cattura SOLO dal pannello Canvas (colonna 3), MAI dalla chat!
        pDoc.addEventListener('scroll', function(e) {
            const el = e.target;
            if (!el || el === pDoc || el === pDoc.body || el === pDoc.documentElement) return;
            if (el.tagName === 'TEXTAREA') return;
            
            const isInsideCanvas = el.closest('div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3)');
            if (!isInsideCanvas) return;

            if (el.clientHeight >= 200 && el.clientHeight <= 1000) {
                if (el.querySelector('h1, h2, h3, p, li')) {
                    const snip = getVisibleSnippet(el);
                    if (snip) pWin.__readingSnippet = snip;
                    const max = el.scrollHeight - el.clientHeight;
                    if (max > 0) pWin.__readingRatio = el.scrollTop / max;
                }
            }
        }, true);

        function saveEditorPos(ed) {
            if (!ed) return;
            const val = ed.value || '';
            if (!val.trim()) return;

            const lines = val.split('\n');
            const totalLines = Math.max(1, lines.length);
            const approxLineH = ed.scrollHeight > 0 ? (ed.scrollHeight / totalLines) : 22.4;

            const maxScroll = Math.max(1, ed.scrollHeight - ed.clientHeight);
            const ratio = ed.scrollTop / maxScroll;
            pWin.__readingRatio = ratio;

            // 1. Individua la riga attiva su cui è concentrata la lettura o il cursore
            let activeLine = -1;
            const selStart = ed.selectionStart;
            if (typeof selStart === 'number' && selStart >= 0) {
                const cursorLine = val.substring(0, selStart).split('\n').length - 1;
                const topVisibleLine = Math.floor(ed.scrollTop / approxLineH);
                const bottomVisibleLine = Math.ceil((ed.scrollTop + ed.clientHeight) / approxLineH);
                if (cursorLine >= topVisibleLine && cursorLine <= bottomVisibleLine) {
                    activeLine = cursorLine;
                }
            }

            // Se il cursore non è visibile, prendi la riga a ~20% dell'altezza del viewport (dove si posa naturalmente lo sguardo)
            if (activeLine === -1) {
                const readingScrollPx = ed.scrollTop + Math.min(100, ed.clientHeight * 0.20);
                activeLine = Math.max(0, Math.min(lines.length - 1, Math.floor(readingScrollPx / approxLineH)));
            }

            // 2. Cerca il landmark più significativo (priorità a Titoli Markdown #, ##, ### nel raggio di 4 righe)
            let landmark = null;
            for (const off of [0, 1, -1, 2, -2, 3, -3, 4, -4]) {
                const idx = activeLine + off;
                if (idx >= 0 && idx < lines.length) {
                    const l = lines[idx].trim();
                    if (l.startsWith('#')) {
                        const clean = l.replace(/^#+\s*/, '').replace(/[\*\_`~\[\]]/g, '').trim();
                        if (clean.length >= 3) {
                            landmark = { type: 'heading', text: clean, line: idx };
                            break;
                        }
                    }
                }
            }

            // Se non ci sono titoli vicini, prendi la prima riga non vuota significativa
            if (!landmark) {
                for (const off of [0, 1, 2, -1, 3, -2, 4]) {
                    const idx = activeLine + off;
                    if (idx >= 0 && idx < lines.length) {
                        const clean = lines[idx]
                            .replace(/^[\*\-\+]\s*/, '')
                            .replace(/^\d+\.\s*/, '')
                            .replace(/^>\s*/, '')
                            .replace(/[\*\_`~\[\]]/g, '')
                            .replace(/<[^>]*>/g, '')
                            .trim();
                        if (clean.length >= 8) {
                            landmark = { type: 'text', text: clean.substring(0, 50), line: idx };
                            break;
                        }
                    }
                }
            }

            pWin.__readingLandmark = landmark;
            pWin.__readingSnippet = landmark ? landmark.text : '';
        }

        function restoreEditor(ed) {
            const snippet = pWin.__readingSnippet;
            const ratio = pWin.__readingRatio || 0;
            const val = ed.value || '';
            let charPos = -1;

            if (snippet && snippet.length >= 4) {
                const searchSnip = snippet.substring(0, 20);
                charPos = val.indexOf(searchSnip);
                if (charPos === -1) charPos = val.indexOf(snippet.substring(0, 10));
                if (charPos === -1) {
                    const words = snippet.split(' ');
                    for (const w of words) {
                        if (w.length >= 4) {
                            const idx = val.indexOf(w);
                            if (idx !== -1) { charPos = idx; break; }
                        }
                    }
                }
            }
            if (charPos === -1 && ratio > 0) {
                charPos = Math.floor(ratio * val.length);
            }

            if (charPos >= 0) {
                const applyEd = () => {
                    const lines = val.substring(0, charPos).split('\n').length;
                    const totalLines = Math.max(1, val.split('\n').length);
                    const approxLineH = ed.scrollHeight > 0 ? (ed.scrollHeight / totalLines) : 22;
                    ed.scrollTop = Math.max(0, (lines - 2) * approxLineH);
                };
                applyEd();
                setTimeout(applyEd, 30);
                setTimeout(applyEd, 80);
                setTimeout(applyEd, 160);
                setTimeout(applyEd, 300);
                setTimeout(applyEd, 500);
            }
        }

        function restorePreview() {
            const c = getCanvasPreviewContainer();
            if (!c) return;

            const landmark = pWin.__readingLandmark;
            const snippet = (landmark ? landmark.text : pWin.__readingSnippet) || '';
            const ratio = pWin.__readingRatio || 0;

            const applyPreview = () => {
                let targetScroll = -1;
                const cRect = c.getBoundingClientRect();

                // 1. Se il landmark è un TITOLO Markdown: trova l'intestazione corrispondente
                if (landmark && landmark.type === 'heading' && snippet.length >= 3) {
                    const headings = c.querySelectorAll('h1, h2, h3, h4, h5, h6');
                    const cleanTarget = snippet.toLowerCase().trim();
                    let bestHeading = null;
                    let bestHeadingScore = 0;

                    headings.forEach((h, hIdx) => {
                        const hText = (h.innerText || h.textContent || '').toLowerCase().trim();
                        let score = 0;
                        if (hText === cleanTarget) {
                            score = 10000;
                        } else if (hText.includes(cleanTarget) || cleanTarget.includes(hText)) {
                            score = 5000;
                        } else {
                            const sub = cleanTarget.substring(0, 15);
                            if (sub.length >= 5 && hText.includes(sub)) score = 2000;
                        }
                        if (score > 0) {
                            const hRatio = headings.length > 1 ? (hIdx / (headings.length - 1)) : 0;
                            const prox = Math.max(0, 500 - Math.abs(hRatio - ratio) * 1000);
                            score += prox;
                        }
                        if (score > bestHeadingScore) {
                            bestHeadingScore = score;
                            bestHeading = h;
                        }
                    });

                    if (bestHeading) {
                        const hRect = bestHeading.getBoundingClientRect();
                        targetScroll = (hRect.top - cRect.top) + c.scrollTop - 25;
                    }
                }

                // 2. Se non è un titolo o se non c'è match, effettua il matching su paragrafi / elementi testuali
                if (targetScroll < 0 && snippet.length >= 6) {
                    const snipClean = snippet.toLowerCase().replace(/[^\w\s\u00C0-\u017F]/g, ' ');
                    const words = snipClean.split(/\s+/).filter(w => w.length >= 4);

                    const els = c.querySelectorAll('h1, h2, h3, h4, h5, p, li, blockquote, tr');
                    let bestEl = null;
                    let bestScore = 0;

                    els.forEach((el, elIdx) => {
                        const elText = (el.innerText || el.textContent || '').trim();
                        if (!elText) return;
                        const elTextLower = elText.toLowerCase();

                        let score = 0;
                        if (elTextLower.includes(snippet.toLowerCase().substring(0, 25))) {
                            score = 3000;
                        } else if (elTextLower.includes(snippet.toLowerCase().substring(0, 15))) {
                            score = 1500;
                        }

                        let wordHits = 0;
                        for (const w of words) {
                            if (elTextLower.includes(w)) wordHits++;
                        }
                        if (words.length > 0 && wordHits >= Math.min(2, words.length)) {
                            score += wordHits * 120;
                        }

                        if (score > 0) {
                            // Proximity bonus fondamentale: favorisce la zona corretta del documento ed evita salti in alto
                            const elRatio = els.length > 1 ? (elIdx / (els.length - 1)) : 0;
                            const prox = Math.max(0, 500 - Math.abs(elRatio - ratio) * 1000);
                            score += prox;
                        }

                        if (score > bestScore) {
                            bestScore = score;
                            bestEl = el;
                        }
                    });

                    if (bestEl && bestScore >= 150) {
                        const elRect = bestEl.getBoundingClientRect();
                        targetScroll = (elRect.top - cRect.top) + c.scrollTop - 25;
                    }
                }

                // 3. Fallback: percentuale di scroll
                if (targetScroll >= 0) {
                    c.scrollTop = Math.max(0, targetScroll);
                } else if (ratio > 0) {
                    const max = c.scrollHeight - c.clientHeight;
                    if (max > 0) c.scrollTop = ratio * max;
                }
            };

            applyPreview();
            setTimeout(applyPreview, 30);
            setTimeout(applyPreview, 80);
            setTimeout(applyPreview, 160);
            setTimeout(applyPreview, 300);
            setTimeout(applyPreview, 500);
        }

        function checkModeAndSync() {
            const editors = pDoc.querySelectorAll('.raw-markdown-editor');
            
            if (editors.length > 0) {
                // MODIFICA MODE
                const ed = editors[0];
                const bridge = pDoc.querySelector('textarea[aria-label="__notes_sync_bridge__"]');
                if (pWin.__currentActiveMode !== 'modifica') {
                    pWin.__currentActiveMode = 'modifica';
                    pWin.__lastSyncedBridgeVal = ed.value || '';
                    if (bridge && bridge.value !== ed.value) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, 'value').set;
                        if (nativeInputValueSetter) {
                            nativeInputValueSetter.call(bridge, ed.value);
                        } else {
                            bridge.value = ed.value;
                        }
                    }
                    restoreEditor(ed);
                }

                if (!ed.__boundSync) {
                    ed.__boundSync = true;
                    const syncToBridge = function(forceCommit) {
                        const bridge = pDoc.querySelector('textarea[aria-label="__notes_sync_bridge__"]');
                        if (bridge) {
                            const txt = ed.value || '';
                            pWin.__lastSyncedBridgeVal = txt;
                            const lastValue = bridge.value;
                            
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, 'value').set;
                            if (nativeInputValueSetter) {
                                nativeInputValueSetter.call(bridge, txt);
                            } else {
                                bridge.value = txt;
                            }
                            
                            const tracker = bridge._valueTracker;
                            if (tracker) {
                                tracker.setValue(lastValue);
                            }
                            
                            bridge.dispatchEvent(new Event('input', { bubbles: true }));
                            bridge.dispatchEvent(new Event('change', { bubbles: true }));

                            if (forceCommit) {
                                bridge.dispatchEvent(new KeyboardEvent('keydown', {
                                    key: 'Enter',
                                    code: 'Enter',
                                    keyCode: 13,
                                    which: 13,
                                    ctrlKey: true,
                                    bubbles: true,
                                    cancelable: true
                                }));
                                try {
                                    bridge.focus();
                                    bridge.blur();
                                } catch(err) {}
                            }
                        }
                    };

                    ed.addEventListener('input', () => syncToBridge(false));
                    ed.addEventListener('change', () => syncToBridge(false));
                    ed.addEventListener('blur', () => syncToBridge(true));
                    
                    ed.addEventListener('keyup', () => saveEditorPos(ed));
                    ed.addEventListener('mouseup', () => saveEditorPos(ed));
                    ed.addEventListener('scroll', () => saveEditorPos(ed));

                    ed.addEventListener('keydown', function(e) {
                        if (e.key === 'Tab') {
                            e.preventDefault();
                            const start = ed.selectionStart;
                            const end = ed.selectionEnd;
                            ed.value = ed.value.substring(0, start) + "    " + ed.value.substring(end);
                            ed.selectionStart = ed.selectionEnd = start + 4;
                            syncToBridge(false);
                        }
                    });

                    if (!pDoc.__canvasHeaderBtnInterceptBound) {
                        pDoc.__canvasHeaderBtnInterceptBound = true;
                        pDoc.addEventListener('mousedown', function(e) {
                            const btn = e.target.closest('button');
                            if (btn && (btn.id?.includes('btn_toggle_canvas_edit_icon') || 
                                        btn.getAttribute('data-testid')?.includes('btn_toggle_canvas_edit_icon') ||
                                        btn.getAttribute('key')?.includes('btn_toggle_canvas_edit_icon') ||
                                        btn.innerText?.includes('👁') || 
                                        btn.innerText?.includes('✏') || 
                                        btn.innerText?.includes('📤') ||
                                        btn.innerText?.includes('🔙'))) {
                                const currentEd = pDoc.querySelector('.raw-markdown-editor');
                                if (currentEd) {
                                    saveEditorPos(currentEd);
                                    syncToBridge(true);
                                }
                            }
                        }, true);
                    }
                }
            } else {
                // ANTEPRIMA MODE (No editor found)
                if (pWin.__currentActiveMode !== 'anteprima') {
                    pWin.__currentActiveMode = 'anteprima';
                    pWin.__lastSyncedBridgeVal = undefined;
                    restorePreview();
                }
            }
        }

        checkModeAndSync();
        const intId = setInterval(checkModeAndSync, 200);
        window.addEventListener('unload', function() {
            clearInterval(intId);
        });
    })();
    </script>
    """
    st.iframe(sync_js, height=1)

def inject_image_paste_drop_js():
    render_notes_sync_bridge()
    inject_scroll_sync_mode_js()
    js_html = generate_image_paste_drop_js()
    if js_html:
        st.iframe(js_html, height=1)

ITALIAN_STOP_WORDS = {
    'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'uno', 'una', 'di', 'a', 'da', 'in', 'con', 'su',
    'per', 'tra', 'fra', 'del', 'dello', 'della', 'dei', 'degli', 'delle', 'al', 'allo', 'alla',
    'ai', 'agli', 'alle', 'dal', 'dallo', 'dalla', 'dai', 'dagli', 'dalle', 'nel', 'nello', 'nella',
    'nei', 'negli', 'nelle', 'sul', 'sullo', 'sulla', 'sui', 'sugli', 'sulle', 'ed', 'ad', 'che',
    'chi', 'cui', 'quale', 'quali', 'questo', 'questa', 'questi', 'queste', 'quello', 'quella',
    'quelli', 'quelle', 'come', 'dove', 'quando', 'perché', 'perche', 'anche', 'non', 'più', 'piu',
    'molto', 'poco', 'tutto', 'tutti', 'tutta', 'tutte', 'cosa', 'cose', 'essere', 'avere', 'fare',
    'dire', 'stato', 'stata', 'stati', 'state', 'sono', 'sei', 'era', 'erano', 'sarà', 'sara',
    'saranno', 'può', 'puo', 'possono', 'quindi', 'infatti', 'inoltre', 'invece', 'però', 'pero',
    'tuttavia', 'cioè', 'cioe', 'ossia', 'abbiamo', 'possiamo', 'notare', 'vedere', 'esempio',
    'caso', 'modo', 'punto', 'parte', 'particolare', 'generale'
}

def prepare_canvas_render_with_marker(raw_markdown, target_snip, edit_mode):
    cleaned = notion_helper.clean_markdown_for_streamlit(raw_markdown or "", default_width="50%").strip()
    if not target_snip or edit_mode:
        return cleaned
    
    clean_target = notion_helper.clean_markdown_for_streamlit(target_snip, default_width="50%").strip()
    pos = -1
    if clean_target and clean_target in cleaned:
        pos = cleaned.find(clean_target)
    elif target_snip in cleaned:
        pos = cleaned.find(target_snip)
    else:
        for line in clean_target.splitlines():
            line_s = line.strip()
            if len(line_s) >= 6 and line_s in cleaned:
                pos = cleaned.find(line_s)
                break
    if pos != -1:
        return cleaned[:pos] + '<span id="canvas-ai-modified-target"></span>\n' + cleaned[pos:]
    return cleaned

def inject_canvas_snippet_scroll_js():
    """
    Inietta uno script JavaScript per effettuare lo scroll automatico e l'evidenziazione
    della sezione del Canvas appena modificata dall'Assistente AI.
    """
    snippet = st.session_state.pop("canvas_scroll_target_snippet", None)
    if not snippet or not str(snippet).strip():
        return
        
    clean_snippet = re.sub(r'[$#*`_\[\]()>\-]', ' ', str(snippet))
    clean_snippet = re.sub(r'\s+', ' ', clean_snippet).strip()
    words = re.findall(r'[a-zA-Z0-9àèéìòùÀÈÉÌÒÙ]{4,}', clean_snippet)
    distinctive = [w for w in words if w.lower() not in ITALIAN_STOP_WORDS]
    seen = set()
    unique_kw = []
    for w in distinctive:
        if w.lower() not in seen:
            seen.add(w.lower())
            unique_kw.append(w)
            
    keywords_json = json.dumps(unique_kw[:8])
    
    raw_js = """
    <script>
    (function() {
        const pDoc = window.parent.document;
        const pWin = window.parent;
        const keywords = __KEYWORDS_PLACEHOLDER__;

        if (keywords && keywords.length > 0) {
            pWin.__readingSnippet = keywords.join(' ');
        }

        let targetFound = false;
        let memoTargetScroll = -1;

        function findCanvasScrollContainer() {
            // 1. Cerca prima tramite l'ancora specifica posta all'interno del Canvas
            const anchor = pDoc.getElementById('canvas-scroll-anchor');
            if (anchor) {
                let curr = anchor.parentElement;
                while (curr && curr !== pDoc.body) {
                    const style = pWin.getComputedStyle(curr);
                    const overflowY = style.overflowY || style.overflow;
                    if (curr.clientHeight >= 200 && (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')) {
                        return curr;
                    }
                    curr = curr.parentElement;
                }
            }

            // 2. Fallback: cerca esplicitamente all'interno della colonna di destra del Canvas
            const studioCols = pDoc.querySelectorAll('[data-testid="stHorizontalBlock"] > div');
            if (studioCols.length >= 3) {
                const rightCol = studioCols[studioCols.length - 1];
                const divs = rightCol.querySelectorAll('div');
                for (const d of divs) {
                    const style = pWin.getComputedStyle(d);
                    const overflowY = style.overflowY || style.overflow;
                    if (d.clientHeight >= 200 && (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')) {
                        return d;
                    }
                }
            }
            return null;
        }

        function tryScroll() {
            const c = findCanvasScrollContainer();
            if (!c) return false;

            const ed = c.querySelector('.raw-markdown-editor, textarea');
            if (ed && ed.value) {
                const val = ed.value.toLowerCase();
                for (const kw of keywords) {
                    const idx = val.indexOf(kw.toLowerCase());
                    if (idx !== -1) {
                        const linesBefore = val.substring(0, idx).split('\\n').length;
                        const totalLines = Math.max(1, val.split('\\n').length);
                        const approxLineH = ed.scrollHeight > 0 ? (ed.scrollHeight / totalLines) : 22;
                        ed.scrollTop = Math.max(0, (linesBefore - 3) * approxLineH);
                        const maxEd = ed.scrollHeight - ed.clientHeight;
                        if (maxEd > 0) pWin.__readingRatio = ed.scrollTop / maxEd;
                        pWin.__readingSnippet = keywords.join(' ');
                        return true;
                    }
                }
            }

            let targetEl = null;

            // 1. Priorità assoluta: marker esatto inserito nel DOM di anteprima
            const marker = c.querySelector('#canvas-ai-modified-target');
            if (marker) {
                let candidate = marker.nextElementSibling;
                if (!candidate) {
                    candidate = marker.parentElement;
                }
                while (candidate && candidate !== c && (candidate.tagName === 'SPAN' || candidate.tagName === 'EM' || candidate.tagName === 'STRONG')) {
                    candidate = candidate.parentElement;
                }
                if (candidate && candidate !== c) {
                    targetEl = candidate;
                }
            }

            // 2. Fallback: punteggio massimo tra tutti gli elementi di testo
            if (!targetEl && keywords && keywords.length > 0) {
                const elements = c.querySelectorAll('h1, h2, h3, h4, h5, h6, p, li, blockquote, pre, div.katex-display');
                let bestEl = null;
                let maxScore = 0;

                for (const el of elements) {
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    if (!text || text.length < 3) continue;

                    let score = 0;
                    for (const kw of keywords) {
                        if (text.includes(kw.toLowerCase())) {
                            score++;
                        }
                    }

                    if (score > maxScore) {
                        maxScore = score;
                        bestEl = el;
                    }
                }

                const minThreshold = Math.min(2, keywords.length);
                if (bestEl && maxScore >= minThreshold) {
                    targetEl = bestEl;
                }
            }

            if (targetEl) {
                const cRect = c.getBoundingClientRect();
                const elRect = targetEl.getBoundingClientRect();
                const targetScroll = Math.max(0, elRect.top - cRect.top + c.scrollTop - 40);
                memoTargetScroll = targetScroll;
                const max = c.scrollHeight - c.clientHeight;
                if (max > 0) pWin.__readingRatio = targetScroll / max;
                if (keywords && keywords.length > 0) pWin.__readingSnippet = keywords.join(' ');
                
                c.scrollTo({
                    top: targetScroll,
                    behavior: targetFound ? 'auto' : 'smooth'
                });

                if (!targetFound) {
                    targetFound = true;
                    try {
                        const origBg = targetEl.style.backgroundColor || 'transparent';
                        const origBorder = targetEl.style.borderLeft || '';
                        
                        targetEl.style.transition = 'all 0.3s ease';
                        targetEl.style.backgroundColor = 'rgba(245, 158, 11, 0.22)';
                        targetEl.style.borderLeft = '3.5px solid #f59e0b';
                        targetEl.style.borderRadius = '4px';
                        targetEl.style.paddingLeft = '8px';

                        pWin.setTimeout(() => {
                            targetEl.style.transition = 'all 0.8s ease';
                            targetEl.style.backgroundColor = origBg;
                            targetEl.style.borderLeft = origBorder;
                            targetEl.style.paddingLeft = '';
                        }, 2500);
                    } catch(e) {}
                }
                return true;
            } else if (targetFound && memoTargetScroll >= 0) {
                c.scrollTop = memoTargetScroll;
                return true;
            }
            return false;
        }

        pWin.setTimeout(tryScroll, 40);
        pWin.setTimeout(tryScroll, 120);
        pWin.setTimeout(tryScroll, 250);
        pWin.setTimeout(tryScroll, 500);
        pWin.setTimeout(tryScroll, 850);
        pWin.setTimeout(tryScroll, 1300);
    })();
    </script>
    """
    js = raw_js.replace("__KEYWORDS_PLACEHOLDER__", keywords_json)
    st.iframe(js, height=1)

def inject_scroll_to_results():
    """
    Inietta uno script JavaScript per effettuare lo scroll automatico e fluido verso la sezione
    degli appunti / trascrizione (id: 'selezione-appunti-trascrizione' o tab bar).
    """
    scroll_js = """
    <script>
    (function() {
        const pDoc = window.parent.document || document;
        const pWin = window.parent || window;
        
        if (pWin.__canvasScrollLock) {
            clearInterval(pWin.__canvasScrollLock);
            pWin.__canvasScrollLock = null;
        }

        let attempts = 0;
        const maxAttempts = 30;
        
        function doScroll() {
            attempts++;
            const target = pDoc.getElementById('selezione-appunti-trascrizione') || 
                           pDoc.querySelector('[data-testid="stTabs"]') || 
                           pDoc.querySelector('.stTabs');
            
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            } else if (attempts < maxAttempts) {
                setTimeout(doScroll, 50);
            }
        }
        
        setTimeout(doScroll, 80);
    })();
    </script>
    """
    st.iframe(scroll_js, height=1)


# --- FRAGMENT NOTIFICA TOAST FLUTTUANTE CON BARRA DI CARICAMENTO ---
@st.fragment(run_every="1s")
def render_active_background_operations_banner():
    is_notion_saving = is_notion_saving_active()
    is_latex_regen = is_latex_regen_active()

    if is_notion_saving or is_latex_regen:
        cards_html = ""
        if is_notion_saving:
            cards_html += "<div class='floating-notification-card' style='border: 1px solid #3b82f6;'><div style='display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;'><span style='color: #60a5fa; font-weight: 600; font-size: 13px;'>📤 Esportazione su Notion in corso...</span><span style='color: #94a3b8; font-size: 11px; margin-left: 12px;'>Background</span></div><div style='width: 100%; background: #1e293b; border-radius: 4px; height: 6px; overflow: hidden;'><div class='custom-progress-bar' style='width: 100%; height: 100%; background-color: #3b82f6;'></div></div></div>"
            
        if is_latex_regen:
            cards_html += "<div class='floating-notification-card' style='border: 1px solid #10b981;'><div style='display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;'><span style='color: #34d399; font-weight: 600; font-size: 13px;'>📄 Generazione LaTeX in corso...</span><span style='color: #94a3b8; font-size: 11px; margin-left: 12px;'>Gemini AI</span></div><div style='width: 100%; background: #1e293b; border-radius: 4px; height: 6px; overflow: hidden;'><div class='custom-progress-bar' style='width: 100%; height: 100%; background-color: #10b981;'></div></div></div>"

        floating_html = f"<style>@keyframes slide-in-notification {{0% {{ transform: translateY(20px); opacity: 0; }} 100% {{ transform: translateY(0); opacity: 1; }} }} @keyframes progress-bar-stripes {{ 0% {{ background-position: 1rem 0; }} 100% {{ background-position: 0 0; }} }} .floating-notification-container {{ position: fixed !important; bottom: 25px !important; right: 25px !important; z-index: 999999999 !important; display: flex !important; flex-direction: column !important; gap: 10px !important; pointer-events: none !important; }} .floating-notification-card {{ pointer-events: auto !important; background: rgba(15, 23, 42, 0.96) !important; backdrop-filter: blur(10px) !important; border-radius: 10px !important; padding: 12px 16px !important; min-width: 310px !important; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7) !important; animation: slide-in-notification 0.3s ease-out !important; }} .custom-progress-bar {{ background-image: linear-gradient(45deg, rgba(255, 255, 255, .2) 25%, transparent 25%, transparent 50%, rgba(255, 255, 255, .2) 50%, rgba(255, 255, 255, .2) 75%, transparent 75%, transparent) !important; background-size: 1rem 1rem !important; animation: progress-bar-stripes 1s linear infinite !important; }}</style><div class='floating-notification-container'>{cards_html}</div>"
        st.markdown(floating_html, unsafe_allow_html=True)

# --- FRAGMENT DI VERIFICA AUTOMATICA THREAD IN BACKGROUND ---
@st.fragment(run_every="2s")
def check_background_threads():
    is_notion_saving_active()
    is_latex_regen_active()

check_background_threads()
is_saving_active = is_notion_saving_active()
is_latex_active = is_latex_regen_active()

# ==============================================================================
# PAGINA DEDICATA: CANVAS STUDIO FULL-SCREEN (SE ATTIVA)
# ==============================================================================
if st.session_state.get("show_canvas_chat", False) and st.session_state.get("appunti_generati") is not None:
    # 100% GUARANTEED SCROLLING CHATGPT CANVAS: Blocco Finestra Globale + 2 Slider Verticali Interni
    canvas_js = """
        <script>
        (function() {
            const pWin = window.parent || window;
            const pDoc = window.parent.document || document;
            try {
                if (pWin.history && pWin.history.scrollRestoration) {
                    pWin.history.scrollRestoration = 'manual';
                }
                const forceZero = () => {
                    pWin.scrollTo(0, 0);
                    if (pDoc.documentElement) pDoc.documentElement.scrollTop = 0;
                    if (pDoc.body) pDoc.body.scrollTop = 0;
                    const views = pDoc.querySelectorAll('[data-testid="stAppViewContainer"], .main, [data-testid="stMain"], .block-container');
                    views.forEach(v => {
                        if (v.scrollTop !== 0) v.scrollTop = 0;
                    });
                };
                forceZero();
                pWin.__canvasScrollLock = setInterval(forceZero, 50);
                
                // Cleanup se l'utente esce dal canvas (es. ricaricando)
                pWin.addEventListener('unload', () => clearInterval(pWin.__canvasScrollLock));
            } catch(e) {}
        })();
        </script>
    """
    
    st.markdown("""
        <style>
            /* Sfondo Grigio Dark ChatGPT #212121 - Blocco Rigido dello Scroll Globale della Finestra */
            html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main {
                background-color: #212121 !important;
                color: #ffffff !important;
                overflow: hidden !important;
                height: 100vh !important;
                max-height: 100vh !important;
                margin: 0 !important;
                padding: 0 !important;
            }
            
            /* ELIMINA OGNI TIPO DI HEADER E DELLO SPAZIO IN ALTO NATIVO DI STREAMLIT */
            header, [data-testid="stHeader"], .stAppHeader, div[data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="collapsedControl"] {
                display: none !important;
                height: 0px !important;
                min-height: 0px !important;
                max-height: 0px !important;
                padding: 0 !important;
                margin: 0 !important;
            }
            /* NASCONDE COMPLETAMENTE GLI IFRAME DI INIEZIONE JS (NO LINEE, NO FRECCE, 0PX) E I PULSANTI FULLSCREEN */
            iframe[data-testid="stIframe"], div[data-testid="stIframe"], div[data-testid="stHtml"], button[title="View fullscreen"], [data-testid="stElementToolbar"] {
                display: none !important;
                height: 0px !important;
                min-height: 0px !important;
                max-height: 0px !important;
                border: none !important;
                margin: 0 !important;
                padding: 0 !important;
            }
            div[data-testid="stElementContainer"]:has(iframe[height="1"]), div[data-testid="stElementContainer"]:has(iframe[height="0"]) {
                display: none !important;
                height: 0px !important;
                margin: 0 !important;
                padding: 0 !important;
            }

            /* POSIZIONA LA CHAT NATIVA ESATTAMENTE SOTTO LA CHAT CUSTOM COSI DA ESSERE SOVRAPPOSTA E INVISIBILE */
            div[data-testid="stChatInput"], div[data-testid="stChatInputContainer"], [data-testid="stBottom"] {
                position: fixed !important;
                bottom: 15px !important;
                left: 15px !important;
                opacity: 0 !important;
                z-index: 0 !important;
                pointer-events: none !important;
                transform: scale(0.01) !important;
            }

            .main, .stMain, [data-testid="stMain"], .block-container, [data-testid="stBlockContainer"],
            div[data-testid="stAppViewContainer"] > section.main {
                padding-top: 0px !important;
                margin-top: 0px !important;
            }
            .main .block-container, [data-testid="stBlockContainer"] {
                padding-top: 8px !important;
                padding-bottom: 8px !important;
                padding-left: 1rem !important;
                padding-right: 1rem !important;
                max-width: 100% !important;
                height: 100vh !important;
                max-height: 100vh !important;
                overflow: hidden !important;
                box-sizing: border-box !important;
            }
            div[data-testid="stAppViewContainer"] {
                padding-top: 0px !important;
                margin-top: 0px !important;
            }
            div[data-testid="stElementContainer"]:first-child,
            div[data-testid="stVerticalBlock"]:first-child,
            div[data-testid="stVerticalBlockGroup"]:first-child {
                margin-top: 0px !important;
                padding-top: 0px !important;
            }

            /* CONTENITORE PRINCIPALE A 3 COLONNE ANCORATO RIGIDAMENTE IN CIMA */
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"],
            .main .block-container > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"],
            .main .block-container div[data-testid="stHorizontalBlock"]:first-of-type {
                flex-wrap: nowrap !important;
                height: calc(100vh - 16px) !important;
                max-height: calc(100vh - 16px) !important;
                overflow: hidden !important;
                margin-top: 0 !important;
                padding-top: 0 !important;
            }

            /* QUALSIASI ST-HORIZONTAL-BLOCK ANNIDATO (Come l'Header Toolbar del Canvas) DEVE AVERE ALTEZZA COMPATTA AUTOMATICA */
            div[data-testid="stColumn"] div[data-testid="stHorizontalBlock"] {
                height: auto !important;
                max-height: 45px !important;
                min-height: 0 !important;
                flex-wrap: nowrap !important;
            }

            div[data-testid="stColumn"] {
                min-width: 0 !important;
            }

            /* FIX FONDAMENTALE: Allinea il contenuto in alto evitando justify-content: space-between */
            div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"],
            div[data-testid="stColumn"] > div[data-testid="stVerticalBlockGroup"] {
                justify-content: flex-start !important;
                align-items: stretch !important;
                gap: 0.1rem !important;
            }

            /* RESET PULITO PER IL CANVAS - Spaziatura naturale per il rendering Markdown */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) [data-testid="stElementContainer"] {
                margin-bottom: 0.5rem !important;
            }

            /* IMPOSTAZIONI PER LA COLONNA DESTRA E SINISTRA (SCROLL) */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1) {
                height: calc(100vh - 35px) !important;
                max-height: calc(100vh - 35px) !important;
                overflow-x: hidden !important;
                overflow-y: auto !important;
                padding-bottom: 110px !important; /* Spazio per la barra custom con eventuale citazione */
                padding-right: 0.5rem !important;
                display: flex !important;
                flex-direction: column !important;
                justify-content: flex-start !important;
            }

            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1) [data-testid="stVerticalBlock"] {
                padding-top: 12px !important;
                padding-bottom: 12px !important;
                padding-left: 6px !important;
                padding-right: 6px !important;
            }

            /* 2. PANNELLO CANVAS (Destra) - ALTEZZA REGOLATA CON HEADER FISSO IN ALTO */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) {
                background-color: #1a1a1a !important;
                border-radius: 16px !important;
                padding: 0.8rem 1.2rem !important;
                border: 1px solid #333333 !important;
                box-shadow: 0 4px 25px rgba(0,0,0,0.5) !important;
                height: calc(100vh - 35px) !important;
                max-height: calc(100vh - 35px) !important;
                overflow: hidden !important;
                display: flex !important;
                flex-direction: column !important;
                justify-content: flex-start !important;
            }

            /* 4. HEADER DEL CANVAS - COMPLETAMENTE FISSO E STATICO IN CIMA */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) [data-testid="stHorizontalBlock"]:first-of-type {
                align-items: center !important;
                min-height: 48px !important;
                height: auto !important;
                background-color: transparent !important;
                background: transparent !important;
                border-bottom: 1px solid #333333 !important;
                padding-bottom: 6px !important;
                margin-bottom: 6px !important;
                gap: 0 !important;
            }
            /* Colonne e wrapper interni dell'header: nessun bordo, nessun padding extra */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type > div[data-testid="stColumn"] {
                max-height: 48px !important;
                min-height: 0 !important;
                height: auto !important;
                background-color: transparent !important;
                background: transparent !important;
                box-shadow: none !important;
                border: 0px none transparent !important;
                outline: none !important;
                overflow: visible !important;
                display: flex !important;
                align-items: center !important;
                justify-content: center !important;
            }
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type [data-testid="stVerticalBlockBorderWrapper"],
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type [data-testid="stVerticalBlockBorderWrapper"] > div,
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type [data-testid="stVerticalBlock"],
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type [data-testid="stVerticalBlockGroup"] {
                max-height: 48px !important;
                min-height: 0 !important;
                height: auto !important;
                padding: 0 !important;
                margin: 0 !important;
                background-color: transparent !important;
                background: transparent !important;
                box-shadow: none !important;
                border: 0px none transparent !important;
                outline: none !important;
                overflow: visible !important;
            }
            /* Il primo stColumn dell'header (col_title) allinea a sinistra */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type > div[data-testid="stColumn"]:first-child {
                justify-content: flex-start !important;
            }
            /* Pulsanti dell'header Canvas: nessun bordo */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type button {
                border: none !important;
                box-shadow: none !important;
                background-color: transparent !important;
                padding: 0.25rem 0.5rem !important;
            }
            /* Elimina margin-bottom dai contenitori widget nell'header */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type [data-testid="stElementContainer"] {
                margin: 0 !important;
                padding: 0 !important;
            }
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type div.stButton {
                margin: 0 !important;
                padding: 0 !important;
            }


            /* STILIZZAZIONE DELLE 2 SCROLLBAR VERTICALI */
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)::-webkit-scrollbar,
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3)::-webkit-scrollbar {
                width: 6px !important;
            }
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)::-webkit-scrollbar-thumb,
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3)::-webkit-scrollbar-thumb {
                background-color: #383838 !important;
                border-radius: 4px !important;
            }
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)::-webkit-scrollbar-thumb:hover,
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3)::-webkit-scrollbar-thumb:hover {
                background-color: #38bdf8 !important;
            }

            /* NASCONDI TUTTE LE ICONE DI LINK ANCORA 🔗 NEI TITOLI */
            .anchor-link, a.anchor-link, [data-testid="stHeaderActionElements"], .stMarkdown a {
                display: none !important;
            }

            /* SCRITTE BIANCO PURO (#FFFFFF) 100% LEGGIBILI OVUNQUE */
            p, li, span, label, td, th, strong, em {
                color: #ffffff !important;
                opacity: 1 !important;
                -webkit-text-fill-color: #ffffff !important;
                font-size: 15px !important;
                line-height: 1.65 !important;
            }

            /* TUTTI I TAG INTERNI AI TITOLI EREDITANO LA LORO GRANDEZZA IMPONENTE */
            h1 *, h2 *, h3 *, h4 *,
            .stMarkdown h1 *, .stMarkdown h2 *, .stMarkdown h3 *, .stMarkdown h4 *,
            [data-testid="stMarkdownContainer"] h1 *, [data-testid="stMarkdownContainer"] h2 *, [data-testid="stMarkdownContainer"] h3 *, [data-testid="stMarkdownContainer"] h4 * {
                font-size: inherit !important;
                font-weight: inherit !important;
                line-height: inherit !important;
            }

            /* GERARCHIA TIPOGRAFICA CON TITOLI GRANDI E IMPONENTI FEDELI AL MARKDOWN */
            h1, .stMarkdown h1, [data-testid="stMarkdownContainer"] h1 {
                font-size: 2.6rem !important; /* 42px - H1 Titolo Principale Molto Grande */
                font-weight: 800 !important;
                line-height: 1.2 !important;
                color: #ffffff !important;
                -webkit-text-fill-color: #ffffff !important;
                margin-top: 1.8rem !important;
                margin-bottom: 0.8rem !important;
                border-bottom: 2px solid #383838 !important;
                padding-bottom: 0.5rem !important;
            }

            h2, .stMarkdown h2, [data-testid="stMarkdownContainer"] h2 {
                font-size: 2.0rem !important; /* 32px - H2 Sottotitolo Grande */
                font-weight: 700 !important;
                line-height: 1.25 !important;
                color: #ffffff !important;
                -webkit-text-fill-color: #ffffff !important;
                margin-top: 1.5rem !important;
                margin-bottom: 0.6rem !important;
                border-bottom: 1px solid #2b2b2b !important;
                padding-bottom: 0.4rem !important;
            }

            h3, .stMarkdown h3, [data-testid="stMarkdownContainer"] h3 {
                font-size: 1.5rem !important; /* 24px - H3 Sezione Media */
                font-weight: 600 !important;
                line-height: 1.3 !important;
                color: #ffffff !important;
                -webkit-text-fill-color: #ffffff !important;
                margin-top: 1.2rem !important;
                margin-bottom: 0.5rem !important;
            }

            h4, .stMarkdown h4, [data-testid="stMarkdownContainer"] h4 {
                font-size: 1.25rem !important; /* 20px - H4 Sottosezione */
                font-weight: 600 !important;
                line-height: 1.35 !important;
                color: #e2e8f0 !important;
                -webkit-text-fill-color: #e2e8f0 !important;
                margin-top: 1.0rem !important;
                margin-bottom: 0.4rem !important;
            }

            /* BLOCCHI DI CITAZIONE MARKDOWN */
            blockquote, .stMarkdown blockquote {
                border-left: 4px solid #60a5fa !important;
                padding-left: 1rem !important;
                color: #cbd5e1 !important;
                font-style: italic !important;
                background: rgba(255, 255, 255, 0.04) !important;
                border-radius: 0 8px 8px 0 !important;
                margin: 0.9rem 0 !important;
            }

            /* LISTE MARKDOWN (UL / OL / LI) CON SPAZIATURA E INDENTAZIONE PULITA */
            .stMarkdown ul, .stMarkdown ol {
                margin-left: 1.4rem !important;
                margin-bottom: 0.9rem !important;
                padding-left: 0.4rem !important;
            }
            .stMarkdown li {
                font-size: 15px !important;
                line-height: 1.65 !important;
                color: #f1f5f9 !important;
                margin-bottom: 0.35rem !important;
            }

            /* TABELLE MARKDOWN */
            .stMarkdown table {
                width: 100% !important;
                border-collapse: collapse !important;
                margin: 1.2rem 0 !important;
                border: 1px solid #383838 !important;
                border-radius: 8px !important;
                overflow: hidden !important;
            }
            .stMarkdown th {
                background-color: #262626 !important;
                color: #ffffff !important;
                font-weight: 700 !important;
                padding: 10px 14px !important;
                border: 1px solid #383838 !important;
                text-align: left !important;
            }
            .stMarkdown td {
                padding: 10px 14px !important;
                border: 1px solid #383838 !important;
                color: #e2e8f0 !important;
                background-color: #1e1e1e !important;
            }

            /* FORMULE LATEX MATEMATICHE IN BIANCO NATURALE */
            .katex, .katex *, .katex-display, .katex-display * {
                color: #ffffff !important;
                opacity: 1 !important;
                -webkit-text-fill-color: #ffffff !important;
            }
            pre, code {
                background-color: #2b2b2b !important;
                color: #ffffff !important;
                border: 1px solid #383838 !important;
                border-radius: 6px !important;
            }

            /* Editor di Testo Manuale SOLO nel Canvas */
            div[data-testid="stColumn"]:nth-child(3) textarea,
            .stTextArea textarea {
                background-color: #2b2b2b !important;
                color: #ffffff !important;
                font-weight: 500 !important;
                border: 1.5px solid #383838 !important;
                border-radius: 8px !important;
                -webkit-text-fill-color: #ffffff !important;
            }

            /* Campo Chat Input POSIZIONATO FISSO IN BASSO A SINISTRA */
            [data-testid="stBottom"],
            div[data-testid="stBottom"] {
                background: transparent !important;
                background-color: transparent !important;
                padding: 0 !important;
                margin: 0 !important;
                border: none !important;
            }

            div[data-testid="stChatInput"] {
                border-radius: 24px !important;
                border: 1.5px solid #3e3e3e !important;
                background-color: #2a2a2a !important;
                box-shadow: 0 4px 20px rgba(0,0,0,0.6) !important;
                position: fixed !important;
                bottom: 15px !important;
                z-index: 99999 !important;
                padding: 4px 8px 4px 14px !important;
                margin: 0 !important;
                display: flex !important;
                align-items: center !important;
            }
            div[data-testid="stChatInput"] > div {
                background-color: transparent !important;
                background: transparent !important;
                border: none !important;
                box-shadow: none !important;
                padding: 0 !important;
                margin: 0 !important;
                display: flex !important;
                align-items: center !important;
                width: 100% !important;
            }
            div[data-testid="stChatInput"] textarea,
            div[data-testid="stChatInput"] textarea:focus {
                color: #ffffff !important;
                background-color: transparent !important;
                background: transparent !important;
                border: none !important;
                outline: none !important;
                box-shadow: none !important;
                -webkit-text-fill-color: #ffffff !important;
                padding: 6px 8px !important;
                margin: 0 !important;
                font-size: 14px !important;
                line-height: 1.4 !important;
            }
            div[data-testid="stChatInput"] button {
                border-radius: 50% !important;
                background-color: #383838 !important;
                color: #ffffff !important;
                border: none !important;
                margin-left: 6px !important;
                align-self: center !important;
                transition: all 0.2s ease !important;
            }
            div[data-testid="stChatInput"] button:hover {
                background-color: #38bdf8 !important;
                color: #ffffff !important;
            }
        </style>
    """, unsafe_allow_html=True)

    # CALCOLO LARGHEZZA DINAMICA DELLE COLONNE DA SLIDER STATE
    canvas_pct = st.session_state.canvas_width_pct
    chat_pct = 100 - canvas_pct - 2
    
    col_chat, col_handle, col_canvas = st.columns([chat_pct, 2, canvas_pct])

    # 1. PANNELLO CANVAS A DESTRA (DOCUMENTO MOSTRATO PRIMA PER PERMETTERE IL LIVE STREAMING)
    canvas_placeholder = None
    with col_canvas:
        col_title, b1, b2, b3, b4 = st.columns([6, 1, 1, 1, 1])
        with col_title:
            st.markdown("<h3 style='margin:0; padding:0; color:#ffffff;'>📄 Canvas Appunti</h3>", unsafe_allow_html=True)
        with b1:
            if 'canvas_edit_mode_toggle' not in st.session_state:
                st.session_state.canvas_edit_mode_toggle = False
            icon_view = "👁️" if st.session_state.canvas_edit_mode_toggle else "✏️"
            help_view = "Anteprima" if st.session_state.canvas_edit_mode_toggle else "Modifica"
            trigger_edit_toggle = st.button(icon_view, help=help_view, key="btn_toggle_canvas_edit_icon")
        with b2:
            trigger_latex_regen = st.button("📄", help="Rigenera LaTeX in background", key="btn_regen_latex_canvas", disabled=is_latex_active)
        with b3:
            has_unsaved_canvas = check_has_unsaved_changes()
            help_notion = "⚠️ Salva su Notion (Modifiche non salvate)" if has_unsaved_canvas else "Salva su Notion"
            trigger_notion_save = st.button("📤", help=help_notion, key="btn_save_notion_icon", disabled=is_saving_active)
        with b4:
            trigger_back = st.button("🔙", help="Torna al Form", key="btn_close_canvas_icon")

        if trigger_edit_toggle:
            st.session_state.canvas_edit_mode_toggle = not st.session_state.canvas_edit_mode_toggle
            cur_idx = st.session_state.get("current_version_index", 0)
            versions = st.session_state.get("notes_versions", [])
            
            if st.session_state.canvas_edit_mode_toggle:
                # Entrando in modalità MODIFICA MANUALE:
                # Sincronizza esplicitamente il testo attivo della versione corrente nel bridge e nell'editor
                raw_text = st.session_state.get("appunti_generati") or (versions[cur_idx] if 0 <= cur_idx < len(versions) else "")
                active_text = notion_helper.normalize_images_to_markdown(raw_text)
                st.session_state.appunti_generati = active_text
                safe_set_session_state("notes_sync_bridge_input", active_text)
                safe_set_session_state("markdown_editor_area_canvas", active_text)
                safe_set_session_state("markdown_editor_area", active_text)
                st.session_state._version_just_switched = True
                st.session_state._version_switch_timestamp = time.time()
            else:
                # Uscendo da modalità MODIFICA (tornando in ANTEPRIMA):
                # Salva le modifiche digitate manualmente nell'editor dentro appunti_generati e nella versione
                bridge_val = st.session_state.get("notes_sync_bridge_input")
                if bridge_val and str(bridge_val).strip():
                    clean_bridge = notion_helper.normalize_images_to_markdown(bridge_val)
                    st.session_state.appunti_generati = clean_bridge
                    st.session_state._last_valid_appunti = clean_bridge
                    if 0 <= cur_idx < len(versions):
                        versions[cur_idx] = clean_bridge
                st.session_state._version_just_switched = True
                st.session_state._version_switch_timestamp = time.time()
            st.rerun()
        if trigger_back:
            if st.session_state.canvas_edit_mode_toggle:
                bridge_val = st.session_state.get("notes_sync_bridge_input")
                if bridge_val and str(bridge_val).strip():
                    clean_bridge = notion_helper.normalize_images_to_markdown(bridge_val)
                    st.session_state.appunti_generati = clean_bridge
                    st.session_state._last_valid_appunti = clean_bridge
                    cur_idx = st.session_state.get("current_version_index", 0)
                    versions = st.session_state.get("notes_versions", [])
                    if 0 <= cur_idx < len(versions):
                        versions[cur_idx] = clean_bridge
            st.session_state.show_canvas_chat = False
            st.rerun()

        if trigger_notion_save:
            if st.session_state.canvas_edit_mode_toggle:
                bridge_val = st.session_state.get("notes_sync_bridge_input")
                if bridge_val and str(bridge_val).strip():
                    clean_bridge = notion_helper.normalize_images_to_markdown(bridge_val)
                    st.session_state.appunti_generati = clean_bridge
                    st.session_state._last_valid_appunti = clean_bridge
                    cur_idx = st.session_state.get("current_version_index", 0)
                    versions = st.session_state.get("notes_versions", [])
                    if 0 <= cur_idx < len(versions):
                        versions[cur_idx] = clean_bridge
            save_current_notes_to_notion()

        if trigger_latex_regen:
            trigger_background_latex_regen()

        if (not st.session_state.appunti_generati or not str(st.session_state.appunti_generati).strip()) and st.session_state.get("_last_valid_appunti"):
            st.session_state.appunti_generati = st.session_state._last_valid_appunti

        if st.session_state.appunti_generati and len(str(st.session_state.appunti_generati).strip()) > 0:
            st.session_state._last_valid_appunti = st.session_state.appunti_generati

        # NOTIFICA CONTINUA ANIMATA PER OPERAZIONI IN BACKGROUND (NOTION & LATEX)
        render_active_background_operations_banner()

        has_unsaved_notes = check_has_unsaved_changes()
        if has_unsaved_notes:
            st.markdown("""
                <div style="
                    background: rgba(245, 158, 11, 0.12);
                    border: 1px solid rgba(245, 158, 11, 0.35);
                    border-left: 3.5px solid #f59e0b;
                    border-radius: 8px;
                    padding: 8px 12px;
                    margin-bottom: 8px;
                    font-size: 13px;
                    color: #fde68a;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    line-height: 1.4;
                ">
                    <span style="font-size: 15px; flex-shrink: 0;">⚠️</span>
                    <span><strong>Modifiche non salvate:</strong> ricordati di salvare gli appunti su Notion (icona 📤 in alto a destra) per non perdere le modifiche.</span>
                </div>
            """, unsafe_allow_html=True)

        canvas_container_height = 585 if has_unsaved_notes else 650
        bottom_spacer_h = "280px" if has_unsaved_notes else "220px"
        editor_h = 540 if has_unsaved_notes else 600
        editor_min_h = 520 if has_unsaved_notes else 580

        if st.session_state.latex_generato:
            tab_canvas_md, tab_canvas_lat = st.tabs(["📚 Appunti (Markdown)", "📄 Codice LaTeX"])
            with tab_canvas_md:
                render_version_navigation_bar("canvas_tab_md")
                st.markdown("<div style='margin-bottom: 4px;'></div>", unsafe_allow_html=True)
                cleaned_render_canvas = prepare_canvas_render_with_marker(
                    st.session_state.appunti_generati,
                    st.session_state.get("canvas_scroll_target_snippet"),
                    st.session_state.canvas_edit_mode_toggle
                )

                canvas_scroll_area_md = st.container(height=canvas_container_height, border=False)
                with canvas_scroll_area_md:
                    st.markdown("<div id='canvas-scroll-anchor' style='display:none;'></div>", unsafe_allow_html=True)
                    if st.session_state.canvas_edit_mode_toggle:
                        escaped_text = html.escape(st.session_state.appunti_generati or "")
                        st.markdown(f"""
                            <textarea id="canvas-inplace-editor" class="raw-markdown-editor" spellcheck="false" style="
                                width: 100%;
                                min-height: {editor_min_h}px;
                                height: {editor_h}px;
                                background-color: #1e1e1e;
                                color: #e2e8f0;
                                font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
                                font-size: 14px;
                                line-height: 1.6;
                                padding: 16px;
                                border: 1.5px solid #383838;
                                border-radius: 8px;
                                white-space: pre;
                                outline: none;
                                resize: vertical;
                                box-sizing: border-box;
                            ">{escaped_text}</textarea>
                            <div style="height: {bottom_spacer_h};"></div>
                        """, unsafe_allow_html=True)
                    else:
                        canvas_placeholder = st.empty()
                        canvas_placeholder.markdown(cleaned_render_canvas + f"\n\n<div style='height: {bottom_spacer_h};'></div>", unsafe_allow_html=True)

            with tab_canvas_lat:
                canvas_scroll_area_lat = st.container(height=canvas_container_height, border=False)
                with canvas_scroll_area_lat:
                    if st.session_state.canvas_edit_mode_toggle:
                        edited_latex_canvas = st.text_area(
                            "Modifica direttamente il codice LaTeX nel Canvas:",
                            value=st.session_state.latex_generato if st.session_state.latex_generato else "",
                            height=editor_min_h,
                            key="latex_editor_area_canvas"
                        )
                        st.session_state.latex_generato = edited_latex_canvas
                        st.markdown(f"<div style='height: {bottom_spacer_h};'></div>", unsafe_allow_html=True)
                    else:
                        st.code(st.session_state.latex_generato, language="latex")
                        st.divider()
                        c_lat1, c_lat2 = st.columns([1, 4])
                        with c_lat1:
                            st.download_button("💾 Scarica .tex", st.session_state.latex_generato, f"appunti_{datetime.date.today().strftime('%d_%m_%Y')}.tex")
                        with c_lat2:
                            st_copy_to_clipboard(st.session_state.latex_generato, "📋 Copia LaTeX")
                        st.markdown(f"<div style='height: {bottom_spacer_h};'></div>", unsafe_allow_html=True)
        else:
            render_version_navigation_bar("canvas_no_tab_md")
            st.markdown("<div style='margin-bottom: 4px;'></div>", unsafe_allow_html=True)
            cleaned_render_canvas = prepare_canvas_render_with_marker(
                st.session_state.appunti_generati,
                st.session_state.get("canvas_scroll_target_snippet"),
                st.session_state.canvas_edit_mode_toggle
            )

            canvas_scroll_area = st.container(height=canvas_container_height, border=False)
            with canvas_scroll_area:
                st.markdown("<div id='canvas-scroll-anchor' style='display:none;'></div>", unsafe_allow_html=True)
                if st.session_state.canvas_edit_mode_toggle:
                    escaped_text = html.escape(st.session_state.appunti_generati or "")
                    st.markdown(f"""
                        <textarea id="canvas-inplace-editor" class="raw-markdown-editor" spellcheck="false" style="
                            width: 100%;
                            min-height: {editor_min_h}px;
                            height: {editor_h}px;
                            background-color: #1e1e1e;
                            color: #e2e8f0;
                            font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
                            font-size: 14px;
                            line-height: 1.6;
                            padding: 16px;
                            border: 1.5px solid #383838;
                            border-radius: 8px;
                            white-space: pre;
                            outline: none;
                            resize: vertical;
                            box-sizing: border-box;
                        ">{escaped_text}</textarea>
                        <div style="height: {bottom_spacer_h};"></div>
                    """, unsafe_allow_html=True)
                else:
                    canvas_placeholder = st.empty()
                    canvas_placeholder.markdown(cleaned_render_canvas + f"\n\n<div style='height: {bottom_spacer_h};'></div>", unsafe_allow_html=True)

        inject_image_paste_drop_js()
        inject_canvas_snippet_scroll_js()

    # 2. SEPARATORE CENTRALE CON DRAG HANDLE TRASCINABILE (#drag-handle-pill-native)
    with col_handle:
        st.markdown("""
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; min-height: 650px; user-select: none;">
                <div id="drag-handle-pill-native" style="
                    width: 18px;
                    height: 64px;
                    background-color: #2b2b2b;
                    border: 1.5px solid #444444;
                    border-radius: 12px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    cursor: col-resize;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
                    transition: background-color 0.2s, transform 0.15s;
                " title="Trascina con il mouse per ridimensionare il Canvas">
                    <span style="color: #888888; font-size: 13px; user-select: none; pointer-events: none;">⋮</span>
                </div>
            </div>
        """, unsafe_allow_html=True)

    # 3. PANNELLO CHAT (SINISTRA) - STREAMING IN TEMPO REALE SUL CANVAS E SULLA CHAT
    with col_chat:
        st.markdown("<h3 style='margin:0 0 0.8rem 0; color:#ffffff;'>💬 Chatbot Assistant</h3>", unsafe_allow_html=True)

        # --- INIEZIONE BARRA CHAT PERSONALIZZATA STILE CHATGPT E GESTIONE PILLOLA #drag-handle-pill-native ---
        is_streaming_js = "true" if st.session_state.pending_agent_stream else "false"
        draggable_handle_js = f"""
        <script>
        (function() {{
            const pDoc = window.parent.document;
            const pWin = pDoc.defaultView || window.parent;
            let userIsNearBottom = true;

            // FLAG STREAMING SUL PARENT WINDOW (condiviso tra tutti gli iframe)
            pWin.__isAgentStreaming = {is_streaming_js};

            function readStreamingFlagFromDOM() {{
                const flagEl = pDoc.getElementById('streaming-state-flag');
                if (flagEl) {{
                    const domSaysStreaming = (flagEl.getAttribute('data-streaming') === 'true');
                    if (domSaysStreaming) {{
                        pWin.__isAgentStreaming = true;
                    }} else {{
                        pWin.__isAgentStreaming = false;
                    }}
                }}
            }}

            function hideNativeChatInput() {{
                const nativeInput = pDoc.querySelector('div[data-testid="stChatInput"]');
                if (nativeInput && nativeInput.getAttribute('data-custom-hidden') !== 'true') {{
                    nativeInput.setAttribute('data-custom-hidden', 'true');
                    nativeInput.style.cssText = 'position:fixed !important; bottom:0 !important; left:0 !important; width:1px !important; height:1px !important; opacity:0 !important; overflow:hidden !important; z-index:-1 !important; clip:rect(0,0,0,0) !important;';
                }}
            }}

            function setQuotedText(text, mode) {{
                if (!text || !text.trim()) return;
                pWin.__activeQuotedText = text.trim();
                pWin.__activeQuoteMode = mode || 'mention';
                const preview = pDoc.getElementById('chatgpt-quote-preview');
                const quoteTextEl = pDoc.getElementById('chatgpt-quote-text');
                const quoteIconEl = pDoc.getElementById('chatgpt-quote-icon');
                const textarea = pDoc.getElementById('custom-chatgpt-textarea');
                if (preview && quoteTextEl) {{
                    const singleLine = text.split(String.fromCharCode(10)).join(' ').split(' ').filter(Boolean).join(' ');
                    quoteTextEl.textContent = singleLine;
                    quoteTextEl.title = text;
                    if (quoteIconEl) {{
                        if (mode === 'targeted') {{
                            quoteIconEl.innerHTML = '<span style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); padding: 2px 7px; border-radius: 5px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px;">&#9998; Modifica solo questo</span>';
                            preview.style.borderLeftColor = '#f59e0b';
                        }} else {{
                            quoteIconEl.innerHTML = '<span style="color: #38bdf8; font-size: 13.5px; font-weight: 700; display: flex; align-items: center; gap: 4px;">&#10077; Menziona</span>';
                            preview.style.borderLeftColor = '#38bdf8';
                        }}
                    }}
                    if (textarea) {{
                        if (mode === 'targeted') {{
                            textarea.placeholder = "Cosa vuoi modificare in questa sezione? (es. espandi, correggi, semplifica)...";
                        }} else {{
                            textarea.placeholder = "Chiedi all'Assistente AI di spiegare o approfondire...";
                        }}
                    }}
                    preview.style.display = 'flex';
                }}
                syncChatInputPos();
                if (textarea) {{
                    textarea.focus();
                }}
            }}

            function clearQuotedText() {{
                pWin.__activeQuotedText = null;
                pWin.__activeQuoteMode = null;
                pWin.__tempSelectedText = null;
                const preview = pDoc.getElementById('chatgpt-quote-preview');
                if (preview) {{
                    preview.style.display = 'none';
                }}
                const textarea = pDoc.getElementById('custom-chatgpt-textarea');
                if (textarea) {{
                    textarea.placeholder = "Chiedi all'Assistente AI di modificare il Canvas...";
                }}
                syncChatInputPos();
            }}

            function isCanvasStudioActive() {{
                return !!(pDoc.getElementById('drag-handle-pill-native') || pDoc.getElementById('canvas-scroll-anchor') || pDoc.querySelector('#canvas-inplace-editor'));
            }}

            function ensureSelectionMentionPill() {{
                if (!isCanvasStudioActive()) {{
                    const existingPill = pDoc.getElementById('selection-mention-pill');
                    if (existingPill) existingPill.remove();
                    return null;
                }}
                let pill = pDoc.getElementById('selection-mention-pill');
                if (!pill) {{
                    pill = pDoc.createElement('div');
                    pill.id = 'selection-mention-pill';
                    pill.innerHTML = `
                        <button id="pill-btn-mention" class="pill-action-btn" type="button" title="Cita in chat per chiarimenti didattici">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                            </svg>
                            <span>Menziona</span>
                        </button>
                        <div class="pill-divider"></div>
                        <button id="pill-btn-edit" class="pill-action-btn pill-action-edit" type="button" title="Riscrivi o espandi solo questa sezione nel Canvas">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M12 20h9"></path>
                                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
                            </svg>
                            <span>Modifica solo questo</span>
                        </button>
                    `;
                    pDoc.body.appendChild(pill);
                }}

                pill.onmousedown = function(e) {{
                    e.preventDefault();
                    e.stopPropagation();
                }};

                const btnMention = pill.querySelector('#pill-btn-mention');
                const btnEdit = pill.querySelector('#pill-btn-edit');

                if (btnMention) {{
                    btnMention.onmousedown = function(e) {{ e.preventDefault(); e.stopPropagation(); }};
                    btnMention.onclick = function(e) {{
                        e.preventDefault();
                        e.stopPropagation();
                        const textToQuote = pWin.__tempSelectedText;
                        hideSelectionPill();
                        const sel = (pDoc.getSelection && pDoc.getSelection()) || (pWin.getSelection && pWin.getSelection()) || window.getSelection();
                        if (sel) {{
                            try {{ sel.removeAllRanges(); }} catch(err) {{}}
                        }}
                        if (textToQuote) {{
                            setQuotedText(textToQuote, 'mention');
                        }}
                        setTimeout(function() {{
                            const ta = pDoc.getElementById('custom-chatgpt-textarea');
                            if (ta) {{
                                ta.focus();
                                const len = ta.value.length;
                                try {{ ta.setSelectionRange(len, len); }} catch(err) {{}}
                            }}
                        }}, 40);
                    }};
                }}

                if (btnEdit) {{
                    btnEdit.onmousedown = function(e) {{ e.preventDefault(); e.stopPropagation(); }};
                    btnEdit.onclick = function(e) {{
                        e.preventDefault();
                        e.stopPropagation();
                        const textToQuote = pWin.__tempSelectedText;
                        hideSelectionPill();
                        const sel = (pDoc.getSelection && pDoc.getSelection()) || (pWin.getSelection && pWin.getSelection()) || window.getSelection();
                        if (sel) {{
                            try {{ sel.removeAllRanges(); }} catch(err) {{}}
                        }}
                        if (textToQuote) {{
                            setQuotedText(textToQuote, 'targeted');
                        }}
                        setTimeout(function() {{
                            const ta = pDoc.getElementById('custom-chatgpt-textarea');
                            if (ta) {{
                                ta.focus();
                                const len = ta.value.length;
                                try {{ ta.setSelectionRange(len, len); }} catch(err) {{}}
                            }}
                        }}, 40);
                    }};
                }}

                return pill;
            }}

            function hideSelectionPill() {{
                const pill = pDoc.getElementById('selection-mention-pill');
                if (pill) {{
                    pill.style.display = 'none';
                }}
            }}

            function setupTextSelectionListener() {{
                ensureSelectionMentionPill();

                // Pulisce vecchi listener registrati da iframe precedenti durante rerun Streamlit
                if (pWin.__cleanupSelectionListeners) {{
                    try {{ pWin.__cleanupSelectionListeners(); }} catch(err) {{}}
                    pWin.__cleanupSelectionListeners = null;
                }}

                let selectionDebounce = null;

                const handleSelection = function(e) {{
                    const pill = ensureSelectionMentionPill();
                    if (e && e.target && pill.contains(e.target)) return;

                    const sel = (pDoc.getSelection && pDoc.getSelection()) || (pWin.getSelection && pWin.getSelection()) || window.getSelection();
                    let selectedText = '';
                    let anchorEl = null;

                    if (sel && !sel.isCollapsed && sel.rangeCount > 0) {{
                        selectedText = sel.toString().trim();
                        const anchorNode = sel.anchorNode;
                        anchorEl = anchorNode ? (anchorNode.nodeType === 3 ? anchorNode.parentElement : anchorNode) : null;
                    }}

                    // Supporto selezione anche dentro textarea (es. editor in-place del Canvas)
                    const activeEl = pDoc.activeElement;
                    if ((!selectedText || selectedText.length < 2) && activeEl && activeEl.id === 'canvas-inplace-editor') {{
                        if (activeEl.selectionStart !== undefined && activeEl.selectionEnd !== undefined && activeEl.selectionEnd > activeEl.selectionStart) {{
                            selectedText = activeEl.value.substring(activeEl.selectionStart, activeEl.selectionEnd).trim();
                            anchorEl = activeEl;
                        }}
                    }}

                    if (!selectedText || selectedText.length < 2 || !anchorEl) {{
                        hideSelectionPill();
                        return;
                    }}

                    // Non mostrare la pillola se si seleziona dentro la barra di input stessa o dentro un bottone
                    const bar = pDoc.getElementById('custom-chatgpt-bar');
                    if (bar && bar.contains(anchorEl)) {{
                        hideSelectionPill();
                        return;
                    }}
                    if (anchorEl.tagName === 'BUTTON' || (anchorEl.closest && anchorEl.closest('button'))) {{
                        hideSelectionPill();
                        return;
                    }}

                    let rect = null;
                    if (sel && sel.rangeCount > 0) {{
                        const range = sel.getRangeAt(0);
                        const rects = range.getClientRects();
                        if (rects && rects.length > 0) {{
                            rect = range.getBoundingClientRect();
                            if (!rect || (rect.width === 0 && rect.height === 0)) {{
                                rect = rects[0];
                            }}
                        }} else {{
                            rect = range.getBoundingClientRect();
                        }}
                    }} else if (anchorEl) {{
                        rect = anchorEl.getBoundingClientRect();
                    }}

                    if (!rect || (rect.width === 0 && rect.height === 0)) {{
                        hideSelectionPill();
                        return;
                    }}

                    pWin.__tempSelectedText = selectedText;
                    pill.style.display = 'flex';
                    
                    const pillWidth = pill.offsetWidth || 230;
                    const pillHeight = pill.offsetHeight || 34;

                    let top = rect.top - pillHeight - 10;
                    if (top < 10) {{
                        top = rect.bottom + 10;
                    }}
                    let left = rect.left + (rect.width / 2) - (pillWidth / 2);
                    left = Math.max(12, Math.min(pWin.innerWidth - pillWidth - 12, left));

                    pill.style.top = top + 'px';
                    pill.style.left = left + 'px';
                }};

                const onMouseUp = function(e) {{
                    clearTimeout(selectionDebounce);
                    selectionDebounce = setTimeout(function() {{ handleSelection(e); }}, 40);
                }};

                const onKeyUp = function(e) {{
                    if (e.key === 'Shift' || e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {{
                        clearTimeout(selectionDebounce);
                        selectionDebounce = setTimeout(function() {{ handleSelection(e); }}, 40);
                    }}
                }};

                const onSelectionChange = function() {{
                    clearTimeout(selectionDebounce);
                    selectionDebounce = setTimeout(function() {{ handleSelection(null); }}, 60);
                }};

                const onMouseDown = function(e) {{
                    const pill = pDoc.getElementById('selection-mention-pill');
                    if (pill && pill.style.display !== 'none') {{
                        if (!pill.contains(e.target)) {{
                            hideSelectionPill();
                        }}
                    }}
                }};

                const onScroll = function() {{
                    const pill = pDoc.getElementById('selection-mention-pill');
                    if (pill && pill.style.display !== 'none') {{
                        hideSelectionPill();
                    }}
                }};

                pDoc.addEventListener('mouseup', onMouseUp);
                pDoc.addEventListener('keyup', onKeyUp);
                pDoc.addEventListener('selectionchange', onSelectionChange);
                pDoc.addEventListener('mousedown', onMouseDown);
                pWin.addEventListener('scroll', onScroll, {{ passive: true }});

                pWin.__cleanupSelectionListeners = function() {{
                    clearTimeout(selectionDebounce);
                    pDoc.removeEventListener('mouseup', onMouseUp);
                    pDoc.removeEventListener('keyup', onKeyUp);
                    pDoc.removeEventListener('selectionchange', onSelectionChange);
                    pDoc.removeEventListener('mousedown', onMouseDown);
                    pWin.removeEventListener('scroll', onScroll);
                }};
            }}

            function ensureCustomChatGPTBar() {{
                if (!isCanvasStudioActive()) {{
                    const existingBar = pDoc.getElementById('custom-chatgpt-bar');
                    if (existingBar) existingBar.remove();
                    return null;
                }}
                let bar = pDoc.getElementById('custom-chatgpt-bar');
                if (bar && !pDoc.getElementById('chatgpt-quote-preview')) {{
                    bar.remove();
                    bar = null;
                }}
                if (!bar) {{
                    bar = pDoc.createElement('div');
                    bar.id = 'custom-chatgpt-bar';
                    bar.innerHTML = `
                        <style>
                            #custom-chatgpt-bar {{
                                position: fixed;
                                bottom: 15px;
                                left: 24px;
                                width: 440px;
                                max-width: calc(100vw - 48px);
                                background: #2f2f2f;
                                border: 1px solid #424242;
                                border-radius: 20px;
                                padding: 8px 12px 8px 16px;
                                box-shadow: 0 8px 24px rgba(0,0,0,0.5);
                                z-index: 99999;
                                display: flex;
                                flex-direction: column;
                                align-items: stretch;
                                gap: 0px;
                                box-sizing: border-box;
                                transition: border-color 0.2s, box-shadow 0.2s;
                            }}
                            #custom-chatgpt-bar:focus-within {{
                                border-color: #555555;
                                box-shadow: 0 8px 28px rgba(0,0,0,0.7);
                            }}
                            #chatgpt-quote-preview {{
                                display: none;
                                align-items: center;
                                justify-content: space-between;
                                background: #232323;
                                border: 1px solid #3d3d3d;
                                border-left: 3.5px solid #38bdf8;
                                border-radius: 10px;
                                padding: 6px 10px;
                                margin-bottom: 6px;
                                width: 100%;
                                box-sizing: border-box;
                                gap: 8px;
                            }}
                            .chatgpt-quote-dismiss-btn {{
                                background: transparent;
                                border: none;
                                color: #888888;
                                cursor: pointer;
                                padding: 2px 6px;
                                border-radius: 4px;
                                font-size: 13px;
                                line-height: 1;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                transition: color 0.15s, background-color 0.15s;
                                flex-shrink: 0;
                            }}
                            .chatgpt-quote-dismiss-btn:hover {{
                                color: #ffffff;
                                background-color: #383838;
                            }}
                            .chatgpt-input-row {{
                                display: flex;
                                flex-direction: row;
                                align-items: flex-end;
                                gap: 12px;
                                width: 100%;
                            }}
                            #custom-chatgpt-textarea {{
                                flex-grow: 1;
                                background: transparent;
                                border: none;
                                outline: none;
                                color: #ececec;
                                font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                                font-size: 14px;
                                line-height: 1.45;
                                resize: none;
                                max-height: 120px;
                                min-height: 24px;
                                padding: 5px 0;
                                box-sizing: border-box;
                            }}
                            #custom-chatgpt-textarea::placeholder {{
                                color: #8e8e8e;
                            }}
                            .chatgpt-send-btn {{
                                width: 32px;
                                height: 32px;
                                min-width: 32px;
                                border-radius: 50%;
                                border: none;
                                background: #424242;
                                color: #8e8e8e;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                cursor: not-allowed;
                                transition: background-color 0.2s, transform 0.1s, color 0.2s;
                                margin-bottom: 2px;
                            }}
                            .chatgpt-send-btn.active {{
                                background: #ffffff !important;
                                color: #000000 !important;
                                cursor: pointer !important;
                            }}
                            .chatgpt-send-btn.active:hover {{
                                transform: scale(1.06);
                            }}
                            .chatgpt-send-btn.streaming {{
                                background: #38bdf8 !important;
                                color: #ffffff !important;
                                cursor: not-allowed !important;
                                animation: pulse-stream 1.5s infinite;
                            }}
                            @keyframes pulse-stream {{
                                0% {{ opacity: 0.6; }}
                                50% {{ opacity: 1; }}
                                100% {{ opacity: 0.6; }}
                            }}
                            #selection-mention-pill {{
                                position: fixed;
                                z-index: 100000;
                                display: none;
                                flex-direction: row;
                                align-items: center;
                                gap: 3px;
                                background: #1c1c1c;
                                border: 1px solid #484848;
                                border-radius: 20px;
                                padding: 3px 5px;
                                box-shadow: 0 8px 24px rgba(0,0,0,0.7);
                                cursor: default;
                                font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                                user-select: none;
                                pointer-events: auto;
                                transition: opacity 0.15s;
                            }}
                            .pill-action-btn {{
                                background: transparent;
                                border: none;
                                color: #f1f5f9;
                                display: flex;
                                align-items: center;
                                gap: 6px;
                                padding: 5px 10px;
                                border-radius: 14px;
                                font-size: 12px;
                                font-weight: 500;
                                cursor: pointer;
                                transition: background 0.15s, color 0.15s, transform 0.1s;
                                white-space: nowrap;
                            }}
                            .pill-action-btn:hover {{
                                background: #2f2f2f;
                                color: #ffffff;
                            }}
                            .pill-action-btn:active {{
                                transform: scale(0.96);
                            }}
                            .pill-action-edit {{
                                color: #fde68a !important;
                            }}
                            .pill-action-edit:hover {{
                                background: rgba(245, 158, 11, 0.2) !important;
                                color: #fef08a !important;
                            }}
                            .pill-divider {{
                                width: 1px;
                                height: 16px;
                                background: #404040;
                                margin: 0 2px;
                            }}
                        </style>
                        <div id="chatgpt-quote-preview">
                            <div style="display: flex; align-items: center; gap: 8px; min-width: 0; flex-grow: 1;">
                                <div id="chatgpt-quote-icon" style="display: flex; align-items: center; flex-shrink: 0;">
                                    <span style="color: #38bdf8; font-size: 13.5px; font-weight: 700;">&#10077; Menziona</span>
                                </div>
                                <span id="chatgpt-quote-text" style="color: #cbd5e1; font-size: 12px; line-height: 1.35; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-grow: 1; font-family: system-ui, -apple-system, sans-serif;"></span>
                            </div>
                            <button id="chatgpt-quote-dismiss-btn" type="button" class="chatgpt-quote-dismiss-btn" title="Rimuovi citazione">&#10005;</button>
                        </div>
                        <div class="chatgpt-input-row">
                            <textarea id="custom-chatgpt-textarea" placeholder="Chiedi all'Assistente AI di modificare il Canvas..." rows="1"></textarea>
                            <button id="custom-chatgpt-send-btn" class="chatgpt-send-btn" title="Invia messaggio">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                    <line x1="12" y1="19" x2="12" y2="5"></line>
                                    <polyline points="5 12 12 5 19 12"></polyline>
                                </svg>
                            </button>
                        </div>
                    `;
                    pDoc.body.appendChild(bar);
                }}

                const textarea = pDoc.getElementById('custom-chatgpt-textarea');
                const sendBtn = pDoc.getElementById('custom-chatgpt-send-btn');
                const dismissBtn = pDoc.getElementById('chatgpt-quote-dismiss-btn');

                if (dismissBtn) {{
                    dismissBtn.onclick = function(e) {{
                        e.preventDefault();
                        e.stopPropagation();
                        clearQuotedText();
                        if (textarea) textarea.focus();
                    }};
                }}

                if (pWin.__activeQuotedText) {{
                    const preview = pDoc.getElementById('chatgpt-quote-preview');
                    const quoteTextEl = pDoc.getElementById('chatgpt-quote-text');
                    const quoteIconEl = pDoc.getElementById('chatgpt-quote-icon');
                    if (preview && quoteTextEl) {{
                        quoteTextEl.textContent = pWin.__activeQuotedText.split(String.fromCharCode(10)).join(' ').split(' ').filter(Boolean).join(' ');
                        quoteTextEl.title = pWin.__activeQuotedText;
                        if (quoteIconEl) {{
                            if (pWin.__activeQuoteMode === 'targeted') {{
                                quoteIconEl.innerHTML = '<span style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); padding: 2px 7px; border-radius: 5px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px;">&#9998; Modifica solo questo</span>';
                                preview.style.borderLeftColor = '#f59e0b';
                            }} else {{
                                quoteIconEl.innerHTML = '<span style="color: #38bdf8; font-size: 13.5px; font-weight: 700; display: flex; align-items: center; gap: 4px;">&#10077; Menziona</span>';
                                preview.style.borderLeftColor = '#38bdf8';
                            }}
                        }}
                        preview.style.display = 'flex';
                    }}
                }}

                function updateSendBtnState() {{
                    if (!textarea || !sendBtn) return;
                    const val = textarea.value.trim();
                    if (pWin.__isAgentStreaming) {{
                        sendBtn.className = 'chatgpt-send-btn streaming';
                        sendBtn.title = "Assistente al lavoro...";
                    }} else if (val.length > 0) {{
                        sendBtn.className = 'chatgpt-send-btn active';
                        sendBtn.title = "Invia messaggio";
                    }} else {{
                        sendBtn.className = 'chatgpt-send-btn';
                        sendBtn.title = "Scrivi un messaggio...";
                    }}
                }}

                function autoResizeTextarea() {{
                    if (!textarea) return;
                    textarea.style.height = 'auto';
                    textarea.style.height = Math.min(120, textarea.scrollHeight) + 'px';
                    updateSendBtnState();
                }}

                function submitCustomMessage() {{
                    if (pWin.__isAgentStreaming) return;
                    if (!textarea) return;
                    const val = textarea.value.trim();
                    if (!val) return;

                    let finalVal = val;
                    if (pWin.__activeQuotedText) {{
                        const rawQuote = pWin.__activeQuotedText.trim();
                        const qLines = rawQuote.split(String.fromCharCode(10));
                        let blockQuote = '';
                        if (pWin.__activeQuoteMode === 'targeted') {{
                            blockQuote = '> 🎯 **[MODIFICA MIRATA SEZIONE]**' + String.fromCharCode(10);
                        }} else {{
                            blockQuote = '> **[Testo selezionato]:**' + String.fromCharCode(10);
                        }}
                        for (let qi = 0; qi < qLines.length; qi++) {{
                            let ql = qLines[qi].replace(String.fromCharCode(13), '').trim();
                            if (ql.length > 0) {{
                                blockQuote += '> ' + ql + String.fromCharCode(10);
                            }}
                        }}
                        blockQuote += String.fromCharCode(10);
                        finalVal = blockQuote + val;
                        clearQuotedText();
                    }}

                    const nativeInputContainer = pDoc.querySelector('div[data-testid="stChatInput"]');
                    if (nativeInputContainer) {{
                        nativeInputContainer.style.cssText = 'position:fixed; bottom:0; left:0; width:1px; height:1px; opacity:0; overflow:hidden; z-index:-1;';
                        nativeInputContainer.removeAttribute('data-custom-hidden');
                    }}
                    const nativeTextarea = nativeInputContainer ? nativeInputContainer.querySelector('textarea') : null;
                    const nativeButton = nativeInputContainer ? (nativeInputContainer.querySelector('button[data-testid="stChatInputSubmitButton"]') || nativeInputContainer.querySelector('button')) : null;

                    if (nativeTextarea && nativeButton) {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, "value").set;
                        nativeSetter.call(nativeTextarea, finalVal);
                        nativeTextarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        nativeTextarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        
                        textarea.value = '';
                        autoResizeTextarea();

                        nativeTextarea.dispatchEvent(new KeyboardEvent('keydown', {{
                            key: 'Enter',
                            code: 'Enter',
                            keyCode: 13,
                            which: 13,
                            bubbles: true,
                            cancelable: true
                        }}));

                        setTimeout(function() {{
                            nativeButton.disabled = false;
                            nativeButton.removeAttribute('disabled');
                            nativeButton.click();
                            
                            // Forza lo scroll subito dopo l'invio
                            autoScrollChatToBottom(true);
                            
                            setTimeout(function() {{
                                const ta = pDoc.getElementById('custom-chatgpt-textarea');
                                if (ta) ta.focus();
                            }}, 50);
                        }}, 150);
                    }}
                }}

                if (textarea) {{
                    textarea.oninput = function(e) {{
                        e.stopPropagation();
                        textarea.style.height = 'auto';
                        textarea.style.height = Math.min(120, textarea.scrollHeight) + 'px';
                        updateSendBtnState();
                    }};
                    textarea.onkeydown = function(e) {{
                        e.stopPropagation();
                        if (e.key === 'Enter' && !e.shiftKey) {{
                            e.preventDefault();
                            if (!pWin.__isAgentStreaming) {{
                                submitCustomMessage();
                            }}
                        }}
                    }};
                    textarea.onkeypress = function(e) {{ e.stopPropagation(); }};
                    textarea.onkeyup = function(e) {{ e.stopPropagation(); }};
                }}

                if (sendBtn) {{
                    sendBtn.onclick = function(e) {{
                        e.preventDefault();
                        if (!pWin.__isAgentStreaming) {{
                            submitCustomMessage();
                        }}
                    }};
                }}

                updateSendBtnState();
            }}

            function getTopLevelCols() {{
                const handle = pDoc.getElementById('drag-handle-pill-native');
                if (!handle) return [];
                const handleCol = handle.closest('[data-testid="stColumn"]');
                if (!handleCol) return [];
                const studioBlock = handleCol.parentElement;
                if (!studioBlock) return [];
                return Array.from(studioBlock.children).filter(el => el.getAttribute('data-testid') === 'stColumn');
            }}

            function getChatBox() {{
                const streamingFlag = pDoc.getElementById('streaming-state-flag');
                if (streamingFlag) {{
                    const chatCol = streamingFlag.closest('[data-testid="stColumn"]');
                    if (chatCol) return chatCol;
                }}
                const cols = getTopLevelCols();
                if (cols && cols.length >= 3) {{
                    const handle = pDoc.getElementById('drag-handle-pill-native');
                    const nonHandleCols = cols.filter(c => !c.contains(handle));
                    if (nonHandleCols.length > 0) return nonHandleCols[0];
                }}
                return (cols && cols[0]) || pDoc.querySelector('[data-testid="stColumn"]');
            }}

            function syncChatInputPos() {{
                hideNativeChatInput();
                ensureCustomChatGPTBar();
                const chatCol = getChatBox();
                const bar = pDoc.getElementById('custom-chatgpt-bar');
                if (chatCol && bar) {{
                    const rect = chatCol.getBoundingClientRect();
                    if (rect.width > 50) {{
                        bar.style.left = (rect.left + 8) + 'px';
                        bar.style.width = Math.max(200, rect.width - 20) + 'px';
                    }}
                }}
            }}

            function autoScrollChatToBottom(force) {{
                const chatBox = getChatBox();
                if (!chatBox) return;

                if (force || userIsNearBottom) {{
                    setTimeout(function() {{
                        chatBox.scrollTop = chatBox.scrollHeight;
                    }}, 40);
                }}
            }}

            function setupScrollListener() {{
                const chatBox = getChatBox();
                if (!chatBox || window.__scrollListenerBound) return;
                
                window.__scrollListenerBound = true;
                chatBox.onscroll = function() {{
                    const distanceToBottom = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight;
                    userIsNearBottom = (distanceToBottom <= 140);
                }};
            }}

            function initChatMutationObserver() {{
                const chatCol = getChatBox();
                if (!chatCol || window.__observerBound) return;
                
                window.__observerBound = true;
                setupScrollListener();
                
                const observer = new MutationObserver(function() {{
                    setupScrollListener();
                    if (userIsNearBottom) {{
                        autoScrollChatToBottom(false);
                    }}
                }});
                
                observer.observe(chatCol, {{
                    childList: true,
                    subtree: true,
                    characterData: true
                }});
            }}

            function initPillDrag() {{
                const pill = pDoc.getElementById('drag-handle-pill-native');
                syncChatInputPos();
                initChatMutationObserver();
                autoScrollChatToBottom(true);
                
                if (!pill || window.__dragBound) return;
                window.__dragBound = true;
                
                const handleCol = pill.closest('[data-testid="stColumn"]');
                if (!handleCol) return;
                
                const studioBlock = handleCol.parentElement;
                if (!studioBlock) return;
                
                const cols = Array.from(studioBlock.children).filter(el => el.getAttribute('data-testid') === 'stColumn');
                if (cols.length < 3) return;
                
                const leftCol = cols[0];
                const midCol = cols[1];
                const rightCol = cols[2];
                
                let isDragging = false;
                
                pill.addEventListener('mouseenter', function() {{
                    if (!isDragging) {{
                        pill.style.backgroundColor = '#38bdf8';
                        pill.style.borderColor = '#38bdf8';
                    }}
                }});
                pill.addEventListener('mouseleave', function() {{
                    if (!isDragging) {{
                        pill.style.backgroundColor = '#2b2b2b';
                        pill.style.borderColor = '#444444';
                    }}
                }});
                
                pill.addEventListener('mousedown', function(e) {{
                    isDragging = true;
                    pDoc.body.style.cursor = 'col-resize';
                    pDoc.body.style.userSelect = 'none';
                    pill.style.backgroundColor = '#38bdf8';
                    pill.style.borderColor = '#38bdf8';
                    pill.style.transform = 'scale(1.15)';
                    studioBlock.style.flexWrap = 'nowrap';
                    leftCol.style.minWidth = '0px';
                    midCol.style.minWidth = '0px';
                    rightCol.style.minWidth = '0px';
                    e.preventDefault();
                }});
                
                pDoc.addEventListener('mousemove', function(e) {{
                    if (!isDragging) return;
                    const rect = studioBlock.getBoundingClientRect();
                    let pct = ((e.clientX - rect.left) / rect.width) * 100;
                    
                    pct = Math.max(20, Math.min(75, pct));
                    let midPct = 2;
                    let rightPct = Math.max(20, 100 - pct - midPct);
                    
                    studioBlock.style.display = 'flex';
                    studioBlock.style.flexDirection = 'row';
                    studioBlock.style.flexWrap = 'nowrap';
                    
                    leftCol.style.flex = '0 0 ' + pct + '%';
                    leftCol.style.maxWidth = pct + '%';
                    leftCol.style.width = pct + '%';
                    
                    midCol.style.flex = '0 0 ' + midPct + '%';
                    midCol.style.maxWidth = midPct + '%';
                    midCol.style.width = midPct + '%';
                    
                    rightCol.style.flex = '0 0 ' + rightPct + '%';
                    rightCol.style.maxWidth = rightPct + '%';
                    rightCol.style.width = rightPct + '%';
                    
                    syncChatInputPos();
                }});
                
                pDoc.addEventListener('mouseup', function(e) {{
                    if (isDragging) {{
                        isDragging = false;
                        pDoc.body.style.cursor = 'default';
                        pDoc.body.style.userSelect = 'auto';
                        pill.style.backgroundColor = '#2b2b2b';
                        pill.style.borderColor = '#444444';
                        pill.style.transform = 'scale(1.0)';
                    }}
                }});
            }}
            
            setTimeout(initPillDrag, 20);
            setTimeout(initChatMutationObserver, 50);
            setTimeout(syncChatInputPos, 50);
            setTimeout(setupTextSelectionListener, 60);
            setTimeout(function() {{ autoScrollChatToBottom(true); }}, 80);
            setTimeout(function() {{ autoScrollChatToBottom(true); }}, 300);
            
            if (pWin.__canvasStudioInterval) {{
                clearInterval(pWin.__canvasStudioInterval);
                pWin.__canvasStudioInterval = null;
            }}
            if (pWin.__syncChatResizeListener) {{
                try {{
                    window.removeEventListener('resize', pWin.__syncChatResizeListener);
                    pWin.removeEventListener('resize', pWin.__syncChatResizeListener);
                }} catch(e) {{}}
                pWin.__syncChatResizeListener = null;
            }}

            pWin.__canvasStudioInterval = setInterval(function() {{
                if (!isCanvasStudioActive()) {{
                    const b = pDoc.getElementById('custom-chatgpt-bar');
                    if (b) b.remove();
                    const p = pDoc.getElementById('selection-mention-pill');
                    if (p) p.remove();
                    if (pWin.__canvasStudioInterval) {{
                        clearInterval(pWin.__canvasStudioInterval);
                        pWin.__canvasStudioInterval = null;
                    }}
                    return;
                }}
                readStreamingFlagFromDOM();
                syncChatInputPos();
                setupScrollListener();
                setupTextSelectionListener();
            }}, 150);
            
            pWin.__syncChatResizeListener = syncChatInputPos;
            window.addEventListener('resize', pWin.__syncChatResizeListener);
            pWin.addEventListener('resize', pWin.__syncChatResizeListener);
        }})();
        </script>
        """
        st.iframe(draggable_handle_js, height=1)

        # Flag streaming nascosto DENTRO col_chat, renderizzato PRIMA del blocco streaming
        _streaming_flag_val = "true" if st.session_state.pending_agent_stream else "false"
        st.markdown(f'<div id="streaming-state-flag" data-streaming="{_streaming_flag_val}" style="display:none !important;"></div>', unsafe_allow_html=True)

        # Contenitore di scroll nativo per la chat senza bordi visibili
        chat_scroll_area = st.container(border=False)
        with chat_scroll_area:
            st.markdown("<div style='height: 16px; width: 100%;'></div>", unsafe_allow_html=True)
            for msg in st.session_state.canvas_chat_history:
                with st.chat_message(msg["role"]):
                    if msg["role"] == "user":
                        st.markdown("<div style='color: #60a5fa; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; text-align: right;'>👤 UTENTE</div>", unsafe_allow_html=True)
                        user_content = msg["content"]
                        t_sec, t_instr = extract_targeted_edit_request(user_content)
                        if t_sec:
                            preview_snippet = t_sec.splitlines()[0][:90] + ("..." if len(t_sec) > 90 else "")
                            st.markdown(f"""
                                <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.35); border-left: 3.5px solid #f59e0b; border-radius: 8px; padding: 6px 10px; margin-bottom: 8px; font-size: 12px; color: #fde68a;">
                                    <div style="font-weight: 700; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #fbbf24; margin-bottom: 2px;">🎯 Modifica mirata su:</div>
                                    <div style="color: #cbd5e1; font-style: italic; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">"{html.escape(preview_snippet)}"</div>
                                </div>
                            """, unsafe_allow_html=True)
                            st.markdown(t_instr)
                        else:
                            st.markdown(user_content)
                    else:
                        st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                        st.markdown(msg["content"])

            if st.session_state.pending_agent_stream:
                stream_container = st.empty()
            
            # Spaziatore inferiore di 30px per dare la giusta spaziatura in fondo all'ultimo messaggio
            st.markdown("<div id='chat-bottom-spacer' style='height: 30px; width: 100%;'></div>", unsafe_allow_html=True)

            if st.session_state.pending_agent_stream:
                chat_response_placeholder = None
                chat_bubble_created = False
                full_raw_response = ""
                try:
                    last_user_prompt = st.session_state.canvas_chat_history[-1]["content"]
                    current_ctx_idx = st.session_state.get("current_version_index", 0)
                    if st.session_state.get("targeted_base_markdown"):
                        base_markdown = st.session_state.targeted_base_markdown
                    elif st.session_state.get("stream_version_created", False) and current_ctx_idx > 0 and len(st.session_state.get("notes_versions", [])) >= current_ctx_idx:
                        base_markdown = st.session_state.notes_versions[current_ctx_idx - 1]
                    else:
                        base_markdown = st.session_state.appunti_generati or ""

                    targeted_section, clean_instruction = extract_targeted_edit_request(last_user_prompt)

                    if targeted_section:
                        # FLUSSO RAPIDO: MODIFICA MIRATA DELLA SOLA SEZIONE SELEZIONATA
                        stream_gen = agent_edit_targeted_stream(
                            current_markdown=base_markdown,
                            target_section=targeted_section,
                            user_instruction=clean_instruction,
                            chat_history=st.session_state.canvas_chat_history[:-1],
                            raw_transcript=st.session_state.testo_estratto,
                            model_name=MODEL_GENERAL
                        )

                        for chunk_text in stream_gen:
                            if not chat_bubble_created:
                                chat_bubble_created = True
                                with stream_container.container():
                                    with st.chat_message("assistant"):
                                        st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                        chat_response_placeholder = st.empty()

                            full_raw_response += chunk_text
                            live_chat_part, live_replacement = parse_targeted_agent_response(full_raw_response)
                            
                            if chat_response_placeholder:
                                chat_response_placeholder.markdown(live_chat_part)
                            
                            if live_replacement and len(live_replacement) > 2:
                                live_canvas, ok = replace_section_in_markdown(base_markdown, targeted_section, live_replacement)
                                if ok:
                                    clean_live = notion_helper.normalize_images_to_markdown(live_canvas)
                                    target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                                    if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                        st.session_state.notes_versions[target_idx] = clean_live
                                    
                                    st.session_state.appunti_generati = clean_live
                                    st.session_state._last_valid_appunti = clean_live
                                    safe_set_session_state("markdown_editor_area", clean_live)
                                    safe_set_session_state("markdown_editor_area_canvas", clean_live)
                                    safe_set_session_state("notes_sync_bridge_input", clean_live)
                                    if canvas_placeholder is not None:
                                        cleaned_live_canvas = notion_helper.clean_markdown_for_streamlit(clean_live, default_width="50%")
                                        canvas_placeholder.markdown(cleaned_live_canvas, unsafe_allow_html=True)

                        if not chat_bubble_created:
                            chat_bubble_created = True
                            with stream_container.container():
                                with st.chat_message("assistant"):
                                    st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                    chat_response_placeholder = st.empty()

                        final_chat_reply, final_replacement = parse_targeted_agent_response(full_raw_response)
                        if final_replacement and len(final_replacement.strip()) > 0:
                            final_canvas, ok = replace_section_in_markdown(base_markdown, targeted_section, final_replacement)
                            if ok:
                                clean_final = notion_helper.normalize_images_to_markdown(final_canvas)
                                target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                                if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                    st.session_state.notes_versions[target_idx] = clean_final
                                st.session_state.appunti_generati = clean_final
                                st.session_state._last_valid_appunti = clean_final
                                safe_set_session_state("markdown_editor_area", clean_final)
                                safe_set_session_state("markdown_editor_area_canvas", clean_final)
                                safe_set_session_state("notes_sync_bridge_input", clean_final)
                                st.session_state._version_just_switched = True
                                st.session_state._version_switch_timestamp = time.time()
                                
                                st.session_state.force_version_sync = target_idx
                                st.session_state.canvas_scroll_target_snippet = final_replacement[:300]
                                st.toast(f"⚡ Sezione aggiornata con successo (Versione {target_idx + 1})!", icon="✏️")
                            else:
                                if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                                    st.session_state.notes_versions.pop()
                                    switch_note_version(len(st.session_state.notes_versions) - 1)
                                st.toast("⚠️ Modifica completata in chat, ma non è stato possibile rintracciare la sezione esatta nel Canvas.", icon="⚠️")
                                final_chat_reply += f"\n\n> ⚠️ **Nota Canvas:** Non è stato possibile localizzare automaticamente il frammento nel documento per la sostituzione diretta. Ecco il testo modificato:\n\n```markdown\n{final_replacement}\n```"
                        else:
                            if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                                st.session_state.notes_versions.pop()
                                switch_note_version(len(st.session_state.notes_versions) - 1)
                            st.toast("💬 Risposta fornita in chat.", icon="ℹ️")

                        st.session_state.targeted_base_markdown = None
                        st.session_state.stream_version_created = False
                        st.session_state.canvas_chat_history.append({"role": "assistant", "content": final_chat_reply or "Ho modificato la sezione selezionata."})
                        st.session_state.pending_agent_stream = False
                        st.rerun()

                    else:
                        # FLUSSO STANDARD: MODIFICA INTERO DOCUMENTO O SPIEGAZIONI
                        stream_gen = agent_edit_notes_stream(
                            current_markdown=base_markdown,
                            user_instruction=last_user_prompt,
                            chat_history=st.session_state.canvas_chat_history[:-1],
                            raw_transcript=st.session_state.testo_estratto,
                            model_name=MODEL_GENERAL
                        )

                        for chunk_text in stream_gen:
                            if not chat_bubble_created:
                                chat_bubble_created = True
                                with stream_container.container():
                                    with st.chat_message("assistant"):
                                        st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                        chat_response_placeholder = st.empty()

                            full_raw_response += chunk_text
                            live_chat_part, live_canvas_part = parse_agent_response(full_raw_response)
                            
                            if chat_response_placeholder:
                                chat_response_placeholder.markdown(live_chat_part)
                            
                            if live_canvas_part and live_canvas_part != "NO_CHANGE" and len(live_canvas_part) > 5:
                                clean_live_part = notion_helper.normalize_images_to_markdown(live_canvas_part)
                                if not st.session_state.get("stream_version_created", False):
                                    add_note_version(clean_live_part)
                                    st.session_state.stream_version_created = True
                                    st.session_state.stream_target_version_index = st.session_state.current_version_index
                                else:
                                    target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                                    if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                        st.session_state.notes_versions[target_idx] = clean_live_part
                                    
                                    if st.session_state.get("current_version_index") == target_idx:
                                        st.session_state.appunti_generati = clean_live_part
                                        st.session_state._last_valid_appunti = clean_live_part
                                        safe_set_session_state("markdown_editor_area", clean_live_part)
                                        safe_set_session_state("markdown_editor_area_canvas", clean_live_part)
                                        safe_set_session_state("notes_sync_bridge_input", clean_live_part)
                                        if canvas_placeholder is not None:
                                            cleaned_live_canvas = notion_helper.clean_markdown_for_streamlit(clean_live_part, default_width="50%")
                                            canvas_placeholder.markdown(cleaned_live_canvas, unsafe_allow_html=True)

                        # Controllo finale: se non ha emesso nulla, crea comunque la bolla
                        if not chat_bubble_created:
                            chat_bubble_created = True
                            with stream_container.container():
                                with st.chat_message("assistant"):
                                    st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                    chat_response_placeholder = st.empty()

                        final_chat_reply, final_canvas = parse_agent_response(full_raw_response)

                        if final_canvas and final_canvas != "NO_CHANGE" and len(final_canvas) > 5:
                            clean_final = notion_helper.normalize_images_to_markdown(final_canvas)
                            if not st.session_state.get("stream_version_created", False):
                                add_note_version(clean_final)
                                target_idx = st.session_state.current_version_index
                            else:
                                target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                                if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                    st.session_state.notes_versions[target_idx] = clean_final
                                switch_note_version(target_idx)
                            st.toast(f"⚡ Canvas aggiornato alla Versione {target_idx + 1}! Ricordati di salvare su Notion.", icon="⚠️")
                        else:
                            if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                                st.session_state.notes_versions.pop()
                                switch_note_version(len(st.session_state.notes_versions) - 1)
                            st.toast("💬 Risposta fornita in chat.", icon="ℹ️")
                            
                        st.session_state.stream_version_created = False
                        st.session_state.canvas_chat_history.append({"role": "assistant", "content": final_chat_reply or "Risposta dell'assistente."})
                        st.session_state.pending_agent_stream = False
                        st.rerun()
                except Exception as e:
                    if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                        st.session_state.notes_versions.pop()
                        switch_note_version(len(st.session_state.notes_versions) - 1)
                    st.session_state.pending_agent_stream = False
                    st.session_state.stream_version_created = False
                    st.error(f"❌ Errore durante la risposta dell'Assistente: {str(e)}")
                    st.session_state.canvas_chat_history.append({"role": "assistant", "content": f"⚠️ Si è verificato un errore durante l'elaborazione: {str(e)}"})
                    st.rerun()

        # Campo chat_input nativo (nascosto visivamente da JS e usato come bridge per inviare i messaggi)
        user_input = st.chat_input("Chiedi all'Assistente AI di modificare il Canvas...")
        if user_input and not st.session_state.pending_agent_stream:
            st.session_state.canvas_chat_history.append({"role": "user", "content": user_input})
            st.session_state.pending_agent_stream = True
            
            targeted_section, clean_instruction = extract_targeted_edit_request(user_input)
            if targeted_section:
                base_text = st.session_state.appunti_generati or ""
                st.session_state.targeted_base_markdown = base_text
                # Crea SUBITO la nuova versione clonando la corrente: la UI passa immediatamente a Versione 2
                add_note_version(base_text)
                st.session_state.stream_version_created = True
                st.session_state.stream_target_version_index = st.session_state.current_version_index
            else:
                st.session_state.stream_version_created = False
                st.session_state.targeted_base_markdown = None
                
            st.rerun()

    # Esegui lo script di blocco scroll come ULTIMO elemento della pagina per evitare che Streamlit spinga in giù il layout
    import streamlit.components.v1 as components
    components.html(canvas_js, height=0)

# ==============================================================================
# PAGINA PRINCIPALE: CONFIGURAZIONE FORM & GENERAZIONE
# ==============================================================================
else:
    # --- CLEANUP: Rimuove la chatbar personalizzata e sblocca lo scroll se l'utente torna alla home ---
    cleanup_js = """
    <script>
    (function() {
        const pWin = window.parent || window;
        const pDoc = window.parent.document || document;
        
        // 1. Arresta tassativamente tutti i timer e gli intervalli del Canvas Studio
        if (pWin.__canvasStudioInterval) {
            clearInterval(pWin.__canvasStudioInterval);
            pWin.__canvasStudioInterval = null;
        }
        if (pWin.__syncChatResizeListener) {
            try {
                window.removeEventListener('resize', pWin.__syncChatResizeListener);
                pWin.removeEventListener('resize', pWin.__syncChatResizeListener);
            } catch(e) {}
            pWin.__syncChatResizeListener = null;
        }
        if (pWin.__canvasScrollLock) {
            clearInterval(pWin.__canvasScrollLock);
            pWin.__canvasScrollLock = null;
        }
        pWin.__canvasSnippetScrolling = false;

        // 2. Rimuove la chatbar e il pill di selezione in modo persistente
        function removeStudioElements() {
            const bar = pDoc.getElementById('custom-chatgpt-bar');
            if (bar) {
                bar.remove();
            }
            const pill = pDoc.getElementById('selection-mention-pill');
            if (pill) {
                pill.remove();
            }
        }
        removeStudioElements();
        pWin.setTimeout(removeStudioElements, 40);
        pWin.setTimeout(removeStudioElements, 150);
        pWin.setTimeout(removeStudioElements, 500);

        // 3. Ripristina lo scroll standard della pagina
        try {
            if (pWin.history && pWin.history.scrollRestoration) {
                pWin.history.scrollRestoration = 'auto';
            }
        } catch(e) {}

        pWin.__activeQuotedText = null;
        pWin.__activeQuoteMode = null;
        pWin.__tempSelectedText = null;
    })();
    </script>
    """
    import streamlit.components.v1 as components
    try:
        st.iframe(cleanup_js, height=1)
    except AttributeError:
        components.html(cleanup_js, height=1)

    @st.dialog("🌟 Scopri le novità")
    def show_onboarding_dialog():
        if "feature_index" not in st.session_state:
            st.session_state.feature_index = 0

        features = [
            {
                "title": "🔗 Da Vimeo a Notion, in automatico",
                "desc": "Inserendo il link della lezione, gli appunti vengono elaborati e generati automaticamente in pochi secondi, per poi essere salvati in modo diretto all'interno del database Notion."
            },
            {
                "title": "🧠 Gestione Intelligente dei Duplicati",
                "desc": "Se viene inserito un video già elaborato, gli appunti salvati su Notion vengono recuperati istantaneamente. Nel caso di una seconda parte di lezione, se materia e data coincidono, le nuove informazioni vengono integrate in automatico in fondo alla pagina originale, evitando la creazione di duplicati."
            },
            {
                "title": "🎨 Appunti su Misura (Prompt Personalizzato)",
                "desc": "È possibile avere il pieno controllo dello stile degli appunti. Modificando le istruzioni di base fornite all'intelligenza artificiale, si ottengono riassunti che si adattano perfettamente a qualsiasi metodo di studio o esigenza accademica."
            },
            {
                "title": "✨ Nuovo Canvas Immersivo e Chat AI",
                "desc": "Gli appunti possono essere revisionati in una modalità a tutto schermo pensata per la massima concentrazione. È possibile apportare modifiche tramite una semplice chat integrata, ottenendo un aggiornamento del documento in tempo reale senza alcuna necessità di riscrittura manuale."
            },
            {
                "title": "⏱️ La \"Macchina del Tempo\" delle versioni",
                "desc": "In caso di errori o ripensamenti durante le modifiche, ogni passaggio viene salvato automaticamente. È possibile scorrere avanti e indietro nel tempo tra le varie versioni con un semplice clic, evitando qualsiasi perdita di dati."
            },
            {
                "title": "📐 Esportazione LaTeX in Background",
                "desc": "È disponibile la formattazione professionale in codice LaTeX per le materie scientifiche. L'operazione viene eseguita in background senza bloccare l'interfaccia, consentendo di continuare a utilizzare l'applicazione liberamente mentre il file .tex viene preparato per il download."
            }
        ]

        def change_feature(delta):
            st.session_state.feature_index += delta

        current_feature = features[st.session_state.feature_index]

        st.markdown(f"#### {current_feature['title']}")
        st.write(current_feature['desc'])
        st.markdown("<br>", unsafe_allow_html=True)
        
        nav_col1, nav_col2, _ = st.columns([1, 1, 1.5])
        nav_col1.button("⬅️ Prec.", on_click=change_feature, args=(-1,), disabled=(st.session_state.feature_index == 0))
        nav_col2.button("Pros. ➡️", on_click=change_feature, args=(1,), disabled=(st.session_state.feature_index == len(features) - 1))
            
        dots = "".join(["🔵 " if i == st.session_state.feature_index else "⚪ " for i in range(len(features))])
        st.caption(f"Slide {st.session_state.feature_index + 1} di {len(features)} &nbsp; {dots}")

    col_title_main, col_btn_news = st.columns([4, 1])
    with col_title_main:
        st.title("🎓 Narderio Transcription")
    with col_btn_news:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("✨ Scopri le Novità", use_container_width=True):
            show_onboarding_dialog()

    st.markdown("Estrai la trascrizione dai video Vimeo, genera ed edita appunti universitari con Gemini e salvali direttamente su **Notion**.")

    col_left, col_right = st.columns([2, 1])

    notion_token = os.getenv("NOTION_API_KEY")
    notion_corsi_id = os.getenv("NOTION_CORSI_PAGE_ID")

    def update_saved_vimeo_url():
        if "vimeo_url_input" in st.session_state:
            new_url = st.session_state.vimeo_url_input
            old_url = st.session_state.get("saved_vimeo_url", "")
            st.session_state.saved_vimeo_url = new_url
            if new_url != old_url:
                st.session_state.appunti_generati = None
                st.session_state.testo_estratto = None
                st.session_state.notes_versions = []
                st.session_state.current_version_index = 0
                st.session_state.latex_generato = None
                st.session_state.notion_status = None
                st.session_state.notion_page_url = None
                st.session_state.canvas_chat_history = []
                st.session_state._last_saved_notion_notes = None
                st.session_state._active_loaded_lesson_id = None
                try:
                    cached_is_video_processed.clear()
                    cached_get_all_lesson_videos.clear()
                except Exception:
                    pass
                v_id, _ = extract_vimeo_ids(new_url) if new_url else (None, None)
                if v_id:
                    is_p, rec = supabase_client.is_video_processed(v_id)
                    if is_p and rec:
                        if rec.get("lesson_date"):
                            try:
                                d_parts = str(rec.get("lesson_date")).split("-")
                                if len(d_parts) == 3:
                                    st.session_state.saved_lesson_date = datetime.date(int(d_parts[0]), int(d_parts[1]), int(d_parts[2]))
                            except Exception:
                                pass
                        if rec.get("course"):
                            st.session_state.selected_course_auto = rec.get("course")
                        if rec.get("notion_page_id"):
                            st.session_state._active_loaded_lesson_id = rec.get("notion_page_id")

    def update_saved_lesson_date():
        if "lesson_date_input" in st.session_state:
            st.session_state.saved_lesson_date = st.session_state.lesson_date_input

    with col_left:
        # 1. Selezione Materia / Corso
        courses_dict = cached_get_available_courses(notion_corsi_id, notion_token)
        if courses_dict:
            course_names = list(courses_dict.keys())
            def_idx = 0
            if "selected_course_auto" in st.session_state and st.session_state.selected_course_auto in course_names:
                def_idx = course_names.index(st.session_state.selected_course_auto)
            elif "selected_course" in st.session_state and st.session_state.selected_course in course_names:
                def_idx = course_names.index(st.session_state.selected_course)
            selected_course = st.selectbox("Materia / Corso (Letto da Notion)", course_names, index=def_idx, key="course_selector_main")
            selected_course_page_id = courses_dict[selected_course]
        else:
            st.info("💡 Nessun corso trovato o chiavi Notion non impostate nel file .env. Puoi scrivere la materia a mano:")
            selected_course = st.text_input("Nome della materia", value="Generale", key="manual_course_name")
            selected_course_page_id = notion_corsi_id

        # Se il corso è cambiato rispetto all'ultimo attivo, resettiamo la lezione caricata
        if st.session_state.get("_last_selected_course") != selected_course:
            st.session_state._last_selected_course = selected_course
            st.session_state._active_loaded_lesson_id = None
            st.session_state.appunti_generati = None
            st.session_state.testo_estratto = None
            st.session_state.notes_versions = []
            st.session_state.current_version_index = 0
            st.session_state.latex_generato = None
            st.session_state.notion_status = None
            st.session_state.notion_page_url = None
            st.session_state.canvas_chat_history = []
            st.session_state._last_saved_notion_notes = None

        st.session_state.selected_course = selected_course
        st.session_state.selected_course_page_id = selected_course_page_id

        # 2. Selettore Lezioni esistenti per la materia selezionata
        NEW_LESSON_TAG = "✨ -- Nuova Lezione (inserisci link Vimeo) --"
        course_lessons = []
        if selected_course_page_id and notion_token:
            course_lessons = cached_get_course_lessons(selected_course_page_id, selected_course, notion_token)

        lesson_labels = [NEW_LESSON_TAG]
        lesson_map = {}
        for l in course_lessons:
            lbl = f"📖 {l['title']}"
            if lbl in lesson_map:
                lbl = f"📖 {l['title']} (ID: {l['id'][:4]})"
            lesson_labels.append(lbl)
            lesson_map[lbl] = l

        lesson_def_idx = 0
        active_lid = st.session_state.get("_active_loaded_lesson_id")
        if active_lid:
            for idx_lbl, lbl_text in enumerate(lesson_labels):
                if lbl_text in lesson_map and lesson_map[lbl_text]["id"] == active_lid:
                    lesson_def_idx = idx_lbl
                    break

        selected_lesson_label = st.selectbox(
            "📖 Seleziona Lezione / Appunti da caricare",
            lesson_labels,
            index=lesson_def_idx,
            key=f"lesson_select_{selected_course}",
            help="Scegli una lezione già presente su Notion per caricarne direttamente gli appunti e le relative trascrizioni video."
        )

        # Gestione cambio lezione selezionata
        if selected_lesson_label != NEW_LESSON_TAG:
            chosen_lesson = lesson_map.get(selected_lesson_label)
            if chosen_lesson and chosen_lesson["id"] != st.session_state.get("_active_loaded_lesson_id"):
                pid = chosen_lesson["id"]
                st.session_state._active_loaded_lesson_id = pid
                st.session_state.current_notion_page_id = pid
                clean_pid = notion_helper.format_notion_id(pid).replace("-", "")
                st.session_state.notion_page_url = f"https://www.notion.so/{clean_pid}"

                parsed_d = parse_date_safely(chosen_lesson.get("date"), chosen_lesson.get("title"))
                st.session_state.saved_lesson_date = parsed_d
                safe_set_session_state("lesson_date_input", parsed_d)
                formatted_d = parsed_d.strftime("%d/%m/%Y")
                st.session_state.formatted_date_str = formatted_d

                with st.spinner(f"Caricamento appunti e trascrizioni per '{chosen_lesson.get('title')}'..."):
                    # 1. Carica appunti Markdown da Notion
                    fetched_notes = cached_get_notion_page_markdown(pid, token=notion_token)
                    if fetched_notes:
                        st.session_state.appunti_generati = fetched_notes
                        st.session_state._last_valid_appunti = fetched_notes
                        st.session_state._last_saved_notion_notes = fetched_notes
                        st.session_state.notes_versions = [fetched_notes]
                        st.session_state.current_version_index = 0
                        safe_set_session_state("markdown_editor_area", fetched_notes)
                        safe_set_session_state("markdown_editor_area_canvas", fetched_notes)

                    # 2. Carica e aggrega tutte le trascrizioni dei video associati alla lezione
                    all_videos = cached_get_all_lesson_videos(
                        course=selected_course,
                        lesson_date=chosen_lesson.get("date") or formatted_d,
                        notion_page_id=pid
                    )
                    video_urls = [v.get("url") for v in all_videos if v.get("url")]
                    if video_urls:
                        success_tr, agg_tr = cached_fetch_aggregated_transcript(tuple(video_urls))
                        if success_tr:
                            st.session_state.testo_estratto = agg_tr
                        else:
                            st.session_state.testo_estratto = None
                        if video_urls[0]:
                            st.session_state.saved_vimeo_url = video_urls[0]
                            safe_set_session_state("vimeo_url_input", video_urls[0])
                    else:
                        st.session_state.testo_estratto = None

                st.session_state.latex_generato = None
                st.session_state.canvas_chat_history = []
                num_vids = len(video_urls) if video_urls else 0
                vid_info = f" ({num_vids} parti video trascritte)" if num_vids > 1 else (" (1 video trascritto)" if num_vids == 1 else "")
                st.session_state.notion_status = f"💡 Appunti e trascrizioni della '{chosen_lesson.get('title')}' caricati con successo da Notion{vid_info}!"
                st.toast(f"✅ Appunti e trascrizioni di '{chosen_lesson.get('title')}' caricati!", icon="📚")
                st.session_state._should_scroll_to_results = True
                st.rerun()

        elif selected_lesson_label == NEW_LESSON_TAG and st.session_state.get("_active_loaded_lesson_id") is not None:
            st.session_state._active_loaded_lesson_id = None
            st.session_state.current_notion_page_id = None
            st.session_state.notion_page_url = None
            st.session_state.appunti_generati = None
            st.session_state.testo_estratto = None
            st.session_state.notes_versions = []
            st.session_state.current_version_index = 0
            st.session_state.latex_generato = None
            st.session_state.notion_status = None
            st.session_state.canvas_chat_history = []
            st.session_state._last_saved_notion_notes = None
            st.session_state.saved_vimeo_url = ""
            safe_set_session_state("vimeo_url_input", "")
            st.rerun()

        # 3. Link video Vimeo
        url = st.text_input(
            "Link video Vimeo",
            placeholder="https://vimeo.com/123456789/hash...",
            key="vimeo_url_input",
            on_change=update_saved_vimeo_url
        )
        st.session_state.saved_vimeo_url = url

    with col_right:
        lesson_date = st.date_input(
            "Data della lezione",
            key="lesson_date_input",
            on_change=update_saved_lesson_date
        )
        st.session_state.saved_lesson_date = lesson_date
        formatted_date_str = lesson_date.strftime("%d/%m/%Y")
        st.caption(f"Etichetta Lezione: **Lezione {formatted_date_str}**")
        st.session_state.formatted_date_str = formatted_date_str

        # Se una lezione è attualmente caricata da Notion, mostriamo il badge e il link diretto
        if st.session_state.get("_active_loaded_lesson_id") and st.session_state.get("notion_page_url"):
            st.markdown(
                """
                <div style="background-color: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 10px 14px; margin-top: 10px;">
                    <div style="color: #60a5fa; font-weight: 600; font-size: 13px; margin-bottom: 4px;">📖 Lezione Caricata da Notion</div>
                    <div style="color: #94a3b8; font-size: 12px;">Appunti e trascrizioni pronti per lo studio e la modifica.</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            st.write("")
            st.link_button("📖 Apri Lezione su Notion", st.session_state.notion_page_url, use_container_width=True)

    video_id_preview, _ = extract_vimeo_ids(url) if url else (None, None)
    already_processed = False
    saved_page_id = None
    existing_notion_url = None

    if video_id_preview:
        is_proc, record = cached_is_video_processed(video_id_preview)
        if is_proc:
            already_processed = True
            saved_page_id = record.get("notion_page_id")
            if not st.session_state.get("_active_loaded_lesson_id"):
                if saved_page_id:
                    st.session_state.current_notion_page_id = saved_page_id
                    clean_pid = notion_helper.format_notion_id(saved_page_id).replace("-", "")
                    existing_notion_url = f"https://www.notion.so/{clean_pid}"
                elif selected_course_page_id:
                    clean_pid = notion_helper.format_notion_id(selected_course_page_id).replace("-", "")
                    existing_notion_url = f"https://www.notion.so/{clean_pid}"
            else:
                existing_notion_url = st.session_state.get("notion_page_url")

    st.session_state.already_processed = already_processed

    if 'chk_do_transcript' not in st.session_state:
        st.session_state.chk_do_transcript = True
    if 'chk_do_markdown_notion' not in st.session_state:
        st.session_state.chk_do_markdown_notion = True
    if 'chk_do_latex' not in st.session_state:
        st.session_state.chk_do_latex = False

    is_reprocess = st.session_state.get("chk_force_reprocess", False)
    is_new_processing = (not already_processed) or is_reprocess
    is_latex = st.session_state.get("chk_do_latex", False)
    is_markdown = st.session_state.get("chk_do_markdown_notion", False)

    is_markdown_disabled = is_new_processing and is_latex
    is_transcript_disabled = is_new_processing and (is_latex or is_markdown)

    if is_markdown_disabled:
        st.session_state.chk_do_markdown_notion = True
    if is_transcript_disabled:
        st.session_state.chk_do_transcript = True

    st.markdown("### 🎯 Seleziona cosa vuoi generare:")
    col_out1, col_out2, col_out3 = st.columns(3)

    with col_out1:
        do_transcript = st.checkbox("📝 Trascrizione Grezza", key="chk_do_transcript", disabled=is_transcript_disabled, help="Estrai il testo originale dal video Vimeo")
    with col_out2:
        do_markdown_notion = st.checkbox("📚 Appunti Markdown & Export Notion", key="chk_do_markdown_notion", on_change=sync_latex_reprocess_checkboxes, disabled=is_markdown_disabled, help="Genera appunti formattati con Gemini e salvali su Notion")
    with col_out3:
        do_latex = st.checkbox("📄 Codice LaTeX", key="chk_do_latex", on_change=sync_latex_reprocess_checkboxes, help="Converti gli appunti Markdown in codice LaTeX per la stampa")

    if do_markdown_notion or do_latex:
        with st.expander("🧠 Configurazione Prompt Gemini", expanded=False):
            prompt_mode = st.radio(
                "Origine Prompt:",
                ["Standard Generico", "Prompt Salvato (Supabase)", "Personalizzato / Nuovo"],
                horizontal=True
            )

            final_prompt = DEFAULT_PROMPT
            if prompt_mode == "Standard Generico":
                st.text_area("Prompt in uso:", DEFAULT_PROMPT, height=150, disabled=True)
            
            elif prompt_mode == "Prompt Salvato (Supabase)":
                saved_prompts = supabase_client.get_saved_prompts()
                if saved_prompts:
                    prompt_options = {p.get("title", f"Prompt #{p.get('id')}"): p.get("prompt_text") for p in saved_prompts}
                    chosen_title = st.selectbox("Seleziona prompt salvato:", list(prompt_options.keys()))
                    final_prompt = prompt_options[chosen_title]
                    st.text_area("Testo del prompt selezionato:", final_prompt, height=150)
                else:
                    st.warning("Nessun prompt salvato trovato su Supabase. Verrà usato il prompt standard.")
                    final_prompt = DEFAULT_PROMPT

            elif prompt_mode == "Personalizzato / Nuovo":
                final_prompt = st.text_area("Scrivi il tuo prompt personalizzato:", value=DEFAULT_PROMPT, height=200)
                
                c_save1, c_save2 = st.columns([3, 1])
                with c_save1:
                    new_title = st.text_input("Titolo del nuovo prompt (per salvataggio futuro):", placeholder="Es. Appunti Formato Sintetico")
                with c_save2:
                    st.write("")
                    st.write("") 
                    if st.button("💾 Salva in Supabase"):
                        if new_title and final_prompt:
                            success_sp, err_sp = supabase_client.save_prompt(new_title, final_prompt)
                            if success_sp:
                                st.success("Prompt salvato con successo su Supabase!")
                            else:
                                st.error(f"Errore: {err_sp}")
                        else:
                            st.warning("Inserisci sia un titolo che un testo per il prompt.")

    force_reprocess = False
    if already_processed:
        col_warn_msg, col_warn_btn = st.columns([3, 1])
        with col_warn_msg:
            st.warning(f"⚠️ **Attenzione:** Questa lezione (Video ID: `{video_id_preview}`) risulta già elaborata il **{record.get('created_at', '')[:10]}** per il corso **{record.get('course')}**.")
        with col_warn_btn:
            if existing_notion_url:
                st.write("")
                st.link_button("📖 Apri su Notion", existing_notion_url, use_container_width=True)

        force_reprocess = st.checkbox(
            "🔄 Elabora ed esporta comunque (creando una nuova versione)",
            value=False,
            key="chk_force_reprocess",
            on_change=sync_latex_reprocess_checkboxes
        )

        if saved_page_id and not force_reprocess and not st.session_state.appunti_generati and not st.session_state.get("_active_loaded_lesson_id"):
            with st.spinner("Recupero appunti e trascrizioni in corso..."):
                fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
                if fetched_notes:
                    add_note_version(fetched_notes)
                    st.session_state._last_saved_notion_notes = fetched_notes
                    st.session_state.notion_status = "💡 Appunti esistenti caricati automaticamente da Notion!"
                    st.session_state.notion_page_url = existing_notion_url
                    st.session_state._should_scroll_to_results = True
                    
                    if not st.session_state.testo_estratto:
                        all_videos = cached_get_all_lesson_videos(
                            video_id=video_id_preview,
                            course=record.get("course") if record else selected_course,
                            lesson_date=record.get("lesson_date") if record else formatted_date_str,
                            notion_page_id=saved_page_id
                        )
                        video_urls = [v.get("url") for v in all_videos if v.get("url")]
                        if url and url not in video_urls:
                            video_urls.append(url)
                        if video_urls:
                            _, agg_tr = fetch_aggregated_transcript(video_urls)
                            st.session_state.testo_estratto = agg_tr
                        elif url:
                            success_tr, text_tr, _ = download_and_process(url)
                            if success_tr:
                                st.session_state.testo_estratto = text_tr

    st.session_state.already_processed = already_processed
    st.divider()

    is_latex_only_existing = already_processed and not force_reprocess and do_latex
    can_start = url and (do_transcript or do_markdown_notion or do_latex) and (not already_processed or force_reprocess or is_latex_only_existing)

    if st.button("🚀 Avvia Elaborazione", type="primary", disabled=not can_start or is_notion_saving_active()):
        if already_processed and not force_reprocess and not do_latex:
            st.error("⛔ Elaborazione bloccata: questa lezione è già stata inserita nel database. Spunta 'Elabora ed esporta comunque' se vuoi rielaborarla.")
            st.stop()

        if (do_markdown_notion or do_latex) and not os.getenv("GOOGLE_API_KEY"):
            st.error("⚠️ Inserisci la Google API Key prima di procedere con Gemini.")
        elif do_markdown_notion and not selected_course_page_id:
            st.error("⚠️ Specifica l'ID della pagina Notion 'Corsi' nel file .env (NOTION_CORSI_PAGE_ID).")
        else:
            if force_reprocess or not already_processed:
                st.session_state.testo_estratto = None
                st.session_state.appunti_generati = None
                st.session_state.notes_versions = []
                st.session_state.current_version_index = 0
                st.session_state.latex_generato = None
                st.session_state.notion_status = None
                st.session_state.notion_page_url = None
                st.session_state.canvas_chat_history = []
                st.session_state._last_saved_notion_notes = None

            with st.status("🚀 Avvio elaborazione...", expanded=True) as status:
                if already_processed and not force_reprocess and do_latex:
                    status.update(label="📄 Caricamento appunti esistenti da Notion e conversione in LaTeX...")
                    if saved_page_id and not st.session_state.appunti_generati:
                        fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
                        if fetched_notes:
                            add_note_version(fetched_notes)
                            st.session_state._last_saved_notion_notes = fetched_notes
                            st.session_state._should_scroll_to_results = True
                            
                    if not st.session_state.testo_estratto:
                        status.update(label="📄 Caricamento appunti da Notion ed estrazione trascrizioni in corso...")
                        all_videos = cached_get_all_lesson_videos(
                            video_id=video_id_preview,
                            course=record.get("course") if record else selected_course,
                            lesson_date=record.get("lesson_date") if record else formatted_date_str,
                            notion_page_id=saved_page_id
                        )
                        video_urls = [v.get("url") for v in all_videos if v.get("url")]
                        if url and url not in video_urls:
                            video_urls.append(url)
                        if video_urls:
                            _, agg_tr = fetch_aggregated_transcript(video_urls)
                            st.session_state.testo_estratto = agg_tr
                        elif url:
                            success_tr, text_tr, _ = download_and_process(url)
                            if success_tr:
                                st.session_state.testo_estratto = text_tr

                    if st.session_state.appunti_generati:
                        success_lat, latex_gen = generate_latex(st.session_state.appunti_generati, model_name=MODEL_GENERAL)
                        if success_lat:
                            st.session_state.latex_generato = latex_gen
                            st.write("✅ Codice LaTeX generato con successo dagli appunti di Notion!")
                        else:
                            st.error(f"Errore durante la generazione LaTeX: {latex_gen}")
                    else:
                        st.error("Impossibile recuperare gli appunti da Notion per generare il codice LaTeX.")
                else:
                    if do_transcript:
                        status.update(label="📝 Estrazione trascrizione da Vimeo in corso...")
                        success_tr, text_tr, _ = download_and_process(url)
                        if success_tr:
                            st.session_state.testo_estratto = text_tr
                            st.write("✅ Trascrizione estratta con successo!")
                        else:
                            st.error(f"Errore trascrizione: {text_tr}")
                            status.update(label="❌ Errore durante la trascrizione", state="error")
                            st.stop()

                    if do_markdown_notion and st.session_state.testo_estratto:
                        status.update(label="🧠 Generazione appunti formattati con Gemini in corso...")
                        success_gen, notes_gen = generate_notes(st.session_state.testo_estratto, custom_prompt=final_prompt, model_name=MODEL_NOTES)
                        if success_gen:
                            add_note_version(notes_gen)
                            st.write("✅ Appunti Markdown generati con successo!")
                        else:
                            st.error(f"Errore Gemini: {notes_gen}")
                            status.update(label="❌ Errore durante la generazione appunti", state="error")
                            st.stop()

                    if do_latex and st.session_state.appunti_generati:
                        status.update(label="📄 Conversione appunti in codice LaTeX in corso...")
                        success_lat, latex_gen = generate_latex(st.session_state.appunti_generati, model_name=MODEL_GENERAL)
                        if success_lat:
                            st.session_state.latex_generato = latex_gen
                            st.write("✅ Codice LaTeX generato con successo!")
                        else:
                            st.error(f"Errore LaTeX: {latex_gen}")

                    if do_markdown_notion and st.session_state.appunti_generati and notion_token and selected_course_page_id:
                        status.update(label="📤 Salvataggio ed esportazione appunti su Notion...")
                        v_id, _ = extract_vimeo_ids(url)
                        active_target_pid = st.session_state.get("current_notion_page_id")
                        
                        # Se è stata caricata una specifica pagina Notion e non è stato richiesto di forzare una nuova versione:
                        if active_target_pid and not force_reprocess:
                            success_notion, err_upd = notion_helper.update_notion_page_in_place(
                                active_target_pid,
                                st.session_state.appunti_generati,
                                api_key=notion_token
                            )
                            msg_notion = "Pagina Notion aggiornata con successo!" if success_notion else (err_upd or "Errore aggiornamento Notion")
                            notion_page_id = active_target_pid if success_notion else None
                        else:
                            success_notion, msg_notion, notion_page_id = export_to_notion(
                                course_name=selected_course,
                                course_page_id=selected_course_page_id,
                                lesson_date_str=formatted_date_str,
                                markdown_text=st.session_state.appunti_generati,
                                is_same_video=already_processed,
                                api_key=notion_token
                            )

                        if not success_notion:
                            st.warning(f"⚠️ Avviso Notion: {msg_notion}")
                            st.session_state.notion_status = f"Errore Notion: {msg_notion}"
                        else:
                            st.session_state.current_notion_page_id = notion_page_id
                            st.session_state.notion_status = f"✅ {msg_notion}"
                            st.write(f"✅ {msg_notion}")
                            st.session_state._last_saved_notion_notes = st.session_state.appunti_generati

                            if notion_page_id:
                                clean_pid = notion_helper.format_notion_id(notion_page_id).replace("-", "")
                                st.session_state.notion_page_url = f"https://www.notion.so/{clean_pid}"

                            if v_id:
                                supabase_client.save_processed_lesson(
                                    video_id=v_id,
                                    url=url,
                                    course=selected_course,
                                    lesson_date=formatted_date_str,
                                    notion_page_id=notion_page_id
                                )
                                try:
                                    cached_is_video_processed.clear()
                                    cached_get_all_lesson_videos.clear()
                                    cached_get_notion_page_markdown.clear()
                                except Exception:
                                    pass

                            # Se gli appunti sono stati aggiunti o accodati, aggiorna il Canvas con la versione completa di Notion
                            # e recupera le trascrizioni aggregate di tutti i video associati alla lezione del giorno
                            if notion_page_id:
                                full_notes = notion_helper.get_notion_page_markdown(notion_page_id, api_key=notion_token)
                                if full_notes:
                                    st.session_state.appunti_generati = full_notes
                                    st.session_state._last_valid_appunti = full_notes
                                    st.session_state._last_saved_notion_notes = full_notes
                                    if len(st.session_state.notes_versions) > 0:
                                        st.session_state.notes_versions[st.session_state.current_version_index] = full_notes
                                    else:
                                        add_note_version(full_notes)

                                all_videos = supabase_client.get_all_lesson_videos(
                                    video_id=v_id,
                                    course=selected_course,
                                    lesson_date=formatted_date_str,
                                    notion_page_id=notion_page_id
                                )
                                video_urls = [v.get("url") for v in all_videos if v.get("url")]
                                if url not in video_urls:
                                    video_urls.append(url)
                                if len(video_urls) > 1:
                                    _, agg_tr = fetch_aggregated_transcript(video_urls)
                                    st.session_state.testo_estratto = agg_tr

                status.update(label="🎉 Elaborazione completata!", state="complete", expanded=False)

    if st.session_state.testo_estratto or st.session_state.appunti_generati:
        st.markdown('<div id="selezione-appunti-trascrizione" style="scroll-margin-top: 30px;"></div>', unsafe_allow_html=True)
        if st.session_state.get("_should_scroll_to_results", False):
            inject_scroll_to_results()
            st.session_state._should_scroll_to_results = False

        st.write("")
        render_active_background_operations_banner()
        is_saving = is_notion_saving_active()

        if st.session_state.notion_status:
            c_status, c_btn = st.columns([3, 1])
            with c_status:
                st.success(st.session_state.notion_status)
            with c_btn:
                if st.session_state.notion_page_url:
                    st.link_button("📖 Apri Lezione su Notion", st.session_state.notion_page_url, use_container_width=True)

        tabs_to_show = []
        if st.session_state.appunti_generati:
            tabs_to_show.append("📚 Appunti (Markdown)")
        if st.session_state.latex_generato:
            tabs_to_show.append("📄 Codice LaTeX")
        if st.session_state.testo_estratto:
            tabs_to_show.append("📝 Trascrizione Grezza")

        if tabs_to_show:
            tabs = st.tabs(tabs_to_show)

            for i, tab_name in enumerate(tabs_to_show):
                with tabs[i]:
                    if "Appunti" in tab_name:
                        col_versions, col_actions = st.columns([1.8, 2.2])
                        with col_versions:
                            render_version_navigation_bar("main_tab")
                        with col_actions:
                            btn_c1, btn_c2, btn_c3 = st.columns([1, 1, 1])
                            with btn_c1:
                                if st.button("🎨 Studio Canvas", type="primary", use_container_width=True, key="btn_open_canvas_chat"):
                                    st.session_state.show_canvas_chat = True
                                    st.rerun()
                            with btn_c2:
                                if st.button("📄 Rigenera LaTeX", use_container_width=True, key="btn_regen_latex_standard", disabled=is_latex_regen_active()):
                                    trigger_background_latex_regen()
                            with btn_c3:
                                render_notion_save_button_tab()

                        st.divider()

                        appunti_container = st.container(border=False)
                        with appunti_container:
                            cleaned_render = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati, default_width="30%")
                            st.markdown(cleaned_render + "\n\n<div style='height: 140px;'></div>", unsafe_allow_html=True)

                        st.divider()
                        
                        # Iniezione script JavaScript per Ctrl+V e Drag & Drop immagini (Home Page)
                        inject_image_paste_drop_js()

                        c1, c2 = st.columns([1, 4])
                        with c1:
                            st.download_button("💾 Scarica .md", st.session_state.appunti_generati, f"appunti_{formatted_date_str.replace('/', '_')}.md")
                        with c2:
                            st_copy_to_clipboard(st.session_state.appunti_generati, "📋 Copia Markdown")

                    elif "LaTeX" in tab_name:
                        latex_view_mode = st.radio(
                            "Modalità visualizzazione:",
                            ["👁️ Anteprima Codice", "✏️ Modifica LaTeX"],
                            horizontal=True,
                            key="standard_latex_view_radio"
                        )
                        st.divider()

                        if latex_view_mode == "✏️ Modifica LaTeX":
                            edited_latex_homepage = st.text_area(
                                "Modifica liberamente il codice LaTeX dell'intera pagina:",
                                value=st.session_state.latex_generato if st.session_state.latex_generato else "",
                                height=550,
                                key="latex_editor_area_homepage"
                            )
                            st.session_state.latex_generato = edited_latex_homepage
                        else:
                            if "latex_editor_area_homepage" in st.session_state and st.session_state.latex_editor_area_homepage:
                                st.session_state.latex_generato = st.session_state.latex_editor_area_homepage
                            st.code(st.session_state.latex_generato, language="latex")

                        st.divider()
                        c3, c4 = st.columns([1, 4])
                        with c3:
                            st.download_button("💾 Scarica .tex", st.session_state.latex_generato, f"appunti_{formatted_date_str.replace('/', '_')}.tex")
                        with c4:
                            st_copy_to_clipboard(st.session_state.latex_generato, "📋 Copia LaTeX")

                    elif "Trascrizione" in tab_name:
                        st.text_area("Testo completo trascritto:", st.session_state.testo_estratto, height=500)
                        st.divider()
                        c5, c6 = st.columns([1, 4])
                        with c5:
                            st.download_button("💾 Scarica .txt", st.session_state.testo_estratto, f"trascrizione_{formatted_date_str.replace('/', '_')}.txt")
                        with c6:
                            st_copy_to_clipboard(st.session_state.testo_estratto, "📋 Copia Trascrizione")
