import urllib.request
import urllib.error
import json
import re
import os
import unicodedata
import difflib
from google import genai
from dotenv import load_dotenv

import notion_helper
import gemini_rate_tracker

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

def fetch_aggregated_transcript(video_urls: list) -> tuple[bool, str]:
    """
    Scarica ed unisce le trascrizioni di una o più parti video della stessa lezione/giornata.
    Se c'è un solo video valido, restituisce la trascrizione diretta.
    Se ci sono più video, li concatena formattando ciascuna sezione con intestazioni distinte.
    """
    if not video_urls:
        return False, "Nessun URL video specificato."
    
    unique_urls = []
    seen = set()
    for u in video_urls:
        if u and isinstance(u, str) and u.strip() and u.strip() not in seen:
            unique_urls.append(u.strip())
            seen.add(u.strip())
            
    if not unique_urls:
        return False, "Nessun URL video valido trovato."
        
    if len(unique_urls) == 1:
        success, text, _ = download_and_process(unique_urls[0])
        return success, text

    parts = []
    total = len(unique_urls)
    for idx, u in enumerate(unique_urls, 1):
        v_id, _ = extract_vimeo_ids(u)
        part_label = "Lezione Principale" if idx == 1 else f"Integrazione Lezione {idx - 1}"
        success, text, _ = download_and_process(u)
        header = f"=== 📝 PARTE {idx} di {total} ({part_label}) - Video ID: {v_id or u} ==="
        if success and text:
            parts.append(f"{header}\n\n{text.strip()}")
        else:
            parts.append(f"{header}\n\n[Trascrizione non disponibile: {text}]")
            
    separator = "\n\n" + ("=" * 60) + "\n\n"
    return True, separator.join(parts)

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
        gemini_rate_tracker.log_request()
        response = client.models.generate_content(
            model=model_name,
            contents=f"{prompt}\n\nTRASCRIZIONE:\n{text}"
        )
        cleaned_notes = notion_helper.sanitize_latex_formulas(response.text)
        return True, cleaned_notes
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
        gemini_rate_tracker.log_request()
        response = client.models.generate_content(
            model=model_name,
            contents=f"{LATEX_PROMPT}\n\nCONTENUTO MARKDOWN:\n{markdown_text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la conversione in LaTeX: {str(e)}"

def export_to_notion(course_name, course_page_id, lesson_date_str, markdown_text, is_same_video=False, api_key=None):
    """
    Workflow di esportazione su Notion:
    1. Cerca/Crea la tabella del corso su Notion.
    2. Cerca/Crea la riga della lezione (accoda se video diverso per stessa data, crea versione se stesso video).
    3. Converte il Markdown in blocchi Notion e li inserisce.
    Ritorna SEMPRE una tupla a 3 elementi: (success_bool, message_str, page_id_or_none)
    """
    # 1. Trova o crea il database del corso
    db_id, err = notion_helper.get_or_create_course_database(course_page_id, course_name, api_key)
    if err or not db_id:
        return False, f"{err}" if err else "Errore preparazione tabella Notion.", None

    # 2. Trova o crea la riga per la lezione/data
    lesson_page_id, is_existing, err_l = notion_helper.get_or_create_lesson_entry(db_id, lesson_date_str, is_same_video=is_same_video, api_key=api_key)
    if err_l or not lesson_page_id:
        return False, f"{err_l}" if err_l else "Errore creazione riga lezione su Notion.", None

    # 3. Trasforma il Markdown in blocchi Notion
    blocks = notion_helper.markdown_to_notion_blocks(markdown_text)

    # 4. Inserisci i blocchi nella pagina Notion
    success_app, err_app = notion_helper.append_notes_to_page(lesson_page_id, blocks, is_append=is_existing, api_key=api_key)
    if not success_app:
        return False, f"{err_app}" if err_app else "Errore scrittura blocchi su Notion.", None

    status_msg = "Appunti accodati alla lezione del giorno su Notion!" if is_existing else "Lezione creata con successo su Notion!"
    return True, status_msg, lesson_page_id

CANVAS_AGENT_PROMPT = """Sei un assistente AI specializzato, affiancato ad un Canvas contenente APPUNTI UNIVERSITARI.
Il tuo ruolo è duplice: aiutare l'utente a SISTEMARE/MODIFICARE gli appunti, ma anche e soprattutto aiutare l'utente a STUDIARE sugli appunti stessi (es. spiegando concetti, chiarendo dubbi).

REGOLE TASSATIVE E INVIOLABILI:
1. IL CANVAS CONTIENE ESCLUSIVAMENTE APPUNTI DIDATTICI ED ACCADEMICI DELLA LEZIONE:
   - NON inserire MAI nel Canvas testo conversazionale, presentazioni personali, guide su cosa sai fare, spiegazioni sul funzionamento dell'AI o meta-commenti.
   - Il Canvas NON deve MAI contenere le tue spiegazioni dei concetti quando l'utente ti fa una domanda per capire meglio. Quelle vanno SOLO in chat.

2. DISTINZIONE RIGIDA DEGLI INTENTI:
   a) STUDIO, SPIEGAZIONI, DOMANDE, SALUTI O CONVERSAZIONE (es. "spiegami questo concetto", "non ho capito X negli appunti", "fammi un esempio su Y", "ciao"):
      - Rispondi in modo professionale ed esauriente SOLO ED ESCLUSIVAMENTE sotto <<<CHAT_RESPONSE>>>. È qui che devi fare da tutor e spiegare i concetti.
      - NON MODIFICARE GLI APPUNTI per queste richieste.
      - Scrivi TASSATIVAMENTE ed unicamente la parola NO_CHANGE sotto <<<UPDATED_CANVAS>>>.
   
   b) ISTRUZIONI ESPLICITE DI MODIFICA DEGLI APPUNTI (es. "aggiungi questo paragrafo nel testo", "sintetizza la sezione 2 degli appunti", "inserisci una formula nel canvas"):
      - Spiega brevemente cosa hai fatto nella chat sotto <<<CHAT_RESPONSE>>>.
      - Fornisci l'INTERO documento Markdown degli appunti aggiornato sotto <<<UPDATED_CANVAS>>> (contenente SOLO ed ESCLUSIVAMENTE materiale didattico).
      - MODIFICA IL CANVAS SOLO QUANDO L'UTENTE LO CHIEDE ESPRESSAMENTE.

3. TRASCRIZIONE MULTIPLA / LEZIONI AGGREGATE:
   - Se la trascrizione grezza contiene più parti (es. PARTE 1, PARTE 2), significa che la lezione del giorno è composta da più video/integrazioni.
   - Utilizza l'insieme di tutte le parti della trascrizione e degli appunti per rispondere con la massima precisione ed accuratezza.

4. PRESERVAZIONE TASSATIVA DELLE IMMAGINI E DEI MEDIA:
   - Se il documento Canvas contiene tag immagine del tipo `![...](URL)`, `![...|50%](URL)` o `![Immagine](https://...)`, DEVI ASSOLUTAMENTE mantenerli intatti e posizionati esattamente nello stesso identico punto contestuale in cui si trovano originariamente.
   - È SEVERAMENTE VIETATO rimuovere, omettere, spostare arbitrariamente o alterare gli URL e i parametri di dimensione dei tag immagine quando riscrivi, sintetizzi, espandi, formatti o aggiorni il Canvas sotto <<<UPDATED_CANVAS>>>.
   - Le immagini sono parte integrante del materiale didattico e devono essere sempre preservate nella loro posizione originale rispetto al testo circostante.

5. CITAZIONI E TESTO SELEZIONATO DALL'UTENTE:
   - Se l'istruzione dell'utente include una citazione o testo di riferimento (es. `> ❝ **Testo selezionato:** ...`), considera tale frammento come il focus primario dell'intervento.
   - Se l'utente chiede chiarimenti, spiegazioni o esempi su quel testo, fornisci la spiegazione didattica approfondita sotto <<<CHAT_RESPONSE>>> e mantieni TASSATIVAMENTE NO_CHANGE sotto <<<UPDATED_CANVAS>>>.
   - Se l'utente chiede una modifica, riscrittura, semplificazione o espansione di quel passaggio, aggiorna l'intero documento sotto <<<UPDATED_CANVAS>>> modificando con precisione chirurgica quel punto specifico e preservando inalterato il resto del documento.

FORMATO DI RISPOSTA TASSATIVO ED OBBLIGATORIO:
<<<CHAT_RESPONSE>>>
[Risposta conversazionale, spiegazioni dei concetti per lo studio o descrizione di cosa hai modificato]
<<<UPDATED_CANVAS>>>
[Testo Markdown degli appunti completi OPPURE la sola parola NO_CHANGE se non è stata richiesta una modifica esplicita agli appunti]"""

CANVAS_SPLIT_REGEX = re.compile(r'(?:\*{0,2}|#{0,3})<{1,4}\s*UPDATED_CANVAS[:\s]*>{0,4}(?:\*{0,2})', re.IGNORECASE)
CHAT_TAG_REGEX = re.compile(r'(?:\*{0,2}|#{0,3})<{1,4}\s*CHAT_RESPONSE[:\s]*>{0,4}(?:\*{0,2})', re.IGNORECASE)

def parse_agent_response(raw_text: str) -> tuple[str, str | None]:
    """
    Normalizza e separa la risposta dell'Agente Canvas in (chat_reply, canvas_part).
    Gestisce con tolleranza eventuali errori di formattazione del modello (es. parentesi mancanti come <<<UPDATED_CANVAS>,
    spaziature anomale, markdown grassetto, tag minuscoli, ecc.).
    Restituisce:
      - chat_reply (str): il messaggio destinato alla chat.
      - canvas_part (str | None): il testo Markdown aggiornato per il Canvas, oppure None/'NO_CHANGE' se non presente/non modificato.
    """
    if not raw_text:
        return "", None

    match = CANVAS_SPLIT_REGEX.search(raw_text)
    if match:
        chat_raw = raw_text[:match.start()]
        canvas_raw = raw_text[match.end():].strip()
        chat_reply = CHAT_TAG_REGEX.sub('', chat_raw).strip()
        
        cleaned_check = canvas_raw.upper().replace('"', '').replace("'", "").replace("`", "").replace("*", "").strip()
        if cleaned_check in ["NO_CHANGE", "NO_CHANGES", "NO CHANGE", "NESSUNA_MODIFICA", "NESSUN_CAMBIAMENTO", "NO-CHANGE", ""]:
            return chat_reply or "Ho elaborato la tua richiesta.", "NO_CHANGE"
        
        return chat_reply or "Ho applicato le modifiche al Canvas.", canvas_raw
    else:
        chat_reply = CHAT_TAG_REGEX.sub('', raw_text).strip()
        return chat_reply or "Risposta dell'assistente.", None

def agent_edit_notes(current_markdown, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite"):
    """
    Agente AI per la modifica interattiva degli appunti nel Canvas.
    Riceve il testo attuale del Canvas, la trascrizione grezza originale (se disponibile), l'istruzione dell'utente e lo storico dialogo.
    Restituisce tupla: (success_bool, chat_reply, updated_markdown)
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata. Assicurati di aver configurato GOOGLE_API_KEY.", current_markdown

    current_markdown = notion_helper.normalize_images_to_markdown(current_markdown or "")
    client = genai.Client(api_key=api_key)

    history_formatted = ""
    if chat_history:
        for msg in chat_history[-6:]:
            role = "Utente" if msg.get("role") == "user" else "Assistente"
            history_formatted += f"{role}: {msg.get('content')}\n"

    transcript_section = f"\n\nTRASCRIZIONE GREZZA ORIGINALE:\n---\n{raw_transcript}\n---" if raw_transcript else ""

    user_payload = f"""DOCUMENTO APPUNTI ATTUALE (CANVAS):
---
{current_markdown}
---{transcript_section}

STORICO DIALOGO RECENTE:
{history_formatted if history_formatted else '(Nessun messaggio precedente)'}

ISTRUZIONE DELL'UTENTE:
{user_instruction}"""

    try:
        gemini_rate_tracker.log_request()
        response = client.models.generate_content(
            model=model_name,
            contents=f"{CANVAS_AGENT_PROMPT}\n\n{user_payload}"
        )
        raw_text = response.text or ""
        
        chat_reply, canvas_part = parse_agent_response(raw_text)
        if canvas_part and canvas_part != "NO_CHANGE" and len(canvas_part) > 5:
            updated_markdown = notion_helper.sanitize_latex_formulas(canvas_part)
            updated_markdown = notion_helper.normalize_images_to_markdown(updated_markdown)
        else:
            updated_markdown = current_markdown

        return True, chat_reply, updated_markdown
    except Exception as e:
        return False, f"Errore durante l'elaborazione con l'Agente AI: {str(e)}", current_markdown

def agent_edit_notes_stream(current_markdown, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite"):
    """
    Generatore streaming per l'Agente AI del Canvas.
    Invia i chunk di testo in tempo reale man mano che arrivano dal modello Gemini, includendo la trascrizione grezza originale se fornita.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Chiave API di Google non trovata. Configura GOOGLE_API_KEY.")

    current_markdown = notion_helper.normalize_images_to_markdown(current_markdown or "")
    client = genai.Client(api_key=api_key)

    history_formatted = ""
    if chat_history:
        for msg in chat_history[-6:]:
            role = "Utente" if msg.get("role") == "user" else "Assistente"
            history_formatted += f"{role}: {msg.get('content')}\n"

    transcript_section = f"\n\nTRASCRIZIONE GREZZA ORIGINALE:\n---\n{raw_transcript}\n---" if raw_transcript else ""

    user_payload = f"""DOCUMENTO APPUNTI ATTUALE (CANVAS):
---
{current_markdown}
---{transcript_section}

STORICO DIALOGO RECENTE:
{history_formatted if history_formatted else '(Nessun messaggio precedente)'}

ISTRUZIONE DELL'UTENTE:
{user_instruction}"""

    gemini_rate_tracker.log_request()
    response_stream = client.models.generate_content_stream(
        model=model_name,
        contents=f"{CANVAS_AGENT_PROMPT}\n\n{user_payload}"
    )

    for chunk in response_stream:
        if chunk.text:
            yield chunk.text


# ==============================================================================
# MODALITÀ: MODIFICA MIRATA DI UNA SINGOLA SEZIONE ("MODIFICA SOLO QUESTO")
# ==============================================================================

CANVAS_TARGETED_EDIT_PROMPT = """Sei un assistente editoriale accademico di altissimo livello specializzato nella revisione e miglioramento mirato di appunti universitari.
Il tuo compito è modificare, riscrivere o espandere ESCLUSIVAMENTE la specifica porzione di testo selezionata dall'utente all'interno del documento Canvas.

CONTESTO COMPLETO:
Ti viene fornito l'INTERO documento Canvas come riferimento per comprendere l'argomento trattato, la terminologia, lo stile didattico, la notazione e la coerenza complessiva.

SEZIONE SPECIFICA DA MODIFICARE (TARGET):
Ti viene fornito il testo esatto della porzione che l'utente intende modificare.

REGOLE TASSATIVE:
1. NON RISCRIVERE L'INTERO DOCUMENTO! Sotto <<<TARGETED_REPLACEMENT>>> devi fornire SOLAMENTE il testo sostitutivo pronto per rimpiazzare quel singolo passaggio nel documento.
2. Il testo generato deve integrarsi alla perfezione e con continuità logica, stilistica e grammaticale con il testo che precede e segue nel documento.
3. Se nella sezione target o nel contesto sono presenti formule matematiche LaTeX ($...$ o $$...$$), mantienile e formattale con la massima cura e correttezza.
4. Se la sezione target contiene un tag immagine del tipo `![...](...)`, conservalo intatto salvo diversa e inequivocabile istruzione dell'utente.
5. TITOLI E INTESTAZIONI: Se la sezione selezionata è un titolo o parte di un titolo (es. `## Titolo`), genera il nuovo titolo con un unico livello appropriato (es. `## Nuovo Titolo` o `### Nuovo Titolo`), evitando tassativamente cancelletti doppi o combinazioni anomale come `## ###`.
6. Sotto <<<CHAT_RESPONSE>>> scrivi una spiegazione sintetica (1-2 frasi) in cui descrivi cordialmente cosa hai modificato nel passaggio.

FORMATO DI RISPOSTA OBBLIGATORIO:
<<<CHAT_RESPONSE>>>
[Breve spiegazione sintetica di cosa hai modificato nel passaggio]
<<<TARGETED_REPLACEMENT>>>
[SOLO ED ESCLUSIVAMENTE il nuovo testo che sostituirà la porzione selezionata]"""

TARGETED_SPLIT_REGEX = re.compile(
    r'(?:'
    r'<{1,4}\s*(?:TARGETED[_\s]*REPLACEMENT|TARGET[_\s]*REPLACEMENT|REPLACEMENT|'
    r'MODIFICA[_\s]*MIRATA|TESTO[_\s]*SOSTITUTIVO|SEZIONE[_\s]*MODIFICATA|'
    r'NUOVO[_\s]*TESTO|NEW[_\s]*TEXT)[:\s]*>{0,4}|'
    r'(?:\*{1,2}|#{1,3})\s*(?:TARGETED[_\s]*REPLACEMENT|REPLACEMENT|MODIFICA[_\s]*MIRATA|'
    r'TESTO[_\s]*SOSTITUTIVO|NUOVA[_\s]*VERSIONE|VERSIONE[_\s]*MODIFICATA)[:\s]*(?:\*{0,2})'
    r')',
    re.IGNORECASE
)

def parse_targeted_agent_response(raw_text: str) -> tuple[str, str | None]:
    """
    Separa la risposta per la modifica mirata in (chat_reply, replacement_part).
    Restituisce:
      - chat_reply (str): messaggio di spiegazione sintetica per la chat.
      - replacement_part (str | None): solo il testo sostitutivo da iniettare nella porzione del Canvas.
    Supporta tag standard, varianti Markdown, code block e formati alternativi in fallback.
    """
    if not raw_text:
        return "", None

    match = TARGETED_SPLIT_REGEX.search(raw_text)
    if match:
        chat_raw = raw_text[:match.start()]
        replacement_raw = raw_text[match.end():].strip()
        chat_reply = CHAT_TAG_REGEX.sub('', chat_raw).strip()
        # Rimuove wrapping di blocco markdown se il modello ha racchiuso il replacement in un code block
        if replacement_raw.startswith("```markdown") and replacement_raw.endswith("```"):
            replacement_raw = replacement_raw[11:-3].strip()
        elif replacement_raw.startswith("```latex") and replacement_raw.endswith("```"):
            replacement_raw = replacement_raw[8:-3].strip()
        elif replacement_raw.startswith("```") and replacement_raw.endswith("```"):
            replacement_raw = replacement_raw[3:-3].strip()
        return chat_reply or "Ho modificato la sezione selezionata.", replacement_raw

    # Fallback 1: Blocco di codice ```markdown ... ``` o ```latex ... ``` presente nel testo
    code_match = re.search(r'```(?:markdown|latex)?\s*\n(.*?)\n```', raw_text, re.DOTALL)
    if code_match:
        chat_raw = raw_text[:code_match.start()]
        chat_reply = CHAT_TAG_REGEX.sub('', chat_raw).strip()
        repl_raw = code_match.group(1).strip()
        return chat_reply or "Ho modificato la sezione selezionata.", repl_raw

    # Fallback 2: Se c'è <<<CHAT_RESPONSE>>> seguito da testo e poi da un doppio a capo
    if CHAT_TAG_REGEX.search(raw_text):
        cleaned = CHAT_TAG_REGEX.sub('', raw_text).strip()
        parts = cleaned.split('\n\n', 1)
        if len(parts) == 2 and len(parts[1].strip()) > 0:
            return parts[0].strip(), parts[1].strip()

    # Fallback 3: Riconoscimento prima riga discorsiva seguita da testo modificato
    cleaned = raw_text.strip()
    if '\n\n' in cleaned:
        first_line, rest = cleaned.split('\n\n', 1)
        if len(first_line) < 160 and any(w in first_line.lower() for w in ["ho ", "ecco", "modificato", "aggiornato", "sostituito", "riscritto", "corretto"]):
            return first_line.strip(), rest.strip()

    return "Ho modificato la sezione selezionata.", cleaned if cleaned else None

def extract_targeted_edit_request(user_prompt: str) -> tuple[str | None, str]:
    """
    Verifica se il messaggio dell'utente richiede una modifica mirata di una sezione.
    Riconosce i marcatori inseriti dall'interfaccia:
    > 🎯 **[MODIFICA MIRATA SEZIONE]**
    > riga 1
    > riga 2

    istruzione utente...

    Restituisce tupla: (target_section, clean_instruction)
    Se non è una modifica mirata, target_section è None e clean_instruction è user_prompt.
    """
    if not user_prompt or ("MODIFICA MIRATA SEZIONE" not in user_prompt and "MODIFICA SEZIONE" not in user_prompt):
        return None, user_prompt

    lines = user_prompt.splitlines()
    target_lines = []
    instruction_lines = []
    is_in_quote = False
    quote_finished = False

    for line in lines:
        stripped = line.strip()
        if "MODIFICA MIRATA SEZIONE" in stripped or "MODIFICA SEZIONE" in stripped:
            is_in_quote = True
            continue
        if is_in_quote and not quote_finished:
            if stripped.startswith('>'):
                content = stripped.lstrip('>').strip()
                target_lines.append(content)
            elif stripped == '':
                if target_lines and target_lines[-1] != '':
                    target_lines.append('')
            else:
                quote_finished = True
                instruction_lines.append(line)
        else:
            instruction_lines.append(line)

    while target_lines and not target_lines[-1]:
        target_lines.pop()

    target_section = "\n".join(target_lines).strip() if target_lines else None
    clean_instruction = "\n".join(instruction_lines).strip() or "Migliora e aggiorna questo passaggio."
    
    return target_section, clean_instruction

def _normalize_char_for_proj(c: str) -> str:
    nfd = unicodedata.normalize('NFD', c.lower())
    return ''.join(ch for ch in nfd if unicodedata.category(ch) != 'Mn')

def replace_section_in_markdown(full_text: str, target_section: str, replacement: str) -> tuple[str, bool]:
    """
    Sostituisce con precisione e resilienza estrema la porzione target_section all'interno di full_text.
    Supera tutte le discrepanze tra anteprima HTML e Markdown sorgente:
    - Grassetto, corsivo, apici e pedici (*, **, _, `)
    - Formule matematiche LaTeX ($ e $$)
    - Punteggiatura tipografica (virgolette smart, trattini en/em-dash)
    - Spaziature, ritorni a capo multipli e indentazioni
    - Titoli ed intestazioni (#), prevenendo duplicazioni (es. '## ###')
    Restituisce: (nuovo_full_text, successo_bool)
    """
    if not full_text or target_section is None:
        return full_text, False

    target_clean = target_section.strip()
    if not target_clean:
        return full_text, False

    def _apply_replacement_with_heading_check(text: str, start_idx: int, end_idx: int, repl: str) -> str:
        line_start = text.rfind('\n', 0, start_idx) + 1
        prefix_on_line = text[line_start:start_idx]
        
        # Se prima della porzione trovata sulla riga ci sono solo cancelletti e spazi (es. '## ')
        # e il testo sostitutivo inizia a sua volta con uno o più cancelletti (es. '### Titolo' o '## Titolo')
        if re.match(r'^\s*#{1,6}\s*$', prefix_on_line) and repl.lstrip().startswith('#'):
            # Sostituiamo partendo dall'inizio della riga per evitare la duplicazione dei cancelletti
            res = text[:line_start] + repl + text[end_idx:]
        else:
            res = text[:start_idx] + repl + text[end_idx:]
            
        # Pulizia globale di sicurezza contro cancelletti doppi/multipli a inizio riga (es. '## ### ')
        res = re.sub(r'(?m)^(\s*#{1,6})\s+(#{1,6}\s+)', r'\2', res)
        return res

    # 1. Ricerca esatta (string match diretto)
    pos = full_text.find(target_clean)
    if pos != -1:
        new_text = _apply_replacement_with_heading_check(full_text, pos, pos + len(target_clean), replacement)
        return new_text, True

    # 2. Ricerca normalizzata su spazi, ritorni a capo e punteggiatura tipografica
    norm_quotes_target = target_clean.replace('“', '"').replace('”', '"').replace('’', "'").replace('‘', "'").replace('–', '-').replace('—', '-').replace('\u00a0', ' ')
    words = norm_quotes_target.split()
    if words:
        escaped_words = [re.escape(w) for w in words]
        pattern_str = r'\s+'.join(escaped_words)
        try:
            pattern = re.compile(pattern_str, re.DOTALL)
            m = pattern.search(full_text)
            if m:
                new_text = _apply_replacement_with_heading_check(full_text, m.start(), m.end(), replacement)
                return new_text, True
        except Exception:
            pass

    # 3. Ricerca tollerante alla formattazione Markdown inline (*, **, _, `, ecc.)
    if len(words) >= 2:
        delim = r'[\s*_~`\[\]()$>#\-=|]+'
        w_pats = [r'[*_~`]*' + re.escape(w.strip('*_~`"\'')) + r'[*_~`]*' for w in words if w.strip('*_~`"\'')]
        if len(w_pats) >= 2:
            try:
                pattern = re.compile(delim.join(w_pats), re.DOTALL)
                m = pattern.search(full_text)
                if m:
                    new_text = _apply_replacement_with_heading_check(full_text, m.start(), m.end(), replacement)
                    return new_text, True
            except Exception:
                pass

    # 4. Ricerca per proiezione alfanumerica (indipendente al 100% da markdown, KaTeX e formattazione)
    proj_chars, proj_indices = [], []
    for i, c in enumerate(full_text):
        if c.isalnum():
            proj_chars.append(_normalize_char_for_proj(c))
            proj_indices.append(i)

    proj_full = "".join(proj_chars)
    proj_target = "".join(_normalize_char_for_proj(c) for c in target_clean if c.isalnum())

    if proj_target and len(proj_target) >= 3:
        p_pos = proj_full.find(proj_target)
        if p_pos != -1:
            s_orig = proj_indices[p_pos]
            e_orig = proj_indices[p_pos + len(proj_target) - 1] + 1
            # Assorbe formattazioni markdown aperte/chiuse adiacenti (es. ** o *)
            while s_orig > 0 and full_text[s_orig - 1] in '*_~`':
                s_orig -= 1
            while e_orig < len(full_text) and full_text[e_orig] in '*_~`':
                e_orig += 1
            new_text = _apply_replacement_with_heading_check(full_text, s_orig, e_orig, replacement)
            return new_text, True

        # 4b. Anchor matching su selezioni estese (ancore testa e coda di 12 caratteri alfanumerici)
        if len(proj_target) >= 16:
            head = proj_target[:12]
            tail = proj_target[-12:]
            h_pos = proj_full.find(head)
            if h_pos != -1:
                t_pos = proj_full.find(tail, h_pos)
                if t_pos != -1:
                    span_len = t_pos + len(tail) - h_pos
                    if 0.65 * len(proj_target) <= span_len <= 1.35 * len(proj_target):
                        s_orig = proj_indices[h_pos]
                        e_orig = proj_indices[t_pos + len(tail) - 1] + 1
                        while s_orig > 0 and full_text[s_orig - 1] in '*_~`':
                            s_orig -= 1
                        while e_orig < len(full_text) and full_text[e_orig] in '*_~`':
                            e_orig += 1
                        new_text = _apply_replacement_with_heading_check(full_text, s_orig, e_orig, replacement)
                        return new_text, True

    # 5. Distinctive Token Anchor & Sequence Matching
    # Risolve discrepanze con formule matematiche LaTeX complesse (\frac, \sqrt, \int), KaTeX MathML, apostrofi e liste numerate
    STOP_WORDS = {'il', 'la', 'lo', 'i', 'gli', 'le', 'un', 'uno', 'una', 'di', 'a', 'da', 'in', 'con', 'su', 'per', 'tra', 'fra', 'e', 'o', 'se', 'ma', 'ed', 'ad'}
    target_tokens = [m.group().lower() for m in re.finditer(r'[a-zA-Z0-9àèéìòùÀÈÉÌÒÙ]{2,}', norm_quotes_target)]
    full_tokens = [(m.start(), m.end(), m.group().lower()) for m in re.finditer(r'[a-zA-Z0-9àèéìòùÀÈÉÌÒÙ]{2,}', full_text)]

    if target_tokens and full_tokens:
        T = len(target_tokens)
        dist_heads = [t for t in target_tokens[:min(4, T)] if t not in STOP_WORDS] or target_tokens[:1]
        dist_tails = [t for t in target_tokens[max(0, T - 4):] if t not in STOP_WORDS] or target_tokens[-1:]

        head_cand = [i for i, t in enumerate(full_tokens) if t[2] in dist_heads]
        tail_cand = [j for j, t in enumerate(full_tokens) if t[2] in dist_tails]

        best_score = 0
        best_span = None

        for h_i in head_cand:
            for t_j in tail_cand:
                if t_j >= h_i:
                    span_tokens = [full_tokens[k][2] for k in range(h_i, t_j + 1)]
                    score = difflib.SequenceMatcher(None, target_tokens, span_tokens).ratio()
                    if score > best_score:
                        best_score = score
                        best_span = (full_tokens[h_i][0], full_tokens[t_j][1])

        if best_span and best_score >= 0.40:
            s_orig, e_orig = best_span
            while s_orig > 0 and full_text[s_orig - 1] in '*_~`':
                s_orig -= 1
            while e_orig < len(full_text) and full_text[e_orig] in '*_~`':
                e_orig += 1
            return _apply_replacement_with_heading_check(full_text, s_orig, e_orig, replacement), True

        # Fallback: Sliding window
        min_w = max(1, int(T * 0.5))
        max_w = min(len(full_tokens), int(T * 2.5) + 1)
        for i in range(0, len(full_tokens)):
            for w_len in range(min_w, min(max_w, len(full_tokens) - i + 1)):
                j = i + w_len
                span_tokens = [full_tokens[k][2] for k in range(i, j)]
                score = difflib.SequenceMatcher(None, target_tokens, span_tokens).ratio()
                if score > best_score:
                    best_score = score
                    best_span = (full_tokens[i][0], full_tokens[j-1][1])

        if best_span and best_score >= 0.40:
            s_orig, e_orig = best_span
            while s_orig > 0 and full_text[s_orig - 1] in '*_~`':
                s_orig -= 1
            while e_orig < len(full_text) and full_text[e_orig] in '*_~`':
                e_orig += 1
            return _apply_replacement_with_heading_check(full_text, s_orig, e_orig, replacement), True

    # 6. Ricerca basata su prime e ultime parole (per selezioni ampie)
    if len(words) >= 6:
        first_part = r'\s+'.join([re.escape(w.strip('*_~`"\'')) for w in words[:3]])
        last_part = r'\s+'.join([re.escape(w.strip('*_~`"\'')) for w in words[-3:]])
        try:
            pattern = re.compile(f"{first_part}.*?{last_part}", re.DOTALL)
            m = pattern.search(full_text)
            if m:
                new_text = _apply_replacement_with_heading_check(full_text, m.start(), m.end(), replacement)
                return new_text, True
        except Exception:
            pass

    return full_text, False

def agent_edit_targeted_stream(current_markdown, target_section, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite"):
    """
    Generatore streaming per la modifica mirata di una singola sezione degli appunti.
    Fornisce l'intero documento Canvas come contesto, ma istruisce il modello a generare SOLO
    il testo sostitutivo per la sezione selezionata.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Chiave API di Google non trovata. Configura GOOGLE_API_KEY.")

    client = genai.Client(api_key=api_key)

    history_formatted = ""
    if chat_history:
        for msg in chat_history[-6:]:
            role = "Utente" if msg.get("role") == "user" else "Assistente"
            history_formatted += f"{role}: {msg.get('content')}\n"

    transcript_section = f"\n\nTRASCRIZIONE GREZZA ORIGINALE (CONTESTO INTEGRATIVO):\n---\n{raw_transcript}\n---" if raw_transcript else ""

    user_payload = f"""DOCUMENTO CANVAS COMPLETO (SOLO PER CONTESTO E COERENZA):
---
{current_markdown}
---{transcript_section}

SEZIONE DA MODIFICARE / SOSTITUIRE (TARGET):
<<<TARGET_SECTION>>>
{target_section}
<<<END_TARGET_SECTION>>>

STORICO DIALOGO RECENTE:
{history_formatted if history_formatted else '(Nessun messaggio precedente)'}

ISTRUZIONE DELL'UTENTE PER QUESTA SEZIONE:
{user_instruction}"""

    gemini_rate_tracker.log_request()
    response_stream = client.models.generate_content_stream(
        model=model_name,
        contents=f"{CANVAS_TARGETED_EDIT_PROMPT}\n\n{user_payload}"
    )

    for chunk in response_stream:
        if chunk.text:
            yield chunk.text



