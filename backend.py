import urllib.request
import urllib.error
import json
import re
import os
from google import genai
from dotenv import load_dotenv

# Carica le variabili d'ambiente dal file .env
load_dotenv()

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

def generate_notes(text):
    """
    Invia la trascrizione a Gemini per generare appunti strutturati.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata. Assicurati di aver configurato GOOGLE_API_KEY nel file .env."

    client = genai.Client(api_key=api_key)
    
    prompt = """Riceverai in input la trascrizione grezza di una lezione universitaria.
Il tuo compito è trasformarla in appunti ordinati, leggibili e ben strutturati, mantenendo il più possibile il contenuto originale.

Regole fondamentali:
- NON fare riassunti.
- NON semplificare eliminando concetti.
- NON omettere esempi fatti dal professore.
- NON aggiungere contenuti inventati.
- NON scrivere introduzioni o conclusioni.
- NON scrivere commenti personali.
- NON scrivere frasi come "ecco gli appunti sistemati".

Mantieni:
- spiegazioni
- esempi
- formule
- analogie
- osservazioni del professore
- passaggi logici
- dettagli tecnici

Se il professore ripete un concetto identico più volte consecutivamente, mantieni una sola versione completa e chiara della spiegazione.
Se il professore fa recap di lezioni precedenti:
- NON includerli
- a meno che introducano nuovi concetti utili alla comprensione.

Organizza gli appunti usando:
- titoli
- sottotitoli
- elenchi puntati
- paragrafi
- blocchi codice
- formule

L'output deve essere SOLO in formato Markdown.

Quando vengono introdotti termini tecnici:
- mantieni i termini originali
- migliora solo la forma grammaticale e la leggibilità.

Se una frase della trascrizione è grammaticalmente rotta ma il significato è chiaro:
- correggi la grammatica
- senza cambiare il significato.

Se ci sono formule:
- usa LaTeX markdown.

Se ci sono codice o comandi:
- usa blocchi markdown con il linguaggio corretto.

Mantieni uno stile discorsivo e adatto allo studio universitario."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{prompt}\n\nTRASCRIZIONE:\n{text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la generazione degli appunti: {str(e)}"

def generate_latex(markdown_text):
    """
    Converte gli appunti Markdown in codice LaTeX professionale.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata."

    client = genai.Client(api_key=api_key)
    
    prompt = """Riceverai in input degli appunti universitari scritti in formato Markdown.
Il tuo compito è convertirli in codice LaTeX ben formattato, mantenendo il contenuto originale il più fedele possibile.

Regole fondamentali:
- NON fare riassunti.
- NON semplificare i concetti.
- NON eliminare esempi.
- NON aggiungere contenuti inventati.
- NON modificare il significato delle spiegazioni.
- Mantieni tutte le formule, esempi, osservazioni e passaggi logici.

Regole di formattazione LaTeX:
- Usa uno stile pulito e leggibile.
- Usa:
  - \chapter{}
  - \section{}
  - \subsection{}
  - \subsubsection*{}
- NON usare:
  - \paragraph{}
  - \subparagraph{}
  - \subsubsection{}
- Dopo ogni titolo o sottotitolo usa sempre: \noindent
- I paragrafi devono essere scritti in forma discorsiva.
- Evita elenchi puntati inutili se il testo è discorsivo.
- Mantieni gli elenchi solo quando realmente utili.

Formule matematiche:
- Usa la sintassi LaTeX corretta.
- Formule inline: $...$
- Formule centrate:
  \[
  ...
  \]

Codice e comandi:
- Usa:
  \begin{lstlisting}
  ...
  \end{lstlisting}

Immagini:
- Se nel markdown è presente un'immagine:
  usa il formato:
  \begin{figure}[H]
      \centering
      \includegraphics[width=0.8\textwidth]{img/nomefile}
      \caption{}
  \end{figure}

Tabelle:
- Converti le tabelle markdown in tabelle LaTeX usando tabular.

Stile:
- Il linguaggio deve essere impersonale e adatto ad appunti universitari.
- Mantieni uno stile tecnico, chiaro e ordinato.
- Non usare emoji.
- Non scrivere introduzioni o conclusioni.

Output:
- Restituisci SOLO codice LaTeX.
- Non racchiudere il risultato in blocchi markdown."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{prompt}\n\nCONTENUTO MARKDOWN:\n{markdown_text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la conversione in LaTeX: {str(e)}"
