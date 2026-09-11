import os
import re
import json
import subprocess
import tempfile
from pathlib import Path
from google import genai
from google.genai import types
import gemini_rate_tracker

PRESENTATION_SYSTEM_PROMPT = r"""Sei un docente universitario esperto e un instructional designer di altissimo livello.
Il tuo compito è trasformare gli appunti completi di una lezione universitaria in una PRESENTAZIONE A SLIDE professionale, ordinata e pedagogicamente impeccabile, in perfetto stile accademico chiaro e moderno.

OBIETTIVO DIDATTICO:
Le slide non devono essere "muri di testo", ma uno strumento visivo snello, sintetico ed efficace per lo studio e la memorizzazione dei concetti chiave.

REGOLE TASSATIVE DI COMPOSIZIONE:
1. STRUTTURA DELLE SLIDE (in media tra 8 e 14 slide in base alla complessità degli appunti):
   - Slide 1: Copertina ("title") con Titolo della lezione, Sottotitolo/Materia, Data o Dettaglio accademico.
   - Slide 2: Indice / Agenda didattica ("agenda") con 4-5 argomenti cardine.
   - Slide 3..N-1: Nucleo Didattico (concetti, definizioni, formule, passaggi logici).
   - Ultima Slide: Sintesi finale dei Takeaway ("summary") e conclusioni.

2. SINTESI E LEGGIBILITÀ (NO MURI DI TESTO):
   - Massimo 3-5 bullet point per slide.
   - Ogni bullet point deve essere sintetico (1-2 righe).
   - Evidenzia le parole chiave più rilevanti in **grassetto**.
   - Usa un linguaggio accademico chiaro, rigoroso e diretto.

3. FORMULE E MATEMATICA (LATEX):
   - Qualsiasi formula, equazione o simbolo matematico (inclusi simboli greci come $\Phi$, $\tau$, $\theta$ e variabili indicizzate come $J_A(q)$) DEVE TASSATIVAMENTE essere racchiuso tra delimitatori LaTeX: '$...$' per formule inline nel testo e nei bullet, oppure '$$...$$' per formule in evidenza e nei callout.
   - Anche all'interno dell'oggetto 'callout', se il contenuto è una formula o relazione matematica, racchiudila sempre tra '$$...$$'.
   - Quando introduci un'equazione cardine, valorizzala e chiarisci sinteticamente il significato delle variabili.

4. VARIETÀ DEI LAYOUT VISIVI (campo "layout"):
   - "title": Slide di apertura con titolo imponente, sottotitolo e corso.
   - "agenda": Elenco degli argomenti trattati nella lezione (inserisci i punti in "bullets").
   - "standard": Slide con titolo, categoria, bullet point ("bullets") ed eventuale box di evidenziazione ("callout").
   - "two_column": Slide con confronto o due prospettive ("left_title", "left_bullets", "right_title", "right_bullets").
   - "formula_focus": Slide dedicata a una legge o equazione fondamentale ("formula", "formula_explanation", "bullets").
   - "summary": Slide di recap finale con i punti essenziali da ricordare (inserisci i punti in "bullets").

5. FORMATO DI OUTPUT E REGOLE JSON:
   - Restituisci ESCLUSIVAMENTE un array JSON valido di oggetti slide (senza markdown di contorno o testo prima/dopo).
   - Per tutti gli elenchi puntati (anche in "agenda" e "summary") usa SEMPRE la chiave "bullets": ["...", "..."].
   - Nelle formule LaTeX all'interno delle stringhe JSON usa SEMPRE il doppio backslash per i comandi matematici (es. "\\\\dot{q}", "\\\\tau", "\\\\frac{a}{b}") per garantire la piena conformità allo standard JSON.
"""

