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

DEFAULT_PROMPT = """You are an expert university professor, academic author, and elite instructional designer.
Your task is to transform the raw transcript of a university lecture into comprehensive, exhaustive, deep, and beautifully structured academic notes (perfect for Notion, university study, and exam preparation).

CRITICAL LANGUAGE RULE:
- You MUST write the notes in the EXACT SAME LANGUAGE as the lecture transcript. 
- If the lecture is in Italian, the notes MUST be written in Italian. 
- If the lecture is in English, the notes MUST be written in English.
- NEVER mix languages.

ABSOLUTE PRINCIPLE: ZERO CONCEPTUAL COMPRESSION (NO AGGRESSIVE SUMMARIES)
- NEVER condense, truncate, or aggressively summarize the lecture into brief, generic paragraphs.
- Maintain ALL explanations, nuances, thought experiments, analogies, proofs, technical details, and examples provided by the professor.
- DO NOT collapse multiple distinct concepts into a single paragraph. Every distinct idea, definition, sub-concept, and nuance must receive its own dedicated space and in-depth treatment.
- If the professor outlines the lecture objectives, roadmap, historical motivation, or syllabus, preserve them fully at the beginning of the notes as a structured overview.

PEDAGOGICAL & NOTION-READY FORMATTING GUIDELINES:

1. GRANULAR MULTI-LEVEL HIERARCHY:
   - Structure the notes logically with numbered headings and subheadings:
     * `# Part [Number] — [Macro Module Title]` for major thematic units, separated by horizontal dividers (`---`).
     * `## [Number]. [Main Topic Title]` (e.g., `## 1. Artificial Intelligence`, `## 2. Is AI Really Intelligent?`).
     * `### [Number].[Number] [Subtopic Title]` for specific sub-concepts, mechanisms, or dichotomies (e.g., `### 2.1 Generalization`, `### 2.2 Out-of-Distribution Data`).
     * `#### [Subtopic]` when further analytical granularity is needed.
   - Never bunch multiple theoretical concepts under one single generic heading.

2. CALLOUTS & BLOCKQUOTES FOR CORE DEFINITIONS AND AXIOMS:
   - Use Markdown blockquotes (`> **...**`) to highlight:
     * Formal definitions and axioms (e.g., `> **Artificial Intelligence:** Artificial Intelligence involves machines that can perform tasks that are characteristic of human intelligence.`)
     * Key didactic questions or thought experiment premises (e.g., `> **Key Question:** How many characters are there?`)
     * Fundamental formulas, principles, or contrasts (e.g., `> **Knowledge ≠ Intelligence**`)
     * Important pedagogical warnings or caveats (e.g., `> **Important:** The fact that a system behaves in a human-like way does not automatically mean that it possesses human intelligence.`)

3. EXHAUSTIVE ENUMERATIONS & BULLET POINTS:
   - Whenever the lecture enumerates items, tasks, examples, roles, industries, capabilities, advantages/disadvantages, or technical dimensions, ALWAYS format them as clean, structured bullet points (`- ...`).
   - DO NOT merge lists into dense comma-separated prose sentences. Bullet points provide visual clarity, scannability, and high study retention.

4. LOGICAL FLOW & CONTRASTIVE SCHEMAS:
   - Use bold arrow notation (`**A → B**`) to visually capture:
     * Causal chains and workflows (e.g., `**Specific task → Train specialized model → Use model for that task**`)
     * Conceptual dichotomies and comparisons (e.g., `**New but similar to training data → Generalization**`, `**Substantially different from training data → Out-of-Distribution problem**`)
     * Evolutionary or paradigm shifts (e.g., `**Narrow AI → increasingly general Generative AI systems → AGI**`).

5. STEP-BY-STEP DECONSTRUCTION OF EXAMPLES & THOUGHT EXPERIMENTS:
   - When the professor provides an example, analogy, or thought experiment (e.g., counting characters in an unfamiliar language, Will Smith's birthday vs reasoning):
     * Explain the setup thoroughly.
     * Deconstruct the reasoning steps sequentially using numbered lists (`1. ...`, `2. ...`).
     * Contrast the human baseline with machine limitations (e.g., training distribution, pattern matching vs genuine reasoning).
     * Clearly state the resulting theoretical insight or implication.

6. TECHNICAL ACCURACY, FORMULAS & DIAGRAMS:
   - Retain all technical terminology, historical names, and dates (e.g., `**John McCarthy in 1956**`).
   - Bold key terms, concepts, and names so the notes are immediately scannable.
   - Format all mathematical expressions and formulas with LaTeX markdown (`$...$` for inline, `$$...$$` or `\\[ ... \\]` for display).
   - If processes, workflows, or architectures are discussed, you may include clean Mermaid diagrams (` ```mermaid ... ``` `). ALWAYS enclose node labels containing parentheses, formulas, or special symbols in double quotes (e.g., `A["Jacobian J(q)"]`).
   - If the transcript explicitly mentions a slide, diagram, or whiteboard screenshot, insert a clean placeholder tag (e.g., `![Screenshot: Description of diagram](screenshot_placeholder.png)`).

7. PURE MARKDOWN OUTPUT:
   - Output ONLY the formatted academic notes in Markdown.
   - DO NOT include conversational openings or closings (e.g., "Here are your notes", "Hope this helps").
   - DO NOT add personal opinions or hallucinations."""

