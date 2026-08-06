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
st.sidebar.subheader("🤖 Modello Gemini")
AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash"
]
selected_model = st.sidebar.selectbox("Scegli il modello:", AVAILABLE_MODELS, index=0)

# --- TITOLO PRINCIPALE ---
st.title("🎓 Da Vimeo ad Appunti & Notion")
st.markdown("Estrai la trascrizione dai video Vimeo, genera ed edita appunti universitari con Gemini e salvali direttamente su **Notion**.")

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

# --- FORM DI INSERIMENTO ---
col_left, col_right = st.columns([2, 1])

notion_token = os.getenv("NOTION_API_KEY")
notion_corsi_id = os.getenv("NOTION_CORSI_PAGE_ID")

with col_left:
    url = st.text_input("Link video Vimeo", placeholder="https://vimeo.com/123456789/hash...")

    # Lettura automatica corsi da Notion (usando chiavi da .env con caching)
    courses_dict = cached_get_available_courses(notion_corsi_id, notion_token)
    if courses_dict:
        course_names = list(courses_dict.keys())
        selected_course = st.selectbox("Materia / Corso (Letto da Notion)", course_names)
        selected_course_page_id = courses_dict[selected_course]
    else:
        st.info("💡 Nessun corso trovato o chiavi Notion non impostate nel file .env. Puoi scrivere la materia a mano:")
        selected_course = st.text_input("Nome della materia", value="Generale")
        selected_course_page_id = notion_corsi_id

with col_right:
    lesson_date = st.date_input("Data della lezione", value=datetime.date.today())
    formatted_date_str = lesson_date.strftime("%d/%m/%Y")
    st.caption(f"Etichetta Lezione: **Lezione {formatted_date_str}**")

# --- SELEZIONE OUTPUT DESIDERATI ---
st.markdown("### 🎯 Seleziona cosa vuoi generare:")
col_out1, col_out2, col_out3 = st.columns(3)

with col_out1:
    do_transcript = st.checkbox("📝 Trascrizione Grezza", value=True, help="Estrai il testo originale dal video Vimeo")
with col_out2:
    do_markdown_notion = st.checkbox("📚 Appunti Markdown & Export Notion", value=True, help="Genera appunti formattati con Gemini e salvali su Notion")
with col_out3:
    do_latex = st.checkbox("📄 Codice LaTeX", value=False, help="Converti gli appunti Markdown in codice LaTeX per la stampa")

# --- SEZIONE PROMPT GEMINI ---
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

# --- CHECK DUPLICATI SUPABASE & AUTOCARICAMENTO ---
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

        # Autocaricamento automatico dell'INTERA pagina di appunti da Notion
        if saved_page_id and not force_reprocess and not st.session_state.appunti_generati:
            fetched_notes = cached_get_notion_page_markdown(saved_page_id, token=notion_token)
            if fetched_notes:
                st.session_state.appunti_generati = fetched_notes
                st.session_state.notion_status = "💡 Appunti esistenti caricati automaticamente da Notion!"
                st.session_state.notion_page_url = existing_notion_url

st.divider()

# --- BOTTONE DI AVVIO ---
can_start = url and (do_transcript or do_markdown_notion or do_latex) and (not already_processed or force_reprocess)

