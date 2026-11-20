import copy
import uuid
from datetime import timedelta

import streamlit as st

from components import ai_disclosure, flash, hero, item_editor, provider_select, timestamp_inputs, upload_once
from validation import EVENT_TYPES, MEAL_TYPES, clean_events, local_now


def set_draft(draft):
    for key in list(st.session_state):
        if key.startswith("ui_draft_"):
            del st.session_state[key]
    st.session_state["ai_draft"] = copy.deepcopy(draft)
    st.session_state["ai_draft_events"] = [
        {"key": uuid.uuid4().hex[:10], "event": copy.deepcopy(event)} for event in draft.get("events", [])
    ]
    st.session_state["ai_draft_reviewed"] = []
    st.session_state["ai_draft_snapshots"] = {}


def clear_draft():
    for key in list(st.session_state):
        if key.startswith("ui_draft_") or key in {"ai_draft", "ai_draft_events", "ai_draft_reviewed", "ai_draft_snapshots", "prior_drafts"}:
            del st.session_state[key]


def draft_review(api, profile, day):
    draft = st.session_state.get("ai_draft")
    if not draft:
        return
    st.divider()
    st.subheader("Review before anything is saved")
    st.warning(
        "AI nutrition values are estimates, not laboratory measurements. Verify food, portion, "
        "ALL nutrition totals, time and every other event. Blank or uncertain fields must be "
        "corrected. This draft does not count toward your dashboard."
    )
    st.caption(f"Draft #{draft['id']} · {draft.get('provider') or 'provider unavailable'} · {draft.get('model') or 'model unavailable'}")
    for warning in draft.get("warnings") or []:
        st.warning(str(warning))
    reviewed = []
    for index, entry in enumerate(st.session_state.get("ai_draft_events", []), 1):
        original = entry["event"]
        prefix = f"ui_draft_{draft['id']}_{entry['key']}"
        with st.container(border=True):
            a, b = st.columns([3, 1])
            a.markdown(f"#### Event {index} · {str(original.get('type') or 'needs classification').title()}")
            remove = b.checkbox("Remove event", key=f"{prefix}_remove")
            if remove:
                st.caption("Excluded from confirmation. Uncheck to restore.")
                continue
            kind = st.selectbox(
                "Event type", EVENT_TYPES, index=EVENT_TYPES.index(original["type"]) if original.get("type") in EVENT_TYPES else None,
                key=f"{prefix}_type", format_func=str.title, placeholder="Choose an event type",
            )
            occurred = st.text_input(
                "Occurred at · ISO date/time with timezone",
                value=original.get("occurred_at") or "",
                key=f"{prefix}_occurred", placeholder="2026-09-08T12:30:00+05:30",
                help=f"Include the offset for {profile.get('timezone')}. Editing the time changes which day receives this log.",
            )
            if not occurred:
                st.caption("The model did not supply a time. Enter the time and timezone explicitly.")
            event = {"type": kind, "occurred_at": occurred}
            if kind == "meal":
                event["meal_type"] = st.selectbox(
                    "Meal type", MEAL_TYPES,
                    index=MEAL_TYPES.index(original["meal_type"]) if original.get("meal_type") in MEAL_TYPES else None,
                    key=f"{prefix}_meal_type", format_func=str.title, placeholder="Choose the meal type",
                )
                event["items"] = item_editor(original.get("items") or [], f"{prefix}_items")
                confidence = [f"{item.get('food_name') or 'Unnamed food'}: {item.get('confidence', 'not supplied')}" for item in original.get("items") or []]
                if confidence:
                    st.caption("Model confidence (not a guarantee): " + " · ".join(confidence))
                if draft.get("image_id") is not None:
                    st.caption("The draft's private uploaded photo will accompany the meal. You can remove or replace it by editing the confirmed meal.")
            elif kind == "water":
                value = original.get("amount_ml")
                event["amount_ml"] = st.number_input("Water (ml)", min_value=1, value=int(value) if value is not None else None, step=50, key=f"{prefix}_water", placeholder="Enter the amount")
            elif kind == "weight":
                value = original.get("weight_kg")
                event["weight_kg"] = st.number_input("Weight (kg)", min_value=0.1, value=float(value) if value is not None else None, step=0.1, key=f"{prefix}_weight", placeholder="Enter the measurement")
            elif kind == "supplement":
                event["name"] = st.text_input("Supplement name", value=original.get("name") or "", key=f"{prefix}_name")
                status = st.selectbox(
                    "Daily status", ["taken", "not_taken"],
                    index=0 if original.get("taken") is True else 1 if original.get("taken") is False else None,
                    key=f"{prefix}_status", format_func=lambda val: "Taken" if val == "taken" else "Not taken", placeholder="Choose the actual status",
                )
                event["taken"] = None if status is None else status == "taken"
                st.caption("Use the name of a configured supplement. Set its daily dose in Settings.")
            reviewed.append(event)
            st.session_state["ai_draft_snapshots"][entry["key"]] = copy.deepcopy(event)
    st.session_state["ai_draft_reviewed"] = copy.deepcopy(reviewed)
    with st.expander("Inspect the exact edited events"):
        st.json(reviewed)
    acknowledged = st.checkbox(
        "I reviewed every remaining event and corrected all estimates. Save these as confirmed records.",
        key=f"ui_draft_{draft['id']}_ack",
    )
    a, b = st.columns(2)
    if a.button("Confirm & save reviewed events", type="primary", disabled=not acknowledged, use_container_width=True, key="confirm_ai_draft"):
        cleaned = clean_events(reviewed)
        api.post("/ai-log/confirm", json={"draft_id": draft["id"], "events": cleaned})
        clear_draft()
        st.session_state.pop("uploaded_ai", None)
        st.session_state["ai_input_revision"] = st.session_state.get("ai_input_revision", 0) + 1
        flash("Reviewed events saved. Your dashboard now includes these confirmed records.")
    discard = b.checkbox("Discard this draft without saving", key=f"ui_draft_{draft['id']}_discard")
    if b.button("Discard draft", disabled=not discard, use_container_width=True, key="discard_ai_draft"):
        api.post(f"/ai-log/{draft['id']}/discard")
        clear_draft()
        flash("Draft discarded. No tracking records were saved.")