PRESENTATION_JSON_SCHEMA = {
    "type": "ARRAY",
    "description": "Lista ordinata di slide della presentazione accademica",
    "items": {
        "type": "OBJECT",
        "properties": {
            "layout": {
                "type": "STRING",
                "enum": ["title", "agenda", "standard", "two_column", "formula_focus", "summary"],
                "description": "Tipologia di layout per la slide"
            },
            "title": {"type": "STRING", "description": "Titolo principale della slide"},
            "subtitle": {"type": "STRING", "description": "Sottotitolo o categoria di appartenenza"},
            "category": {"type": "STRING", "description": "Badge di categoria (es. Cinematica Inversa)"},
            "bullets": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Punti elenco sintetici con testo e parole in **grassetto**"
            },
            "callout": {
                "type": "OBJECT",
                "properties": {
                    "type": {"type": "STRING", "enum": ["definition", "theorem", "note", "example"]},
                    "title": {"type": "STRING", "description": "Titolo del box (es. Teorema o Definizione)"},
                    "content": {"type": "STRING", "description": "Contenuto del box o formula LaTeX"}
                },
                "required": ["type", "content"]
            },
            "left_title": {"type": "STRING", "description": "Titolo colonna sinistra per layout two_column"},
            "left_bullets": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Punti colonna sinistra"
            },
            "right_title": {"type": "STRING", "description": "Titolo colonna destra per layout two_column"},
            "right_bullets": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Punti colonna destra"
            },
            "formula": {"type": "STRING", "description": "Formula LaTeX principale in evidenza"},
            "formula_explanation": {"type": "STRING", "description": "Spiegazione sintetica dei termini della formula"},
            "footer": {"type": "STRING", "description": "Didascalia o nota a piè di slide"}
        },
        "required": ["layout", "title"]
    }
}

def find_browser_executable() -> str | None:
    """
    Rileva la presenza di un browser Chromium-based (Microsoft Edge o Google Chrome)
    su Windows per l'esportazione headless in PDF.
    """
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None

def generate_presentation_slides(markdown_notes: str, course_name: str = "", lesson_date: str = "", model_name: str = "gemini-3.5-flash-lite") -> list[dict]:
    """
    Interroga Gemini per generare la struttura JSON delle slide a partire dagli appunti della lezione.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Chiave API di Google non trovata. Configura GOOGLE_API_KEY nel file .env.")

    if not markdown_notes or not markdown_notes.strip():
        raise ValueError("Il documento degli appunti è vuoto. Genera prima gli appunti della lezione.")

    client = genai.Client(api_key=api_key)

    context_info = f"Materia/Corso: {course_name}\nData Lezione: {lesson_date}\n\n" if (course_name or lesson_date) else ""
    user_prompt = f"""{context_info}APPUNTI DELLA LEZIONE UNIVERSITARIA:
---
{markdown_notes}
---

Genera ora la presentazione a slide completa in formato JSON secondo le regole di didattica universitaria e sintesi fornite."""

    gemini_rate_tracker.log_request()
    
    response = client.models.generate_content(
        model=model_name,
        contents=f"{PRESENTATION_SYSTEM_PROMPT}\n\n{user_prompt}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PRESENTATION_JSON_SCHEMA,
            temperature=0.2,
        )
    )

    raw_text = (response.text or "").strip()
    slides = safe_parse_slides_json(raw_text)
    return slides

def safe_parse_slides_json(raw_text: str) -> list[dict]:
    """
    Decodifica in modo resiliente l'output JSON restituito da Gemini,
    riparando automaticamente eventuali sequenze di backslash non escapate tipiche delle formule LaTeX
    (es. \\dot, \\tau, \\frac, \\gamma, \\partial, ecc.).
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Risposta vuota ricevuta dal modello.")

    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Tentativo 1: parsing JSON diretto
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            for k in ["slides", "presentation", "deck", "items", "pages"]:
                if k in data and isinstance(data[k], list):
                    data = data[k]
                    break
            else:
                if all(isinstance(v, dict) for v in data.values()):
                    data = list(data.values())
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Tentativo 2: Normalizzazione backslash LaTeX non escapati
    # Trasforma qualsiasi \ che non sia già raddoppiato o non sia \" in \\
    fixed = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', cleaned)
    try:
        data = json.loads(fixed, strict=False)
        if isinstance(data, dict):
            for k in ["slides", "presentation", "deck", "items", "pages"]:
                if k in data and isinstance(data[k], list):
                    data = data[k]
                    break
            else:
                if all(isinstance(v, dict) for v in data.values()):
                    data = list(data.values())
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Tentativo 3: Raddoppio forzato di tutti i backslash preservando le virgolette escapate \"
    fixed2 = re.sub(r'\\', r'\\\\', cleaned)
    fixed2 = fixed2.replace(r'\\"', r'\"')
    try:
        data = json.loads(fixed2, strict=False)
        if isinstance(data, dict):
            for k in ["slides", "presentation", "deck", "items", "pages"]:
                if k in data and isinstance(data[k], list):
                    data = data[k]
                    break
            else:
                if all(isinstance(v, dict) for v in data.values()):
                    data = list(data.values())
        if isinstance(data, list):
            return data
    except json.JSONDecodeError as err:
        raise ValueError(f"Errore decodifica JSON delle slide ({str(err)}).")

    raise ValueError("Il modello non ha restituito una lista di slide valida.")

