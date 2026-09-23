import os
import re
from google import genai
import gemini_rate_tracker
import notion_helper

EXHAUSTIVE_TOPIC_SYSTEM_PROMPT = """Sei un chiarissimo ed insigne professore universitario e autore di testi accademici avanzati.
Il tuo compito è redigere un CAPITOLO ACCADEMICO COMPLETO, INTEGRALE ED ESTREMAMENTE APPROFONDITO su un argomento specifico, estraendolo dalle lezioni fornite.

REGOLE TASSATIVE E INVIOLABILI:
1. FILTRO DI ISOLAMENTO TEMATICO RIGOROSO (PERTINENZA ASSOLUTA):
   - DEVI trattare ed estrarre ESCLUSIVAMENTE il materiale didattico che riguarda l'ARGOMENTO RICHIESTO (e le sole nozioni propedeutiche o conseguenze strettamente e direttamente collegate ad esso).
   - È SEVERAMENTE VIETATO includere altri argomenti o capitoli differenti affrontati dal professore nelle stesse lezioni.
   - ESEMPIO FONDAMENTALE: Se la lezione ha trattato sia "Cinematica del punto" sia "Termodinamica", e l'utente ha richiesto solo "Cinematica del punto", DEVI SCARTARE COMPLETAMENTE la parte di Termodinamica! Non deve comparire minimamente nel testo.
   - Concentrati al 100% solo sul tema richiesto, ignorando ogni divagazione o capitolo slegato presente nella lezione.

2. MASSIMA COMPLETEZZA SUL TEMA SCELTO (ZERO OMISSIONI DIDATTICHE SUL TOPIC):
   - Per l'argomento richiesto, NON riassumere all'osso e NON omettere passaggi algebrici, definizioni o esempi pertinenti.
   - È SEVERAMENTE VIETATO usare formule di comodo come "si dimostra facilmente che...", "per brevità omettiamo...", "analogamente...".
   - Se nelle lezioni compare una dimostrazione su questo tema, DEVI riportarla passo-passo con tutti i passaggi algebrici e le motivazioni logiche in LaTeX.

3. RIGORE SCIENTIFICO ACCADEMICO:
   - Riporta per esteso le definizioni formali: insieme di definizione, ipotesi, tesi, notazione formale e significato intuitivo.
   - Includi tutti i casi limite, le eccezioni, le condizioni di validità e le note del docente pertinenti a questo argomento.

4. FORMULE MATEMATICHE IN LATEX:
   - Usa $ ... $ per la notazione in linea nel testo.
   - Usa $$ su riga separata per tutte le formule, equazioni e dimostrazioni principali.

5. PRESERVAZIONE INTEGRALE DEI MEDIA PERTINENTI:
   - Se negli appunti delle lezioni compaiono immagini nel formato `![alt](url)` o `![alt|dim](url)` che riguardano questo argomento, DEVI PRESERVARLE TUTTE E COLLOCARLE NEL PUNTO CONTESTUALE ESATTO della trattazione.
   - Se compaiono diagrammi Mermaid (```mermaid ... ```) pertinenti, mantienili intatti e racchiudi SEMPRE il testo dei nodi tra virgolette doppie (es. A["testo con & o /"] --> B["altro: testo"]) per prevenire errori lessicali di visualizzazione.

6. STRUTTURA DEL CAPITOLO A "CONNESSIONI":
   - Intestazione principale chiara: `# [Nome Esaustivo dell'Argomento]`
   - Box fonti:
     `> 📖 **Compendio Tematico Integrale** | *Fonti: [Elenco lezioni d'origine con data]*`
   - NESSUN INDICE O SOMMARIO: NON inserire indici, sommari o elenchi puntati di navigazione all'inizio. Dopo il box fonti passa direttamente alla prima sezione.
   - Sezioni logiche dettagliate:
     - `## 1. Definizione ed Enunciato Formale`
     - `## 2. Dimostrazioni e Trattazione Matematica Dettagliata`
     - `## 3. Proprietà, Condizioni di Validità ed Eccezioni`
     - `## 4. Esempi Notabili, Esercizi Applicativi e Casi d'Uso`
     - `## 5. Mappa Concettuale e Connessioni con altri Argomenti` (elenca gli argomenti propedeutici e successivi del corso correlati).

7. NESSUN TESTO CONVERSAZIONALE:
   - Restituisci ESCLUSIVAMENTE il documento Markdown pronto per lo studio, senza frasi introduttive o conclusive.
"""