def prior_drafts(api):
    with st.expander("Resume an earlier AI draft"):
        st.caption("Only draft-status records can be resumed. Confirmed or discarded drafts are not saved again.")
        if st.button("Refresh saved draft list", key="list_ai_drafts"):
            st.session_state["prior_drafts"] = api.get("/ai-log") or []
        records = st.session_state.get("prior_drafts", [])
        drafts = [record for record in records if record.get("status") == "draft"]
        if "prior_drafts" in st.session_state and not drafts:
            st.info("No pending drafts.")
        if drafts:
            by_id = {record["id"]: record for record in drafts}
            selected = st.selectbox(
                "Pending draft", list(by_id), key="resume_draft_id",
                format_func=lambda value: f"#{value} · {by_id[value].get('created_at', '')} · {str(by_id[value].get('text') or 'Image or mixed input')[:65]}",
            )
            if st.session_state.get("ai_draft"):
                st.caption("Confirm or discard your current draft before resuming another.")
            if st.button("Resume selected draft", disabled=bool(st.session_state.get("ai_draft"))):
                draft = api.get(f"/ai-log/{selected}")
                if draft.get("status") != "draft":
                    st.warning("This draft is no longer pending. Refresh the list.")
                else:
                    set_draft(draft)
                    st.rerun()