def format_presentation_text(text: str) -> str:
    """
    Formatta in HTML il testo di una slide, convertendo la sintassi Markdown
    (**grassetto**, *corsivo*, `codice`) in tag HTML e proteggendo al contempo
    le formule matematiche LaTeX ($...$, $$...$$, \\[...\\], \\(...\\)).
    Auto-racchiude inoltre eventuali espressioni LaTeX isolate prive di delimitatori.
    """
    if not text or not isinstance(text, str):
        return ""
    
    # 1. Proteggi blocchi matematici esistenti ($$...$$, $...$, \[...\], \(...\))
    math_blocks = []
    def _save_math(match):
        math_blocks.append(match.group(0))
        return f"%%MATHBLOCK{len(math_blocks)-1}%%"

    s = re.sub(r'\$\$.*?\$\$', _save_math, text, flags=re.DOTALL)
    s = re.sub(r'(?<!\\)\$.*?(?<!\\)\$', _save_math, s)
    s = re.sub(r'\\\[.*?\\\]', _save_math, s, flags=re.DOTALL)
    s = re.sub(r'\\\(.*?\\\)', _save_math, s)

    # 2. Se ci sono comandi LaTeX isolati non racchiusi in $ (es. \Phi, \dot{q}, \tau),
    # racchiudili in $...$
    def _wrap_latex(match):
        expr = match.group(0).strip()
        return f"${expr}$"
    
    s = re.sub(r'(?:[a-zA-Z0-9_+*/^=()\[\]-]*\\[a-zA-Z]+[a-zA-Z0-9_+*/^=()\[\]-]*(?:\s*[=+\-*/]\s*[a-zA-Z0-9_+*/^=()\[\]\\]+)*)', _wrap_latex, s)

    # 3. Converti Markdown standard in HTML
    # Grassetto: **testo** o __testo__
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'__(.+?)__', r'<strong>\1</strong>', s)
    
    # Codice: `codice`
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    
    # Corsivo: *testo* (non preceduto o seguito da asterisco)
    s = re.sub(r'(?<!\*)\*([^\*\s][^\*]*?[^\*\s])\*(?!\*)', r'<em>\1</em>', s)

    # 4. Ripristina i blocchi matematici originali
    for idx, mb in enumerate(math_blocks):
        s = s.replace(f"%%MATHBLOCK{idx}%%", mb)

    return s

def format_callout_content(content: str) -> str:
    """
    Formatta il contenuto di un callout. Se il contenuto è un'equazione o contiene
    simboli matematici LaTeX senza delimitatori, lo racchiude automaticamente in $$...$$
    per un rendering KaTeX ottimale.
    """
    if not content or not isinstance(content, str):
        return ""
    stripped = content.strip()
    if "$" in stripped or r"\[" in stripped or r"\(" in stripped:
        return format_presentation_text(stripped)
    
    # Se contiene comandi LaTeX o uguaglianze matematiche
    has_latex = bool(re.search(r'\\[a-zA-Z]+', stripped))
    has_math_eq = ("=" in stripped or ">=" in stripped or "<=" in stripped) and len(stripped.split()) < 15
    if has_latex or has_math_eq:
        return f"$${stripped}$$"
    
    return format_presentation_text(stripped)