SMART_MERGE_TOPIC_SYSTEM_PROMPT = """Sei un accademico e autore esperto incaricato di aggiornare e ampliare un Compendio di Studio Universitario già esistente.
Riceverai:
1. Il capitolo già redatto e consolidato sull'argomento.
2. Gli appunti di una NUOVA LEZIONE da cui estrarre ulteriori concetti inerenti a questo argomento.

IL TUO OBIETTIVO:
Eseguire una FUSIONE ORGANICA (Smart Merge), integrando armoniosamente le nuove nozioni nel capitolo esistente SENZA PERDERE NULLA di quanto già scritto e SENZA INTRODURRE TEMI NON PERTINENTI.

REGOLE TASSATIVE:
1. FILTRO TEMATICO RIGOROSO:
   - Dalla nuova lezione estrai ed integra ESCLUSIVAMENTE ciò che arricchisce o approfondisce l'argomento del capitolo.
   - SCARTA categoricamente qualsiasi altra parte della nuova lezione che tratti argomenti diversi e non collegati.
2. PRESERVAZIONE TOTALE DEL TESTO PRECEDENTE:
   - NON cancellare, non riassumere e non accorciare i concetti, formule e dimostrazioni già formalizzati nel documento originario.
3. COLLOCAZIONE INTELLIGENTE DELLE NUOVE NOZIONI:
   - Se la nuova lezione aggiunge un corollario o un approfondimento a una sezione esistente, inseriscilo direttamente in quella sezione.
   - Se introduce un nuovo sotto-argomento o una nuova applicazione inerente, crea una nuova sezione `##` o `###` armonizzata.
4. FILTRO DELLE RIDONDANZE TEMPORALI:
   - Evita di duplicare spiegazioni identiche già presenti: integra solo sfumature, esempi nuovi o formule aggiuntive.
5. FORMULE E MEDIA:
   - Mantieni tutte le formule LaTeX ($ e $$) e tutti i tag immagine `![alt](url)` del testo originale, aggiungendo quelli della nuova lezione.
6. AGGIORNAMENTO DELLE FONTI:
   - Aggiorna il box iniziale delle fonti includendo la nuova lezione aggiunta.
7. OUTPUT:
   - Restituisci ESCLUSIVAMENTE l'intero documento Markdown aggiornato, senza alcun testo conversazionale attorno.
"""

EXAM_QUESTIONS_PROMPT = """Analizza il seguente compendio di studio universitario ed elabora 3 o 4 DOMANDE TIPICHE D'ESAME (orale o scritto) ad alto rigore didattico.
Per ogni domanda:
1. Formula una domanda stimolante, complessa e pertinente (che richieda ragionamento, definizioni esatte o dimostrazioni).
2. Fornisci la risposta modello completa ed esaustiva, racchiusa in un blocco a comparsa compatibile HTML/Markdown:
<details>
<summary>👉 Mostra Risposta Modello e Criteri di Valutazione</summary>

[Spiegazione completa, formule LaTeX pertinenti e punti chiave che il docente pretende all'esame]
</details>

Restituisci solo ed esclusivamente il blocco delle domande formattate.
"""

def extract_topic_suggestions_from_lessons(lessons_list: list) -> list[str]:
    """
    Estrae suggerimenti di argomenti a COSTO ZERO (0 chiamate LLM).
    Legge il campo 'topics' già salvato nella tabella Lezioni di Notion.
    """
    if not lessons_list:
        return []
    
    raw_topics = []
    for l in lessons_list:
        t_text = l.get("topics") or ""
        if not t_text:
            title = l.get("title", "")
            if "-" in title:
                parts = title.split("-", 1)
                t_text = parts[1].strip()
        
        if t_text:
            chunks = re.split(r'[,;\n•\-\|]|\be\b', t_text)
            for c in chunks:
                cleaned = c.strip(" .()[]:\"'")
                if len(cleaned) >= 4 and not cleaned.isdigit():
                    raw_topics.append(cleaned.capitalize())

    unique = []
    seen = set()
    for t in raw_topics:
        low = t.lower()
        if low not in seen and len(t) < 60:
            seen.add(low)
            unique.append(t)

    return unique[:15]

def generate_exhaustive_topic(topic_name: str, lesson_notes_list: list, course_name: str = "", model_name: str = "gemini-3.5-flash-lite", api_key: str = None) -> tuple[bool, str]:
    """
    Genera un capitolo di studio tematico completo, esaustivo e integrato in 1 SOLA chiamata LLM.
    `lesson_notes_list` è una lista di dict: [{"title": "Lezione X", "date": "...", "notes": "..."}]
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non configurata."

    if not lesson_notes_list:
        return False, "Nessun appunto fornito per le lezioni selezionate."

    sources_summary = []
    notes_payload = []
    for idx, item in enumerate(lesson_notes_list, 1):
        lbl = item.get("title") or f"Lezione {idx}"
        d_str = item.get("date") or "Data non specificata"
        sources_summary.append(f"{lbl} ({d_str})")
        
        notes_content = (item.get("notes") or "").strip()
        if not notes_content:
            notes_content = "[Nessun appunto registrato per questa lezione]"
        
        notes_payload.append(f"=== FONTE {idx}: {lbl} (Data: {d_str}) ===\n{notes_content}")

    sources_str = ", ".join(sources_summary)
    full_payload = "\n\n" + ("=" * 50) + "\n\n".join(notes_payload)

    user_instructions = f"""ARGOMENTO DA SVILUPPARE: {topic_name}
