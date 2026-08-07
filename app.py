import streamlit as st
import streamlit.components.v1 as components
import os
import time
import datetime
import threading
import importlib
from dotenv import load_dotenv

import backend
import notion_helper
import supabase_client

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
    components.html(copy_js, height=50)

# --- CALLBACK PER SINCRONIZZARE L'EDITOR CON LO STATO ---
def update_appunti_from_editor():
    if "markdown_editor_area" in st.session_state and st.session_state.markdown_editor_area:
        st.session_state.appunti_generati = st.session_state.markdown_editor_area
    elif "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
        st.session_state.appunti_generati = st.session_state.markdown_editor_area_canvas

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

# --- FUNZIONE DI VERIFICA LOCK NOTION ---
def is_notion_saving_active():
    thread = st.session_state.get("notion_save_thread")
    if thread:
        if thread.is_alive():
            return True
        else:
            st.session_state.notion_save_thread = None
            st.session_state.notion_status = "✅ Pagina aggiornata su Notion con successo!"
            st.toast("✅ Pagina aggiornata su Notion con successo!", icon="🎉")
            return False
    return False

# --- FUNZIONE DI SALVATAGGIO SU NOTION CON LOCK E SNAPSHOT ---
def save_current_notes_to_notion():
    if is_notion_saving_active():
        st.warning("⏳ Un salvataggio su Notion è già in corso. Attendi che il salvataggio precedente sia completato.")
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
        st.session_state.notion_save_thread = bg_thread
        bg_thread.start()

        clean_pid = notion_helper.format_notion_id(target_pid).replace("-", "")
        st.session_state.notion_page_url = f"https://www.notion.so/{clean_pid}"
        st.session_state.notion_status = "⚡ Salvataggio avviato su Notion con snapshot protetto!"
        st.session_state.canvas_view_radio = "👁️ Anteprima Formattata"
        st.session_state.standard_view_radio = "👁️ Anteprima Formattata"
        st.toast("⚡ Salvataggio avviato su Notion! Passato ad Anteprima Formattata.", icon="🚀")
        st.rerun()
    else:
        st.error("Impossibile individuare la pagina Notion da aggiornare.")

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

