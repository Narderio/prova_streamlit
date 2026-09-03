import os
import re
import time
import random
import datetime
import logging
import concurrent.futures
from notion_client import Client
from notion_client.errors import APIResponseError, RequestTimeoutError, HTTPResponseError
from dotenv import load_dotenv

logging.getLogger("notion_client").setLevel(logging.ERROR)

load_dotenv()

def format_notion_id(id_str: str) -> str:
    """
    Pulisce e formatta qualsiasi stringa/URL ID di Notion in un UUID valido (36 caratteri con trattini).
    """
    if not id_str:
        return ""
    raw = id_str.split("?")[0].split("/")[-1]
    hex_only = re.sub(r'[^a-fA-F0-9]', '', raw)
    if len(hex_only) >= 32:
        clean_hex = hex_only[-32:]
        return f"{clean_hex[:8]}-{clean_hex[8:12]}-{clean_hex[12:16]}-{clean_hex[16:20]}-{clean_hex[20:]}"
    return id_str.strip()

VALID_NOTION_LANGUAGES = {
    "abap", "abc", "agda", "arduino", "ascii art", "assembly", "bash", "basic",
    "bnf", "c", "c#", "c++", "clojure", "coffeescript", "coq", "css", "dart",
    "dhall", "diff", "docker", "ebnf", "elixir", "elm", "erlang", "f#", "flow",
    "fortran", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "hcl",
    "html", "idris", "java", "javascript", "json", "julia", "kotlin", "latex",
    "less", "lisp", "livescript", "llvm ir", "lua", "makefile", "markdown",
    "markup", "matlab", "mathematica", "mermaid", "nix", "notion formula",
    "objective-c", "ocaml", "pascal", "perl", "php", "plain text", "powershell",
    "prolog", "protobuf", "purescript", "python", "r", "racket", "reason",
    "ruby", "rust", "sass", "scala", "scheme", "scss", "shell", "smalltalk",
    "solidity", "sql", "swift", "toml", "typescript", "vb.net", "verilog",
    "vhdl", "visual basic", "webassembly", "xml", "yaml", "java/c/c++/c#"
}

LANGUAGE_ALIASES = {
    "text": "plain text",
    "txt": "plain text",
    "plaintext": "plain text",
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "sh": "bash",
    "zsh": "bash",
    "shell": "shell",
    "cpp": "c++",
    "cs": "c#",
    "csharp": "c#",
    "rb": "ruby",
    "yml": "yaml",
    "html/xml": "html",
    "code": "plain text"
}

def sanitize_code_language(lang: str) -> str:
    if not lang:
        return "plain text"
    lang_clean = lang.strip().lower()
    if lang_clean in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[lang_clean]
    if lang_clean in VALID_NOTION_LANGUAGES:
        return lang_clean
    return "plain text"


def format_iso_date(date_val) -> str:
    """
    Convertitore universale di date in formato ISO 'YYYY-MM-DD'.
    """
    if not date_val:
        return datetime.date.today().isoformat()
    if isinstance(date_val, (datetime.date, datetime.datetime)):
        return date_val.strftime("%Y-%m-%d")
    date_str = str(date_val).strip()
    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return date_str

def normalize_images_to_markdown(text: str) -> str:
    """
    Riconverte blocchi HTML <div class="resizable-img-wrapper"> nel formato standard ![alt](url) o ![alt|dim](url)
    in modo che il markdown memorizzato nello stato interno, negli editor e su Notion rimanga sempre Markdown pulito.
    """
    if not text or "resizable-img-wrapper" not in text:
        return text
    
    def repl_wrapper(m):
        block = m.group(0)
        
        # Estrai URL: prima da data-raw-url, altrimenti da src dell'img
        url_m = re.search(r'data-raw-url="([^"]+)"', block)
        if not url_m:
            url_m = re.search(r'<img[^>]+src="([^"]+)"', block)
        raw_url = url_m.group(1).replace('&quot;', '"').strip() if url_m else ""
        
        # Se l'URL è incapsulato per errore in formato markdown link [url](url), estrai il vero URL
        link_inside = re.match(r'^\[(https?://[^\]]+)\]\((?:https?://[^\)]+)\)$', raw_url)
        if link_inside:
            raw_url = link_inside.group(1).strip()

        # Estrai Alt: prima da data-raw-alt, altrimenti da alt dell'img
        alt_m = re.search(r'data-raw-alt="([^"]*)"', block)
        if not alt_m:
            alt_m = re.search(r'<img[^>]+alt="([^"]*)"', block)
        raw_alt = alt_m.group(1).replace('&quot;', '"').strip() if alt_m else "Immagine"
        if not raw_alt:
            raw_alt = "Immagine"

        # Estrai larghezza custom (se specificata)
        w_m = re.search(r'width:\s*([^;%"\s]+[%px]*)', block)
        w_val = w_m.group(1).strip() if w_m else ""

        clean_alt = raw_alt.split("|")[0].strip() or "Immagine"
        if w_val and w_val not in ["100%", "30%", "50%"]:
            return f"![{clean_alt}|{w_val}]({raw_url})"
        else:
            return f"![{clean_alt}]({raw_url})"

    # Match completo del container resizable-img-wrapper con i suoi div annidati
    pattern_nested = r'<div class="resizable-img-wrapper"[^>]*>[\s\S]*?</div>\s*</div>\s*</div>(?:\s*</div>)?'
    cleaned = re.sub(pattern_nested, repl_wrapper, text)
    
    # Fallback per strutture con 2 soli closing div
    pattern_fallback = r'<div class="resizable-img-wrapper"[^>]*>[\s\S]*?</div>\s*</div>(?:\s*</div>)?'
    cleaned = re.sub(pattern_fallback, repl_wrapper, cleaned)

    # Pulizia di eventuali tag </div> orfani rimasti subito dopo un tag immagine ![...](...)
    cleaned = re.sub(r'(!\[[^\]]*\]\([^\)]+\))\s*</div>', r'\1', cleaned)

    return cleaned