CORSO / MATERIA: {course_name or 'Corso Universitario'}
FONTI DA UTILIZZARE: {sources_str}

Ecco il testo integrale degli appunti delle lezioni da cui estrarre e unificare le informazioni:
{full_payload}

IMPORTANTE: Ricorda le regole di MASSIMA ESAUSTIVITÀ e RIGORE SCIENTIFICO. Riporta tutte le definizioni, passaggi algebrici, formule LaTeX ($ e $$) e immagini originali. Non omettere nessun dettaglio didattico.
"""

    try:
        client = genai.Client(api_key=api_key)
        gemini_rate_tracker.log_request()
        
        response = client.models.generate_content(
            model=model_name,
            contents=[
                {"role": "user", "parts": [{"text": EXHAUSTIVE_TOPIC_SYSTEM_PROMPT + "\n\n" + user_instructions}]}
            ]
        )
        
        raw_text = response.text or ""
        cleaned = notion_helper.sanitize_latex_formulas(raw_text)
        cleaned = notion_helper.normalize_images_to_markdown(cleaned)
        return True, cleaned
    except Exception as e:
        return False, f"Errore durante la generazione dell'argomento con {model_name}: {str(e)}"

def smart_merge_topic(existing_markdown: str, new_lesson_item: dict, course_name: str = "", model_name: str = "gemini-3.5-flash-lite", api_key: str = None) -> tuple[bool, str]:
    """
    Fonde ed espande organicamente un capitolo esistente con gli appunti di una nuova lezione in 1 SOLA chiamata LLM.
    `new_lesson_item`: {"title": "...", "date": "...", "notes": "..."}
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non configurata."

    if not existing_markdown or not existing_markdown.strip():
        return False, "Testo del capitolo esistente mancante."

    new_title = new_lesson_item.get("title") or "Nuova Lezione"
    new_date = new_lesson_item.get("date") or "Data odierna"
    new_notes = (new_lesson_item.get("notes") or "").strip()

    if not new_notes:
        return False, "La nuova lezione selezionata non contiene appunti da integrare."

    user_instructions = f"""CORSO: {course_name or 'Corso Universitario'}
NUOVA LEZIONE DA INTEGRARE: {new_title} (Data: {new_date})

--- CAPITOLO ESISTENTE DA AGGIORNARE ---
{existing_markdown}

--- APPUNTI DELLA NUOVA LEZIONE DA INTEGRARE ---
{new_notes}

Istruzione: Esegui la fusione organica (Smart Merge). Preserva tutto il capitolo esistente, inserisci le nuove nozioni al posto logico opportuno e aggiorna le fonti. Non tagliare nulla.
"""

    try:
        client = genai.Client(api_key=api_key)
        gemini_rate_tracker.log_request()
        
        response = client.models.generate_content(
            model=model_name,
            contents=[
                {"role": "user", "parts": [{"text": SMART_MERGE_TOPIC_SYSTEM_PROMPT + "\n\n" + user_instructions}]}
            ]
        )
        
        raw_text = response.text or ""
        cleaned = notion_helper.sanitize_latex_formulas(raw_text)
        cleaned = notion_helper.normalize_images_to_markdown(cleaned)
        return True, cleaned
    except Exception as e:
        return False, f"Errore durante l'integrazione incrementale con {model_name}: {str(e)}"

def generate_exam_questions(markdown_text: str, model_name: str = "gemini-3.5-flash-lite", api_key: str = None) -> tuple[bool, str]:
    """
    Genera 3-4 quesiti d'esame simulati con risposta verificabile on-demand (1 sola chiamata LLM).
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return False, "Chiave API di Google non configurata."

    if not markdown_text or not markdown_text.strip():
        return False, "Nessun testo fornito per la simulazione."

    try:
        client = genai.Client(api_key=api_key)
        gemini_rate_tracker.log_request()
        
        response = client.models.generate_content(
            model=model_name,
            contents=[
                {"role": "user", "parts": [{"text": f"{EXAM_QUESTIONS_PROMPT}\n\nCONTENUTO DEL COMPENDIO:\n{markdown_text}"}]}
            ]
        )
        return True, response.text or ""
    except Exception as e:
        return False, f"Errore simulazione domande: {str(e)}"
