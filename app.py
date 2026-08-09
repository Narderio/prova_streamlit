import streamlit as st
import os
import time
import datetime
import threading
import importlib
from dotenv import load_dotenv

import backend
import notion_helper
import supabase_client
from streamlit.runtime.scriptrunner import add_script_run_ctx

# Ricarica dinamica moduli per garantire che le modifiche al codice backend siano sempre applicate
importlib.reload(backend)
importlib.reload(notion_helper)
importlib.reload(supabase_client)

from backend import download_and_process, generate_notes, generate_latex, export_to_notion, extract_vimeo_ids, agent_edit_notes, agent_edit_notes_stream, DEFAULT_PROMPT

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
    if 'notes_versions' not in st.session_state or not isinstance(st.session_state.notes_versions, list):
        st.session_state.notes_versions = []
    
    st.session_state.notes_versions.append(new_notes)
    new_idx = len(st.session_state.notes_versions) - 1
    st.session_state.current_version_index = new_idx
    st.session_state.appunti_generati = new_notes
    st.session_state._last_valid_appunti = new_notes
    safe_set_session_state("markdown_editor_area", new_notes)
    safe_set_session_state("markdown_editor_area_canvas", new_notes)
    st.session_state._version_just_switched = True

    new_ver_str = str(new_idx + 1)
    safe_sync_version_tabs(new_ver_str)

def switch_note_version(target_index):
    """Cambia la versione attiva degli appunti."""
    if 'notes_versions' in st.session_state and 0 <= target_index < len(st.session_state.notes_versions):
        st.session_state.current_version_index = target_index
        selected_notes = st.session_state.notes_versions[target_index]
        st.session_state.appunti_generati = selected_notes
        st.session_state._last_valid_appunti = selected_notes
        safe_set_session_state("markdown_editor_area", selected_notes)
        safe_set_session_state("markdown_editor_area_canvas", selected_notes)
        st.session_state._version_just_switched = True

        new_ver_str = str(target_index + 1)
        safe_sync_version_tabs(new_ver_str)

def update_appunti_from_editor():
    updated_val = None
    if "markdown_editor_area" in st.session_state and st.session_state.markdown_editor_area:
        updated_val = st.session_state.markdown_editor_area
    elif "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
        updated_val = st.session_state.markdown_editor_area_canvas
    
    if updated_val:
        st.session_state.appunti_generati = updated_val
        st.session_state._last_valid_appunti = updated_val
        if 'notes_versions' in st.session_state and st.session_state.notes_versions:
            idx = st.session_state.get('current_version_index', 0)
            if 0 <= idx < len(st.session_state.notes_versions):
                st.session_state.notes_versions[idx] = updated_val

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
        
        # Sincronizza la chiave del widget solo se non esiste, non è tra le opzioni o se c'è stato un cambio programmatico di versione
        if widget_key not in st.session_state or st.session_state[widget_key] not in options or st.session_state.get("_version_just_switched", False):
            safe_set_session_state(widget_key, target_str)
            st.session_state._version_just_switched = False

        selected_v = st.segmented_control(
            "Versione",
            options=options,
            selection_mode="single",
            required=True,
            label_visibility="collapsed",
            key=widget_key
        )
        
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
def cached_is_video_processed(v_id):
    return supabase_client.is_video_processed(v_id)

@st.cache_data(ttl=600, show_spinner=False)
def cached_get_notion_page_markdown(page_id, token):
    return notion_helper.get_notion_page_markdown(page_id, api_key=token)

# --- INIZIO INTERFACCIA STREAMLIT ---
st.set_page_config(page_title="Vimeo to Notion University Notes", page_icon="🎓", layout="wide")

# --- SIDEBAR CONFIGURAZIONE ---
st.sidebar.title("⚙️ Configurazione")

# 1. Google API Key (senza mai esporre la chiave in .env nell'interfaccia)
env_key = os.getenv("GOOGLE_API_KEY")
if env_key:
    st.sidebar.success("🟢 Google API Key caricata da .env")
    api_key_override = st.sidebar.text_input("Sovrascrivi API Key (opzionale)", type="password", help="Lascia vuoto per usare la chiave in .env")
    if api_key_override.strip():
        os.environ["GOOGLE_API_KEY"] = api_key_override.strip()
else:
    user_api_key = st.sidebar.text_input("Google API Key", type="password", help="Inserisci la tua chiave API per Gemini")
    if user_api_key.strip():
        os.environ["GOOGLE_API_KEY"] = user_api_key.strip()

st.sidebar.divider()

# 2. Selezione Modello Gemini (Default: gemini-3.5-flash-lite)
# --- TITOLO E CONFIGURAZIONE SIDEBAR ---
AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash"
]
selected_model = st.sidebar.selectbox("Scegli il modello:", AVAILABLE_MODELS, index=0)

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
if 'saved_lesson_date' not in st.session_state:
    st.session_state.saved_lesson_date = datetime.date.today()

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

def trigger_background_latex_regen(selected_model):
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
            
    t = threading.Thread(target=_worker, args=(notes_snap, selected_model), daemon=True)
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

    target_pid = st.session_state.get("current_notion_page_id")
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

    if target_pid:
        markdown_snapshot = str(st.session_state.appunti_generati)
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
        if st.button("📤 Salva su Notion", use_container_width=True, key="btn_save_edited_notion"):
            save_current_notes_to_notion()