def sanitize_latex_formulas(text: str) -> str:
    """
    Pulisce la sintassi LaTeX/Markdown per garantire che Streamlit (KaTeX) e Notion renderizzino
    le formule senza errori o caratteri rotti.
    """
    if not text:
        return ""
    cleaned = re.sub(r'\\begin\{equation\*?\}', '', text)
    cleaned = re.sub(r'\\end\{equation\*?\}', '', cleaned)
    cleaned = re.sub(r'([^\n])\$\$', r'\1\n$$', cleaned)
    cleaned = re.sub(r'\$\$([^\n])', r'$$\n\1', cleaned)
    return cleaned

def format_markdown_images_for_streamlit(text: str, default_width: str = "30%") -> str:
    """
    Converte i tag immagine Markdown standard o con dimensione (![alt|50%](url))
    in contenitori HTML con maniglia di trascinamento (drag-to-resize) per l'anteprima formattata.
    Se non è specificata una dimensione nel tag, usa default_width (es. 30% in Home, 50% in Canvas).
    """
    if not text:
        return ""
    
    # 1. Normalizza prima qualsiasi residuo HTML in Markdown
    text = normalize_images_to_markdown(text)
    
    clean_default_w = str(default_width).strip()
    if clean_default_w.isdigit():
        clean_default_w = f"{clean_default_w}%"
    elif not clean_default_w.endswith("%") and not clean_default_w.endswith("px"):
        clean_default_w = "30%"
    
    def repl_img(match):
        alt_raw = (match.group(1) or "").strip()
        img_url = match.group(2).strip()
        
        alt_clean = alt_raw
        width_style = clean_default_w
        width_badge = clean_default_w
        
        if "|" in alt_raw:
            parts = alt_raw.split("|", 1)
            alt_clean = parts[0].strip()
            dim_str = parts[1].strip().lower()
            if dim_str.endswith("%") or dim_str.endswith("px"):
                width_style = dim_str
                width_badge = dim_str
            elif dim_str.isdigit():
                width_style = f"{dim_str}%"
                width_badge = f"{dim_str}%"

        safe_url = img_url.replace('"', '&quot;')
        safe_alt = (alt_clean or "Immagine").replace('"', '&quot;')
        
        return (
            f'\n\n<div class="resizable-img-wrapper" data-raw-url="{safe_url}" data-raw-alt="{safe_alt}" '
            f'style="position: relative; display: block; margin: 18px auto; width: {width_style}; max-width: 100%; transition: width 0.05s ease;">'
            f'<div style="position: relative; display: inline-block; width: 100%;">'
            f'<img src="{safe_url}" alt="{safe_alt}" style="width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,0.3); display: block;" loading="lazy" />'
            f'<div class="img-drag-handle" title="Trascina con il mouse per ridimensionare">'
            f'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            f'<line x1="21" y1="9" x2="9" y2="21"></line>'
            f'<line x1="21" y1="15" x2="15" y2="21"></line>'
            f'<line x1="21" y1="21" x2="21" y2="21"></line>'
            f'</svg>'
            f'</div>'
            f'<div class="img-size-badge" style="display: none;">{width_badge}</div>'
            f'</div>'
            f'</div>\n\n'
        )

    pattern = r'!\[([^\]]*)\]\((https?://[^)\s]+)\)'
    return re.sub(pattern, repl_img, text)

def clean_markdown_for_streamlit(text: str, default_width: str = "30%") -> str:
    """
    Pulisce la sintassi LaTeX/Markdown per garantire che Streamlit (KaTeX) e Notion renderizzino
    le formule senza errori o caratteri rotti, e arricchisce le immagini con i controlli interattivi di ridimensionamento.
    """
    if not text:
        return ""
    cleaned = sanitize_latex_formulas(text)
    cleaned = format_markdown_images_for_streamlit(cleaned, default_width=default_width)
    return cleaned

def split_notion_page_sections(markdown_text: str) -> dict:
    """
    Scompone il testo Markdown di una pagina Notion con più lezioni accodate in sezioni distinte.
    Restituisce un dizionario {titolo_sezione: testo_sezione}.
    """
    if not markdown_text:
        return {"📖 Pagina Completa": ""}

    # Separa il testo SOLO in corrispondenza dei banner di integrazione (non sui divisori --- interni della lezione)
    parts = re.split(r'(\n(?:---|>\s*📌[^\n]*)\n)', markdown_text)
    
    # Filtra solo le parti che contengono realmente "Integrazione Lezione" o 📌
    has_integrations = any("Integrazione Lezione" in p or "📌" in p for p in parts)
    if not has_integrations:
        return {"📖 Lezione Principale": markdown_text}

    sections = {}
    current_title = "📖 1. Lezione Principale"
    current_content = []
    integ_count = 1

    for part in parts:
        if "Integrazione Lezione" in part or "📌" in part:
            if current_content:
                text_block = "".join(current_content).strip()
                if text_block:
                    sections[current_title] = text_block
                current_content = []
            
            integ_count += 1
            match_date = re.search(r'Aggiunta il ([^\n]+)', part)
            if match_date:
                current_title = f"📌 {integ_count}. Integrazione ({match_date.group(1).strip()})"
            else:
                current_title = f"📌 {integ_count}. Integrazione Lezione"
            current_content.append(part)
        else:
            current_content.append(part)

    if current_content:
        text_block = "".join(current_content).strip()
        if text_block:
            sections[current_title] = text_block

    sections["🌐 Tutti gli Appunti della Pagina (Completo)"] = markdown_text
    return sections

