import os
import io
import re
import json
import zipfile
import mimetypes
from PIL import Image
from google import genai
from google.genai import types

import supabase_client
import gemini_rate_tracker

# Formati immagine supportati
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

IMAGE_PLACEMENT_SYSTEM_PROMPT = """Sei un docente universitario esperto e un instructional designer di altissimo livello.
Il tuo compito è analizzare visivamente una collezione numerata di immagini (schemi, grafici, tabelle, slide, formule o foto della lavagna) e individuare il PUNTO ESATTO all'interno degli appunti universitari in cui ciascuna immagine deve essere posizionata per fornire il massimo valore didattico allo studente.

REGOLE TASSATIVE:
1. Analizza attentamente ciascuna immagine numerata: comprendi il concetto teorico, il modello matematico o il diagramma rappresentato.
2. Analizza il testo Markdown degli appunti fornito.
3. Per ciascuna immagine:
   - "image_index": l'indice numerico (1-based) corrispondente all'immagine analizzata.
   - "caption": una didascalia didattica chiara, sintetica e professionale (es. "Figura: Rappresentazione geometrica dello spazio operativo").
   - "section_heading": il titolo esatto della sezione più pertinente presente negli appunti (es. "## Cinematica Differenziale e Jacobiano").
   - "insert_after_snippet": un frammento di testo ESATTO (da 5 a 15 parole consecutive) presente negli appunti al termine del paragrafo o della frase più attinente, DOPO il quale l'immagine deve essere inserita.
4. Non inventare frasi: "insert_after_snippet" DEVE essere una citazione esatta e letterale tratta dal testo degli appunti.
5. Assegna ogni immagine alla sua collocazione naturale più coerente dal punto di vista didattico.
"""

IMAGE_PLACEMENT_JSON_SCHEMA = {
    "type": "ARRAY",
    "description": "Elenco ordinato dei posizionamenti per ciascuna immagine nel testo degli appunti",
    "items": {
        "type": "OBJECT",
        "properties": {
            "image_index": {
                "type": "INTEGER",
                "description": "Indice 1-based dell'immagine analizzata"
            },
            "caption": {
                "type": "STRING",
                "description": "Didascalia accademica descrittiva per la figura"
            },
            "section_heading": {
                "type": "STRING",
                "description": "Intestazione Markdown della sezione più pertinente (es. '## Cinematica Inversa')"
            },
            "insert_after_snippet": {
                "type": "STRING",
                "description": "Frammento di testo letterale ed esatto (5-15 parole) dopo cui posizionare l'immagine"
            }
        },
        "required": ["image_index", "caption", "insert_after_snippet"]
    }
}


def process_uploaded_files(uploaded_files: list) -> list[dict]:
    """
    Estrae le immagini dai file caricati (singoli file immagine o archivi .zip).
    Implementa la Doppia Pipeline:
    - Conserva i byte originali al 100% per Supabase Storage (zero perdita di qualità).
    - Genera in memoria una copia leggera a max 1024px WebP destinata esclusivamente a Gemini.
    """
    prepared_images = []

    for uf in uploaded_files:
        filename = uf.name if hasattr(uf, "name") else "immagine"
        raw_bytes = uf.getvalue() if hasattr(uf, "getvalue") else uf.read()

        # Caso 1: Archivio ZIP contenente una cartella di immagini
        if filename.lower().endswith(".zip") or getattr(uf, "type", "") in {"application/zip", "application/x-zip-compressed"}:
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                    for zip_info in z.infolist():
                        if zip_info.is_dir():
                            continue
                        name_lower = zip_info.filename.lower()
                        # Escludi cartelle di sistema Mac/Windows
                        if "__macosx" in name_lower or ".ds_store" in name_lower:
                            continue
                        ext = os.path.splitext(name_lower)[1]
                        if ext in SUPPORTED_IMAGE_EXTS:
                            img_bytes = z.read(zip_info.filename)
                            base_name = os.path.basename(zip_info.filename)
                            item = _prepare_single_image(img_bytes, base_name)
                            if item:
                                prepared_images.append(item)
            except Exception as e:
                print(f"Errore lettura file ZIP {filename}: {e}")
        else:
            # Caso 2: Singolo file immagine
            ext = os.path.splitext(filename.lower())[1]
            if ext in SUPPORTED_IMAGE_EXTS:
                item = _prepare_single_image(raw_bytes, filename)
                if item:
                    prepared_images.append(item)

    return prepared_images


