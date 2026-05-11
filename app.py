import streamlit as st
import subprocess
import tempfile
import os
from backend import download_and_process, generate_notes, generate_latex

# --- INIZIO INTERFACCIA STREAMLIT ---
st.set_page_config(page_title="Vimeo to University Notes", page_icon="🎓", layout="wide")

st.sidebar.title("⚙️ Configurazione")
api_key = st.sidebar.text_input("Google API Key", type="password", help="Ottieni una chiave su https://aistudio.google.com/app/apikey")
if api_key:
    os.environ["GOOGLE_API_KEY"] = api_key

st.title("🎓 Da Vimeo ad Appunti Professionali")
st.markdown("Trasforma le tue lezioni Vimeo in appunti Markdown e codice LaTeX pronti per lo studio.")

# Inizializziamo lo stato
if 'testo_estratto' not in st.session_state:
    st.session_state.testo_estratto = None
if 'appunti_generati' not in st.session_state:
    st.session_state.appunti_generati = None
if 'latex_generato' not in st.session_state:
    st.session_state.latex_generato = None

# Input utente
url = st.text_input("Link video Vimeo", placeholder="https://vimeo.com/...")

if st.button("🚀 Avvia Elaborazione Completa", type="primary"):
    if not url:
        st.warning("⚠️ Inserisci il link di Vimeo.")
    elif not api_key and not os.getenv("GOOGLE_API_KEY"):
        st.warning("⚠️ Inserisci la Google API Key nella barra laterale.")
    else:
        # Reset stati precedenti
        st.session_state.testo_estratto = None
        st.session_state.appunti_generati = None
        st.session_state.latex_generato = None
        
        # Step 1: Trascrizione (Mostrata subito dopo il download)
        with st.spinner("1/3 - Scaricamento trascrizione..."):
            success_vimeo, result_vimeo = download_and_process(url)
            
        if success_vimeo:
            st.session_state.testo_estratto = result_vimeo
            st.success("✅ Trascrizione ottenuta!")
            # Forza l'aggiornamento per mostrare la trascrizione nel tab
            st.rerun()

# Se abbiamo la trascrizione ma non ancora il resto, avviamo l'IA automaticamente
if st.session_state.testo_estratto and not st.session_state.appunti_generati:
    # Step 2: Generazione Appunti Markdown
    with st.spinner("2/3 - Generazione appunti Markdown..."):
        success_llm, result_llm = generate_notes(st.session_state.testo_estratto)
    
    if success_llm:
        st.session_state.appunti_generati = result_llm
        
        # Step 3: Conversione LaTeX
        with st.spinner("3/3 - Conversione in codice LaTeX..."):
            success_latex, result_latex = generate_latex(result_llm)
        
        if success_latex:
            st.session_state.latex_generato = result_latex
            st.success("✅ Elaborazione completata!")
            st.rerun()
        else:
            st.error(f"❌ Errore LaTeX: {result_latex}")
    else:
        st.error(f"❌ Errore IA (Markdown): {result_llm}")

# Visualizzazione Risultati
if st.session_state.testo_estratto is not None:
    # Definiamo i tab. Se l'IA ha finito, mostriamo gli appunti per primi, 
    # altrimenti mostriamo la trascrizione.
    if st.session_state.appunti_generati:
        tab_list = ["📚 Appunti (Markdown)", "📄 Codice LaTeX", "📝 Trascrizione Grezza"]
    else:
        tab_list = ["📝 Trascrizione Grezza", "📚 Appunti (In corso...)", "📄 Codice LaTeX (In corso...)"]
        
    tabs = st.tabs(tab_list)
    
    # Gestione dinamica dei contenuti nei tab basata sulla lista tab_list
    for i, name in enumerate(tab_list):
        with tabs[i]:
            if "Trascrizione" in name:
                testo = st.session_state.testo_estratto
                st.text_area("Testo originale", testo, height=400)
                st.download_button("💾 Scarica .txt", testo, "trascrizione.txt", "text/plain")
                
            elif "Appunti" in name:
                if st.session_state.appunti_generati:
                    appunti = st.session_state.appunti_generati
                    st.markdown(appunti)
                    st.divider()
                    c1, c2 = st.columns(2)
                    with c1:
                        st.download_button("💾 Scarica .md", appunti, "appunti.md", "text/markdown")
                    with c2:
                        if st.button("📋 Copia Markdown"):
                            try:
                                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as tf:
                                    tf.write(appunti); tp = tf.name
                                subprocess.run(["powershell", "-command", f"Get-Content -LiteralPath '{tp}' -Encoding UTF8 | Set-Clipboard"], check=True)
                                os.remove(tp); st.toast("Copiato!", icon="📋")
                            except: st.error("Errore copia")
                else:
                    st.info("L'IA sta scrivendo gli appunti... attendi un istante.")

            elif "LaTeX" in name:
                if st.session_state.latex_generato:
                    latex = st.session_state.latex_generato
                    st.code(latex, language="latex")
                    st.divider()
                    c3, c4 = st.columns(2)
                    with c3:
                        st.download_button("💾 Scarica .tex", latex, "appunti.tex", "text/plain")
                    with c4:
                        if st.button("📋 Copia LaTeX"):
                            try:
                                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as tf:
                                    tf.write(latex); tp = tf.name
                                subprocess.run(["powershell", "-command", f"Get-Content -LiteralPath '{tp}' -Encoding UTF8 | Set-Clipboard"], check=True)
                                os.remove(tp); st.toast("Copiato!", icon="📋")
                            except: st.error("Errore copia")
                else:
                    st.info("Il codice LaTeX sarà pronto subito dopo gli appunti.")