LATEX_PROMPT = r"""You will receive university notes written in Markdown format.
Your task is to convert them into well-formatted LaTeX code, keeping the original content as faithful as possible.

CRITICAL LANGUAGE RULE:
- You MUST write the LaTeX content in the EXACT SAME LANGUAGE as the original Markdown notes.
- If the notes are in Italian, the LaTeX text MUST be in Italian.
- If the notes are in English, the LaTeX text MUST be in English.
- NEVER mix languages.

Fundamental rules:
- DO NOT summarize.
- DO NOT simplify concepts.
- DO NOT remove examples.
- DO NOT add invented content.
- DO NOT change the meaning of the explanations.
- Maintain all formulas, examples, observations, and logical steps.

LaTeX formatting rules:
- Use a clean and readable style.
- Use:
  - \chapter{}
  - \section{}
  - \subsection{}
  - \subsubsection*{}
- DO NOT use:
  - \paragraph{}
  - \subparagraph{}
  - \subsubsection{}
- After each title or subtitle always use: \noindent
- Paragraphs must be written in a clear, academic form.
- Faithfully preserve all bullet points and numbered lists present in the Markdown notes using \begin{itemize} and \begin{enumerate}.
- Faithfully convert Markdown blockquotes (> ...) into \begin{quote} ... \end{quote}.

Mathematical formulas:
- Use the correct LaTeX syntax.
- Inline formulas: $...$
- Centered formulas:
  \[
  ...
  \]

Code and commands:
- Use:
  \begin{lstlisting}
  ...
  \end{lstlisting}

Images:
- If there is an image in the markdown:
  use the format:
  \begin{figure}[H]
      \centering
      \includegraphics[width=0.8\textwidth]{img/filename}
      \caption{}
  \end{figure}

Tables:
- Convert markdown tables to LaTeX tables using tabular.

Style:
- The language must be impersonal and suitable for university notes.
- Maintain a technical, clear, and neat style.
- Do not use emojis.
- Do not write introductions or conclusions.

Output:
- Return ONLY LaTeX code.
- Do not wrap the result in markdown blocks."""

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

def generate_notes(text, model_name="gemini-3.5-flash-lite", custom_prompt=None, api_key=None):
    """
    Invia la trascrizione a Gemini per generare appunti strutturati.
    Default model: gemini-3.5-flash-lite
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata. Assicurati di aver configurato GOOGLE_API_KEY nel file .env o nella sidebar."

    client = genai.Client(api_key=api_key)
    prompt = custom_prompt if (custom_prompt and custom_prompt.strip()) else DEFAULT_PROMPT
    
    try:
        gemini_rate_tracker.log_request()
        response = gemini_rate_tracker.execute_with_retry(client.models.generate_content, 
            model=model_name,
            contents=f"{prompt}\n\nTRASCRIZIONE:\n{text}"
        )
        cleaned_notes = notion_helper.sanitize_latex_formulas(response.text)
        return True, cleaned_notes
    except Exception as e:
        return False, f"Errore durante la generazione degli appunti con {model_name}: {str(e)}"

def generate_latex(markdown_text, model_name="gemini-3.5-flash-lite", api_key=None):
    """
    Converte gli appunti Markdown in codice LaTeX professionale.
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non trovata."

    client = genai.Client(api_key=api_key)
    
    try:
        gemini_rate_tracker.log_request()
        response = gemini_rate_tracker.execute_with_retry(client.models.generate_content, 
            model=model_name,
            contents=f"{LATEX_PROMPT}\n\nCONTENUTO MARKDOWN:\n{markdown_text}"
        )
        return True, response.text
    except Exception as e:
        return False, f"Errore durante la conversione in LaTeX: {str(e)}"