# ==============================================================================
# PAGINA DEDICATA: CANVAS STUDIO FULL-SCREEN (SE ATTIVA)
# ==============================================================================
if st.session_state.show_canvas_chat and st.session_state.appunti_generati:
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
            
            [data-testid="stSidebar"], [data-testid="collapsedControl"], header[data-testid="stHeader"] {
                display: none !important;
            }
            .main .block-container {
                padding-top: 3.5rem !important;
                padding-bottom: 1rem !important;
                padding-left: 1.5rem !important;
                padding-right: 1.5rem !important;
                max-width: 100% !important;
                height: 100vh !important;
                overflow: hidden !important;
            }

            /* CONTENITORE PRINCIPALE A 3 COLONNE CON AMPIO SPAZIO SUPERIORE */
            .main .block-container > div[data-testid="stElementContainer"] > div[data-testid="stHorizontalBlock"] {
                flex-wrap: nowrap !important;
                height: calc(100vh - 80px) !important;
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

            /* 1. PANNELLO CHAT (Sinistra) - HEADER FISSO IN ALTO */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1) {
                height: calc(100vh - 90px) !important;
                max-height: calc(100vh - 90px) !important;
                overflow: hidden !important;
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

            /* 2. PANNELLO CANVAS (Destra) - HEADER E PULSANTI BLOCCATI PERMANENTEMENTE IN ALTO */
            div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(3) {
                background-color: #1a1a1a !important;
                border-radius: 16px !important;
                padding: 1rem 1.2rem !important;
                border: 1px solid #333333 !important;
                box-shadow: 0 4px 25px rgba(0,0,0,0.5) !important;
                height: calc(100vh - 90px) !important;
                max-height: calc(100vh - 90px) !important;
                overflow: hidden !important;
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

            /* 4. RESET TOTALE PER QUALSIASI SOTTO-COLONNA ANNIDATA (Header del Canvas) */
            div[data-testid="stColumn"] div[data-testid="stColumn"] {
                height: auto !important;
                max-height: 45px !important;
                min-height: 0 !important;
                background-color: transparent !important;
                background: transparent !important;
                box-shadow: none !important;
                border: none !important;
                overflow: visible !important;
            }

            /* STILE PULITO UNIFORME PER TUTTI I PULSANTI ICONA NELL'HEADER */
            div.stButton > button, 
            div[data-testid="stButton"] > button {
                background-color: #2b2b2b !important;
                border: 1px solid #383838 !important;
                border-radius: 8px !important;
                color: #ffffff !important;
                box-shadow: none !important;
                padding: 6px 12px !important;
                height: auto !important;
                min-height: 38px !important;
                width: auto !important;
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                transition: all 0.2s ease !important;
            }
            div.stButton > button:hover,
            div[data-testid="stButton"] > button:hover {
                background-color: #383838 !important;
                border-color: #38bdf8 !important;
                color: #38bdf8 !important;
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

    is_saving = is_notion_saving_active()
    if is_saving:
        st.caption("⏳ **Sincronizzazione Notion in corso in background...** Puoi continuare a modificare gli appunti o chattare!")

    # 1. PANNELLO CANVAS A DESTRA (DOCUMENTO MOSTRATO PRIMA PER PERMETTERE IL LIVE STREAMING)
    canvas_placeholder = None
    with col_canvas:
        col_title, col_btns = st.columns([6, 4])
        with col_title:
            st.markdown("<h3 style='margin:0; padding:0; color:#ffffff;'>📄 Canvas Appunti</h3>", unsafe_allow_html=True)
        with col_btns:
            btn_group_left, btn_back_col = st.columns([2, 1])
            with btn_group_left:
                b1, b2 = st.columns([1, 1])
                with b1:
                    if 'canvas_edit_mode_toggle' not in st.session_state:
                        st.session_state.canvas_edit_mode_toggle = False
                    
                    icon_view = "👁️" if st.session_state.canvas_edit_mode_toggle else "✏️"
                    help_view = "Passa ad Anteprima Formattata" if st.session_state.canvas_edit_mode_toggle else "Passa a Modifica Manuale"
                    
                    if st.button(icon_view, help=help_view, key="btn_toggle_canvas_edit_icon"):
                        st.session_state.canvas_edit_mode_toggle = not st.session_state.canvas_edit_mode_toggle
                        st.rerun()
                with b2:
                    if st.button("📤", help="Salva subito gli appunti su Notion", key="btn_save_notion_icon"):
                        save_current_notes_to_notion()
            with btn_back_col:
                if st.button("🔙", help="Torna al Form di configurazione Vimeo", key="btn_close_canvas_icon"):
                    st.session_state.show_canvas_chat = False
                    st.rerun()

        st.markdown("<hr style='margin: 0.1rem 0 0.1rem 0; border: none; border-top: 1px solid #333333;' />", unsafe_allow_html=True)

        if "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
            st.session_state.appunti_generati = st.session_state.markdown_editor_area_canvas
        
        cleaned_render_canvas = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati).strip()
        
        canvas_scroll_area = st.container(height=650, border=False)
        with canvas_scroll_area:
            if st.session_state.canvas_edit_mode_toggle:
                edited_text_canvas = st.text_area(
                    "Modifica direttamente il testo nel Canvas:",
                    value=st.session_state.appunti_generati,
                    height=580,
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
        
        # Contenitore di scroll nativo per la chat senza bordi visibili
        chat_scroll_area = st.container(height=640, border=False)
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
                with st.chat_message("assistant"):
                    st.markdown("<div style='color: #34d399; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;'>🤖 ASSISTENTE AI</div>", unsafe_allow_html=True)
                    chat_response_placeholder = st.empty()
                    full_raw_response = ""
                    try:
                        last_user_prompt = st.session_state.canvas_chat_history[-1]["content"]
                        stream_gen = agent_edit_notes_stream(
                            current_markdown=st.session_state.appunti_generati,
                            user_instruction=last_user_prompt,
                            chat_history=st.session_state.canvas_chat_history[:-1],
                            raw_transcript=st.session_state.testo_estratto,
                            model_name=selected_model
                        )
                        for chunk_text in stream_gen:
                            full_raw_response += chunk_text
                            if "<<<UPDATED_CANVAS>>>" in full_raw_response:
                                parts = full_raw_response.split("<<<UPDATED_CANVAS>>>")
                                chat_part = parts[0].replace("<<<CHAT_RESPONSE>>>", "").strip()
                                canvas_part = parts[1].strip()
                                chat_response_placeholder.markdown(chat_part)
                                if canvas_part.upper() != "NO_CHANGE" and canvas_placeholder is not None:
                                    cleaned_live_canvas = notion_helper.clean_markdown_for_streamlit(canvas_part)
                                    canvas_placeholder.markdown(cleaned_live_canvas)
                                    st.session_state.appunti_generati = cleaned_live_canvas
                            else:
                                chat_part = full_raw_response.replace("<<<CHAT_RESPONSE>>>", "").strip()
                                chat_response_placeholder.markdown(chat_part)
                        
                        if "<<<CHAT_RESPONSE>>>" in full_raw_response and "<<<UPDATED_CANVAS>>>" in full_raw_response:
                            parts = full_raw_response.split("<<<UPDATED_CANVAS>>>")
                            final_chat_reply = parts[0].replace("<<<CHAT_RESPONSE>>>", "").strip()
                            raw_canvas = parts[1].strip()
                            if raw_canvas.upper() != "NO_CHANGE" and len(raw_canvas) > 5:
                                st.session_state.appunti_generati = notion_helper.clean_markdown_for_streamlit(raw_canvas)
                                st.toast("⚡ Canvas aggiornato in tempo reale!", icon="✅")
                            else:
                                st.toast("💬 Risposta fornita in chat.", icon="ℹ️")
                        else:
                            final_chat_reply = full_raw_response.replace("<<<CHAT_RESPONSE>>>", "").strip() or "Risposta dell'assistente."
                            st.toast("💬 Risposta fornita in chat.", icon="ℹ️")
                        
                        st.session_state.canvas_chat_history.append({"role": "assistant", "content": final_chat_reply})
                        st.session_state.pending_agent_stream = False
                        st.rerun()
                    except Exception as e:
                        st.session_state.pending_agent_stream = False
            # Spaziatore inferiore di 180px per dare ampio spazio in fondo all'ultimo messaggio
            st.markdown("<div id='chat-bottom-spacer' style='height: 180px; width: 100%;'></div>", unsafe_allow_html=True)

        # Campo chat_input POSIZIONATO FISSO IN BASSO A SINISTRA
        user_input = st.chat_input("Chiedi all'Assistente AI di modificare il Canvas...", disabled=st.session_state.pending_agent_stream)
        if user_input and not st.session_state.pending_agent_stream:
            st.session_state.canvas_chat_history.append({"role": "user", "content": user_input})
            st.session_state.pending_agent_stream = True
            st.rerun()

    # --- INIEZIONE JAVASCRIPT DRAGGABLE PER IL TASTO PILLOLA #drag-handle-pill-native ---
    draggable_handle_js = """
    <script>
    (function() {
        const pDoc = window.parent.document;
        let userIsNearBottom = true;

        function getChatBox() {
            const chatCol = pDoc.querySelector('div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)');
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
            const chatCol = pDoc.querySelector('div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)');
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
            const leftCol = pDoc.querySelector('div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1)');
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
            
            const cols = studioBlock.querySelectorAll('[data-testid="stColumn"]');
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
    components.html(draggable_handle_js, height=0)

# ==============================================================================
# PAGINA PRINCIPALE: CONFIGURAZIONE FORM & GENERAZIONE
# ==============================================================================
else:
    st.title("🎓 Da Vimeo ad Appunti & Notion")
    st.markdown("Estrai la trascrizione dai video Vimeo, genera ed edita appunti universitari con Gemini e salvali direttamente su **Notion**.")

    col_left, col_right = st.columns([2, 1])

    notion_token = os.getenv("NOTION_API_KEY")
    notion_corsi_id = os.getenv("NOTION_CORSI_PAGE_ID")

    with col_left:
        url = st.text_input("Link video Vimeo", placeholder="https://vimeo.com/123456789/hash...")

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
        lesson_date = st.date_input("Data della lezione", value=datetime.date.today())
        formatted_date_str = lesson_date.strftime("%d/%m/%Y")
        st.caption(f"Etichetta Lezione: **Lezione {formatted_date_str}**")
        st.session_state.formatted_date_str = formatted_date_str

    st.markdown("### 🎯 Seleziona cosa vuoi generare:")
    col_out1, col_out2, col_out3 = st.columns(3)

    with col_out1:
        do_transcript = st.checkbox("📝 Trascrizione Grezza", value=True, help="Estrai il testo originale dal video Vimeo")
    with col_out2:
        do_markdown_notion = st.checkbox("📚 Appunti Markdown & Export Notion", value=True, help="Genera appunti formattati con Gemini e salvali su Notion")
    with col_out3:
        do_latex = st.checkbox("📄 Codice LaTeX", value=False, help="Converti gli appunti Markdown in codice LaTeX per la stampa")

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

    video_id_preview, _ = extract_vimeo_ids(url) if url else (None, None)
    already_processed = False
    force_reprocess = False

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
            else:
                existing_notion_url = None

            col_warn_msg, col_warn_btn = st.columns([3, 1])
            with col_warn_msg:
                st.warning(f"⚠️ **Attenzione:** Questa lezione (Video ID: `{video_id_preview}`) risulta già elaborata il **{record.get('created_at', '')[:10]}** per il corso **{record.get('course')}**.")
            with col_warn_btn:
                if existing_notion_url:
                    st.write("")
                    st.link_button("📖 Apri su Notion", existing_notion_url, use_container_width=True)

            force_reprocess = st.checkbox("🔄 Elabora ed esporta comunque (creando una nuova versione)", value=False)

            if saved_page_id and not force_reprocess and not st.session_state.appunti_generati:
                fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
                if fetched_notes:
                    st.session_state.appunti_generati = fetched_notes
                    st.session_state.notion_status = "💡 Appunti esistenti caricati automaticamente da Notion!"
                    st.session_state.notion_page_url = existing_notion_url

    st.session_state.already_processed = already_processed
    st.divider()

    can_start = url and (do_transcript or do_markdown_notion or do_latex) and (not already_processed or force_reprocess)

    if st.button("🚀 Avvia Elaborazione", type="primary", disabled=not can_start or is_notion_saving_active()):
        if already_processed and not force_reprocess:
            st.error("⛔ Elaborazione bloccata: questa lezione è già stata inserita nel database. Spunta 'Elabora ed esporta comunque' se vuoi rielaborarla.")
            st.stop()

        if (do_markdown_notion or do_latex) and not os.getenv("GOOGLE_API_KEY"):
            st.error("⚠️ Inserisci la Google API Key prima di procedere con Gemini.")
        elif do_markdown_notion and not selected_course_page_id:
            st.error("⚠️ Specifica l'ID della pagina Notion 'Corsi' nel file .env (NOTION_CORSI_PAGE_ID).")
        else:
            st.session_state.testo_estratto = None
            st.session_state.appunti_generati = None
            st.session_state.latex_generato = None
            st.session_state.notion_status = None
            st.session_state.notion_page_url = None
            st.session_state.canvas_chat_history = []

            with st.status("🚀 Avvio elaborazione...", expanded=True) as status:
                if do_transcript:
                    status.update(label="📝 Estrazione trascrizione da Vimeo in corso...")
                    res_tr = download_and_process(url)
                    if res_tr.get("success"):
                        st.session_state.testo_estratto = res_tr.get("text")
                        st.write("✅ Trascrizione estratta con successo!")
                    else:
                        st.error(f"Errore trascrizione: {res_tr.get('error')}")
                        status.update(label="❌ Errore durante la trascrizione", state="error")
                        st.stop()

                if do_markdown_notion and st.session_state.testo_estratto:
                    status.update(label="🧠 Generazione appunti formattati con Gemini in corso...")
                    res_gen = generate_notes(st.session_state.testo_estratto, custom_prompt=final_prompt, model_name=selected_model)
                    if res_gen.get("success"):
                        st.session_state.appunti_generati = res_gen.get("markdown")
                        st.write("✅ Appunti Markdown generati con successo!")
                    else:
                        st.error(f"Errore Gemini: {res_gen.get('error')}")
                        status.update(label="❌ Errore durante la generazione appunti", state="error")
                        st.stop()

                if do_latex and st.session_state.appunti_generati:
                    status.update(label="📄 Conversione appunti in codice LaTeX in corso...")
                    res_lat = generate_latex(st.session_state.appunti_generati, model_name=selected_model)
                    if res_lat.get("success"):
                        st.session_state.latex_generato = res_lat.get("latex")
                        st.write("✅ Codice LaTeX generato con successo!")

                if do_markdown_notion and st.session_state.appunti_generati and notion_token and selected_course_page_id:
                    status.update(label="📤 Creazione riga ed esportazione appunti su Notion...")
                    v_id, _ = extract_vimeo_ids(url)
                    success_notion, notion_page_id, msg_notion = export_to_notion(
                        course_name=selected_course,
                        lesson_date_str=formatted_date_str,
                        markdown_text=st.session_state.appunti_generati,
                        corsi_page_id=selected_course_page_id,
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
                        sub_col1, sub_col2 = st.columns([2, 2])
                        with sub_col1:
                            view_mode = st.radio("Modalità visualizzazione:", ["👁️ Anteprima Formattata", "✏️ Modifica Markdown"], horizontal=True, key="standard_view_radio")
                        with sub_col2:
                            st.write("")
                            btn_c1, btn_c2 = st.columns([1, 1])
                            with btn_c1:
                                if st.button("🎨 Apri Studio Canvas (Split-Screen)", type="primary", use_container_width=True, key="btn_open_canvas_chat"):
                                    st.session_state.show_canvas_chat = True
                                    st.rerun()
                            with btn_c2:
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
                            if "markdown_editor_area" in st.session_state and st.session_state.markdown_editor_area:
                                st.session_state.appunti_generati = st.session_state.markdown_editor_area
                            cleaned_render = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati)
                            st.markdown(cleaned_render)

                        st.divider()

                        c1, c2 = st.columns([1, 4])
                        with c1:
                            st.download_button("💾 Scarica .md", st.session_state.appunti_generati, f"appunti_{formatted_date_str.replace('/', '_')}.md")
                        with c2:
                            st_copy_to_clipboard(st.session_state.appunti_generati, "📋 Copia Markdown")

                    elif "LaTeX" in tab_name:
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