def parse_inline_markdown(text: str):
    """
    Scompone una stringa di testo contenente sintassi Markdown inline 
    (*corsivo*, **grassetto**, `codice`, $equazione$) in un array 'rich_text' formattato per Notion.
    """
    if not text:
        return []
    
    pattern = re.compile(
        r'(\$\$.*?\$\$|\$[^\$\n]+?\$|\\\[.*?\\\]|\\\([^\n]*?\\\)|`[^`]+?`|\*\*\*.*?\*\*\*|\*\*.*?\*\*|__.*?__|(?<!\w)\*.*?\*(?!\w)|(?<!\w)_.*?_(?!\w))'
    )
    
    rich_text_list = []
    last_idx = 0
    
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last_idx:
            rich_text_list.append({
                "type": "text",
                "text": {"content": text[last_idx:start]}
            })
            
        token = match.group(0)
        
        # 1. Math / Equazioni Notion
        if (token.startswith("$$") and token.endswith("$$")) or (token.startswith("$") and token.endswith("$")):
            expr = token.strip("$").strip()
            expr = re.sub(r'\\begin\{equation\*?\}', '', expr)
            expr = re.sub(r'\\end\{equation\*?\}', '', expr).strip()
            if expr:
                rich_text_list.append({
                    "type": "equation",
                    "equation": {"expression": expr}
                })
        elif (token.startswith(r"\[") and token.endswith(r"\]")) or (token.startswith(r"\(") and token.endswith(r"\)")):
            expr = token[2:-2].strip()
            if expr:
                rich_text_list.append({
                    "type": "equation",
                    "equation": {"expression": expr}
                })
        # 2. Codice inline `code`
        elif token.startswith("`") and token.endswith("`"):
            rich_text_list.append({
                "type": "text",
                "text": {"content": token[1:-1]},
                "annotations": {"code": True}
            })
        # 3. Grassetto + Corsivo ***text***
        elif token.startswith("***") and token.endswith("***"):
            rich_text_list.append({
                "type": "text",
                "text": {"content": token[3:-3]},
                "annotations": {"bold": True, "italic": True}
            })
        # 4. Grassetto **text** o __text__
        elif (token.startswith("**") and token.endswith("**")) or (token.startswith("__") and token.endswith("__")):
            rich_text_list.append({
                "type": "text",
                "text": {"content": token[2:-2]},
                "annotations": {"bold": True}
            })
        # 5. Corsivo *text* o _text_
        elif (token.startswith("*") and token.endswith("*")) or (token.startswith("_") and token.endswith("_")):
            rich_text_list.append({
                "type": "text",
                "text": {"content": token[1:-1]},
                "annotations": {"italic": True}
            })
        else:
            rich_text_list.append({
                "type": "text",
                "text": {"content": token}
            })
            
        last_idx = end

    if last_idx < len(text):
        rich_text_list.append({
            "type": "text",
            "text": {"content": text[last_idx:]}
        })

    return rich_text_list if rich_text_list else [{"type": "text", "text": {"content": text[:2000]}}]

def rich_text_to_markdown(rich_text_list) -> str:
    """
    Converte un array 'rich_text' di Notion in una stringa Markdown formattata 
    (ripristinando Grassetto **, Corsivo *, Codice `, ed Equazioni LaTeX $...$).
    """
    if not rich_text_list:
        return ""
    
    result = []
    for item in rich_text_list:
        i_type = item.get("type")
        
        # 1. Equazione LaTeX nativa di Notion
        if i_type == "equation":
            expr = item.get("equation", {}).get("expression", "")
            if expr:
                expr_clean = re.sub(r'\\begin\{equation\*?\}', '', expr)
                expr_clean = re.sub(r'\\end\{equation\*?\}', '', expr_clean).strip()
                if "\n" in expr_clean or r"\begin{" in expr_clean or r"\\" in expr_clean or len(expr_clean) > 50:
                    result.append(f"\n\n$$\n{expr_clean}\n$$\n\n")
                else:
                    result.append(f"${expr_clean}$")
            continue
            
        # 2. Testo normale con annotazioni
        content = item.get("text", {}).get("content") or item.get("plain_text", "")
        if not content:
            continue
            
        ann = item.get("annotations", {})
        bold = ann.get("bold", False)
        italic = ann.get("italic", False)
        code = ann.get("code", False)
        strikethrough = ann.get("strikethrough", False)
        
        formatted = content
        if code:
            formatted = f"`{formatted}`"
        else:
            if bold and italic:
                formatted = f"***{formatted}***"
            elif bold:
                formatted = f"**{formatted}**"
            elif italic:
                formatted = f"*{formatted}*"
            if strikethrough:
                formatted = f"~~{formatted}~~"
                
        result.append(formatted)
        
    return "".join(result)

def _wrap_client_with_retry(client: Client, max_retries: int = 5) -> Client:
    """
    Avvolge il client Notion per gestire automaticamente i retry con exponential backoff
    in caso di:
    - Status 529 (Site is overloaded / sovraccarico momentaneo dei server Notion)
    - Status 429 (Rate limit superato)
    - Status 500/502/503/504 (Errori temporanei server o gateway)
    - Errori di rete e timeout
    """
    orig_request = client.request

    def retry_request(*args, **kwargs):
        last_exception = None
        for attempt in range(1, max_retries + 1):
            try:
                return orig_request(*args, **kwargs)
            except APIResponseError as e:
                last_exception = e
                # Status 529 (Overloaded), 429 (Rate limit), 5xx (Gateway/Server)
                if e.status in (529, 429, 500, 502, 503, 504) and attempt < max_retries:
                    retry_after = 0.0
                    try:
                        if hasattr(e, "headers") and e.headers and "Retry-After" in e.headers:
                            retry_after = float(e.headers.get("Retry-After", 0))
                    except (ValueError, TypeError):
                        pass

                    backoff = retry_after if retry_after > 0 else (1.5 ** attempt) + random.uniform(0.2, 0.6)
                    print(f"⚠️ Notion API temporaneamente sovraccarico (Status {e.status}). Tentativo {attempt}/{max_retries}, attesa {backoff:.1f}s prima di riprovare...")
                    time.sleep(backoff)
                else:
                    raise e
            except (RequestTimeoutError, HTTPResponseError, ConnectionError, TimeoutError) as e:
                last_exception = e
                if attempt < max_retries:
                    backoff = (1.5 ** attempt) + random.uniform(0.2, 0.6)
                    print(f"⚠️ Errore di connessione Notion ({type(e).__name__}). Tentativo {attempt}/{max_retries}, attesa {backoff:.1f}s...")
                    time.sleep(backoff)
                else:
                    raise e
        if last_exception:
            raise last_exception

    client.request = retry_request
    return client

