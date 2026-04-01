import streamlit as st
import urllib.request
import urllib.error
import json
import re

def extrat_clean_text_from_vtt(vtt_content):
    """
    Prende il contenuto in formato VTT in memoria e restituisce solo il testo della trascrizione.
    """
    cleaned_lines = []
    lines = vtt_content.splitlines()
        
    for line in lines:
        line = line.strip()
        
        # Salta righe vuote
        if not line:
            continue
            
        # Salta l'intestazione WEBVTT e metadati
        if line == 'WEBVTT' or line.startswith('Kind:') or line.startswith('Language:'):
            continue
            
        # Salta i numeri identificativi dei cue (solo numeri)
        if re.match(r'^\d+$', line):
            continue
            
        # Salta i timestamp (es. 00:00:00.000 --> 00:00:05.000)
        if '-->' in line:
            continue
            
        cleaned_lines.append(line)
        
    return ' '.join(cleaned_lines)

def extract_vimeo_ids(url):
    """Estrae l'ID e l'hash del video dal link Vimeo."""
    match = re.search(r'vimeo\.com/(\d+)/([a-zA-Z0-9]+)', url)
    if match:
        return match.group(1), match.group(2)
    return None, None

def download_and_process(url):
    video_id, hash_id = extract_vimeo_ids(url)
    
    if not video_id or not hash_id:
        return False, "Link non valido. Assicurati che sia nel formato https://vimeo.com/ID/HASH?..."
        
    api_url = f"https://player.vimeo.com/video/{video_id}/config?h={hash_id}"
    
    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        return False, f"Errore durante la chiamata a Vimeo: {e}"
        
    vtt_link = None
    tracks = data.get('request', {}).get('text_tracks', [])
    for track in tracks:
        vtt_link = track.get('url')
        if vtt_link:
            break
            
    if not vtt_link:
        return False, "Nessuna trascrizione autogenerata trovata per questo video."
        
    try:
        vtt_req = urllib.request.Request(vtt_link, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(vtt_req) as response:
            vtt_content = response.read().decode('utf-8')
    except urllib.error.URLError as e:
        return False, f"Errore durante il download del file VTT: {e}"

    # Pulisci il testo del VTT direttamente in memoria
    clean_text = extrat_clean_text_from_vtt(vtt_content)
    
    return True, clean_text

# --- INIZIO INTERFACCIA STREAMLIT ---
st.set_page_config(page_title="Vimeo Transcript Downloader", page_icon="📝")

st.title("📝 Scarica Trascrizioni Vimeo")
st.markdown("Inserisci il link del video Vimeo per ottenere immediatamente il testo pulito della trascrizione senza salvare file sul computer.")

# Inizializziamo lo stato
if 'testo_estratto' not in st.session_state:
    st.session_state.testo_estratto = None

# Input utente
url = st.text_input("Link Vimeo", placeholder="https://vimeo.com/1178624350/97e387d121?...")

if st.button("Ottieni Trascrizione", type="primary"):
    if not url:
        st.warning("⚠️ Inserisci il link di Vimeo.")
    else:
        with st.spinner("Scaricamento e pulizia in corso..."):
            success, result_message = download_and_process(url)
            
        if success:
            st.success("✅ Trascrizione ottenuta e pulita con successo!")
            st.session_state.testo_estratto = result_message
        else:
            st.error(f"❌ Errore: {result_message}")

# Se abbiamo del testo caricato, mostriamo i bottoni
if st.session_state.testo_estratto is not None:
    testo_completo = st.session_state.testo_estratto
    
    anteprima = testo_completo[:500] + "..."
    st.write("### Anteprima del testo:")
    st.info(anteprima)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.download_button(
            label="💾 Scarica file .txt",
            data=testo_completo,
            file_name="trascrizione.txt",
            mime="text/plain"
        )
    
    with col2:
        if st.button("📋 Copia Trascrizione", type="secondary"):
            import subprocess
            import tempfile
            import os
            try:
                # Scriviamo il testo in un file temporaneo in UTF-8 per evitare problemi di codifica 
                # della shell di Windows (che spesso rovina le lettere accentate)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as temp_file:
                    temp_file.write(testo_completo)
                    temp_path = temp_file.name
                    
                # Usiamo Get-Content di powershell dicendogli esplicitamente di leggere in UTF-8
                subprocess.run(
                    ["powershell", "-command", f"Get-Content -LiteralPath '{temp_path}' -Encoding UTF8 | Set-Clipboard"],
                    check=True
                )
                
                # Cancelliamo il file temporaneo
                os.remove(temp_path)
                
                st.toast("✅ Testo copiato negli appunti con successo", icon="📋")
            except Exception as ex:
                st.error(f"Errore nella copia: {ex}")