def build_html_presentation(slides: list[dict], course_name: str = "Corso Universitario", lesson_date: str = "") -> str:
    """
    Genera un file HTML autonomo (standalone) contenente le slide in stile accademico chiaro,
    con KaTeX nativo per le formule matematiche, navigazione interattiva a schermo e supporto stampa PDF 16:9.
    """
    valid_slides = []
    for s in (slides or []):
        if isinstance(s, dict):
            valid_slides.append(s)
        elif isinstance(s, str) and s.strip():
            valid_slides.append({"layout": "standard", "title": s.strip(), "bullets": []})
    slides = valid_slides
    total_slides = len(slides)
    slides_html_list = []

    for idx, slide in enumerate(slides):
        slide_num = idx + 1
        layout = str(slide.get("layout", "standard") or "standard")
        title = str(slide.get("title", f"Slide {slide_num}") or f"Slide {slide_num}")
        subtitle = str(slide.get("subtitle", "") or "")
        category = str(slide.get("category", "") or "")
        
        # Recupero multi-chiave resiliente per i bullet point (bullets, content, items, agenda, points, topics)
        raw_bullets = (
            slide.get("bullets")
            or slide.get("content")
            or slide.get("items")
            or slide.get("agenda")
            or slide.get("points")
            or slide.get("topics")
            or []
        )
        if isinstance(raw_bullets, str):
            raw_bullets = [raw_bullets]
        elif not isinstance(raw_bullets, list):
            raw_bullets = []
        bullets = [str(b).strip() for b in raw_bullets if str(b).strip()]
            
        callout_raw = slide.get("callout")
        callout = None
        if isinstance(callout_raw, str) and callout_raw.strip():
            callout = {"type": "note", "title": "Nota Fondamentale", "content": callout_raw.strip()}
        elif isinstance(callout_raw, dict):
            callout = {
                "type": str(callout_raw.get("type", "definition") or "definition"),
                "title": str(callout_raw.get("title", "Nota Fondamentale") or "Nota Fondamentale"),
                "content": str(callout_raw.get("content", "") or callout_raw.get("text", "") or "")
            }

        left_title = str(slide.get("left_title", "") or "")
        left_bullets = slide.get("left_bullets") or slide.get("left_content") or []
        if isinstance(left_bullets, str):
            left_bullets = [left_bullets]
        elif not isinstance(left_bullets, list):
            left_bullets = []

        right_title = str(slide.get("right_title", "") or "")
        right_bullets = slide.get("right_bullets") or slide.get("right_content") or []
        if isinstance(right_bullets, str):
            right_bullets = [right_bullets]
        elif not isinstance(right_bullets, list):
            right_bullets = []

        # Se layout a due colonne non ha liste separate ma ci sono bullets generali, ripartiscili tra le due colonne
        if not left_bullets and not right_bullets and bullets:
            mid = max(1, len(bullets) // 2)
            left_bullets = bullets[:mid]
            right_bullets = bullets[mid:]

        formula = str(
            slide.get("formula")
            or slide.get("equation")
            or slide.get("latex")
            or slide.get("math")
            or ""
        ).strip()

        # Se la formula non è definita e il layout è formula_focus, cercala dentro callout o nei bullets
        if not formula:
            if callout and callout.get("content"):
                c_text = callout["content"]
                if "$$" in c_text or "$" in c_text:
                    formula = c_text
                else:
                    formula = f"$${c_text}$$"
            elif bullets:
                for b_idx, b in enumerate(bullets):
                    if "$$" in b or (b.startswith("$") and b.endswith("$")):
                        formula = b
                        bullets.pop(b_idx)
                        break

        if formula and not formula.startswith("$"):
            formula = f"$${formula}$$"

        formula_exp = str(
            slide.get("formula_explanation")
            or slide.get("variables")
            or slide.get("explanation")
            or slide.get("description")
            or ""
        ).strip()

        footer_text = str(slide.get("footer", "") or (f"{course_name} • {lesson_date}" if lesson_date else course_name))

        # Formatta i testi con supporto Markdown (grassetto, corsivo, codice) e formule matematiche
        title_fmt = format_presentation_text(title)
        subtitle_fmt = format_presentation_text(subtitle)
        category_fmt = format_presentation_text(category)
        bullets_fmt = [format_presentation_text(b) for b in bullets]
        left_title_fmt = format_presentation_text(left_title)
        left_bullets_fmt = [format_presentation_text(b) for b in left_bullets]
        right_title_fmt = format_presentation_text(right_title)
        right_bullets_fmt = [format_presentation_text(b) for b in right_bullets]
        formula_exp_fmt = format_presentation_text(formula_exp)
        footer_text_fmt = format_presentation_text(footer_text)

        badge_html = f'<span class="badge">{category_fmt}</span>' if category_fmt else ''
        header_html = f"""
        <div class="slide-header">
            {badge_html}
            <h2 class="slide-title">{title_fmt}</h2>
            {f'<p class="slide-subtitle">{subtitle_fmt}</p>' if subtitle_fmt else ''}
        </div>
        """

        content_html = ""

        if layout == "title":
            content_html = f"""
            <div class="title-slide-container">
                <div class="academic-crest">🎓</div>
                <div class="title-badge">{course_name}</div>
                <h1 class="main-title">{title_fmt}</h1>
                {f'<h3 class="main-subtitle">{subtitle_fmt}</h3>' if subtitle_fmt else ''}
                <div class="title-footer-meta">
                    <span>📅 {lesson_date or 'Anno Accademico'}</span>
                    <span>•</span>
                    <span>🏛️ Dispense Didattiche</span>
                </div>
            </div>
            """
        elif layout == "agenda":
            bullets_li = "".join([f'<li class="agenda-item"><span class="agenda-num">0{i+1}</span><div class="agenda-text">{b}</div></li>' for i, b in enumerate(bullets_fmt)])
            content_html = f"""
            {header_html}
            <div class="agenda-container">
                <ul class="agenda-list">
                    {bullets_li}
                </ul>
            </div>
            """
        elif layout == "two_column":
            left_li = "".join([f'<li>{b}</li>' for b in left_bullets_fmt])
            right_li = "".join([f'<li>{b}</li>' for b in right_bullets_fmt])
            content_html = f"""
            {header_html}
            <div class="two-col-container">
                <div class="col-card">
                    <h3 class="col-title">{left_title_fmt or 'Approccio A'}</h3>
                    <ul class="bullet-list">{left_li}</ul>
                </div>
                <div class="col-card col-highlight">
                    <h3 class="col-title">{right_title_fmt or 'Approccio B'}</h3>
                    <ul class="bullet-list">{right_li}</ul>
                </div>
            </div>
            """
        elif layout == "formula_focus":
            bullets_li = "".join([f'<li>{b}</li>' for b in bullets_fmt])
            if formula:
                content_html = f"""
                {header_html}
                <div class="formula-focus-container">
                    <div class="formula-hero-box">
                        <div class="formula-display">{formula}</div>
                        {f'<div class="formula-caption">{formula_exp_fmt}</div>' if formula_exp_fmt else ''}
                    </div>
                    {f'<ul class="bullet-list mt-4">{bullets_li}</ul>' if bullets_li else ''}
                </div>
                """
            else:
                content_html = f"""
                {header_html}
                <div class="standard-body">
                    <ul class="bullet-list">{bullets_li}</ul>
                </div>
                """
        elif layout == "summary":
            bullets_li = "".join([f'<li class="summary-item"><span class="check-icon">✓</span><div>{b}</div></li>' for b in bullets_fmt])
            content_html = f"""
            {header_html}
            <div class="summary-container">
                <ul class="summary-list">
                    {bullets_li}
                </ul>
            </div>
            """
        else: # standard layout
            bullets_li = "".join([f'<li>{b}</li>' for b in bullets_fmt])
            callout_html = ""
            if callout and isinstance(callout, dict):
                c_type = callout.get("type", "definition")
                c_title = format_presentation_text(callout.get("title", "Nota Fondamentale"))
                c_content = format_callout_content(callout.get("content", ""))
                callout_html = f"""
                <div class="callout-card callout-{c_type}">
                    <div class="callout-header">
                        <span class="callout-icon">📌</span>
                        <strong>{c_title}</strong>
                    </div>
                    <div class="callout-body">{c_content}</div>
                </div>
                """
            content_html = f"""
            {header_html}
            <div class="standard-body">
                <ul class="bullet-list">
                    {bullets_li}
                </ul>
                {callout_html}
            </div>
            """

        slide_classes = f"slide slide-{layout}"
        slide_block = f"""
        <section class="{slide_classes}" id="slide-{slide_num}" data-slide-index="{idx}">
            <div class="slide-inner">
                {content_html}
            </div>
            <div class="slide-footer">
                <div class="footer-left">{footer_text_fmt}</div>
                <div class="footer-right">{slide_num} / {total_slides}</div>
            </div>
        </section>
        """
        slides_html_list.append(slide_block)

    all_slides_html = "\n".join(slides_html_list)

    html_template = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{course_name} - Presentazione Didattica</title>

<!-- Font Google: Inter & JetBrains Mono -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">

<!-- KaTeX per rendering matematico perfetto LaTeX -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>

<style>
/* ==============================================================================
   RESET E DESIGN SYSTEM ACCADEMICO CHIARO (16:9)
   ============================================================================== */
:root {{
    --bg-main: #f8fafc;
    --slide-bg: #ffffff;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --text-muted: #94a3b8;
    
    --primary: #1e3a8a;       /* Deep Navy Accademico */
    --primary-light: #eff6ff;
    --primary-border: #bfdbfe;
    
    --accent-indigo: #4338ca;
    --accent-cyan: #0284c7;
    --accent-emerald: #047857;
    
    --card-bg: #f8fafc;
    --card-border: #e2e8f0;
    
    --border-radius-lg: 16px;
    --border-radius-md: 10px;
    --border-radius-sm: 6px;
    
    --shadow-slide: 0 10px 25px -5px rgba(15, 23, 42, 0.08), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
}}

* {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

body {{
    font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background-color: var(--bg-main);
    color: var(--text-primary);
    overflow: hidden;
    height: 100vh;
    width: 100vw;
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
}}

/* ==============================================================================
   CONTENITORE SLIDE (RATIO 16:9)
   ============================================================================== */
#deck-viewport {{
    position: relative;
    width: 100vw;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background-color: var(--bg-main);
}}