def get_notion_client(api_key=None) -> Client | None:
    token = api_key or os.getenv("NOTION_API_KEY")
    if not token:
        return None
    try:
        raw_client = Client(auth=token)
        return _wrap_client_with_retry(raw_client)
    except Exception as e:
        print(f"Errore inizializzazione client Notion: {e}")
        return None

def get_target_data_source_id(client: Client, database_id: str):
    """
    Individua il Data Source ID associato al Database (Notion API 2026 / SDK 3.x).
    Restituisce (target_id, is_data_source).
    """
    clean_id = format_notion_id(database_id)
    try:
        db_info = client.databases.retrieve(database_id=clean_id)
        data_sources = db_info.get("data_sources", [])
        if data_sources and len(data_sources) > 0 and hasattr(client, "data_sources"):
            return data_sources[0].get("id"), True
    except Exception:
        pass
    return clean_id, False

def query_notion_database(client: Client, database_id: str, filter_dict: dict = None):
    """
    Interroga la tabella Notion usando il Data Source ID o il Database ID.
    """
    target_id, is_data_source = get_target_data_source_id(client, database_id)
    kwargs = {}
    if filter_dict:
        kwargs["filter"] = filter_dict
    
    # 1. Prova con data_sources.query (Notion SDK 3.x)
    if is_data_source and hasattr(client, "data_sources"):
        try:
            return client.data_sources.query(data_source_id=target_id, **kwargs)
        except Exception as e:
            print(f"Notion data_sources.query notice: {e}")

    # 2. Prova con databases.query (Notion SDK 2.x legacy)
    if hasattr(client, "databases") and hasattr(client.databases, "query"):
        try:
            return client.databases.query(database_id=target_id, **kwargs)
        except Exception as e:
            print(f"Notion databases.query notice: {e}")

    # 3. Prova tramite client.request con data_sources o databases
    try:
        path_str = f"data_sources/{target_id}/query" if is_data_source else f"databases/{target_id}/query"
        body = {"filter": filter_dict} if filter_dict else {}
        return client.request(
            path=path_str,
            method="POST",
            body=body
        )
    except Exception as e:
        print(f"Notion request query notice: {e}")

    return {"results": []}

def get_database_schema_props(client: Client, database_id: str):
    """
    Ispeziona ed allinea lo schema del Database Notion (Notion API v3 data_sources).
    Garantisce la presenza di:
    - Colonna Title: 'Lezione'
    - Colonna Checkbox: 'Appunti'
    - Colonna Date: 'Data'
    """
    target_id, is_data_source = get_target_data_source_id(client, database_id)
    title_prop_name = None
    checkbox_prop_name = None
    date_prop_name = None

    try:
        if is_data_source and hasattr(client, "data_sources"):
            ds_info = client.data_sources.retrieve(data_source_id=target_id)
            props = ds_info.get("properties", {})
        else:
            db_info = client.databases.retrieve(database_id=target_id)
            props = db_info.get("properties", {})

        for p_name, p_val in props.items():
            p_type = p_val.get("type")
            if p_type == "title":
                title_prop_name = p_name
            elif p_type == "checkbox":
                checkbox_prop_name = p_name
            elif p_type == "date":
                date_prop_name = p_name

        update_payload = {}
        if title_prop_name and title_prop_name != "Lezione":
            update_payload[title_prop_name] = {"name": "Lezione"}
        if not checkbox_prop_name:
            update_payload["Appunti"] = {"checkbox": {}}
        if not date_prop_name:
            update_payload["Data"] = {"date": {}}

        if update_payload:
            try:
                if is_data_source and hasattr(client, "data_sources"):
                    upd_res = client.data_sources.update(data_source_id=target_id, properties=update_payload)
                else:
                    upd_res = client.databases.update(database_id=target_id, properties=update_payload)
                
                new_props = upd_res.get("properties", {})
                for p_name, p_val in new_props.items():
                    p_type = p_val.get("type")
                    if p_type == "title":
                        title_prop_name = p_name
                    elif p_type == "checkbox":
                        checkbox_prop_name = p_name
                    elif p_type == "date":
                        date_prop_name = p_name
            except Exception as e_upd:
                print(f"Avviso aggiornamento schema Notion: {e_upd}")
    except Exception as e:
        print(f"Avviso lettura schema Notion: {e}")

    if not title_prop_name:
        title_prop_name = "Lezione"
    if not checkbox_prop_name:
        checkbox_prop_name = "Appunti"
    if not date_prop_name:
        date_prop_name = "Data"

    return title_prop_name, checkbox_prop_name, date_prop_name

