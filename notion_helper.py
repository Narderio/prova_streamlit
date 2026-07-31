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

def query_notion_database(client: Client, database_id: str, filter_dict: dict):
    """
    Interroga la tabella Notion usando il Data Source ID o il Database ID.
    """
    target_id, is_data_source = get_target_data_source_id(client, database_id)
    
    # 1. Prova con data_sources.query (Notion SDK 3.x)
    if is_data_source and hasattr(client, "data_sources"):
        try:
            return client.data_sources.query(data_source_id=target_id, filter=filter_dict)
        except Exception as e:
            print(f"Notion data_sources.query notice: {e}")

    # 2. Prova con databases.query (Notion SDK 2.x legacy)
    if hasattr(client, "databases") and hasattr(client.databases, "query"):
        try:
            return client.databases.query(database_id=target_id, filter=filter_dict)
        except Exception as e:
            print(f"Notion databases.query notice: {e}")

    # 3. Prova tramite client.request con data_sources o databases
    try:
        path_str = f"data_sources/{target_id}/query" if is_data_source else f"databases/{target_id}/query"
        return client.request(
            path=path_str,
            method="POST",
            body={"filter": filter_dict}
        )
    except Exception as e:
        print(f"Notion request query notice: {e}")

    return {"results": []}

def get_database_schema_props(client: Client, database_id: str):
    """
    Ispeziona ed allinea lo schema del Database Notion (Notion API v3 data_sources).
    Trova o imposta il nome della colonna 'title' su 'Lezione' e della colonna 'checkbox' su 'Appunti'.
    """
    target_id, is_data_source = get_target_data_source_id(client, database_id)
    title_prop_name = None
    checkbox_prop_name = None

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

        update_payload = {}
        if title_prop_name and title_prop_name != "Lezione":
            update_payload[title_prop_name] = {"name": "Lezione"}
        if not checkbox_prop_name:
            update_payload["Appunti"] = {"checkbox": {}}

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
            except Exception as e_upd:
                print(f"Avviso aggiornamento schema Notion: {e_upd}")
    except Exception as e:
        print(f"Avviso lettura schema Notion: {e}")

    if not title_prop_name:
        title_prop_name = "Lezione"
    if not checkbox_prop_name:
        checkbox_prop_name = "Appunti"

    return title_prop_name, checkbox_prop_name

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
    Altrimenti crea un DATABASE INLINE direttamente nella pagina con colonne 'Lezione' (Title) e 'Appunti' (Checkbox).
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
                "Appunti": {"checkbox": {}}
            }
        )
        return new_db.get("id"), None
    except Exception as e:
        return None, f"Errore creazione tabella inline su Notion: {e}"

def get_or_create_lesson_entry(database_id, lesson_date_str, api_key=None):
    """
    Trova o crea la riga della lezione per la data specificata nella tabella.
    Rileva dinamicamente i nomi reali delle colonne del Database Notion per evitare errori.
    Restituisce (page_id, is_existing).
    """
    client = get_notion_client(api_key)
    if not client or not database_id:
        return None, False, "Client Notion o Database ID mancante."

    clean_db_id = format_notion_id(database_id)
    target_id, is_data_source = get_target_data_source_id(client, clean_db_id)
    title_prop, checkbox_prop = get_database_schema_props(client, clean_db_id)
    lesson_title = f"Lezione {lesson_date_str}"

    filter_dict = {
        "property": title_prop,
        "title": {
            "contains": lesson_date_str
        }
    }

    try:
        query_res = query_notion_database(client, clean_db_id, filter_dict)
        results = query_res.get("results", []) if isinstance(query_res, dict) else []
        if results:
            page_id = results[0].get("id")
            if checkbox_prop:
                try:
                    client.pages.update(
                        page_id=page_id,
                        properties={checkbox_prop: {"checkbox": True}}
                    )
                except Exception:
                    pass
            return page_id, True, None
    except Exception as e:
        print(f"Avviso ricerca riga esistente lezione su Notion: {e}")

    # Se non trovata, crea una nuova riga nella tabella agganciandosi al data_source_id (Notion SDK 3.x) o database_id
    try:
        page_props = {
            title_prop: {
                "title": [{"text": {"content": lesson_title}}]
            }
        }
        if checkbox_prop:
            page_props[checkbox_prop] = {"checkbox": True}

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
    interpretando ed applicando la formattazione inline (Grassetto, Corsivo, Equazioni LaTeX, Codice).
    """
    blocks = []
    lines = markdown_text.splitlines()
    in_code_block = False
    code_lines = []
    code_language = "plain text"

    for line in lines:
        stripped = line.strip()

        # Blocchi di codice ```
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

        if not stripped:
            continue

        # Separatore ---
        if stripped in ["---", "***", "___"]:
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
            continue

        # Titoli #, ##, ###
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
