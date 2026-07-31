import urllib.request
import urllib.error
import json
import re
import os
from google import genai
from dotenv import load_dotenv

import notion_helper

load_dotenv()

DEFAULT_PROMPT = """Riceverai in input la trascrizione grezza di una lezione universitaria.
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

LATEX_PROMPT = r"""Riceverai in input degli appunti universitari scritti in formato Markdown.
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

def extrat_clean_text_from_vtt(vtt_content):
    """
    Prende il contenuto in formato VTT in memoria e restituisce solo il testo della trascrizione.
    """
    cleaned_lines = []
    lines = vtt_content.splitlines()
        
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
            
        if line == 'WEBVTT' or line.startswith('Kind:') or line.startswith('Language:'):
            continue
            
        if re.match(r'^\d+$', line):
            continue
            
        if '-->' in line:
            continue
            
        cleaned_lines.append(line)
        
    return ' '.join(cleaned_lines)

def extract_vimeo_ids(url):
    """Estrae l'ID e l'hash del video dal link Vimeo."""
    match = re.search(r'vimeo\.com/(\d+)/([a-zA-Z0-9]+)', url)
    if match:
        return match.group(1), match.group(2)
    match_simple = re.search(r'vimeo\.com/(\d+)', url)
    if match_simple:
        return match_simple.group(1), ""
    return None, None

def download_and_process(url):
    video_id, hash_id = extract_vimeo_ids(url)
    
    if not video_id:
        return False, "Link non valido. Assicurati che sia nel formato https://vimeo.com/ID/HASH?...", None
        
    api_url = f"https://player.vimeo.com/video/{video_id}/config"
    if hash_id:
        api_url += f"?h={hash_id}"
    
    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        return False, f"Errore durante la chiamata a Vimeo: {e}", video_id
        
    vtt_link = None
    tracks = data.get('request', {}).get('text_tracks', [])
    for track in tracks:
        vtt_link = track.get('url')
        if vtt_link:
            break
            
    if not vtt_link:
        return False, "Nessuna trascrizione autogenerata trovata per questo video.", video_id
        
    try:
        vtt_req = urllib.request.Request(vtt_link, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(vtt_req) as response:
            vtt_content = response.read().decode('utf-8')
    except urllib.error.URLError as e:
        return False, f"Errore durante il download del file VTT: {e}", video_id

    clean_text = extrat_clean_text_from_vtt(vtt_content)
    return True, clean_text, video_id

def generate_notes(text, model_name="gemini-3.5-flash-lite", custom_prompt=None):
    """
    Invia la trascrizione a Gemini per generare appunti strutturati.
    Default model: gemini-3.5-flash-lite
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata. Assicurati di aver configurato GOOGLE_API_KEY nel file .env o nella sidebar."

    client = genai.Client(api_key=api_key)
    prompt = custom_prompt if (custom_prompt and custom_prompt.strip()) else DEFAULT_PROMPT
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=f"{prompt}\n\nTRASCRIZIONE:\n{text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la generazione degli appunti con {model_name}: {str(e)}"

def generate_latex(markdown_text, model_name="gemini-3.5-flash-lite"):
    """
    Converte gli appunti Markdown in codice LaTeX professionale.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata."

    client = genai.Client(api_key=api_key)
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=f"{LATEX_PROMPT}\n\nCONTENUTO MARKDOWN:\n{markdown_text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la conversione in LaTeX: {str(e)}"

def export_to_notion(course_name, course_page_id, lesson_date_str, markdown_text, api_key=None):
    """
    Workflow di esportazione su Notion:
    1. Cerca/Crea la tabella del corso su Notion.
    2. Cerca/Crea la riga della lezione per la data.
    3. Converte il Markdown in blocchi Notion e li inserisce/accoda.
    Ritorna SEMPRE una tupla a 3 elementi: (success_bool, message_str, page_id_or_none)
    """
    # 1. Trova o crea il database del corso
    db_id, err = notion_helper.get_or_create_course_database(course_page_id, course_name, api_key)
    if err or not db_id:
        return False, f"Errore preparazione tabella Notion: {err}", None

    # 2. Trova o crea la riga per la lezione/data
    lesson_page_id, is_existing, err_l = notion_helper.get_or_create_lesson_entry(db_id, lesson_date_str, api_key)
    if err_l or not lesson_page_id:
        return False, f"Errore creazione riga lezione su Notion: {err_l}", None

    # 3. Trasforma il Markdown in blocchi Notion
    blocks = notion_helper.markdown_to_notion_blocks(markdown_text)

    # 4. Inserisci/Accoda i blocchi nella pagina Notion
    success_app, err_app = notion_helper.append_notes_to_page(lesson_page_id, blocks, is_append=is_existing, api_key=api_key)
    if not success_app:
        return False, f"Errore scrittura blocchi su Notion: {err_app}", None

    status_msg = "Appunti accodati alla lezione esistente!" if is_existing else "Nuova lezione creata con appunti su Notion!"
    return True, status_msg, lesson_page_id