def get_available_courses(corsi_page_id=None, api_key=None):
    """
    Legge le sottopagine e i database presenti dentro la pagina radice 'Corsi' su Notion.
    Restituisce un dizionario {nome_corso: page_id}.
    """
    client = get_notion_client(api_key)
    page_id = corsi_page_id or os.getenv("NOTION_CORSI_PAGE_ID")
    
    if not client or not page_id:
        return {}

    clean_page_id = format_notion_id(page_id)

    try:
        response = client.blocks.children.list(block_id=clean_page_id)
        courses = {}
        for block in response.get("results", []):
            block_type = block.get("type")
            if block_type == "child_page":
                title = block.get("child_page", {}).get("title", "")
                if title:
                    courses[title] = block.get("id")
            elif block_type == "child_database":
                title = block.get("child_database", {}).get("title", "")
                if title:
                    courses[title] = block.get("id")
        return courses
    except Exception as e:
        print(f"Errore durante il recupero dei corsi da Notion: {e}")
        return {}

def get_or_create_course_database(course_page_id, course_name="Corso", api_key=None):
    """
    Cerca il Database delle Lezioni direttamente dentro la pagina del corso.
    Se la pagina del corso contiene un database inline, lo usa.
    Altrimenti crea un DATABASE INLINE direttamente nella pagina con colonne 'Lezione' (Title), 'Appunti' (Checkbox) e 'Data' (Date).
    """
    client = get_notion_client(api_key)
    if not client or not course_page_id:
        return None, "Client Notion non configurato o Page ID del corso mancante."

    clean_id = format_notion_id(course_page_id)

    # 1. Cerca se dentro la pagina del corso c'è già un child_database inline
    try:
        blocks = client.blocks.children.list(block_id=clean_id)
        for block in blocks.get("results", []):
            if block.get("type") == "child_database":
                return block.get("id"), None
    except Exception as e:
        print(f"Avviso scansione blocchi corso: {e}")

    # 2. Controlla se la pagina del corso È GIÀ essa stessa un Database
    try:
        db_info = client.databases.retrieve(database_id=clean_id)
        if db_info and db_info.get("object") == "database":
            return clean_id, None
    except Exception:
        pass

    # 3. Se non esiste un database inline nella pagina, lo creiamo INLINE (is_inline=True) direttamente nella pagina
    try:
        new_db = client.databases.create(
            parent={"type": "page_id", "page_id": clean_id},
            is_inline=True,  # Inserisce la tabella direttamente INLINE nella pagina del corso!
            title=[{"type": "text", "text": {"content": f"Lezioni {course_name}"}}],
            properties={
                "Lezione": {"title": {}},
                "Appunti": {"checkbox": {}},
                "Data": {"date": {}}
            }
        )
        return new_db.get("id"), None
    except Exception as e:
        return None, f"Errore creazione tabella inline su Notion: {e}"

def get_course_lessons(course_page_id, course_name="Corso", api_key=None) -> list:
    """
    Recupera l'elenco di tutte le lezioni presenti nel database Notion per il corso specificato.
    Restituisce una lista di dizionari:
    [
        {
            "id": page_id,
            "title": title_str,
            "date": date_iso,
            "has_notes": bool,
            "url": url_str,
            "created_time": created_time
        }, ...
    ]
    Ordinati in modo decrescente per data e creazione (lezione più recente prima).
    """
    client = get_notion_client(api_key)
    if not client or not course_page_id:
        return []

    db_id, err = get_or_create_course_database(course_page_id, course_name, api_key)
    if not db_id or err:
        return []

    clean_db_id = format_notion_id(db_id)
    title_prop, checkbox_prop, date_prop = get_database_schema_props(client, clean_db_id)

    try:
        query_res = query_notion_database(client, clean_db_id)
        results = query_res.get("results", []) if isinstance(query_res, dict) else []
        
        lessons = []
        for page in results:
            pid = page.get("id")
            props = page.get("properties", {})
            
            # Titolo
            t_list = props.get(title_prop, {}).get("title", [])
            title_str = t_list[0].get("plain_text", "").strip() if t_list else ""
            if not title_str:
                title_str = f"Lezione ({pid[:6]})"

            # Data
            date_info = props.get(date_prop, {}).get("date")
            date_iso = date_info.get("start") if date_info else None
            if not date_iso:
                m_date = re.search(r'(\d{2})[/.-](\d{2})[/.-](\d{4})', title_str)
                if m_date:
                    date_iso = f"{m_date.group(3)}-{m_date.group(2)}-{m_date.group(1)}"
                else:
                    created_time = page.get("created_time", "")
                    date_iso = created_time[:10] if created_time else datetime.date.today().isoformat()

            # Checkbox Appunti
            has_notes = props.get(checkbox_prop, {}).get("checkbox", False) if checkbox_prop else False
            
            clean_pid = format_notion_id(pid).replace("-", "")
            notion_url = f"https://www.notion.so/{clean_pid}"

            lessons.append({
                "id": pid,
                "title": title_str,
                "date": date_iso,
                "has_notes": has_notes,
                "url": notion_url,
                "created_time": page.get("created_time", "")
            })

        def sort_key(item):
            d = item.get("date") or "1970-01-01"
            c = item.get("created_time") or ""
            return (d, c)

        lessons.sort(key=sort_key, reverse=True)
        return lessons
    except Exception as e:
        print(f"Errore recupero lezioni del corso {course_name} da Notion: {e}")
        return []

def find_original_version_page(results, title_prop):
    """
    Individua SEMPRE la pagina della prima versione originale (senza suffisso Versione X o la più vecchia per data di creazione).
    """
    if not results:
        return None
    
    # Cerca la pagina che NON ha (Versione nel titolo
    for page in results:
        t_list = page.get("properties", {}).get(title_prop, {}).get("title", [])
        title_str = t_list[0].get("plain_text", "") if t_list else ""
        if "(Versione" not in title_str:
            return page.get("id")
            
    # Fallback: ordina per data di creazione (più vecchia prima)
    sorted_pages = sorted(results, key=lambda x: x.get("created_time", ""))
    return sorted_pages[0].get("id")