.slide {{
    display: none;
    position: absolute;
    width: min(94vw, calc(94vh * (16 / 9)));
    height: min(94vh, calc(94vw * (9 / 16)));
    background: var(--slide-bg);
    border-radius: var(--border-radius-lg);
    box-shadow: var(--shadow-slide);
    border: 1px solid var(--card-border);
    padding: 3.5rem 4.5rem 3rem 4.5rem;
    flex-direction: column;
    justify-content: space-between;
    opacity: 0;
    transform: scale(0.98);
    transition: opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1), transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
}}

.slide.active {{
    display: flex;
    opacity: 1;
    transform: scale(1);
    z-index: 10;
}}

.slide-inner {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    overflow-y: auto;
}}

/* ==============================================================================
   HEADER E TYPOGRAPHY SLIDE
   ============================================================================== */
.slide-header {{
    margin-bottom: 2rem;
}}

.badge {{
    display: inline-block;
    padding: 0.3rem 0.85rem;
    background-color: var(--primary-light);
    color: var(--primary);
    border: 1px solid var(--primary-border);
    border-radius: 9999px;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    margin-bottom: 0.75rem;
}}

.slide-title {{
    font-size: 2.25rem;
    font-weight: 800;
    color: var(--text-primary);
    line-height: 1.25;
    letter-spacing: -0.025em;
}}