def _prepare_single_image(raw_bytes: bytes, filename: str) -> dict | None:
    """
    Costruisce l'oggetto immagine con pipeline sdoppiata:
    1. original_bytes intatti per Supabase.
    2. gemini_bytes compresso a max 1024px in RAM per la Vision API.
    """
    if not raw_bytes or len(raw_bytes) < 10:
        return None

    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type or not mime_type.startswith("image/"):
        ext = os.path.splitext(filename.lower())[1].replace(".", "")
        mime_type = f"image/{ext}" if ext else "image/png"

    # Generazione copia compressa per Gemini
    gemini_bytes = raw_bytes
    gemini_mime = mime_type
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # Converti in RGB se necessario (es. RGBA, P, CMYK)
            if img.mode in ("RGBA", "LA", "P"):
                # Mantieni trasparenza per WebP o converti in RGB su sfondo bianco
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            else:
                rgb_img = img.convert("RGB")

            # Ridimensiona a max 1024x1024 se supera tale risoluzione
            max_dim = 1024
            if max(rgb_img.width, rgb_img.height) > max_dim:
                rgb_img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            rgb_img.save(buf, format="WEBP", quality=82, method=4)
            gemini_bytes = buf.getvalue()
            gemini_mime = "image/webp"
    except Exception as e:
        print(f"Warning compressione immagine {filename} per Gemini: {e}")
        gemini_bytes = raw_bytes
        gemini_mime = mime_type

    return {
        "name": filename,
        "original_bytes": raw_bytes,
        "original_mime": mime_type,
        "original_size": len(raw_bytes),
        "gemini_bytes": gemini_bytes,
        "gemini_mime": gemini_mime,
        "gemini_size": len(gemini_bytes),
        "public_url": None
    }


def upload_images_to_supabase(prepared_images: list[dict]) -> tuple[bool, str | None, list[dict]]:
    """
    Carica i file originali (100% risoluzione) su Supabase Storage nel bucket 'canvas-images'.
    Aggiorna ciascun elemento con il rispettivo 'public_url'.
    """
    if not prepared_images:
        return False, "Nessuna immagine fornita per l'upload.", prepared_images

    uploaded_count = 0
    errors = []

    for img in prepared_images:
        # Se già caricata (es. retry)
        if img.get("public_url"):
            uploaded_count += 1
            continue

        success, res = supabase_client.upload_canvas_image(
            file_bytes=img["original_bytes"],
            filename=img["name"],
            content_type=img["original_mime"]
        )
        if success and res:
            img["public_url"] = res
            uploaded_count += 1
        else:
            errors.append(f"{img['name']}: {res}")

    if uploaded_count == 0:
        return False, f"Upload fallito per tutte le immagini: {'; '.join(errors)}", prepared_images

    return True, None, prepared_images


def analyze_and_match_image_positions(
    markdown_notes: str,
    prepared_images: list[dict],
    course_name: str = "",
    model_name: str = "gemini-3.5-flash-lite"
) -> list[dict]:
    """
    Esegue UNA SINGOLA CHIAMATA multimodale a Gemini passando il testo del Canvas
    e tutte le immagini compresse per individuare la posizione ideale di ciascuna.
    Restituisce la lista di mappature JSON conformi a IMAGE_PLACEMENT_JSON_SCHEMA.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY non trovata. Configurala nel file .env.")

    if not markdown_notes or not markdown_notes.strip():
        raise ValueError("Il documento degli appunti è vuoto.")

    if not prepared_images:
        raise ValueError("Nessuna immagine valida da analizzare.")

    client = genai.Client(api_key=api_key)

    # Costruzione del payload unificato
    course_info = f"Materia/Corso: {course_name}\n" if course_name else ""
    user_header = f"""{course_info}TESTO ATTUALE DEGLI APPUNTI (CANVAS):
---
{markdown_notes}
---

