import streamlit as st
import datetime
import re
import notion_helper
import studio_helper

@st.cache_data(ttl=60, show_spinner=False)
def cached_get_course_topics(course_page_id, course_name, token):
    return notion_helper.get_course_topics(course_page_id, course_name=course_name, api_key=token)

def render_studio_page(notion_corsi_id: str, notion_token: str, google_api_key: str, active_model: str, courses_dict: dict, cached_get_course_lessons_func, cached_get_notion_page_markdown_func):
    """
    Renderizza la pagina 'Studio' (Compendio Tematico e Wiki Accademica).
    Layout 100% Full-Width: gli appunti occupano l'intera larghezza della pagina per uno studio confortevole e privo di distrazioni.
    """
    # Stili CSS per massimizzare la leggibilità a piena larghezza
    st.markdown("""
    <style>
    .studio-top-bar {
        background-color: var(--background-color, #ffffff);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 12px;
        padding: 16px 22px;
        margin-bottom: 20px;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.03);
    }
    .studio-reader-fullwidth {
        background-color: var(--background-color, #ffffff);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 14px;
        padding: 36px 48px;
        margin-top: 14px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.04);
        line-height: 1.75;
        font-size: 1.05rem;
        width: 100% !important;
        box-sizing: border-box;
    }
    .studio-badge {
        display: inline-flex;
        align-items: center;
        padding: 4px 12px;
        border-radius: 16px;
        font-size: 12.5px;
        font-weight: 600;
        background-color: rgba(59, 130, 246, 0.12);
        color: #2563eb;
        margin-right: 8px;
    }
    .studio-connections-box {
        margin-top: 32px;
        padding: 18px 24px;
        border-radius: 10px;
        background-color: rgba(241, 245, 249, 0.6);
        border: 1px dashed rgba(148, 163, 184, 0.5);
    }
    </style>
    """, unsafe_allow_html=True)

    # Verifica corsi disponibili
    if not courses_dict:
        st.warning("⚠️ Nessun corso trovato su Notion o credenziali non configurate. Verifica la pagina radice Corsi nel file .env.")
        return

    course_names = list(courses_dict.keys())
    curr_course = st.session_state.get("selected_course")
    def_course_idx = course_names.index(curr_course) if (curr_course in course_names) else 0

    # 1. BARRA DI CONTROLLO ORIZZONTALE SUPERIORE (FULL WIDTH)
    col_c1, col_c2, col_c3, col_c4 = st.columns([1.5, 2.2, 1.1, 1.2], vertical_alignment="bottom")

    with col_c1:
        selected_course = st.selectbox(
            "📚 Materia / Corso:",
            course_names,
            index=def_course_idx,
            key="studio_course_selector"
        )
    
    st.session_state.selected_course = selected_course
    course_page_id = courses_dict.get(selected_course)

    # Carica dati Notion per il corso
    all_lessons = []
    if course_page_id and notion_token:
        all_lessons = cached_get_course_lessons_func(course_page_id, selected_course, notion_token)

    all_topics = []
    if course_page_id and notion_token:
        all_topics = cached_get_course_topics(course_page_id, selected_course, notion_token)

    # Mappatura argomenti
    active_topic = None
    topic_labels = []
    topic_map = {}
    if all_topics:
        for t in all_topics:
            stat_icon = t.get("status", "🟡")
            lbl = f"{stat_icon} {t.get('title')}"
            topic_labels.append(lbl)
            topic_map[lbl] = t

    with col_c2:
        if topic_labels:
            def_t_idx = 0
            # Se è presente un argomento memorizzato nella sessione per questo corso
            saved_tid = st.session_state.get(f"active_studio_topic_id_{selected_course}")
            if saved_tid:
                for idx_lbl, lbl_name in enumerate(topic_labels):
                    if topic_map[lbl_name]["id"] == saved_tid:
                        def_t_idx = idx_lbl
                        break
            
            chosen_topic_lbl = st.selectbox(
                "📖 Seleziona Capitolo da studiare:",
                topic_labels,
                index=def_t_idx,
                key=f"studio_topic_sel_{selected_course}"
            )
            active_topic = topic_map.get(chosen_topic_lbl)
            if active_topic:
                st.session_state[f"active_studio_topic_id_{selected_course}"] = active_topic["id"]
        else:
            st.selectbox("📖 Capitolo da studiare:", ["Nessun argomento salvato"], disabled=True)

    with col_c3:
        if active_topic:
            curr_stat = active_topic.get("status", "🟡 Da Studiare")
            stat_options = ["🟡 Da Studiare", "🔵 In Ripasso", "🟢 Padroneggiato"]
            stat_idx = stat_options.index(curr_stat) if curr_stat in stat_options else 0
            new_stat = st.selectbox("Stato:", stat_options, index=stat_idx, key=f"stat_sel_top_{active_topic['id']}")
            if new_stat != curr_stat:
                client = notion_helper.get_notion_client(notion_token)
                if client:
                    try:
                        client.pages.update(page_id=notion_helper.format_notion_id(active_topic["id"]), properties={"Stato Studio": {"select": {"name": new_stat}}})
                        cached_get_course_topics.clear()
                        st.toast("✅ Stato aggiornato su Notion!", icon="🎉")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Errore: {e}")
        else:
            st.caption(f"Capitoli creati: **{len(all_topics)}**")

    with col_c4:
        if active_topic:
            st.markdown(f"""
            <a href="{active_topic.get('url')}" target="_blank" style="
                display: inline-flex; align-items: center; justify-content: center; width: 100%;
                padding: 7px 12px; border-radius: 8px; margin-bottom: 2px;
                background-color: #f8fafc; color: #0f172a; font-weight: 500; font-size: 13px;
                border: 1px solid rgba(0,0,0,0.12); text-decoration: none;
            ">
                🔗 Apri su Notion
            </a>
            """, unsafe_allow_html=True)
        else:
            st.caption(f"Lezioni disponibili: **{len(all_lessons)}**")

    st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

    # 2. SEZIONE COLLASSABILE PER LE AZIONI (Nuovo argomento, Merge, Domande d'esame)
    # Mostra l'expander "Crea Nuovo" aperto solo se non ci sono argomenti ancora creati
    exp_create_open = (active_topic is None)
    
    with st.expander("✨ **Crea Nuovo Argomento** (Estrai da una o più lezioni con isolamento laser)", expanded=exp_create_open):
        st.markdown("##### 📝 Genera un Capitolo Focalizzato Esclusivamente sul Tema Richiesto")
        st.caption("L'AI estrarrà **SOLO ed ESCLUSIVAMENTE** ciò che riguarda l'argomento richiesto, scartando categoricamente altri temi spiegati nella stessa lezione.")

        if not all_lessons:
            st.warning("Nessuna lezione trovata su Notion per questo corso.")
        else:
            lesson_labels_all = [f"📖 {l['title']} ({l.get('date') or ''})" for l in all_lessons]
            lesson_dict_lookup = {f"📖 {l['title']} ({l.get('date') or ''})": l for l in all_lessons}

            col_chk, _ = st.columns([1, 2])
            with col_chk:
                select_all_btn = st.checkbox("Seleziona tutte le lezioni del corso", value=False, key="studio_chk_sel_all")

            def_selected = lesson_labels_all if select_all_btn else []
            selected_lessons_keys = st.multiselect(
                "Seleziona lezioni sorgente da cui estrarre:",
                lesson_labels_all,
                default=def_selected,
                key="studio_multisel_lessons"
            )

            selected_lessons_data = [lesson_dict_lookup[k] for k in selected_lessons_keys if k in lesson_dict_lookup]

            # Suggerimenti a costo ZERO (0 token)
            if selected_lessons_data:
                suggested_topics = studio_helper.extract_topic_suggestions_from_lessons(selected_lessons_data)
                if suggested_topics:
                    st.caption("💡 **Argomenti rilevati nelle lezioni scelte (clicca per selezionare):**")
                    cols_sug = st.columns(min(4, len(suggested_topics)))
                    for s_idx, s_topic in enumerate(suggested_topics[:8]):
                        col_target = cols_sug[s_idx % len(cols_sug)]
                        if col_target.button(s_topic, key=f"btn_sug_topic_{s_idx}", use_container_width=True):
                            st.session_state.studio_custom_topic_name = s_topic

            col_tname, col_btn_gen = st.columns([2.5, 1.2], vertical_alignment="bottom")
            with col_tname:
                topic_name_val = st.session_state.get("studio_custom_topic_name", "")
                topic_title_input = st.text_input(
                    "Nome preciso dell'argomento desiderato:",
                    value=topic_name_val,
                    placeholder="Es. Principio di Conservazione dell'Energia",
                    key="studio_input_topic_name_widget"
                )
            with col_btn_gen:
                btn_create = st.button("🚀 Genera Capitolo ed Esporta", type="primary", use_container_width=True)

            if btn_create:
                if not selected_lessons_data:
                    st.error("Seleziona almeno una lezione sorgente.")
                elif not topic_title_input or not topic_title_input.strip():
                    st.error("Inserisci il titolo dell'argomento desiderato.")
                else:
                    clean_topic_title = topic_title_input.strip()
                    with st.spinner(f"Estrazione selettiva e generazione esaustiva di '{clean_topic_title}' (Zero Omissioni)..."):
                        notes_payload = []
                        lesson_page_ids = []
                        for item in selected_lessons_data:
                            lid = item.get("id")
                            lesson_page_ids.append(lid)
                            l_notes = cached_get_notion_page_markdown_func(lid, token=notion_token)
                            if l_notes and l_notes.strip():
                                notes_payload.append({
                                    "title": item.get("title"),
                                    "date": item.get("date"),
                                    "notes": l_notes
                                })

                        if not notes_payload:
                            st.error("Nessuna delle lezioni selezionate contiene appunti registrati su Notion.")
                        else:
                            ok_gen, gen_md = studio_helper.generate_exhaustive_topic(
                                topic_name=clean_topic_title,
                                lesson_notes_list=notes_payload,
                                course_name=selected_course,
                                model_name=active_model,
                                api_key=google_api_key
                            )
                            if not ok_gen:
                                st.error(f"Errore generazione: {gen_md}")
                            else:
                                ok_save, msg_save, new_topic_id = notion_helper.save_topic_to_notion(
                                    course_name=selected_course,
                                    course_page_id=course_page_id,
                                    topic_title=clean_topic_title,
                                    markdown_text=gen_md,
                                    lesson_page_ids=lesson_page_ids,
                                    api_key=notion_token
                                )
                                if ok_save:
                                    cached_get_course_topics.clear()
                                    st.session_state[f"active_studio_topic_id_{selected_course}"] = new_topic_id
                                    st.success(f"🎉 Capitolo '{clean_topic_title}' creato e salvato su Notion!")
                                    st.toast("Capitolo salvato con successo!", icon="✅")
                                    st.rerun()
                                else:
                                    st.error(f"Errore salvataggio Notion: {msg_save}")

    # Se un argomento è aperto, mostriamo gli expander di integrazione e domande d'esame
    if active_topic:
        col_exp1, col_exp2 = st.columns(2)
        with col_exp1:
            with st.expander("🔄 **Integra Nuova Lezione a questo Capitolo** (Smart Merge)", expanded=False):
                st.caption("Aggiungi nozioni da un'altra lezione senza perdere ciò che è già presente e senza introdurre temi slegati.")
                lesson_map = {f"📖 {l['title']} ({l.get('date') or ''})": l for l in all_lessons}
                if lesson_map:
                    chosen_merge_label = st.selectbox("Scegli lezione da integrare:", list(lesson_map.keys()), key=f"merge_sel_{active_topic['id']}")
                    chosen_lesson = lesson_map[chosen_merge_label]
                    
                    if st.button("🚀 Espandi ed Integra Capitolo", key=f"btn_merge_{active_topic['id']}", type="secondary", use_container_width=True):
                        with st.spinner("Fusione e integrazione in corso..."):
                            existing_md = cached_get_notion_page_markdown_func(active_topic["id"], token=notion_token)
                            new_lesson_md = cached_get_notion_page_markdown_func(chosen_lesson["id"], token=notion_token)
                            
                            if not new_lesson_md:
                                st.error("La lezione scelta non contiene appunti su Notion.")
                            else:
                                new_item = {"title": chosen_lesson.get("title"), "date": chosen_lesson.get("date"), "notes": new_lesson_md}
                                ok_m, merged_md = studio_helper.smart_merge_topic(
                                    existing_markdown=existing_md,
                                    new_lesson_item=new_item,
                                    course_name=selected_course,
                                    model_name=active_model,
                                    api_key=google_api_key
                                )
                                if ok_m:
                                    rel_lessons = list(active_topic.get("lessons_related") or [])
                                    if chosen_lesson["id"] not in rel_lessons:
                                        rel_lessons.append(chosen_lesson["id"])
                                    ok_s, msg_s, _ = notion_helper.save_topic_to_notion(
                                        course_name=selected_course,
                                        course_page_id=course_page_id,
                                        topic_title=active_topic["title"],
                                        markdown_text=merged_md,
                                        lesson_page_ids=rel_lessons,
                                        topic_page_id=active_topic["id"],
                                        api_key=notion_token
                                    )
                                    if ok_s:
                                        cached_get_course_topics.clear()
                                        cached_get_notion_page_markdown_func.clear()
                                        st.toast("🎉 Capitolo integrato con successo!", icon="✅")
                                        st.rerun()
                                    else:
                                        st.error(msg_s)
                                else:
                                    st.error(merged_md)

        with col_exp2:
            with st.expander("❓ **Simulazione Domande d'Esame** (On-Demand)", expanded=False):
                st.caption("Genera 3 domande d'esame simulate ad alto rigore con risposta modello verificabile.")
                active_tid = active_topic["id"]
                q_key = f"exam_questions_{active_tid}"
                if st.button("Genera 3 Domande su questo Tema", key=f"btn_gen_q_{active_tid}", use_container_width=True):
                    curr_md = cached_get_notion_page_markdown_func(active_tid, token=notion_token)
                    if curr_md:
                        with st.spinner("Elaborazione quesiti d'esame..."):
                            ok_q, res_q = studio_helper.generate_exam_questions(curr_md, model_name=active_model, api_key=google_api_key)
                            if ok_q:
                                st.session_state[q_key] = res_q
                                st.toast("Domande d'esame generate!", icon="🎓")
                    else:
                        st.warning("Impossibile caricare il testo da Notion.")

                if q_key in st.session_state and st.session_state[q_key]:
                    st.markdown(st.session_state[q_key], unsafe_allow_html=True)

    # =========================================================================
    # 3. IL VISUALIZZATORE DEGLI APPUNTI: 100% FULL-WIDTH (A TUTTA PAGINA)
    # =========================================================================
    st.markdown("<div style='margin-bottom: 16px;'></div>", unsafe_allow_html=True)

    if active_topic:
        tid = active_topic.get("id")
        topic_title = active_topic.get("title")
        
        with st.spinner(f"Caricamento integrale di '{topic_title}' da Notion..."):
            topic_markdown = cached_get_notion_page_markdown_func(tid, token=notion_token)

        if not topic_markdown or not topic_markdown.strip():
            st.warning("⚠️ Questo capitolo su Notion non contiene ancora testo. Puoi integrarlo o rigenerarlo dalla barra superiore.")
        else:
            # Intestazione rapida del capitolo
            col_h1, col_h2 = st.columns([3, 1])
            with col_h1:
                st.markdown(f"### 📘 {topic_title}")
            with col_h2:
                st.markdown(f"<div style='text-align: right; padding-top: 8px;'><span class='studio-badge'>{active_topic.get('status', '🟡 Da Studiare')}</span><span style='font-size:12px; color:#64748b;'>Aggiornato: {active_topic.get('updated_at') or 'Recente'}</span></div>", unsafe_allow_html=True)

            # Rendering del Markdown con immagini, LaTeX e Mermaid
            rendered_md = notion_helper.clean_markdown_for_streamlit(topic_markdown, default_width="50%")

            # CARD 100% FULL-WIDTH
            st.markdown("""
            <div class="studio-reader-fullwidth">
            """, unsafe_allow_html=True)
            
            st.markdown(rendered_md, unsafe_allow_html=True)
            
            # --- SEZIONE CONNESSIONI & ALTRI ARGOMENTI (BACKLINKS E COLLEGAMENTI) ---
            other_topics = [t for t in all_topics if t.get("id") != tid]
            if other_topics:
                st.markdown("""
                <div class="studio-connections-box">
                    <h5 style="margin-bottom: 8px; color: #334155;">🔗 Altri Argomenti del Corso (Navigazione Rapida):</h5>
                """, unsafe_allow_html=True)
                
                cols_conn = st.columns(min(4, len(other_topics)))
                for o_idx, o_top in enumerate(other_topics):
                    col_target = cols_conn[o_idx % len(cols_conn)]
                    o_title = o_top.get("title")
                    if col_target.button(f"👉 {o_title}", key=f"btn_jump_topic_{o_idx}", use_container_width=True):
                        st.session_state[f"active_studio_topic_id_{selected_course}"] = o_top["id"]
                        st.rerun()

                st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("""
            </div>
            """, unsafe_allow_html=True)
    else:
        # Schermata di benvenuto a tutto schermo quando nessun argomento è selezionato
        st.markdown("""
        <div style="
            border: 2px dashed rgba(128, 128, 128, 0.25);
            border-radius: 16px;
            padding: 56px 32px;
            text-align: center;
            background-color: rgba(248, 250, 252, 0.5);
            margin-top: 10px;
            width: 100%;
        ">
            <div style="font-size: 52px; margin-bottom: 12px;">📖</div>
            <h2 style="color: #1e293b; margin-bottom: 8px;">Nessun argomento attualmente aperto</h2>
            <p style="color: #64748b; max-width: 600px; margin: 0 auto 24px auto; font-size: 1rem;">
                Seleziona un capitolo esistente dalla barra superiore oppure apri il pannello <b>'✨ Crea Nuovo Argomento'</b> per unificare il materiale di più lezioni.
            </p>
            <div style="display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; margin-top: 24px;">
                <div style="background: white; border: 1px solid rgba(0,0,0,0.08); border-radius: 10px; padding: 16px 20px; font-size: 13.5px; text-align: left; max-width: 280px; box-shadow: 0 2px 8px rgba(0,0,0,0.03);">
                    <b>📐 Lettura a Piena Pagina</b><br>
                    <span style="color: #64748b;">Gli appunti prendono il 100% dello schermo: formule KaTeX grandi e leggibili.</span>
                </div>
                <div style="background: white; border: 1px solid rgba(0,0,0,0.08); border-radius: 10px; padding: 16px 20px; font-size: 13.5px; text-align: left; max-width: 280px; box-shadow: 0 2px 8px rgba(0,0,0,0.03);">
                    <b>🎯 Isolamento Laser</b><br>
                    <span style="color: #64748b;">L'AI include solo il tema richiesto, scartando categoricamente altri argomenti slegati.</span>
                </div>
                <div style="background: white; border: 1px solid rgba(0,0,0,0.08); border-radius: 10px; padding: 16px 20px; font-size: 13.5px; text-align: left; max-width: 280px; box-shadow: 0 2px 8px rgba(0,0,0,0.03);">
                    <b>🔄 Merge Incrementale</b><br>
                    <span style="color: #64748b;">Aggiungi lezioni successive senza toccare o perdere le dimostrazioni già scritte.</span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