.slide-subtitle {{
    font-size: 1.1rem;
    color: var(--text-secondary);
    margin-top: 0.4rem;
    font-weight: 400;
}}

/* ==============================================================================
   BULLET POINTS ACCADEMICI
   ============================================================================== */
.bullet-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 1.15rem;
}}

.bullet-list li {{
    position: relative;
    padding-left: 1.75rem;
    font-size: 1.2rem;
    line-height: 1.6;
    color: var(--text-secondary);
}}

.bullet-list li strong {{
    color: var(--text-primary);
    font-weight: 700;
}}

.bullet-list li::before {{
    content: '';
    position: absolute;
    left: 0;
    top: 0.65rem;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background-color: var(--primary);
}}

/* ==============================================================================
   CALLOUT CARD (TEOREMA, DEFINIZIONE, ESEMPIO)
   ============================================================================== */
.callout-card {{
    margin-top: 1.5rem;
    padding: 1.25rem 1.75rem;
    border-radius: var(--border-radius-md);
    background-color: var(--card-bg);
    border-left: 5px solid var(--primary);
    border-top: 1px solid var(--card-border);
    border-right: 1px solid var(--card-border);
    border-bottom: 1px solid var(--card-border);
}}

.callout-header {{
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--primary);
    font-size: 1.05rem;
    font-weight: 700;
    margin-bottom: 0.5rem;
}}

.callout-body {{
    font-size: 1.15rem;
    line-height: 1.55;
    color: var(--text-primary);
}}

.callout-theorem {{ border-left-color: var(--accent-indigo); }}
.callout-theorem .callout-header {{ color: var(--accent-indigo); }}

.callout-definition {{ border-left-color: var(--accent-cyan); }}
.callout-definition .callout-header {{ color: var(--accent-cyan); }}

.callout-example {{ border-left-color: var(--accent-emerald); }}
.callout-example .callout-header {{ color: var(--accent-emerald); }}

/* ==============================================================================
   LAYOUT: TITLE SLIDE (COPERTINA)
   ============================================================================== */
.title-slide-container {{
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    height: 100%;
    text-align: center;
    padding: 2rem;
}}

.academic-crest {{
    font-size: 3.5rem;
    margin-bottom: 1.2rem;
}}

.title-badge {{
    font-size: 1.1rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--primary);
    background-color: var(--primary-light);
    border: 1px solid var(--primary-border);
    padding: 0.4rem 1.25rem;
    border-radius: 9999px;
    margin-bottom: 1.5rem;
}}

.main-title {{
    font-size: 3.1rem;
    font-weight: 900;
    color: var(--text-primary);
    line-height: 1.15;
    letter-spacing: -0.03em;
    max-width: 90%;
    margin-bottom: 1.2rem;
}}

.main-subtitle {{
    font-size: 1.45rem;
    font-weight: 400;
    color: var(--text-secondary);
    max-width: 80%;
    margin-bottom: 2.5rem;
}}

.title-footer-meta {{
    display: flex;
    align-items: center;
    gap: 1rem;
    font-size: 1.05rem;
    color: var(--text-muted);
    font-weight: 500;
}}

/* ==============================================================================
   LAYOUT: AGENDA
   ============================================================================== */