def export_to_notion(course_name, course_page_id, lesson_date_str, markdown_text, transcript_text=None, is_same_video=False, api_key=None):
    """
    Workflow di esportazione su Notion con due sottopagine:
    1. Cerca/Crea la tabella del corso su Notion.
    2. Estrae il titolo dagli appunti per 'Argomenti trattati'.
    3. Cerca/Crea la riga della lezione (Lezione N: DD-MM-YYYY).
    4. Cerca/Crea la sottopagina 'Trascrizione' e inserisce la trascrizione grezza.
    5. Cerca/Crea la sottopagina 'Appunti' e inserisce gli appunti Markdown formattati.
    Ritorna SEMPRE una tupla a 3 elementi: (success_bool, message_str, page_id_or_none)
    """
    client = notion_helper.get_notion_client(api_key)
    if not client:
        return False, "Client Notion non configurato.", None

    # 1. Trova o crea il database del corso
    db_id, err = notion_helper.get_or_create_course_database(course_page_id, course_name, api_key)
    if err or not db_id:
        return False, f"{err}" if err else "Errore preparazione tabella Notion.", None

    # 2. Estrai il titolo dell'argomento per la colonna 'Argomenti trattati'
    topics_title = notion_helper.extract_notes_title(markdown_text)

    # 3. Trova o crea la riga per la lezione/data (Lezione N: DD-MM-YYYY)
    lesson_page_id, is_existing, err_l = notion_helper.get_or_create_lesson_entry(
        db_id, 
        lesson_date_str, 
        is_same_video=is_same_video, 
        api_key=api_key,
        topics_title=topics_title
    )
    if err_l or not lesson_page_id:
        return False, f"{err_l}" if err_l else "Errore creazione riga lezione su Notion.", None

    # 4. Gestione Sottopagina "Trascrizione"
    if transcript_text and str(transcript_text).strip():
        subpage_tr_id = notion_helper.get_or_create_subpage(client, lesson_page_id, "Trascrizione", emoji="🎙️")
        if subpage_tr_id:
            tr_blocks = notion_helper.transcript_to_notion_blocks(transcript_text)
            if tr_blocks:
                notion_helper.append_notes_to_page(subpage_tr_id, tr_blocks, is_append=is_existing, api_key=api_key)

    # 5. Gestione Sottopagina "Appunti"
    subpage_app_id = notion_helper.get_or_create_subpage(client, lesson_page_id, "Appunti", emoji="📝")
    target_notes_id = subpage_app_id or lesson_page_id

    # Converte il Markdown in blocchi Notion e li inserisce
    blocks = notion_helper.markdown_to_notion_blocks(markdown_text)
    success_app, err_app = notion_helper.append_notes_to_page(target_notes_id, blocks, is_append=is_existing, api_key=api_key)
    if not success_app:
        return False, f"{err_app}" if err_app else "Errore scrittura blocchi appunti su Notion.", None

    status_msg = "Appunti e trascrizione accodati alla lezione del giorno su Notion!" if is_existing else "Lezione creata con successo su Notion con sottopagine Trascrizione e Appunti!"
    return True, status_msg, lesson_page_id