if st.button("🚀 Avvia Elaborazione", type="primary", disabled=not can_start):
    # Blocco tassativo se il video è duplicato e l'utente non ha spuntato force_reprocess
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
        st.session_state.show_canvas_chat = False
        st.session_state.canvas_chat_history = []

        with st.status("⚙️ Elaborazione in corso...", expanded=True) as status:
            # 1. Download trascrizione Vimeo
            status.update(label="📹 Scaricamento trascrizione da Vimeo...")
            success_vimeo, result_vimeo, v_id = download_and_process(url)
            
            if not success_vimeo:
                status.update(label="❌ Errore Vimeo", state="error")
                st.error(f"Errore Vimeo: {result_vimeo}")
            else:
                st.session_state.testo_estratto = result_vimeo
                st.write("✅ Trascrizione scaricata con successo.")

                # Salva subito su Supabase per bloccare rielaborazioni future dello stesso link
                if v_id:
                    supabase_client.save_processed_lesson(
                        video_id=v_id,
                        url=url,
                        course=selected_course,
                        lesson_date=formatted_date_str,
                        notion_page_id=None
                    )

                # 2. Generazione Appunti con Gemini (se richiesto)
                if do_markdown_notion or do_latex:
                    status.update(label=f"🧠 Generazione appunti con Gemini (`{selected_model}`)...")
                    success_notes, result_notes = generate_notes(result_vimeo, model_name=selected_model, custom_prompt=final_prompt)

                    if not success_notes:
                        status.update(label="❌ Errore Generazione Gemini", state="error")
                        st.error(f"Errore Gemini: {result_notes}")
                    else:
                        st.session_state.appunti_generati = result_notes
                        st.write("✅ Appunti Markdown generati.")

                        # 3. Conversione in LaTeX (se richiesto)
                        if do_latex:
                            status.update(label=f"📄 Conversione in codice LaTeX (`{selected_model}`)...")
                            success_latex, result_latex = generate_latex(result_notes, model_name=selected_model)
                            if success_latex:
                                st.session_state.latex_generato = result_latex
                                st.write("✅ Codice LaTeX generato.")
                            else:
                                st.warning(f"Errore LaTeX: {result_latex}")

                        # 4. Esportazione su Notion (se richiesto)
                        if do_markdown_notion:
                            status.update(label="📝 Esportazione ed aggiornamento su Notion...")
                            success_notion, msg_notion, notion_page_id = export_to_notion(
                                course_name=selected_course,
                                course_page_id=selected_course_page_id,
                                lesson_date_str=formatted_date_str,
                                markdown_text=result_notes,
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

                                # Aggiorna record Supabase con l'ID della pagina Notion creata
                                if v_id:
                                    supabase_client.save_processed_lesson(
                                        video_id=v_id,
                                        url=url,
                                        course=selected_course,
                                        lesson_date=formatted_date_str,
                                        notion_page_id=notion_page_id
                                    )

                status.update(label="🎉 Elaborazione completata!", state="complete", expanded=False)

# --- FUNZIONE DI SALVATAGGIO SU NOTION ---
def save_current_notes_to_notion():
    target_pid = st.session_state.get("current_notion_page_id")
    if not target_pid:
        db_id, _ = notion_helper.get_or_create_course_database(selected_course_page_id, selected_course, notion_token)
        target_pid, _, _ = notion_helper.get_or_create_lesson_entry(db_id, formatted_date_str, is_same_video=already_processed, api_key=notion_token)
        st.session_state.current_notion_page_id = target_pid

    if target_pid:
        bg_thread = threading.Thread(
            target=notion_helper.update_notion_page_in_place,
            args=(target_pid, st.session_state.appunti_generati, notion_token),
            daemon=True
        )
        bg_thread.start()

        clean_pid = notion_helper.format_notion_id(target_pid).replace("-", "")
        st.session_state.notion_page_url = f"https://www.notion.so/{clean_pid}"
        st.session_state.notion_status = "⚡ Salvataggio inviato in background! La pagina Notion si aggiornerà in pochissimi istanti."
        st.toast("⚡ Aggiornamento Notion inviato in background!", icon="🚀")
        st.success("⚡ Salvataggio inviato su Notion in modalità asincrona! Puoi continuare a lavorare subito.")
    else:
        st.error("Impossibile individuare la pagina Notion da aggiornare.")

# --- RENDERING DEI RISULTATI ---
if st.session_state.testo_estratto or st.session_state.appunti_generati:
    st.write("")
    if st.session_state.notion_status:
        c_status, c_btn = st.columns([3, 1])
        with c_status:
            st.success(st.session_state.notion_status)
        with c_btn:
            if st.session_state.notion_page_url:
                st.link_button("📖 Apri Lezione su Notion", st.session_state.notion_page_url, use_container_width=True)

    # --- MODALITÀ CANVAS SPLIT-SCREEN CHAT ---
    if st.session_state.show_canvas_chat and st.session_state.appunti_generati:
        st.divider()
        tb_col1, tb_col2, tb_col3 = st.columns([2, 2, 1])
        with tb_col1:
            st.markdown("## 🎨 Canvas Editor & AI Assistant")
            word_count = len(st.session_state.appunti_generati.split())
            token_est = len(st.session_state.appunti_generati) // 4
            st.caption(f"📌 Contesto Attivo | 📊 Parole: **{word_count}** | ⚡ Tokens: **~{token_est}**")
        with tb_col2:
            canvas_view_mode = st.radio("Modalità Canvas:", ["👁️ Anteprima Formattata", "✏️ Modifica Manuale"], horizontal=True, key="canvas_view_radio")
        with tb_col3:
            st.write("")
            if st.button("💾 Salva su Notion", type="primary", key="btn_save_canvas_split", use_container_width=True):
                save_current_notes_to_notion()
            if st.button("❌ Chiudi Chat", use_container_width=True, key="btn_close_canvas_chat"):
                st.session_state.show_canvas_chat = False
                st.rerun()

        st.divider()

        col_chat, col_canvas = st.columns([2, 3])

        quick_prompt = None

        # Pre-renderizza la colonna Canvas a destra per consentire lo streaming live
        with col_canvas:
            st.markdown("### 📄 Canvas Appunti (Live)")
            canvas_container = st.container(height=520)
            with canvas_container:
                canvas_placeholder = st.empty()
                if not st.session_state.pending_agent_stream:
                    if canvas_view_mode == "✏️ Modifica Manuale":
                        edited_text_canvas = st.text_area(
                            "Modifica direttamente il testo nel Canvas:",
                            value=st.session_state.appunti_generati,
                            height=460,
                            key="markdown_editor_area_canvas",
                            on_change=update_appunti_from_editor
                        )
                        st.session_state.appunti_generati = edited_text_canvas
                    else:
                        if "markdown_editor_area_canvas" in st.session_state and st.session_state.markdown_editor_area_canvas:
                            st.session_state.appunti_generati = st.session_state.markdown_editor_area_canvas
                        cleaned_render_canvas = notion_helper.clean_markdown_for_streamlit(st.session_state.appunti_generati)
                        canvas_placeholder.markdown(cleaned_render_canvas)

        with col_chat:
            st.markdown("### 💬 Chatbot Assistant")
            
            chat_container = st.container(height=520)

            with chat_container:
                if not st.session_state.canvas_chat_history:
                    st.info("👋 Ciao! Ho agganciato i tuoi appunti al Canvas. Scrivi una richiesta qui sotto per modificarli in tempo reale con l'Agente AI.")
                
                with st.expander("⚡ Azioni Rapide (Prompt Pronti)", expanded=False):
                    qc1, qc2 = st.columns(2)
                    with qc1:
                        if st.button("🔍 Espandi concetti", use_container_width=True, key="qp_expand"):
                            quick_prompt = "Trova ed espandi i concetti chiave o meno dettagliati negli appunti."
                        if st.button("💡 Aggiungi riepilogo", use_container_width=True, key="qp_summary"):
                            quick_prompt = "Aggiungi una sezione di riepilogo con i punti chiave all'inizio degli appunti."
                    with qc2:
                        if st.button("✂️ Sintetizza", use_container_width=True, key="qp_shorten"):
                            quick_prompt = "Sintetizza i paragrafi più lunghi mantenendo concetti e formule intatte."
                        if st.button("📝 Migliora stile", use_container_width=True, key="qp_style"):
                            quick_prompt = "Migliora la forma grammaticale, la leggibilità e la formattazione dello stile."

                for msg in st.session_state.canvas_chat_history:
                    with st.chat_message(msg["role"]):
                        st.markdown(msg["content"])

                # Se in attesa dello streaming, avvia il doppio streaming simultaneo su Chat e Canvas!
                if st.session_state.pending_agent_stream:
                    with st.chat_message("assistant"):
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
                                    canvas_placeholder.markdown(notion_helper.clean_markdown_for_streamlit(canvas_part))
                                else:
                                    chat_part = full_raw_response.replace("<<<CHAT_RESPONSE>>>", "").strip()
                                    chat_response_placeholder.markdown(chat_part)
                            
                            if "<<<CHAT_RESPONSE>>>" in full_raw_response and "<<<UPDATED_CANVAS>>>" in full_raw_response:
                                parts = full_raw_response.split("<<<UPDATED_CANVAS>>>")
                                final_chat_reply = parts[0].replace("<<<CHAT_RESPONSE>>>", "").strip()
                                final_canvas_md = notion_helper.clean_markdown_for_streamlit(parts[1].strip())
                            else:
                                final_chat_reply = full_raw_response.strip() or "Ho applicato le modifiche agli appunti nel Canvas."
                                final_canvas_md = notion_helper.clean_markdown_for_streamlit(full_raw_response)
                            
                            st.session_state.appunti_generati = final_canvas_md
                            st.session_state.canvas_chat_history.append({"role": "assistant", "content": final_chat_reply})
                            st.session_state.pending_agent_stream = False
                            st.toast("⚡ Canvas aggiornato in tempo reale!", icon="✅")
                            st.rerun()

                        except Exception as e:
                            st.session_state.pending_agent_stream = False
                            st.error(f"Errore durante l'elaborazione con l'Agente AI: {str(e)}")

            user_input = st.chat_input("Chiedi all'Agente AI di modificare il Canvas...")
            active_prompt = user_input or quick_prompt
            if active_prompt and not st.session_state.pending_agent_stream:
                st.session_state.canvas_chat_history.append({"role": "user", "content": active_prompt})
                st.session_state.pending_agent_stream = True
                st.rerun()

    # --- MODALITÀ TABS STANDARD (SCHERMO INTERO) ---
    else:
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
                            view_mode = st.radio("Modalità visualizzazione:", ["👁️ Anteprima Formattata", "✏️ Modifica Markdown"], horizontal=True)
                        with sub_col2:
                            st.write("")
                            btn_c1, btn_c2 = st.columns([1, 1])
                            with btn_c1:
                                if st.button("💬 Inserisci in Chat", type="primary", use_container_width=True, key="btn_open_canvas_chat"):
                                    st.session_state.show_canvas_chat = True
                                    st.rerun()
                            with btn_c2:
                                if st.button("📤 Salva su Notion", use_container_width=True, key="btn_save_edited_notion"):
                                    save_current_notes_to_notion()

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
                        st.text_area("Testo originale trascritto", st.session_state.testo_estratto, height=400)
                        ct1, ct2 = st.columns([1, 4])
                        with ct1:
                            st.download_button("💾 Scarica .txt", st.session_state.testo_estratto, "trascrizione.txt")
                        with ct2:
                            st_copy_to_clipboard(st.session_state.testo_estratto, "📋 Copia Trascrizione")