.agenda-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 1.2rem;
    margin-top: 0.5rem;
}}

.agenda-item {{
    display: flex;
    align-items: center;
    gap: 1.5rem;
    padding: 1rem 1.5rem;
    background-color: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: var(--border-radius-md);
    transition: transform 0.15s ease;
}}

.agenda-num {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.3rem;
    font-weight: 800;
    color: var(--primary);
    background: var(--primary-light);
    padding: 0.35rem 0.75rem;
    border-radius: var(--border-radius-sm);
}}

.agenda-text {{
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
}}

/* ==============================================================================
   LAYOUT: TWO COLUMN
   ============================================================================== */
.two-col-container {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2rem;
    height: 100%;
    align-items: stretch;
}}

.col-card {{
    background-color: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: var(--border-radius-md);
    padding: 1.75rem 2rem;
    display: flex;
    flex-direction: column;
}}

.col-highlight {{
    border-color: var(--primary-border);
    background-color: #fdfefe;
}}

.col-title {{
    font-size: 1.35rem;
    font-weight: 700;
    color: var(--primary);
    margin-bottom: 1.2rem;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid var(--primary-light);
}}

/* ==============================================================================
   LAYOUT: FORMULA FOCUS
   ============================================================================== */
.formula-hero-box {{
    background: linear-gradient(135deg, #f8fafc 0%, #edf2f7 100%);
    border: 2px solid var(--primary-border);
    border-radius: var(--border-radius-lg);
    padding: 2.2rem;
    text-align: center;
    margin: 1.5rem 0;
}}

.formula-display {{
    font-size: 1.9rem;
    margin-bottom: 0.8rem;
    color: var(--text-primary);
}}

.formula-caption {{
    font-size: 1.1rem;
    color: var(--text-secondary);
    font-style: italic;
}}

/* ==============================================================================
   LAYOUT: SUMMARY
   ============================================================================== */
.summary-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}}

.summary-item {{
    display: flex;
    align-items: flex-start;
    gap: 1.25rem;
    font-size: 1.25rem;
    line-height: 1.55;
    color: var(--text-primary);
    padding: 1rem 1.4rem;
    background-color: var(--card-bg);
    border-radius: var(--border-radius-md);
    border-left: 4px solid var(--accent-emerald);
}}

.check-icon {{
    color: var(--accent-emerald);
    font-weight: 800;
    font-size: 1.35rem;
}}

/* ==============================================================================
   FOOTER SLIDE
   ============================================================================== */
.slide-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-top: 1.2rem;
    border-top: 1px solid var(--card-border);
    font-size: 0.95rem;
    color: var(--text-muted);
    font-weight: 500;
}}

/* ==============================================================================
   TOOLBAR E CONTROLLI NAVIGAZIONE SCHERMO
   ============================================================================== */
.deck-controls {{
    position: fixed;
    bottom: 20px;
    right: 25px;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    background: rgba(255, 255, 255, 0.94);
    backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    padding: 0.45rem 0.85rem;
    border-radius: 9999px;
    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.08);
    z-index: 1000;
}}

.control-btn {{
    background: transparent;
    border: none;
    cursor: pointer;
    font-size: 1.15rem;
    color: var(--text-secondary);
    width: 34px;
    height: 34px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s ease;
}}

.control-btn:hover {{
    background-color: var(--primary-light);
    color: var(--primary);
}}

.slide-counter {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-primary);
    padding: 0 0.5rem;
}}

.progress-bar-container {{
    position: fixed;
    bottom: 0;
    left: 0;
    width: 100vw;
    height: 4px;
    background: rgba(226, 232, 240, 0.6);
    z-index: 1000;
}}

.progress-bar-fill {{
    height: 100%;
    width: 0%;
    background: var(--primary);
    transition: width 0.25s ease;
}}

/* ==============================================================================
   REGOLE CSS STAMPA & CONVERSIONE PDF VETTORIALE (@media print)
   ============================================================================== */
@media print {{
    @page {{
        size: 16in 9in;
        margin: 0;
    }}
    
    body, html {{
        background: #ffffff !important;
        width: 100% !important;
        height: 100% !important;
        overflow: visible !important;
        display: block !important;
    }}
    
    #deck-viewport {{
        display: block !important;
        width: 100% !important;
        height: auto !important;
        background: #ffffff !important;
    }}
    
    .slide {{
        display: flex !important;
        opacity: 1 !important;
        transform: none !important;
        position: relative !important;
        width: 16in !important;
        height: 9in !important;
        max-width: 16in !important;
        max-height: 9in !important;
        page-break-after: always !important;
        page-break-inside: avoid !important;
        box-shadow: none !important;
        border: none !important;
        border-radius: 0 !important;
        padding: 1.2in 1.4in !important;
        margin: 0 !important;
        background: #ffffff !important;
    }}
    
    .deck-controls, .progress-bar-container {{
        display: none !important;
    }}
}}
</style>
</head>
<body>

