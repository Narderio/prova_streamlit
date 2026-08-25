# Piano di Implementazione: Integrazione Immagini con Ctrl+V e Drag & Drop (Home & Canvas)

Questo documento descrive l'architettura, le strategie di ottimizzazione e i passaggi tecnici per consentire all'utente di inserire immagini negli appunti **esclusivamente tramite `Ctrl+V` (paste da clipboard) e `Drag & Drop` (trascinamento file)**, sia dalla **Home Page** che dallo **Studio Canvas**, con inserimento posizionale esatto, esportazione nativa su Notion e preservazione assoluta dei tag da parte dell'LLM (Gemini).

---

## 🎯 Obiettivi Principali

1. **Inserimento Immediato (Zero-Button / Zero-Clutter):** Nessun pulsante o uploader ingombrante. Inserimento tramite gesture native:
   - **`Ctrl+V`**: incolla istantaneamente screenshot o immagini copiate negli appunti.
   - **`Drag & Drop`**: trascina file immagine dal desktop direttamente sull'area desiderata degli appunti.
2. **Inserimento Posizionale Esatto:**
   - In modalità Editor (`st.text_area`), l'immagine viene inserita esattamente alla **posizione del cursore o alle coordinate di drop**.
   - In modalità Anteprima Formattata, l'immagine viene inserita **esattamente tra i paragrafi o blocchi in cui è stata rilasciata**.
3. **Preservazione Assoluta delle Immagini da parte dell'LLM (Gemini):**
   - L'LLM riceve solo la sintassi testuale standard `![alt](URL_SUPABASE)` (consumo 0 token visivi).
   - Istruzione tassativa nel prompt di sistema dell'Agente Canvas: quando l'AI modifica, sintetizza, espande o formatta il Canvas, **ha l'obbligo assoluto di mantenere intatti i tag immagine e la loro posizione contestuale originale**, senza mai eliminarli o alterarli.
4. **Esportazione Nativa Notion:** Il parser `notion_helper.py` riconosce `![alt](URL)` e crea automaticamente blocchi `image` nativi su Notion via API.
5. **Caricamento Istantaneo (0 ms latency):** Strategia di caching HTTP, compressione WebP client-side e Lazy Loading per evitare qualsiasi rallentamento dell'interfaccia.

---

## 🚀 Strategia Prestazioni & Caching

1. **Optimistic UI (0 ms):** All'incolla (`Ctrl+V`) o trascinamento, l'immagine viene mostrata **istantaneamente** tramite Blob locale mentre l'upload su Supabase avviene in background.
2. **Compressione WebP Client-Side:** Le immagini vengono compresse dell'80%-90% (es. 5 MB -> 200 KB) in JavaScript via Canvas prima dell'upload.
3. **Browser Cache & CDN:** Le immagini caricate su Supabase Storage utilizzano l'header HTTP `Cache-Control: public, max-age=31536000, immutable` per caricamenti a 0 ms ai ricaricamenti.
4. **Lazy Loading (`loading="lazy"`):** Il testo Markdown e le equazioni LaTeX si caricano per primi senza alcuna attesa. Le immagini si caricano in background solo quando visibili a schermo.
5. **Skeleton Loaders:** Placeholder animati sfumati che prevengono i salti improvvisi di layout (Cumulative Layout Shift = 0).

---

## 🏗️ Architettura dei Componenti

### 1. Storage Supabase (`supabase_client.py`)
- Bucket pubblico: `canvas-images`
- Funzione `ensure_canvas_images_bucket()`
- Funzione `upload_canvas_image(file_bytes, filename, content_type)`
- Funzione `upload_canvas_image_base64(base64_data, filename)`

### 2. Preservazione Immagini nell'Agente AI (`backend.py`)
- Regola tassativa in `CANVAS_AGENT_PROMPT` per obbligare Gemini a preservare sempre i tag immagine `![...](URL)` nella loro posizione contestuale originale durante le modifiche al Canvas sotto `<<<UPDATED_CANVAS>>>`.

### 3. Parser Notion (`notion_helper.py`)
- **Markdown -> Notion:** Estensione di `markdown_to_notion_blocks` per convertire `![alt](url)` in blocchi di tipo `image` (`{"type": "image", "image": {"type": "external", "external": {"url": img_url}}}`).
- **Notion -> Markdown:** Estensione di `get_notion_page_markdown` per convertire i blocchi `image` di Notion in `![Immagine](url)`.

### 4. Gestione Eventi Ctrl+V e Drag & Drop Posizionale (`app.py`)
- Script JavaScript unificato con listener per `paste` e `drop` con calcolo delle coordinate / offset del cursore.
- Target: sia l'editor della Home (`markdown_editor_area`) che quello del Canvas (`markdown_editor_area_canvas`).
- Inserimento del tag Markdown `![alt](url)` nel punto esatto e sincronizzazione con lo stato Streamlit.

---

## 📋 Piano dei Test & Verifica

- [ ] Verificare che `upload_canvas_image` generi URL pubblici validi da Supabase Storage.
- [ ] Verificare che `markdown_to_notion_blocks` converta correttamente i tag `![alt](url)` in blocchi `image` Notion nativi.
- [ ] Verificare che `get_notion_page_markdown` legga i blocchi immagine da Notion e li riconverta in Markdown.
- [ ] Verificare l'inserimento posizionale con `Ctrl+V` e `Drag & Drop` nella Home Page.
- [ ] Verificare l'inserimento posizionale con `Ctrl+V` e `Drag & Drop` nello Studio Canvas.
- [ ] Verificare che l'Agente AI mantenga intatti i link alle immagini nella medesima posizione durante le risposte e le modifiche del Canvas.