COLLEZIONE IMMAGINI DA POSIZIONARE ({len(prepared_images)} immagini allegate):"""

    contents = [
        types.Part.from_text(text=f"{IMAGE_PLACEMENT_SYSTEM_PROMPT}\n\n{user_header}")
    ]

    for idx, img in enumerate(prepared_images):
        img_label = f"\n=== IMMAGINE {idx + 1} (File: {img['name']}) ==="
        contents.append(types.Part.from_text(text=img_label))
        contents.append(types.Part.from_bytes(
            data=img["gemini_bytes"],
            mime_type=img["gemini_mime"]
        ))

    contents.append(types.Part.from_text(
        text="\nGenera ora la mappatura JSON contenente la posizione ideale e la didascalia per ciascuna immagine."
    ))

    # Log della singola richiesta nel tracker di sicurezza (1 chiamata = 1 slot RPM)
    gemini_rate_tracker.log_request()

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=IMAGE_PLACEMENT_JSON_SCHEMA,
            temperature=0.1
        )
    )

    raw_text = response.text or ""
    return _parse_placement_json(raw_text)


def _parse_placement_json(raw_text: str) -> list[dict]:
    """Decodifica resiliente del JSON di posizionamento restituito da Gemini."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            for k in ["placements", "images", "items", "results"]:
                if k in data and isinstance(data[k], list):
                    return data[k]
    except Exception as err:
        print(f"Errore parsing JSON posizionamento: {err}\nOutput: {raw_text[:300]}")

    return []


def inject_images_into_markdown(
    markdown_notes: str,
    placements: list[dict],
    prepared_images: list[dict]
) -> tuple[str, list[dict]]:
    """
    Inserisce in modo deterministico e chirurgico i tag Markdown delle immagini
    senza alterare o riscrivere alcuna parte del testo originale.
    Restituisce (nuovo_markdown, resoconto_posizionamenti).
    """
    updated_md = markdown_notes
    summary_report = []

    # Mappa delle immagini per indice (1-based)
    img_by_idx = {i + 1: img for i, img in enumerate(prepared_images)}

    for p in placements:
        img_idx = p.get("image_index")
        if img_idx not in img_by_idx:
            continue

        img_obj = img_by_idx[img_idx]
        public_url = img_obj.get("public_url")
        if not public_url:
            continue

        caption = str(p.get("caption", img_obj["name"])).strip()
        section_heading = str(p.get("section_heading", "")).strip()
        snippet = str(p.get("insert_after_snippet", "")).strip()

        # Tag Markdown con figura e didascalia centrata/corsiva
        image_tag = f"\n\n![{caption}]({public_url})\n*{caption}*\n"

        inserted = False

        # Strategia 1: Cerca lo snippet esatto
        if snippet and snippet in updated_md:
            # Trova la prima occorrenza dello snippet e inserisci subito dopo il blocco
            pos = updated_md.find(snippet) + len(snippet)
            # Avanza fino al termine del paragrafo (doppio a capo o fine riga)
            next_newline = updated_md.find("\n", pos)
            insert_pos = next_newline if next_newline != -1 else pos
            updated_md = updated_md[:insert_pos] + image_tag + updated_md[insert_pos:]
            inserted = True

        # Strategia 2: Se lo snippet ha piccole discrepanze, cerca le prime 6 parole
        if not inserted and snippet:
            words = snippet.split()
            if len(words) >= 4:
                sub_snippet = " ".join(words[:5])
                if sub_snippet in updated_md:
                    pos = updated_md.find(sub_snippet) + len(sub_snippet)
                    next_newline = updated_md.find("\n", pos)
                    insert_pos = next_newline if next_newline != -1 else pos
                    updated_md = updated_md[:insert_pos] + image_tag + updated_md[insert_pos:]
                    inserted = True

        # Strategia 3: Se lo snippet non è trovato, colloca sotto la sezione indicata
        if not inserted and section_heading:
            # Cerca l'intestazione della sezione (es. '## Cinematica Inversa')
            heading_pos = updated_md.find(section_heading)
            if heading_pos != -1:
                # Trova la fine del primo o secondo paragrafo dopo l'intestazione
                after_heading = heading_pos + len(section_heading)
                # Salta una riga o paragrafo
                p_end = updated_md.find("\n\n", after_heading)
                insert_pos = (p_end + 2) if p_end != -1 else after_heading
                updated_md = updated_md[:insert_pos] + image_tag + updated_md[insert_pos:]
                inserted = True

        # Strategia 4: Fallback sicuro, appendi in fondo al documento
        if not inserted:
            updated_md = updated_md.rstrip() + "\n" + image_tag
            inserted = True

        summary_report.append({
            "image_name": img_obj["name"],
            "caption": caption,
            "section": section_heading or "Testo Principale",
            "url": public_url,
            "original_size_kb": round(img_obj["original_size"] / 1024, 1),
            "gemini_size_kb": round(img_obj["gemini_size"] / 1024, 1)
        })

    return updated_md, summary_report