<div id="deck-viewport">
    {all_slides_html}
</div>

<!-- Barra di Progresso -->
<div class="progress-bar-container">
    <div class="progress-bar-fill" id="progress-fill"></div>
</div>

<!-- Toolbar di Controllo -->
<div class="deck-controls">
    <button class="control-btn" id="btn-prev" title="Slide precedente (←)">◀</button>
    <span class="slide-counter" id="slide-counter">1 / {total_slides}</span>
    <button class="control-btn" id="btn-next" title="Slide successiva (→ o Spazio)">▶</button>
    <button class="control-btn" id="btn-fullscreen" title="Schermo Intero">⛶</button>
    <button class="control-btn" id="btn-print" title="Stampa o Salva come PDF">🖨️</button>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {{
    const slides = document.querySelectorAll(".slide");
    const total = slides.length;
    let currentIdx = 0;

    const counter = document.getElementById("slide-counter");
    const progressFill = document.getElementById("progress-fill");
    const btnPrev = document.getElementById("btn-prev");
    const btnNext = document.getElementById("btn-next");
    const btnFullscreen = document.getElementById("btn-fullscreen");
    const btnPrint = document.getElementById("btn-print");

    function showSlide(idx) {{
        if (idx < 0) idx = 0;
        if (idx >= total) idx = total - 1;
        currentIdx = idx;

        slides.forEach((s, i) => {{
            if (i === currentIdx) {{
                s.classList.add("active");
            }} else {{
                s.classList.remove("active");
            }}
        }});

        if (counter) counter.innerText = `${{currentIdx + 1}} / ${{total}}`;
        if (progressFill) {{
            const pct = ((currentIdx + 1) / total) * 100;
            progressFill.style.width = pct + "%";
        }}
    }}

    btnPrev.addEventListener("click", () => showSlide(currentIdx - 1));
    btnNext.addEventListener("click", () => showSlide(currentIdx + 1));

    btnFullscreen.addEventListener("click", () => {{
        if (!document.fullscreenElement) {{
            document.documentElement.requestFullscreen().catch(() => {{}});
        }} else {{
            document.exitFullscreen().catch(() => {{}});
        }}
    }});

    btnPrint.addEventListener("click", () => {{
        window.print();
    }});

    // Navigazione da tastiera
    document.addEventListener("keydown", (e) => {{
        if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") {{
            showSlide(currentIdx + 1);
        }} else if (e.key === "ArrowLeft" || e.key === "PageUp") {{
            showSlide(currentIdx - 1);
        }} else if (e.key === "Home") {{
            showSlide(0);
        }} else if (e.key === "End") {{
            showSlide(total - 1);
        }} else if (e.key === "f" || e.key === "F") {{
            btnFullscreen.click();
        }}
    }});

    // Rendering matematico automatico con KaTeX
    if (typeof renderMathInElement === "function") {{
        renderMathInElement(document.body, {{
            delimiters: [
                {{left: '$$', right: '$$', display: true}},
                {{left: '\\\\[', right: '\\\\]', display: true}},
                {{left: '$', right: '$', display: false}},
                {{left: '\\\\(', right: '\\\\)', display: false}}
            ],
            throwOnError: false
        }});
    }}

    showSlide(0);
}});
</script>

</body>
</html>
"""
    return html_template

def convert_html_to_pdf(html_content: str) -> bytes | None:
    """
    Converte il documento HTML delle slide in un file PDF vettoriale 16:9
    utilizzando il motore headless nativo di Microsoft Edge (o Google Chrome).
    """
    browser_exe = find_browser_executable()
    if not browser_exe:
        return None

    temp_dir = tempfile.mkdtemp(prefix="deck_")
    html_path = os.path.join(temp_dir, "presentation.html")
    pdf_path = os.path.join(temp_dir, "presentation.pdf")

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        args = [
            browser_exe,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            f"--print-to-pdf={pdf_path}",
            html_path
        ]
        
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=35
        )

        if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 500:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            return pdf_bytes
        return None
    except Exception:
        return None
    finally:
        # Pulizia file temporanei
        try:
            if os.path.exists(html_path):
                os.remove(html_path)
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except Exception:
            pass
