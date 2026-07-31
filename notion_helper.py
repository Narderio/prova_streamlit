import os
import re
import datetime
from notion_client import Client
from dotenv import load_dotenv

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

def clean_markdown_for_streamlit(text: str) -> str:
    """
    Pulisce la sintassi LaTeX/Markdown per garantire che Streamlit (KaTeX) e Notion renderizzino
    le formule senza errori o caratteri rotti.
    """
    if not text:
        return ""
    # Rimuove \begin{equation} e \end{equation} ridondanti che rompono KaTeX in Streamlit
    cleaned = re.sub(r'\\begin\{equation\*?\}', '', text)
    cleaned = re.sub(r'\\end\{equation\*?\}', '', cleaned)
    # Assicura che i blocchi $$ siano a capo e staccati dal testo successivo
    cleaned = re.sub(r'([^\n])\$\$', r'\1\n$$', cleaned)
    cleaned = re.sub(r'\$\$([^\n])', r'$$\n\1', cleaned)
    return cleaned

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

def get_notion_client(api_key=None) -> Client | None:
    token = api_key or os.getenv("NOTION_API_KEY")
    if not token:
        return None
    try:
        return Client(auth=token)
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
    Legge i blocchi di una pagina Notion e ricostruisce fedelmente il testo in formato Markdown
    ripristinando formule LaTeX ($...$), grassetto, corsivo e codice.
    """
    client = get_notion_client(api_key)
    if not client or not page_id:
        return ""
    clean_id = format_notion_id(page_id)
    try:
        response = client.blocks.children.list(block_id=clean_id)
        lines = []
        for block in response.get("results", []):
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
                    lines.append(f"> 📌 {text_content}")
                elif b_type == "code":
                    lang = block.get("code", {}).get("language", "")
                    lines.append(f"```{lang}\n{text_content}\n```")
                else:
                    lines.append(text_content)
            elif b_type == "divider":
                lines.append("---")
                
        raw_markdown = "\n\n".join(lines)
        return clean_markdown_for_streamlit(raw_markdown)
    except Exception as e:
        print(f"Errore lettura blocchi da Notion: {e}")
        return ""

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

def markdown_to_notion_blocks(markdown_text: str):
    """
    Converte una stringa di testo Markdown in una lista di blocchi Notion API,
    interpretando ed applicando la formattazione inline, formule LaTeX a blocco e titoli a tutti i livelli.
    """
    blocks = []
    lines = markdown_text.splitlines()
    
    in_code_block = False
    code_lines = []
    code_language = "plain text"
    
    in_math_block = False
    math_lines = []

    for line in lines:
        stripped = line.strip()

        # 1. Blocchi di codice ```
        if stripped.startswith("```"):
            if in_code_block:
                code_content = "\n".join(code_lines)
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": code_content[:2000]}}],
                        "language": code_language or "plain text"
                    }
                })
                in_code_block = False
                code_lines = []
            else:
                in_code_block = True
                lang = stripped.replace("```", "").strip()
                code_language = lang if lang else "plain text"
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # 2. Formule matematiche a blocco $$ ... $$ o \begin{equation} ... \end{equation}
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
            continue

        if stripped.startswith("$$") or stripped.startswith(r"\begin{equation"):
            if in_math_block:
                math_content = "\n".join(math_lines)
                expr = math_content.replace("$$", "").strip()
                expr = re.sub(r'\\begin\{equation\*?\}', '', expr)
                expr = re.sub(r'\\end\{equation\*?\}', '', expr).strip()
                if expr:
                    blocks.append({
                        "object": "block",
                        "type": "equation",
                        "equation": {"expression": expr[:2000]}
                    })
                in_math_block = False
                math_lines = []
            else:
                in_math_block = True
                cleaned_start = stripped.replace("$$", "").strip()
                if cleaned_start and not cleaned_start.startswith(r"\begin{equation"):
                    math_lines.append(cleaned_start)
            continue

        if in_math_block:
            if stripped.endswith("$$") or stripped.startswith(r"\end{equation"):
                cleaned_end = stripped.replace("$$", "").strip()
                if cleaned_end and not cleaned_end.startswith(r"\end{equation"):
                    math_lines.append(cleaned_end)
                math_content = "\n".join(math_lines)
                expr = re.sub(r'\\begin\{equation\*?\}', '', math_content)
                expr = re.sub(r'\\end\{equation\*?\}', '', expr).strip()
                if expr:
                    blocks.append({
                        "object": "block",
                        "type": "equation",
                        "equation": {"expression": expr[:2000]}
                    })
                in_math_block = False
                math_lines = []
            else:
                math_lines.append(line)
            continue

        if not stripped:
            continue

        # 3. Separatore ---
        if stripped in ["---", "***", "___"]:
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
            continue

        # 4. Titoli #, ##, ###, ####, #####, ######
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
        # Liste puntate - o *
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_inline_markdown(stripped[2:])}
            })
        # Liste numerate 1. 2.
        elif re.match(r'^\d+\.\s', stripped):
            content = re.sub(r'^\d+\.\s', '', stripped)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline_markdown(content)}
            })
        # Quotes / Citazioni
        elif stripped.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": parse_inline_markdown(stripped[2:])}
            })
        # Paragrafi normali
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": parse_inline_markdown(stripped)}
            })

    return blocks

def append_notes_to_page(lesson_page_id, blocks, is_append=False, api_key=None):
    """
    Invia i blocchi Notion alla pagina specificata. Se is_append è True,
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
                        "icon": {"type": "emoji", "emoji": "📝"}
                    }
                }
            ])
        final_blocks.extend(blocks)

        # Notion accetta massimo 100 blocchi per chiamata API
        chunk_size = 80
        for i in range(0, len(final_blocks), chunk_size):
            chunk = final_blocks[i:i + chunk_size]
            client.blocks.children.append(
                block_id=clean_page_id,
                children=chunk
            )
        return True, None
    except Exception as e:
        return False, f"Errore durante l'inserimento degli appunti su Notion: {e}"