CANVAS_AGENT_PROMPT = """You are a specialized AI assistant paired with a Canvas containing UNIVERSITY NOTES.
Your role is twofold: to help the user FIX/MODIFY the notes, but also and primarily to help the user STUDY from the notes themselves (e.g., explaining concepts, clarifying doubts).

STRICT AND INVIOLABLE RULES:

0. CRITICAL LANGUAGE RULE:
   - You MUST communicate and generate text in the EXACT SAME LANGUAGE as the user's prompt and the notes context.
   - If the notes/prompt are in Italian, reply and generate text in Italian.
   - If they are in English, reply and generate text in English.

1. THE CANVAS CONTAINS EXCLUSIVELY ACADEMIC AND EDUCATIONAL NOTES:
   - NEVER insert conversational text, personal introductions, guides on what you can do, explanations on how AI works, or meta-comments into the Canvas.
   - The Canvas MUST NEVER contain your explanations of concepts when the user asks you a question to understand better. Those go ONLY in the chat.

2. STRICT DISTINCTION OF INTENTS:
   a) STUDY, EXPLANATIONS, QUESTIONS, GREETINGS, OR CONVERSATION (e.g., "explain this concept", "I didn't understand X in the notes", "give me an example of Y", "hi"):
      - Respond professionally and exhaustively ONLY AND EXCLUSIVELY under <<<CHAT_RESPONSE>>>. This is where you act as a tutor and explain concepts.
      - DO NOT MODIFY THE NOTES for these requests.
      - Write STRICTLY and only the word NO_CHANGE under <<<UPDATED_CANVAS>>>.
   
   b) EXPLICIT INSTRUCTIONS TO MODIFY THE NOTES (e.g., "add this paragraph to the text", "summarize section 2 of the notes", "insert a formula in the canvas"):
      - Briefly explain what you did in the chat under <<<CHAT_RESPONSE>>>.
      - Provide the ENTIRE updated Markdown document of the notes under <<<UPDATED_CANVAS>>> (containing ONLY AND EXCLUSIVELY educational material).
      - MODIFY THE CANVAS ONLY WHEN THE USER EXPRESSLY ASKS FOR IT.

3. MULTIPLE TRANSCRIPTIONS / AGGREGATED LECTURES:
   - If the raw transcript contains multiple parts (e.g., PART 1, PART 2), it means the lecture of the day consists of multiple videos/additions.
   - Use the entirety of all transcript parts and notes to answer with maximum precision and accuracy.

4. STRICT PRESERVATION OF IMAGES AND MEDIA:
   - If the Canvas document contains image tags like `![...](URL)`, `![...|50%](URL)` or `![Image](https://...)`, you MUST ABSOLUTELY keep them intact and positioned in the exact same contextual point where they were originally located.
   - It is STRICTLY FORBIDDEN to remove, omit, arbitrarily move, or alter the URLs and size parameters of the image tags when you rewrite, synthesize, expand, format, or update the Canvas under <<<UPDATED_CANVAS>>>.
   - Images are an integral part of the educational material and must always be preserved in their original position relative to the surrounding text.

5. CITATIONS AND USER-SELECTED TEXT:
   - If the user's instruction includes a citation or reference text (e.g., `> ❝ **Selected text:** ...`), consider that fragment as the primary focus of the intervention.
   - If the user asks for clarifications, explanations, or examples on that text, provide the in-depth educational explanation under <<<CHAT_RESPONSE>>> and STRICTLY maintain NO_CHANGE under <<<UPDATED_CANVAS>>>.
   - If the user asks for a modification, rewrite, simplification, or expansion of that passage, update the entire document under <<<UPDATED_CANVAS>>> by modifying with surgical precision that specific point and preserving the rest of the document unchanged.

6. MERMAID DIAGRAM SYNTAX:
   - If you generate or modify Mermaid diagrams (```mermaid ... ```), ALWAYS enclose in double quotes the labels of nodes containing parentheses, formulas, or special characters (e.g., write A["Geometric Jacobian J(Q)"] and NEVER A[Geometric Jacobian J(Q)]).
   - Inserting parentheses or mathematical characters not enclosed in double quotes inside node shapes like [...], (...), {...} is strictly forbidden because it causes a critical rendering error (Parse error).

STRICT AND MANDATORY RESPONSE FORMAT:
<<<CHAT_RESPONSE>>>
[Conversational response, explanations of concepts for studying, or description of what you modified]
<<<UPDATED_CANVAS>>>
[Markdown text of the complete notes OR just the word NO_CHANGE if an explicit modification to the notes was not requested]"""

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