def get_notion_page_markdown(page_id, api_key=None) -> str:
    """
    Legge TUTTI i blocchi di una pagina Notion (usando la paginazione completa)
    e ricostruisce fedelmente l'intero testo in formato Markdown.
    """
    client = get_notion_client(api_key)
    if not client or not page_id:
        return ""
    clean_id = format_notion_id(page_id)
    try:
        results = []
        has_more = True
        start_cursor = None

        # Paginazione completa per recuperare TUTTI i blocchi della pagina (anche oltre 100 blocchi)
        while has_more:
            kwargs = {"block_id": clean_id}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            
            response = client.blocks.children.list(**kwargs)
            results.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")

        lines = []
        for block in results:
            b_type = block.get("type")
            if b_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "quote", "code", "callout", "equation"]:
                
                if b_type == "equation":
                    expr = block.get("equation", {}).get("expression", "")
                    if expr:
                        lines.append(f"$$\n{expr}\n$$")
                    continue

                r_text = block.get(b_type, {}).get("rich_text", [])
                text_content = rich_text_to_markdown(r_text)
                
                if b_type == "heading_1":
                    lines.append(f"# {text_content}")
                elif b_type == "heading_2":
                    lines.append(f"## {text_content}")
                elif b_type == "heading_3":
                    lines.append(f"### {text_content}")
                elif b_type == "bulleted_list_item":
                    lines.append(f"- {text_content}")
                elif b_type == "numbered_list_item":
                    lines.append(f"1. {text_content}")
                elif b_type == "quote":
                    lines.append(f"> {text_content}")
                elif b_type == "callout":
                    icon_emoji = block.get("callout", {}).get("icon", {}).get("emoji", "📌")
                    lines.append(f"> {icon_emoji} {text_content}")
                elif b_type == "code":
                    lang = block.get("code", {}).get("language", "")
                    lines.append(f"```{lang}\n{text_content}\n```")
                else:
                    lines.append(text_content)
            elif b_type == "table":
                table_id = block.get("id")
                has_header = block.get("table", {}).get("has_column_header", True)
                try:
                    row_res = client.blocks.children.list(block_id=table_id)
                    row_blocks = row_res.get("results", [])
                    table_lines = []
                    for r_idx, r_block in enumerate(row_blocks):
                        if r_block.get("type") == "table_row":
                            cells = r_block.get("table_row", {}).get("cells", [])
                            cell_texts = [rich_text_to_markdown(c) for c in cells]
                            table_lines.append("| " + " | ".join(cell_texts) + " |")
                            if r_idx == 0 and has_header:
                                sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                                table_lines.append(sep)
                    if table_lines:
                        lines.append("\n".join(table_lines))
                except Exception as e_tbl:
                    print(f"Avviso lettura tabella Notion: {e_tbl}")
            elif b_type == "divider":
                lines.append("---")
            elif b_type == "image":
                # Blocco immagine nativo di Notion -> Markdown ![caption](url)
                img_data = block.get("image", {})
                img_type = img_data.get("type", "")
                img_url = ""
                if img_type == "external":
                    img_url = img_data.get("external", {}).get("url", "")
                elif img_type == "file":
                    img_url = img_data.get("file", {}).get("url", "")
                if img_url:
                    caption_rt = img_data.get("caption", [])
                    caption_text = rich_text_to_markdown(caption_rt) if caption_rt else "Immagine"
                    lines.append(f"![{caption_text}]({img_url})")
                
        raw_markdown = "\n\n".join(lines)
        raw_markdown = normalize_images_to_markdown(raw_markdown)
        return sanitize_latex_formulas(raw_markdown)
    except Exception as e:
        print(f"Errore lettura blocchi da Notion: {e}")
        return ""

def update_notion_page_in_place(lesson_page_id, markdown_text, api_key=None):
    """
    Svuota i blocchi esistenti della pagina Notion e vi riscrive i nuovi blocchi in ordine esatto.
    """
    return overwrite_notion_page(lesson_page_id, markdown_text, api_key=api_key)