if 'canvas_ratio_mode' not in st.session_state:
    st.session_state.canvas_ratio_mode = "Canvas XXL"
if 'canvas_width_pct' not in st.session_state:
    st.session_state.canvas_width_pct = 55

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
    st.markdown("""
        <style>
            /* Sfondo Grigio Dark ChatGPT #212121 - Blocco Rigido dello Scroll Globale della Finestra */
            html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main {
                background-color: #212121 !important;
                color: #ffffff !important;
                overflow: hidden !important;
                height: 100vh !important;
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
                padding-top: 0px !important;
                padding-bottom: 0.5rem !important;
                padding-left: 1rem !important;
                padding-right: 1rem !important;
                max-width: 100% !important;
                height: 100vh !important;
                overflow: hidden !important;
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

            /* CONTENITORE PRINCIPALE A 3 COLONNE CON AMPIO SPAZIO SUPERIORE */
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"],
            .main .block-container > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"],
            .main .block-container div[data-testid="stHorizontalBlock"]:first-of-type {
                flex-wrap: nowrap !important;
                height: calc(100vh - 25px) !important;
                overflow: visible !important;
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
                padding-bottom: 80px !important; /* Spazio per la barra custom */
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

            /* 2. PANNELLO CANVAS (Destra) - ALTEZZA REGOLATA PER VISIBILITÀ COMPLETA */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) {
                background-color: #1a1a1a !important;
                border-radius: 16px !important;
                padding: 0.8rem 1.2rem 2rem 1.2rem !important;
                border: 1px solid #333333 !important;
                box-shadow: 0 4px 25px rgba(0,0,0,0.5) !important;
                height: calc(100vh - 35px) !important;
                max-height: calc(100vh - 35px) !important;
                overflow-x: hidden !important;
                overflow-y: auto !important;
                display: flex !important;
                flex-direction: column !important;
                justify-content: flex-start !important;
            }

            /* 3. DIFFERENZIAZIONE VISIVA CHAT UTENTE VS ASSISTENTE AI */
            div[data-testid="stChatMessage"] {
                border-radius: 16px !important;
                padding: 0.8rem 1.1rem !important;
                margin-top: 8px !important;
                margin-bottom: 0.9rem !important;
                transition: all 0.2s ease !important;
                box-sizing: border-box !important;
            }

            /* MESSAGGIO UTENTE (Allineato a DESTRA con Bordo Azzurro) */
            div[data-testid="stChatMessage"]:has([data-testid*="user"]),
            div[data-testid="stChatMessage"]:has([data-testid*="User"]),
            div[data-testid="stChatMessage"][aria-label*="user"],
            div[data-testid="stChatMessage"][aria-label*="User"] {
                background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important;
                border: 1.5px solid #3b82f6 !important;
                border-right: 5px solid #60a5fa !important;
                border-radius: 16px 16px 4px 16px !important;
                box-shadow: 0 4px 14px rgba(59, 130, 246, 0.25) !important;
                margin-left: auto !important;
                margin-right: 0.4rem !important;
                max-width: 85% !important;
                width: fit-content !important;
            }

            /* MESSAGGIO ASSISTENTE AI (Allineato a SINISTRA con Bordo Smeraldo) */
            div[data-testid="stChatMessage"]:has([data-testid*="assistant"]),
            div[data-testid="stChatMessage"]:has([data-testid*="Assistant"]),
            div[data-testid="stChatMessage"][aria-label*="assistant"],
            div[data-testid="stChatMessage"][aria-label*="Assistant"] {
                background: linear-gradient(135deg, #262626 0%, #171717 100%) !important;
                border: 1.5px solid #383838 !important;
                border-left: 5px solid #10b981 !important;
                border-radius: 16px 16px 16px 4px !important;
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4) !important;
                margin-left: 0.4rem !important;
                margin-right: auto !important;
                max-width: 88% !important;
                width: fit-content !important;
            }

            div[data-testid="stChatMessage"] > div {
                background-color: transparent !important;
                border: none !important;
            }

            /* 4. RESET TOTALE PER L'HEADER DEL CANVAS (prima riga con i pulsanti) */
            /* Allineamento verticale centrato per la riga header */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) > div > div > div[data-testid="stHorizontalBlock"]:first-of-type {
                align-items: center !important;
                max-height: 48px !important;
                min-height: 0 !important;
                height: auto !important;
                background-color: transparent !important;
                background: transparent !important;
                box-shadow: none !important;
                border: 0px none transparent !important;
                outline: none !important;
                overflow: visible !important;
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
            trigger_notion_save = st.button("📤", help="Salva su Notion", key="btn_save_notion_icon")
        with b4:
            trigger_back = st.button("🔙", help="Torna al Form", key="btn_close_canvas_icon")

        if trigger_edit_toggle:
            st.session_state.canvas_edit_mode_toggle = not st.session_state.canvas_edit_mode_toggle
            st.rerun()
        if trigger_back:
            st.session_state.show_canvas_chat = False
            st.rerun()

        if trigger_notion_save:
            save_current_notes_to_notion()

        if trigger_latex_regen:
            trigger_background_latex_regen(selected_model)

        # PROTEZIONE ANTI-SPARIZIONE APPUNTI: Sincronizza ed esegui il fallback sul backup protetto
        if st.session_state.get("canvas_edit_mode_toggle") and "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
            st.session_state.appunti_generati = st.session_state.markdown_editor_area_canvas

        if (not st.session_state.appunti_generati or not str(st.session_state.appunti_generati).strip()) and st.session_state.get("_last_valid_appunti"):
            st.session_state.appunti_generati = st.session_state._last_valid_appunti

        if st.session_state.appunti_generati and len(str(st.session_state.appunti_generati).strip()) > 0:
            st.session_state._last_valid_appunti = st.session_state.appunti_generati

        # NOTIFICA CONTINUA ANIMATA PER OPERAZIONI IN BACKGROUND (NOTION & LATEX)
        render_active_background_operations_banner()

        if st.session_state.latex_generato:
            tab_canvas_md, tab_canvas_lat = st.tabs(["📚 Appunti (Markdown)", "📄 Codice LaTeX"])
            with tab_canvas_md:
                render_version_navigation_bar("canvas_tab_md")
                st.markdown("<div style='margin-bottom: 4px;'></div>", unsafe_allow_html=True)
                cleaned_render_canvas = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati or "").strip()

                canvas_scroll_area_md = st.container(border=False)
                with canvas_scroll_area_md:
                    if st.session_state.canvas_edit_mode_toggle:
                        edited_text_canvas = st.text_area(
                            "Modifica direttamente il testo nel Canvas:",
                            value=st.session_state.appunti_generati,
                            height=520,
                            key="markdown_editor_area_canvas",
                            on_change=update_appunti_from_editor
                        )
                        st.session_state.appunti_generati = edited_text_canvas
                    else:
                        canvas_placeholder = st.empty()
                        canvas_placeholder.markdown(cleaned_render_canvas)

            with tab_canvas_lat:
                canvas_scroll_area_lat = st.container(border=False)
                with canvas_scroll_area_lat:
                    if st.session_state.canvas_edit_mode_toggle:
                        edited_latex_canvas = st.text_area(
                            "Modifica direttamente il codice LaTeX nel Canvas:",
                            value=st.session_state.latex_generato if st.session_state.latex_generato else "",
                            height=560,
                            key="latex_editor_area_canvas"
                        )
                        st.session_state.latex_generato = edited_latex_canvas
                    else:
                        st.code(st.session_state.latex_generato, language="latex")
                        st.divider()
                        c_lat1, c_lat2 = st.columns([1, 4])
                        with c_lat1:
                            st.download_button("💾 Scarica .tex", st.session_state.latex_generato, f"appunti_{datetime.date.today().strftime('%d_%m_%Y')}.tex")
                        with c_lat2:
                            st_copy_to_clipboard(st.session_state.latex_generato, "📋 Copia LaTeX")
        else:
            render_version_navigation_bar("canvas_no_tab_md")
            st.markdown("<div style='margin-bottom: 4px;'></div>", unsafe_allow_html=True)
            cleaned_render_canvas = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati or "").strip()

            canvas_scroll_area = st.container(border=False)
            with canvas_scroll_area:
                if st.session_state.canvas_edit_mode_toggle:
                    edited_text_canvas = st.text_area(
                        "Modifica direttamente il testo nel Canvas:",
                        value=st.session_state.appunti_generati,
                        height=560,
                        key="markdown_editor_area_canvas",
                        on_change=update_appunti_from_editor
                    )
                    st.session_state.appunti_generati = edited_text_canvas
                else:
                    canvas_placeholder = st.empty()
                    canvas_placeholder.markdown(cleaned_render_canvas)

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

        # --- INIEZIONE JAVASCRIPT DRAGGABLE PER IL TASTO PILLOLA #drag-handle-pill-native ---
        draggable_handle_js = """
        <script>
        (function() {
            const pDoc = window.parent.document;
            let userIsNearBottom = true;

            function getTopLevelCols() {
                const handle = pDoc.getElementById('drag-handle-pill-native');
                if (!handle) return [];
                const handleCol = handle.closest('[data-testid="stColumn"]');
                if (!handleCol) return [];
                const studioBlock = handleCol.parentElement;
                if (!studioBlock) return [];
                return Array.from(studioBlock.children).filter(el => el.getAttribute('data-testid') === 'stColumn');
            }

            function getChatBox() {
                const cols = getTopLevelCols();
                const chatCol = cols[0];
                if (!chatCol) return null;
                return chatCol.querySelector('[data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]') ||
                       chatCol.querySelector('div[data-testid="stElementContainer"]') ||
                       chatCol;
            }

            function autoScrollChatToBottom(force) {
                const chatBox = getChatBox();
                if (!chatBox) return;

                if (force || userIsNearBottom) {
                    chatBox.scrollTop = chatBox.scrollHeight;
                    const spacer = pDoc.getElementById('chat-bottom-spacer');
                    if (spacer) {
                        spacer.scrollIntoView({ behavior: 'instant', block: 'end' });
                    }
                }
            }

            function setupScrollListener() {
                const chatBox = getChatBox();
                if (!chatBox || chatBox.getAttribute('data-scroll-listener') === 'true') return;
                
                chatBox.setAttribute('data-scroll-listener', 'true');
                chatBox.addEventListener('scroll', function() {
                    const distanceToBottom = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight;
                    userIsNearBottom = (distanceToBottom <= 140);
                });
            }

            function initChatMutationObserver() {
                const cols = getTopLevelCols();
                const chatCol = cols[0];
                if (!chatCol || chatCol.getAttribute('data-scroll-observer') === 'true') return;
                
                chatCol.setAttribute('data-scroll-observer', 'true');
                setupScrollListener();
                
                const observer = new MutationObserver(function() {
                    setupScrollListener();
                    if (userIsNearBottom) {
                        autoScrollChatToBottom(false);
                    }
                });
                
                observer.observe(chatCol, {
                    childList: true,
                    subtree: true,
                    characterData: true
                });
            }

            function syncChatInputPos() {
                const cols = getTopLevelCols();
                const leftCol = cols[0];
                const chatInput = pDoc.querySelector('div[data-testid="stChatInput"]');
                if (leftCol && chatInput) {
                    const rect = leftCol.getBoundingClientRect();
                    chatInput.style.position = 'fixed';
                    chatInput.style.bottom = '15px';
                    chatInput.style.left = (rect.left + 5) + 'px';
                    chatInput.style.width = Math.max(180, rect.width - 25) + 'px';
                    chatInput.style.zIndex = '99999';
                }
            }

            function initPillDrag() {
                const pill = pDoc.getElementById('drag-handle-pill-native');
                syncChatInputPos();
                initChatMutationObserver();
                autoScrollChatToBottom(true);
                
                if (!pill || pill.getAttribute('data-drag-bound') === 'true') return;
                pill.setAttribute('data-drag-bound', 'true');
                
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
                
                pill.addEventListener('mouseenter', function() {
                    if (!isDragging) {
                        pill.style.backgroundColor = '#38bdf8';
                        pill.style.borderColor = '#38bdf8';
                    }
                });
                pill.addEventListener('mouseleave', function() {
                    if (!isDragging) {
                        pill.style.backgroundColor = '#2b2b2b';
                        pill.style.borderColor = '#444444';
                    }
                });
                
                pill.addEventListener('mousedown', function(e) {
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
                });
                
                pDoc.addEventListener('mousemove', function(e) {
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
                });
                
                pDoc.addEventListener('mouseup', function() {
                    if (isDragging) {
                        isDragging = false;
                        pDoc.body.style.cursor = 'default';
                        pDoc.body.style.userSelect = 'auto';
                        pill.style.backgroundColor = '#2b2b2b';
                        pill.style.borderColor = '#444444';
                        pill.style.transform = 'scale(1.0)';
                    }
                });
            }
            
            setTimeout(initPillDrag, 20);
            setTimeout(initChatMutationObserver, 50);
            setTimeout(function() { autoScrollChatToBottom(true); }, 80);
            
            setInterval(function() {
                syncChatInputPos();
                setupScrollListener();
            }, 150);
            
            window.addEventListener('resize', syncChatInputPos);
        })();
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
                    else:
                        st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                    st.markdown(msg["content"])

            if st.session_state.pending_agent_stream:
                stream_container = st.empty()
                chat_response_placeholder = None
                chat_bubble_created = False
                full_raw_response = ""
                try:
                    last_user_prompt = st.session_state.canvas_chat_history[-1]["content"]
                    current_ctx_idx = st.session_state.get("current_version_index", 0)
                    if st.session_state.get("stream_version_created", False) and current_ctx_idx > 0 and len(st.session_state.get("notes_versions", [])) >= current_ctx_idx:
                        base_markdown = st.session_state.notes_versions[current_ctx_idx - 1]
                    else:
                        base_markdown = st.session_state.appunti_generati or ""

                    stream_gen = agent_edit_notes_stream(
                        current_markdown=base_markdown,
                        user_instruction=last_user_prompt,
                        chat_history=st.session_state.canvas_chat_history[:-1],
                        raw_transcript=st.session_state.testo_estratto,
                        model_name=selected_model
                    )

                    for chunk_text in stream_gen:
                        if not chat_bubble_created:
                            chat_bubble_created = True
                            with stream_container.container():
                                with st.chat_message("assistant"):
                                    st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                    chat_response_placeholder = st.empty()

                        full_raw_response += chunk_text
                        if "<<<UPDATED_CANVAS>>>" in full_raw_response:
                            parts = full_raw_response.split("<<<UPDATED_CANVAS>>>")
                            chat_part = parts[0].replace("<<<CHAT_RESPONSE>>>", "").strip()
                            canvas_part = parts[1].strip()
                            if chat_response_placeholder:
                                chat_response_placeholder.markdown(chat_part)
                            if canvas_part.upper() != "NO_CHANGE" and len(canvas_part) > 5:
                                cleaned_live_canvas = notion_helper.clean_markdown_for_streamlit(canvas_part)
                                if not st.session_state.get("stream_version_created", False):
                                    add_note_version(cleaned_live_canvas)
                                    st.session_state.stream_version_created = True
                                    st.session_state.stream_target_version_index = st.session_state.current_version_index
                                else:
                                    target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                                    if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                        st.session_state.notes_versions[target_idx] = cleaned_live_canvas
                                    
                                    if st.session_state.get("current_version_index") == target_idx:
                                        st.session_state.appunti_generati = cleaned_live_canvas
                                        st.session_state._last_valid_appunti = cleaned_live_canvas
                                        safe_set_session_state("markdown_editor_area", cleaned_live_canvas)
                                        safe_set_session_state("markdown_editor_area_canvas", cleaned_live_canvas)
                                        if canvas_placeholder is not None:
                                            canvas_placeholder.markdown(cleaned_live_canvas)
                        else:
                            chat_part = full_raw_response.replace("<<<CHAT_RESPONSE>>>", "").strip()
                            if chat_response_placeholder:
                                chat_response_placeholder.markdown(chat_part)

                    # Controllo finale: se non ha emesso nulla, crea comunque la bolla
                    if not chat_bubble_created:
                        chat_bubble_created = True
                        with stream_container.container():
                            with st.chat_message("assistant"):
                                st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                                chat_response_placeholder = st.empty()

                    if "<<<CHAT_RESPONSE>>>" in full_raw_response and "<<<UPDATED_CANVAS>>>" in full_raw_response:
                        parts = full_raw_response.split("<<<UPDATED_CANVAS>>>")
                        final_chat_reply = parts[0].replace("<<<CHAT_RESPONSE>>>", "").strip()
                        raw_canvas = parts[1].strip()
                        if raw_canvas.upper() != "NO_CHANGE" and len(raw_canvas) > 5:
                            cleaned_canvas = notion_helper.clean_markdown_for_streamlit(raw_canvas)
                            target_idx = st.session_state.get("stream_target_version_index", st.session_state.current_version_index)
                            if 'notes_versions' in st.session_state and 0 <= target_idx < len(st.session_state.notes_versions):
                                st.session_state.notes_versions[target_idx] = cleaned_canvas
                            switch_note_version(target_idx)
                            st.toast(f"⚡ Canvas aggiornato alla Versione {target_idx + 1}!", icon="✅")
                        else:
                            if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                                st.session_state.notes_versions.pop()
                                switch_note_version(len(st.session_state.notes_versions) - 1)
                            st.toast("💬 Risposta fornita in chat.", icon="ℹ️")
                    else:
                        if st.session_state.get("stream_version_created", False) and len(st.session_state.get("notes_versions", [])) > 1:
                            st.session_state.notes_versions.pop()
                            switch_note_version(len(st.session_state.notes_versions) - 1)
                        final_chat_reply = full_raw_response.replace("<<<CHAT_RESPONSE>>>", "").strip() or "Risposta dell'assistente."
                        st.toast("💬 Risposta fornita in chat.", icon="ℹ️")
                        
                    st.session_state.stream_version_created = False
                    st.session_state.canvas_chat_history.append({"role": "assistant", "content": final_chat_reply})
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
            # Spaziatore inferiore di 30px per dare la giusta spaziatura in fondo all'ultimo messaggio
            st.markdown("<div id='chat-bottom-spacer' style='height: 30px; width: 100%;'></div>", unsafe_allow_html=True)

        # Campo chat_input nativo (nascosto visivamente da JS e usato come bridge per inviare i messaggi)
        user_input = st.chat_input("Chiedi all'Assistente AI di modificare il Canvas...")
        if user_input and not st.session_state.pending_agent_stream:
            st.session_state.canvas_chat_history.append({"role": "user", "content": user_input})
            st.session_state.pending_agent_stream = True
            st.session_state.stream_version_created = False
            st.rerun()


    # --- INIEZIONE BARRA CHAT PERSONALIZZATA STILE CHATGPT E GESTIONE PILLOLA #drag-handle-pill-native ---
    is_streaming_js = "true" if st.session_state.pending_agent_stream else "false"
    model_badge_text = f"✨ {selected_model.replace('gemini-', 'Gemini ').replace('-', ' ').title()}"
    draggable_handle_js = f"""
    <script>
    (function() {{
        const pDoc = window.parent.document;
        const pWin = pDoc.defaultView || window.parent;
        let userIsNearBottom = true;

        // FLAG STREAMING SUL PARENT WINDOW (condiviso tra tutti gli iframe)
        pWin.__isAgentStreaming = {is_streaming_js};

        // Legge il flag streaming dal DOM - usato SOLO per sbloccare più velocemente
        // dopo la fine dello streaming (mai per impostare true→false durante streaming)
        function readStreamingFlagFromDOM() {{
            const flagEl = pDoc.getElementById('streaming-state-flag');
            if (flagEl) {{
                const domSaysStreaming = (flagEl.getAttribute('data-streaming') === 'true');
                // Se il DOM dice "streaming", imposta true (sicuro)
                // Se il DOM dice "non streaming", imposta false SOLO se il flag era true
                // (sblocca dopo fine streaming)
                if (domSaysStreaming) {{
                    pWin.__isAgentStreaming = true;
                }} else {{
                    pWin.__isAgentStreaming = false;
                }}
            }}
            // Se il flag NON è nel DOM, NON toccare pWin.__isAgentStreaming
        }}

        // 1. Nasconde la barra nativa ma la lascia FUNZIONALE per click programmatici
        function hideNativeChatInput() {{
            const nativeInput = pDoc.querySelector('div[data-testid="stChatInput"]');
            if (nativeInput && nativeInput.getAttribute('data-custom-hidden') !== 'true') {{
                nativeInput.setAttribute('data-custom-hidden', 'true');
                nativeInput.style.cssText = 'position:fixed !important; bottom:0 !important; left:0 !important; width:1px !important; height:1px !important; opacity:0 !important; overflow:hidden !important; z-index:-1 !important; clip:rect(0,0,0,0) !important;';
            }}
        }}

        // 2. Crea o aggiorna la barra personalizzata stile ChatGPT
        function ensureCustomChatGPTBar() {{
            let bar = pDoc.getElementById('custom-chatgpt-bar');
            if (!bar) {{
                bar = pDoc.createElement('div');
                bar.id = 'custom-chatgpt-bar';
                bar.innerHTML = `
                    <style>
                        #custom-chatgpt-bar {{
                            position: fixed;
                            bottom: 15px;
                            background: #2f2f2f;
                            border: 1px solid #424242;
                            border-radius: 20px;
                            padding: 8px 12px 8px 16px;
                            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
                            z-index: 99999;
                            display: flex;
                            flex-direction: row;
                            align-items: flex-end;
                            gap: 12px;
                            box-sizing: border-box;
                            transition: border-color 0.2s, box-shadow 0.2s;
                        }}
                        #custom-chatgpt-bar:focus-within {{
                            border-color: #555555;
                            box-shadow: 0 8px 28px rgba(0,0,0,0.7);
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
                    </style>
                    <textarea id="custom-chatgpt-textarea" placeholder="Chiedi all'Assistente AI di modificare il Canvas..." rows="1"></textarea>
                    <button id="custom-chatgpt-send-btn" class="chatgpt-send-btn" title="Invia messaggio">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <line x1="12" y1="19" x2="12" y2="5"></line>
                            <polyline points="5 12 12 5 19 12"></polyline>
                        </svg>
                    </button>
                `;
                pDoc.body.appendChild(bar);
            }}

            const textarea = pDoc.getElementById('custom-chatgpt-textarea');
            const sendBtn = pDoc.getElementById('custom-chatgpt-send-btn');

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

                // Rende la chat nativa temporaneamente accessibile per il click
                const nativeInputContainer = pDoc.querySelector('div[data-testid="stChatInput"]');
                if (nativeInputContainer) {{
                    nativeInputContainer.style.cssText = 'position:fixed; bottom:0; left:0; width:1px; height:1px; opacity:0; overflow:hidden; z-index:-1;';
                    nativeInputContainer.removeAttribute('data-custom-hidden');
                }}
                const nativeTextarea = nativeInputContainer ? nativeInputContainer.querySelector('textarea') : null;
                const nativeButton = nativeInputContainer ? (nativeInputContainer.querySelector('button[data-testid="stChatInputSubmitButton"]') || nativeInputContainer.querySelector('button')) : null;

                if (nativeTextarea && nativeButton) {{
                    // React-compatible value setter
                    const nativeSetter = Object.getOwnPropertyDescriptor(pWin.HTMLTextAreaElement.prototype, "value").set;
                    nativeSetter.call(nativeTextarea, val);
                    nativeTextarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    nativeTextarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    
                    // Salva il testo nella custom bar per ripristino se necessario
                    textarea.value = '';
                    autoResizeTextarea();

                    // Simula l'invio premendo Enter direttamente sulla textarea nativa
                    nativeTextarea.dispatchEvent(new KeyboardEvent('keydown', {{
                        key: 'Enter',
                        code: 'Enter',
                        keyCode: 13,
                        which: 13,
                        bubbles: true,
                        cancelable: true
                    }}));

                    // Attende che React aggiorni lo stato interno prima di fare click
                    setTimeout(function() {{
                        // Rimuovi eventuali disabled dal pulsante nativo
                        nativeButton.disabled = false;
                        nativeButton.removeAttribute('disabled');
                        nativeButton.click();
                        
                        // Rimetti il focus sulla textarea custom dopo l'invio
                        setTimeout(function() {{
                            const ta = pDoc.getElementById('custom-chatgpt-textarea');
                            if (ta) ta.focus();
                        }}, 50);
                    }}, 150);
                }}
            }}

            // Assegna i listener UNA sola volta (data-events-bound).
            // I listener leggono pWin.__isAgentStreaming che è aggiornato dal parent window.
            if (textarea && !textarea.getAttribute('data-events-bound')) {{
                textarea.setAttribute('data-events-bound', 'true');
                textarea.addEventListener('input', function(e) {{
                    e.stopPropagation();
                    textarea.style.height = 'auto';
                    textarea.style.height = Math.min(120, textarea.scrollHeight) + 'px';
                    updateSendBtnState();
                }});
                // Blocca la propagazione di TUTTI gli eventi tastiera verso Streamlit
                // (altrimenti tasti come 'C' aprono "Clear caches", 'R' fa rerun, ecc.)
                textarea.addEventListener('keydown', function(e) {{
                    e.stopPropagation();
                    if (e.key === 'Enter' && !e.shiftKey) {{
                        e.preventDefault();
                        if (!pWin.__isAgentStreaming) {{
                            submitCustomMessage();
                        }}
                        // Se in streaming, non fa nulla: il testo resta nella textarea
                    }}
                }});
                textarea.addEventListener('keypress', function(e) {{ e.stopPropagation(); }});
                textarea.addEventListener('keyup', function(e) {{ e.stopPropagation(); }});
            }}

            if (sendBtn && !sendBtn.getAttribute('data-events-bound')) {{
                sendBtn.setAttribute('data-events-bound', 'true');
                sendBtn.addEventListener('click', function(e) {{
                    e.preventDefault();
                    if (!pWin.__isAgentStreaming) {{
                        submitCustomMessage();
                    }}
                }});
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

        function syncChatInputPos() {{
            hideNativeChatInput();
            ensureCustomChatGPTBar();
            const cols = getTopLevelCols();
            const leftCol = cols[0];
            const bar = pDoc.getElementById('custom-chatgpt-bar');
            if (leftCol && bar) {{
                const rect = leftCol.getBoundingClientRect();
                bar.style.left = (rect.left + 8) + 'px';
                bar.style.width = Math.max(200, rect.width - 20) + 'px';
            }}
        }}

        function getChatBox() {{
            const cols = getTopLevelCols();
            const chatCol = cols[0];
            if (!chatCol) return null;
            return chatCol.querySelector('[data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]') ||
                   chatCol.querySelector('div[data-testid="stElementContainer"]') ||
                   chatCol;
        }}

        function autoScrollChatToBottom(force) {{
            const chatBox = getChatBox();
            if (!chatBox) return;

            if (force || userIsNearBottom) {{
                chatBox.scrollTop = chatBox.scrollHeight;
                const spacer = pDoc.getElementById('chat-bottom-spacer');
                if (spacer) {{
                    spacer.scrollIntoView({{ behavior: 'instant', block: 'end' }});
                }}
            }}
        }}

        function setupScrollListener() {{
            const chatBox = getChatBox();
            if (!chatBox || chatBox.getAttribute('data-scroll-listener') === 'true') return;
            
            chatBox.setAttribute('data-scroll-listener', 'true');
            chatBox.addEventListener('scroll', function() {{
                const distanceToBottom = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight;
                userIsNearBottom = (distanceToBottom <= 140);
            }});
        }}

        function initChatMutationObserver() {{
            const cols = getTopLevelCols();
            const chatCol = cols[0];
            if (!chatCol || chatCol.getAttribute('data-scroll-observer') === 'true') return;
            
            chatCol.setAttribute('data-scroll-observer', 'true');
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
            
            if (!pill || pill.getAttribute('data-drag-bound') === 'true') return;
            pill.setAttribute('data-drag-bound', 'true');
            
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
            
            pDoc.addEventListener('mouseup', function() {{
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
        setTimeout(function() {{ autoScrollChatToBottom(true); }}, 80);
        
        setInterval(function() {{
            readStreamingFlagFromDOM();
            syncChatInputPos();
            setupScrollListener();
        }}, 150);
        
        window.addEventListener('resize', syncChatInputPos);
    }})();
    </script>
    """
    st.iframe(draggable_handle_js, height=1)

# ==============================================================================
# PAGINA PRINCIPALE: CONFIGURAZIONE FORM & GENERAZIONE
# ==============================================================================
else:
    # --- CLEANUP: Rimuove la chatbar personalizzata se l'utente torna alla home ---
    cleanup_js = """
    <script>
    (function() {
        const pDoc = window.parent.document;
        const bar = pDoc.getElementById('custom-chatgpt-bar');
        if (bar) {
            bar.remove();
        }
    })();
    </script>
    """
    import streamlit.components.v1 as components
    try:
        st.iframe(cleanup_js, height=1)
    except AttributeError:
        components.html(cleanup_js, height=1)

    st.title("🎓 Da Vimeo ad Appunti & Notion")
    st.markdown("Estrai la trascrizione dai video Vimeo, genera ed edita appunti universitari con Gemini e salvali direttamente su **Notion**.")

    col_left, col_right = st.columns([2, 1])

    notion_token = os.getenv("NOTION_API_KEY")
    notion_corsi_id = os.getenv("NOTION_CORSI_PAGE_ID")

    def update_saved_vimeo_url():
        if "vimeo_url_input" in st.session_state:
            st.session_state.saved_vimeo_url = st.session_state.vimeo_url_input

    def update_saved_lesson_date():
        if "lesson_date_input" in st.session_state:
            st.session_state.saved_lesson_date = st.session_state.lesson_date_input

    with col_left:
        url = st.text_input(
            "Link video Vimeo",
            value=st.session_state.saved_vimeo_url,
            placeholder="https://vimeo.com/123456789/hash...",
            key="vimeo_url_input",
            on_change=update_saved_vimeo_url
        )
        st.session_state.saved_vimeo_url = url

        courses_dict = cached_get_available_courses(notion_corsi_id, notion_token)
        if courses_dict:
            course_names = list(courses_dict.keys())
            selected_course = st.selectbox("Materia / Corso (Letto da Notion)", course_names)
            selected_course_page_id = courses_dict[selected_course]
        else:
            st.info("💡 Nessun corso trovato o chiavi Notion non impostate nel file .env. Puoi scrivere la materia a mano:")
            selected_course = st.text_input("Nome della materia", value="Generale")
            selected_course_page_id = notion_corsi_id

        st.session_state.selected_course = selected_course
        st.session_state.selected_course_page_id = selected_course_page_id

    with col_right:
        lesson_date = st.date_input(
            "Data della lezione",
            value=st.session_state.saved_lesson_date,
            key="lesson_date_input",
            on_change=update_saved_lesson_date
        )
        st.session_state.saved_lesson_date = lesson_date
        formatted_date_str = lesson_date.strftime("%d/%m/%Y")
        st.caption(f"Etichetta Lezione: **Lezione {formatted_date_str}**")
        st.session_state.formatted_date_str = formatted_date_str

    video_id_preview, _ = extract_vimeo_ids(url) if url else (None, None)
    already_processed = False
    saved_page_id = None
    existing_notion_url = None

    if video_id_preview:
        is_proc, record = cached_is_video_processed(video_id_preview)
        if is_proc:
            already_processed = True
            saved_page_id = record.get("notion_page_id")
            if saved_page_id:
                st.session_state.current_notion_page_id = saved_page_id
                clean_pid = notion_helper.format_notion_id(saved_page_id).replace("-", "")
                existing_notion_url = f"https://www.notion.so/{clean_pid}"
            elif selected_course_page_id:
                clean_pid = notion_helper.format_notion_id(selected_course_page_id).replace("-", "")
                existing_notion_url = f"https://www.notion.so/{clean_pid}"

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

        if saved_page_id and not force_reprocess and not st.session_state.appunti_generati:
            fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
            if fetched_notes:
                add_note_version(fetched_notes)
                st.session_state.notion_status = "💡 Appunti esistenti caricati automaticamente da Notion!"
                st.session_state.notion_page_url = existing_notion_url

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

            with st.status("🚀 Avvio elaborazione...", expanded=True) as status:
                if already_processed and not force_reprocess and do_latex:
                    status.update(label="📄 Caricamento appunti esistenti da Notion e conversione in LaTeX...")
                    if saved_page_id and not st.session_state.appunti_generati:
                        fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
                        if fetched_notes:
                            add_note_version(fetched_notes)

                    if st.session_state.appunti_generati:
                        success_lat, latex_gen = generate_latex(st.session_state.appunti_generati, model_name=selected_model)
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
                        success_gen, notes_gen = generate_notes(st.session_state.testo_estratto, custom_prompt=final_prompt, model_name=selected_model)
                        if success_gen:
                            add_note_version(notes_gen)
                            st.write("✅ Appunti Markdown generati con successo!")
                        else:
                            st.error(f"Errore Gemini: {notes_gen}")
                            status.update(label="❌ Errore durante la generazione appunti", state="error")
                            st.stop()

                    if do_latex and st.session_state.appunti_generati:
                        status.update(label="📄 Conversione appunti in codice LaTeX in corso...")
                        success_lat, latex_gen = generate_latex(st.session_state.appunti_generati, model_name=selected_model)
                        if success_lat:
                            st.session_state.latex_generato = latex_gen
                            st.write("✅ Codice LaTeX generato con successo!")
                        else:
                            st.error(f"Errore LaTeX: {latex_gen}")

                    if do_markdown_notion and st.session_state.appunti_generati and notion_token and selected_course_page_id:
                        status.update(label="📤 Creazione riga ed esportazione appunti su Notion...")
                        v_id, _ = extract_vimeo_ids(url)
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

                status.update(label="🎉 Elaborazione completata!", state="complete", expanded=False)

    if st.session_state.testo_estratto or st.session_state.appunti_generati:
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
                        sub_col1, sub_col2, sub_col3 = st.columns([2, 1.5, 2.5])
                        with sub_col1:
                            view_mode = st.radio("Modalità visualizzazione:", ["👁️ Anteprima Formattata", "✏️ Modifica Markdown"], horizontal=True, key="standard_view_radio")
                        with sub_col2:
                            st.write("")
                            render_version_navigation_bar("main_tab")
                        with sub_col3:
                            st.write("")
                            btn_c1, btn_c2, btn_c3 = st.columns([1, 1, 1])
                            with btn_c1:
                                if st.button("🎨 Studio Canvas", type="primary", use_container_width=True, key="btn_open_canvas_chat"):
                                    st.session_state.show_canvas_chat = True
                                    st.rerun()
                            with btn_c2:
                                if st.button("📄 Rigenera LaTeX", use_container_width=True, key="btn_regen_latex_standard", disabled=is_latex_regen_active()):
                                    trigger_background_latex_regen(selected_model)
                            with btn_c3:
                                render_notion_save_button_tab()

                        st.divider()

                        if view_mode == "✏️ Modifica Markdown":
                            edited_text = st.text_area(
                                "Modifica liberamente il testo Markdown dell'intera pagina:",
                                value=st.session_state.appunti_generati,
                                height=550,
                                key="markdown_editor_area",
                                on_change=update_appunti_from_editor
                            )
                            st.session_state.appunti_generati = edited_text
                        else:
                            cleaned_render = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati)
                            st.markdown(cleaned_render)

                        st.divider()

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