def agent_edit_notes(current_markdown, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite", api_key=None):
    """
    Agente AI per la modifica interattiva degli appunti nel Canvas.
    Riceve il testo attuale del Canvas, la trascrizione grezza originale (se disponibile), l'istruzione dell'utente e lo storico dialogo.
    Restituisce tupla: (success_bool, chat_reply, updated_markdown)
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
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
        response = gemini_rate_tracker.execute_with_retry(client.models.generate_content, 
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

def agent_edit_notes_stream(current_markdown, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite", api_key=None):
    """
    Generatore streaming per l'Agente AI del Canvas.
    Invia i chunk di testo in tempo reale man mano che arrivano dal modello Gemini, includendo la trascrizione grezza originale se fornita.
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
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
    response_stream = gemini_rate_tracker.execute_with_retry(client.models.generate_content_stream, 
        model=model_name,
        contents=f"{CANVAS_AGENT_PROMPT}\n\n{user_payload}"
    )

    for chunk in response_stream:
        if chunk.text:
            yield chunk.text


# ==============================================================================
# MODALITÀ: MODIFICA MIRATA DI UNA SINGOLA SEZIONE ("MODIFICA SOLO QUESTO")
# ==============================================================================

CANVAS_TARGETED_EDIT_PROMPT = """You are a top-level academic editorial assistant specialized in the targeted revision and improvement of university notes.
Your task is EXCLUSIVELY to modify, rewrite, or expand the specific portion of text selected by the user within the Canvas document.

COMPLETE CONTEXT:
You are provided with the ENTIRE Canvas document as a reference to understand the topic, terminology, educational style, notation, and overall consistency.

SPECIFIC SECTION TO MODIFY (TARGET):
You are provided with the exact text of the portion the user intends to modify.

STRICT AND INVIOLABLE RULES:

0. CRITICAL LANGUAGE RULE:
   - You MUST communicate and generate text in the EXACT SAME LANGUAGE as the user's prompt and the notes context.
   - If the notes/prompt are in Italian, reply and generate text in Italian.
   - If they are in English, reply and generate text in English.
   - NEVER mix languages.

1. DO NOT REWRITE THE ENTIRE DOCUMENT! Under <<<TARGETED_REPLACEMENT>>> you must ONLY provide the replacement text ready to replace that single passage in the document.
2. The generated text must integrate perfectly and with logical, stylistic, and grammatical continuity with the preceding and following text in the document.
3. If there are LaTeX mathematical formulas ($...$ or $$...$$) in the target section or context, keep them and format them with the utmost care and correctness.
4. If the target section contains an image tag like `![...](...)`, keep it intact unless otherwise and unequivocally instructed by the user.
5. TITLES AND HEADINGS: If the selected section is a title or part of a title (e.g. `## Title`), generate the new title with an appropriate single level (e.g. `## New Title` or `### New Title`), strictly avoiding double hashes or anomalous combinations like `## ###`.
6. MERMAID DIAGRAMS: If you create or modify Mermaid diagrams (```mermaid ... ```), ALWAYS enclose in double quotes the labels of nodes containing parentheses or special characters (e.g. A["Geometric Jacobian J(Q)"] and NEVER A[Geometric Jacobian J(Q)]).
7. Under <<<CHAT_RESPONSE>>> write a brief explanation (1-2 sentences) describing cordially what you modified in the passage.

MANDATORY RESPONSE FORMAT:
<<<CHAT_RESPONSE>>>
[Brief synthetic explanation of what you modified in the passage]
<<<TARGETED_REPLACEMENT>>>
[ONLY AND EXCLUSIVELY the new text that will replace the selected portion]"""

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

def agent_edit_targeted_stream(current_markdown, target_section, user_instruction, chat_history=None, raw_transcript=None, model_name="gemini-3.5-flash-lite", api_key=None):
    """
    Generatore streaming per la modifica mirata di una singola sezione degli appunti.
    Fornisce l'intero documento Canvas come contesto, ma istruisce il modello a generare SOLO
    il testo sostitutivo per la sezione selezionata.
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
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
    response_stream = gemini_rate_tracker.execute_with_retry(client.models.generate_content_stream, 
        model=model_name,
        contents=f"{CANVAS_TARGETED_EDIT_PROMPT}\n\n{user_payload}"
    )

    for chunk in response_stream:
        if chunk.text:
            yield chunk.text