def overwrite_notion_page(lesson_page_id, markdown_text, api_key=None):
    """
    Fallback: Svuota i blocchi esistenti della pagina Notion e vi scrive i nuovi blocchi.
    """
    client = get_notion_client(api_key)
    if not client or not lesson_page_id:
        return False, "Client Notion o Page ID mancante."

    clean_page_id = format_notion_id(lesson_page_id)

    # 1. Svuota tutti i blocchi esistenti della pagina (con paginazione)
    try:
        children_list = []
        has_more = True
        start_cursor = None
        while has_more:
            kwargs = {"block_id": clean_page_id}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            res = client.blocks.children.list(**kwargs)
            children_list.extend(res.get("results", []))
            has_more = res.get("has_more", False)
            start_cursor = res.get("next_cursor")

        def delete_single_block(block):
            b_id = block.get("id")
            try:
                client.blocks.delete(block_id=b_id)
            except Exception as e_del:
                print(f"Avviso cancellazione blocco Notion {b_id}: {e_del}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(delete_single_block, b) for b in children_list]
            concurrent.futures.wait(futures)

    except Exception as e:
        print(f"Avviso scansione blocchi da cancellare su Notion: {e}")

    # 2. Converte il nuovo Markdown modificato in blocchi Notion
    blocks = markdown_to_notion_blocks(markdown_text)

    # 3. Inserisce i nuovi blocchi nella pagina svuotata
    return append_notes_to_page(lesson_page_id, blocks, is_append=False, api_key=api_key)

def get_or_create_lesson_entry(database_id, lesson_date_str, is_same_video=False, api_key=None):
    """
    Trova o crea la riga della lezione per la data specificata nella tabella.
    - Se la data coincide MA il video è DIVERSO (is_same_video=False): accoda gli appunti alla prima versione originale.
    - Se è lo STESSO video rielaborato (is_same_video=True): crea una nuova riga distinta per la versione (Versione X).
    Restituisce (page_id, is_existing).
    """
    client = get_notion_client(api_key)
    if not client or not database_id:
        return None, False, "Client Notion o Database ID mancante."

    clean_db_id = format_notion_id(database_id)
    target_id, is_data_source = get_target_data_source_id(client, clean_db_id)
    title_prop, checkbox_prop, date_prop = get_database_schema_props(client, clean_db_id)
    base_lesson_title = f"Lezione {lesson_date_str}"
    iso_date = format_iso_date(lesson_date_str)

    filter_dict = {
        "property": title_prop,
        "title": {
            "contains": lesson_date_str
        }
    }

    try:
        query_res = query_notion_database(client, clean_db_id, filter_dict)
        results = query_res.get("results", []) if isinstance(query_res, dict) else []
        
        # CASO A: La data coincide MA il link video è DIVERSO (is_same_video=False) -> ACCODA alla prima versione originale del giorno!
        if results and not is_same_video:
            first_page_id = find_original_version_page(results, title_prop)
            if checkbox_prop:
                try:
                    client.pages.update(
                        page_id=first_page_id,
                        properties={checkbox_prop: {"checkbox": True}}
                    )
                except Exception:
                    pass
            return first_page_id, True, None

        # CASO B: Lo STESSO link video viene rielaborato (is_same_video=True) -> Crea una NUOVA RIGA per la versione!
        if results and is_same_video:
            version_num = len(results) + 1
            lesson_title = f"{base_lesson_title} (Versione {version_num})"
        else:
            lesson_title = base_lesson_title

    except Exception as e:
        print(f"Avviso ricerca riga esistente lezione su Notion: {e}")
        lesson_title = base_lesson_title

    # Crea la nuova riga con il valore del campo Data impostato al formato ISO YYYY-MM-DD
    try:
        page_props = {
            title_prop: {
                "title": [{"text": {"content": lesson_title}}]
            }
        }
        if checkbox_prop:
            page_props[checkbox_prop] = {"checkbox": True}
        if date_prop:
            page_props[date_prop] = {"date": {"start": iso_date}}

        parent_dict = {"data_source_id": target_id} if is_data_source else {"database_id": target_id}

        try:
            new_page = client.pages.create(
                parent=parent_dict,
                properties=page_props
            )
            return new_page.get("id"), False, None
        except Exception as e_create:
            new_page = client.pages.create(
                parent=parent_dict,
                properties={
                    title_prop: {
                        "title": [{"text": {"content": lesson_title}}]
                    }
                }
            )
            return new_page.get("id"), False, None
    except Exception as e:
        return None, False, f"Errore creazione riga lezione su Notion: {e}"

def parse_markdown_table(table_lines: list) -> dict | None:
    """
    Converte una lista di linee Markdown che costituiscono una tabella in un blocco 'table' nativo per Notion API,
    con relative righe 'table_row' e celle formattate tramite parse_inline_markdown.
    """
    if not table_lines:
        return None

    parsed_rows = []
    has_column_header = False

    for line in table_lines:
        s = line.strip()
        if not s:
            continue
        parts = s.split("|")
        if s.startswith("|"):
            parts = parts[1:]
        if s.endswith("|"):
            parts = parts[:-1]

        cells = [p.strip() for p in parts]
        if not cells or all(c == "" for c in cells):
            continue

        # Verifica riga separatrice (es. | :--- | :--- |)
        is_separator = all(re.match(r'^:?-+:?$', c) for c in cells if c)
        if is_separator:
            has_column_header = True
            continue

        parsed_rows.append(cells)

    if not parsed_rows:
        return None

    table_width = max(len(row) for row in parsed_rows)
    if table_width == 0:
        return None

    table_rows = []
    for row in parsed_rows:
        cells_payload = []
        for cell_text in row:
            rt = parse_inline_markdown(cell_text)
            if not rt:
                rt = [{"type": "text", "text": {"content": ""}}]
            cells_payload.append(rt)

        while len(cells_payload) < table_width:
            cells_payload.append([{"type": "text", "text": {"content": ""}}])

        table_rows.append({
            "type": "table_row",
            "table_row": {
                "cells": cells_payload
            }
        })

    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": has_column_header,
            "has_row_header": False,
            "children": table_rows
        }
    }

