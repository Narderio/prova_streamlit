import streamlit as st
import streamlit.components.v1 as components
import os
import time
from backend import download_and_process, generate_notes, generate_latex

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

# --- INIZIO INTERFACCIA STREAMLIT ---
st.set_page_config(page_title="Vimeo to University Notes", page_icon="🎓", layout="wide")

st.sidebar.title("⚙️ Configurazione")
api_key = st.sidebar.text_input("Google API Key", type="password", help="Ottieni una chiave su https://aistudio.google.com/app/apikey")
if api_key:
    os.environ["GOOGLE_API_KEY"] = api_key

st.title("🎓 Da Vimeo ad Appunti Professionali")
st.markdown("Ottieni istantaneamente la trascrizione e genera appunti strutturati in Markdown e LaTeX.")

# Inizializziamo lo stato
if 'testo_estratto' not in st.session_state:
    st.session_state.testo_estratto = None
if 'appunti_generati' not in st.session_state:
    st.session_state.appunti_generati = None
if 'latex_generato' not in st.session_state:
    st.session_state.latex_generato = None
if 'is_processing' not in st.session_state:
    st.session_state.is_processing = False

# Input utente
url = st.text_input("Link video Vimeo", placeholder="https://vimeo.com/...")

if st.button("🚀 Avvia Elaborazione", type="primary"):
    if not url:
        st.warning("⚠️ Inserisci il link di Vimeo.")
    elif not api_key and not os.getenv("GOOGLE_API_KEY"):
        st.warning("⚠️ Inserisci la Google API Key nella barra laterale.")
    else:
        st.session_state.testo_estratto = None
        st.session_state.appunti_generati = None
        st.session_state.latex_generato = None
        st.session_state.is_processing = True
        
        with st.spinner("Scaricamento trascrizione..."):
            success, result = download_and_process(url)
            if success:
                st.session_state.testo_estratto = result
                st.rerun()
            else:
                st.error(f"❌ Errore Vimeo: {result}")
                st.session_state.is_processing = False

# --- LOGICA DI VISUALIZZAZIONE ---
if st.session_state.testo_estratto:
    # Mostriamo prima la trascrizione se l'IA non ha finito
    if not st.session_state.appunti_generati:
        tab_list = ["📝 Trascrizione Grezza", "📚 Appunti (In arrivo...)", "📄 Codice LaTeX"]
    else:
        tab_list = ["📚 Appunti (Markdown)", "📄 Codice LaTeX", "📝 Trascrizione Grezza"]
        
    tabs = st.tabs(tab_list)
    
    for i, name in enumerate(tab_list):
        with tabs[i]:
            if "Trascrizione" in name:
                st.text_area("Testo originale", st.session_state.testo_estratto, height=400)
                c_t1, c_t2 = st.columns([1, 4])
                with c_t1:
                    st.download_button("💾 Scarica .txt", st.session_state.testo_estratto, "trascrizione.txt")
                with c_t2:
                    st_copy_to_clipboard(st.session_state.testo_estratto, "📋 Copia Trascrizione")
                
            elif "Appunti" in name:
                if st.session_state.appunti_generati:
                    st.markdown(st.session_state.appunti_generati)
                    st.divider()
                    c1, c2 = st.columns([1, 4])
                    with c1:
                        st.download_button("💾 Scarica .md", st.session_state.appunti_generati, "appunti.md")
                    with c2:
                        st_copy_to_clipboard(st.session_state.appunti_generati, "📋 Copia Markdown")
                else:
                    st.info("⌛ L'IA sta elaborando gli appunti... compaiono qui tra pochi secondi.")

            elif "LaTeX" in name:
                if st.session_state.latex_generato:
                    st.code(st.session_state.latex_generato, language="latex")
                    st.divider()
                    c3, c4 = st.columns([1, 4])
                    with c3:
                        st.download_button("💾 Scarica .tex", st.session_state.latex_generato, "appunti.tex")
                    with c4:
                        st_copy_to_clipboard(st.session_state.latex_generato, "📋 Copia LaTeX")
                else:
                    st.info("⌛ Il codice LaTeX sarà generato subito dopo gli appunti.")

    # --- TRIGGER AUTOMATICO IA (Eseguito DOPO aver renderizzato i tab) ---
    if st.session_state.is_processing and not st.session_state.appunti_generati:
        with st.status("🧠 L'IA sta lavorando per te...", expanded=True) as status:
            st.write("Generazione appunti Markdown...")
            success_n, result_n = generate_notes(st.session_state.testo_estratto)
            if success_n:
                st.session_state.appunti_generati = result_n
                st.write("Conversione in codice LaTeX...")
                success_l, result_l = generate_latex(result_n)
                if success_l:
                    st.session_state.latex_generato = result_l
                    status.update(label="✅ Elaborazione completata!", state="complete", expanded=False)
                    st.session_state.is_processing = False
                    time.sleep(1) # Breve attesa per far vedere il successo
                    st.rerun()
                else:
                    st.error(f"Errore LaTeX: {result_l}")
            else:
                st.error(f"Errore IA: {result_n}")
            st.session_state.is_processing = False