def ai_logging(api, profile, day):
    st.markdown("#### Describe it. Review it. Then save it.")
    ai_disclosure()
    revision = st.session_state.get("ai_input_revision", 0)
    text = st.text_area(
        "What would you like to log?",
        placeholder="I ate rice and dal for lunch, drank 500 ml water, and took my vitamin D.",
        key=f"ai_input_text_{revision}", height=110,
    )
    image = st.file_uploader("Optional meal image", type=["jpg", "jpeg", "png", "webp"], key=f"ai_input_image_{revision}")
    st.caption(
        "Text, image, or both. Images upload only when you click Generate draft; selecting a file does not send it. "
        "AI receives a resized copy; your original photo stays unchanged in private storage."
    )
    occurred = timestamp_inputs(profile, day, f"ai_input_{revision}")
    provider = provider_select("ai_log_provider")
    consent = st.checkbox("I agree to send these selected inputs to the external AI provider.", key="ai_log_consent")
    active = bool(st.session_state.get("ai_draft"))
    if active:
        st.info("You have a draft awaiting review below. Confirm or discard it before generating another.")
    if st.button("Generate draft · nothing saved yet", type="primary", disabled=not consent or active, use_container_width=True):
        if not text.strip() and image is None:
            st.warning("Describe an event, choose a meal photo, or provide both.")
        else:
            with st.spinner("Interpreting your input… this can take up to three minutes."):
                image_id = upload_once(api, image, "ai")
                draft = api.post("/ai-log", ai=True, json={"text": text.strip(), "image_id": image_id, "occurred_at": occurred, "provider": provider})
            if draft.get("status") != "draft":
                st.error("The service did not return a reviewable draft. Refresh the draft list before retrying.")
            else:
                draft.setdefault("image_id", image_id)
                set_draft(draft)
                st.rerun()
    draft_review(api, profile, day)
    prior_drafts(api)


def coach_page(api, profile):
    hero("A thoughtful check-in", "Ask about your real tracked data. Facts first, guidance second.", "AI Coach")
    ai_disclosure()
    today = local_now(profile).date()
    a, b = st.columns(2)
    start = a.date_input("From", value=today - timedelta(days=6), key="coach_start")
    end = b.date_input("Through", value=today, key="coach_end")
    st.caption("Choose up to 93 calendar days, inclusive. Unlogged days are missing tracking, not proof of zero intake.")
    st.markdown("**Try a question**")
    questions = [
        "How did I do this week, and which days have incomplete tracking?",
        "How consistent was my protein and water intake?",
        "What does my recorded weight trend show?",
    ]
    for index, question in enumerate(questions):
        if st.button(question, key=f"coach_example_{index}", use_container_width=True):
            st.session_state["coach_question"] = question
    question = st.text_area("Your question", key="coach_question", placeholder="Ask about your tracked meals, hydration, weight or consistency.", height=110)
    provider = provider_select("coach_provider")
    consent = st.checkbox("I agree to send my question and relevant tracked facts to the external AI provider.", key="coach_consent")
    if st.button("Ask AI Coach", type="primary", disabled=not consent, use_container_width=True):
        if not question.strip():
            st.warning("Enter a question first.")
        elif start > end or (end - start).days + 1 > 93:
            st.warning("Choose a valid date range of no more than 93 days, inclusive.")
        else:
            with st.spinner("Checking your confirmed data and preparing a response…"):
                result = api.post("/coach", ai=True, json={"question": question.strip(), "start_date": start.isoformat(), "end_date": end.isoformat(), "provider": provider})
            st.session_state["coach_response"] = {"result": result, "question": question, "start": start.isoformat(), "end": end.isoformat()}
    saved = st.session_state.get("coach_response")
    if saved:
        st.divider()
        st.caption(f"Response for {saved['start']} through {saved['end']} · {saved['question']}")
        st.subheader("Verified tracking facts")
        st.caption("Deterministic backend calculations from confirmed records, separate from the model's interpretation.")
        st.json(saved["result"].get("facts", {}), expanded=True)
        st.subheader("Coach's perspective")
        st.markdown(saved["result"].get("answer") or "The provider did not return a narrative. Your tracking facts are shown above.")
        st.caption(f"Provider: {saved['result'].get('provider', '—')} · Model: {saved['result'].get('model', '—')}")
    else:
        st.info("Your first check-in starts with a question above. No example or mock answers are shown here.")
    st.caption("AI can be wrong. General wellbeing information only — not diagnosis, treatment or a substitute for professional advice.")