def markdown_to_notion_blocks(markdown_text: str):
    """
    Converte una stringa di testo Markdown in una lista di blocchi Notion API,
    interpretando ed applicando formattazione inline, formule LaTeX, tabelle native, titoli, Callout (📌) e Immagini.
    """
    if not markdown_text:
        return []
    markdown_text = normalize_images_to_markdown(markdown_text)
    blocks = []
    lines = markdown_text.splitlines()
    N = len(lines)
    idx = 0

    while idx < N:
        line = lines[idx]
        stripped = line.strip()

        # 1. Blocchi di codice ```
        if stripped.startswith("```"):
            code_lines = []
            lang = stripped.replace("```", "").strip()
            code_language = sanitize_code_language(lang)
            idx += 1
            while idx < N:
                if lines[idx].strip().startswith("```"):
                    idx += 1
                    break
                code_lines.append(lines[idx])
                idx += 1
            code_content = "\n".join(code_lines)
            rich_text = []
            for i in range(0, max(1, len(code_content)), 2000):
                chunk_text = code_content[i:i+2000]
                if chunk_text:
                    rich_text.append({"type": "text", "text": {"content": chunk_text}})
            if not rich_text:
                rich_text = [{"type": "text", "text": {"content": ""}}]

            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": rich_text,
                    "language": code_language
                }
            })
            continue

        # 2. Formule matematiche a blocco $$ ... $$ o \begin{equation}
        if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            expr = stripped[2:-2].strip()
            expr = re.sub(r'\\begin\{equation\*?\}', '', expr)
            expr = re.sub(r'\\end\{equation\*?\}', '', expr).strip()
            if expr:
                blocks.append({
                    "object": "block",
                    "type": "equation",
                    "equation": {"expression": expr[:2000]}
                })
            idx += 1
            continue

        if stripped.startswith("$$") or stripped.startswith(r"\begin{equation"):
            math_lines = []
            cleaned_start = stripped.replace("$$", "").strip()
            if cleaned_start and not cleaned_start.startswith(r"\begin{equation"):
                math_lines.append(cleaned_start)
            idx += 1
            while idx < N:
                cur = lines[idx].strip()
                if cur.endswith("$$") or cur.startswith(r"\end{equation"):
                    cleaned_end = cur.replace("$$", "").strip()
                    if cleaned_end and not cleaned_end.startswith(r"\end{equation"):
                        math_lines.append(cleaned_end)
                    idx += 1
                    break
                math_lines.append(lines[idx])
                idx += 1
            math_content = "\n".join(math_lines)
            expr = re.sub(r'\\begin\{equation\*?\}', '', math_content)
            expr = re.sub(r'\\end\{equation\*?\}', '', expr).strip()
            if expr:
                blocks.append({
                    "object": "block",
                    "type": "equation",
                    "equation": {"expression": expr[:2000]}
                })
            continue

        # 3. Tabelle Markdown (righe che iniziano con '|')
        if stripped.startswith("|"):
            table_lines = []
            while idx < N:
                cur = lines[idx].strip()
                if cur.startswith("|"):
                    table_lines.append(lines[idx])
                    idx += 1
                elif not cur and idx + 1 < N and lines[idx + 1].strip().startswith("|"):
                    idx += 1
                else:
                    break
            tbl_block = parse_markdown_table(table_lines)
            if tbl_block:
                blocks.append(tbl_block)
            continue

        if not stripped:
            idx += 1
            continue

        # 3.5 Immagini Markdown ![alt](url) o ![alt|50%](url)
        img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', stripped)
        if not img_match and "resizable-img-wrapper" in stripped:
            norm_s = normalize_images_to_markdown(stripped)
            img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', norm_s.strip())
        if img_match:
            alt_text = img_match.group(1) or "Immagine"
            img_url = img_match.group(2).strip()
            if "|" in alt_text:
                alt_text = alt_text.split("|")[0].strip()
            caption_rt = [{"type": "text", "text": {"content": alt_text}}] if alt_text and alt_text != "Immagine" else []
            blocks.append({
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": img_url},
                    "caption": caption_rt
                }
            })
            idx += 1
            continue

        # 4. Separatore ---
        if stripped in ["---", "***", "___"]:
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
            idx += 1
            continue

        # 5. Titoli #, ##, ###, ####, #####, ######
        if stripped.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": parse_inline_markdown(stripped[2:])}
            })
        elif stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": parse_inline_markdown(stripped[3:])}
            })
        elif stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": parse_inline_markdown(stripped[4:])}
            })
        elif re.match(r'^#{4,6}\s', stripped):
            cleaned_title = re.sub(r'^#{4,6}\s', '', stripped)
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": parse_inline_markdown(cleaned_title)}
            })
        # 6. Callout con emoji (📌, 📝, 💡, ⚠️, 🎓, ℹ️)
        elif stripped.startswith("> ") and any(e in stripped for e in ["📌", "📝", "💡", "⚠️", "🎓", "ℹ️"]):
            match_callout = re.match(r'^>\s*([📌📝💡⚠️🎓ℹ️])\s*(.*)$', stripped)
            if match_callout:
                emoji_char = match_callout.group(1)
                callout_text = match_callout.group(2)
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": parse_inline_markdown(callout_text),
                        "icon": {"type": "emoji", "emoji": emoji_char}
                    }
                })
            else:
                blocks.append({
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": parse_inline_markdown(stripped[2:])}
                })
        # 7. Liste puntate - o *
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_inline_markdown(stripped[2:])}
            })
        # 8. Liste numerate 1. 2.
        elif re.match(r'^\d+\.\s', stripped):
            content = re.sub(r'^\d+\.\s', '', stripped)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline_markdown(content)}
            })
        # 9. Quotes / Citazioni
        elif stripped.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": parse_inline_markdown(stripped[2:])}
            })
        # 10. Paragrafi normali
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": parse_inline_markdown(stripped)}
            })

        idx += 1

    return blocks

def append_notes_to_page(lesson_page_id, blocks, is_append=False, api_key=None):
    """
    Invia i blocchi Notion alla pagina specificata in PARALLELO. Se is_append è True,
    inserisce un divisore ed un'intestazione di integrazione.
    """
    client = get_notion_client(api_key)
    if not client or not lesson_page_id:
        return False, "Client Notion o Page ID mancante."

    clean_page_id = format_notion_id(lesson_page_id)

    try:
        final_blocks = []
        if is_append:
            today_time = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
            final_blocks.extend([
                {"object": "block", "type": "divider", "divider": {}},
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {"content": f"📌 Integrazione Lezione - Aggiunta il {today_time}"}}],
                        "icon": {"type": "emoji", "emoji": "📌"}
                    }
                }
            ])
        final_blocks.extend(blocks)

        chunk_size = 50
        chunks = [final_blocks[i:i + chunk_size] for i in range(0, len(final_blocks), chunk_size)]

        for chunk in chunks:
            try:
                client.blocks.children.append(
                    block_id=clean_page_id,
                    children=chunk
                )
            except Exception as e_chunk:
                print(f"Avviso inserimento chunk Notion ({e_chunk}), tentativo blocco per blocco...")
                for single_block in chunk:
                    try:
                        client.blocks.children.append(
                            block_id=clean_page_id,
                            children=[single_block]
                        )
                    except Exception as e_single:
                        print(f"Errore inserimento singolo blocco su Notion: {e_single}")

        return True, None
    except Exception as e:
        return False, f"Errore durante l'inserimento degli appunti su Notion: {e}"
