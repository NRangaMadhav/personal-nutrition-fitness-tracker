import streamlit as st

from components import delete_button, display_image, flash, item_editor, nice, timestamp_inputs, upload_once
from validation import MEAL_TYPES, NUTRIENTS, clean_items, local_now, local_timestamp, parse_timestamp


def manual_meal(api, profile, day):
    st.markdown("#### Build a meal")
    st.caption("Enter label or recipe values for the portion you actually ate. Unknown values need your correction before saving.")
    revision = st.session_state.get("manual_meal_revision", 0)
    with st.form(f"manual_meal_{revision}"):
        kind = st.selectbox("Meal type", MEAL_TYPES, format_func=str.title)
        occurred = timestamp_inputs(profile, day, f"manual_meal_{revision}")
        items = item_editor([], f"manual_items_{revision}")
        photo = st.file_uploader("Optional meal photo", type=["jpg", "jpeg", "png", "webp"], key=f"manual_photo_{revision}")
        st.caption("The photo is uploaded privately only when you click Save meal. No AI is used.")
        save = st.form_submit_button("Save meal", type="primary", use_container_width=True)
    if save:
        cleaned = clean_items(items)
        image_id = upload_once(api, photo, "manual")
        api.post("/meals", json={"meal_type": kind, "logged_at": occurred, "image_id": image_id, "items": cleaned})
        st.session_state["manual_meal_revision"] = revision + 1
        st.session_state.pop("uploaded_manual", None)
        flash("Meal saved to your confirmed history.")


def water_logging(api, profile, day):
    st.markdown("#### A little hydration goes a long way")
    st.caption(f"Quick additions use {day:%d %b %Y} and the current local time in {profile.get('timezone')}.")
    columns = st.columns(3)
    for column, amount in zip(columns, (250, 500, 750)):
        if column.button(f"＋ {amount} ml", key=f"quick_water_{amount}", use_container_width=True):
            occurred = local_timestamp(day, local_now(profile).time(), profile.get("timezone"))
            api.post("/water", json={"amount_ml": amount, "logged_at": occurred})
            flash(f"{amount} ml water added.")
    with st.form("custom_water"):
        amount = st.number_input("Custom amount (ml)", min_value=1, max_value=10000, value=250, step=50)
        occurred = timestamp_inputs(profile, day, "water")
        if st.form_submit_button("Save water", type="primary"):
            api.post("/water", json={"amount_ml": amount, "logged_at": occurred})
            flash(f"{amount} ml water added.")


def weight_logging(api, profile, day):
    st.markdown("#### Check in with your weight")
    st.caption("Daily fluctuations are normal. Track a trend rather than judge a single measurement.")
    with st.form("new_weight"):
        weight = st.number_input("Weight (kg)", min_value=0.01, max_value=650.0, value=float(profile.get("current_weight_kg") or 65), step=0.1)
        occurred = timestamp_inputs(profile, day, "weight")
        if st.form_submit_button("Save weight", type="primary"):
            api.post("/weight", json={"weight_kg": weight, "recorded_at": occurred})
            flash("Weight measurement saved.")


def supplement_logging(api, dashboard, day, key="dashboard"):
    supplements = dashboard.get("supplements") or []
    if not supplements:
        st.info("Your supplement routine is empty for this day. Configure a name and daily dose in Settings → Supplements.")
        return
    labels = {None: "○ Unlogged", "taken": "✓ Taken", "not_taken": "− Not taken"}
    for supplement in supplements:
        sid = supplement["id"]
        with st.container(border=True):
            st.markdown(f"**{supplement['name']}**")
            st.caption(f"{supplement['dosage']} {supplement['unit']} · one daily dose · {labels.get(supplement.get('status'), '○ Unlogged')}")
            if not supplement.get("active", True):
                st.caption("Inactive on this day. Existing status is preserved; reset it below if it was logged by mistake.")
            a, b, c = st.columns(3)
            for column, taken, label in ((a, True, "✓ Taken"), (b, False, "Not taken")):
                if column.button(label, key=f"{key}_supp_{sid}_{taken}", use_container_width=True, disabled=not supplement.get("active", True)):
                    api.post("/supplement-logs", json={"supplement_id": sid, "log_date": day.isoformat(), "taken": taken})
                    flash(f"{supplement['name']}: {'taken' if taken else 'not taken'} recorded.")
            log = next((item for item in dashboard.get("supplement_logs", []) if item.get("supplement_id") == sid), None)
            if c.button("Reset to unlogged", key=f"{key}_supp_reset_{sid}", disabled=log is None, use_container_width=True):
                api.delete(f"/supplement-logs/{log['id']}")
                flash("Supplement status reset to unlogged.")


def meal_details(api, meal, profile, day, key, editable=True):
    mid = meal["id"]
    occurred = parse_timestamp(meal.get("logged_at"), profile, day)
    items = meal.get("items") or []
    calories = sum(float(item.get("calories") or 0) for item in items)
    foods = ", ".join(str(item.get("food_name", "")) for item in items)
    with st.expander(f"{str(meal.get('meal_type', 'Meal')).title()} · {occurred:%H:%M} · {nice(calories)} kcal — {foods[:80]}"):
        st.caption(f"Confirmed · {occurred:%d %b %Y, %H:%M %Z}")
        st.dataframe(
            [{field: row.get(field) for field in ("food_name", "quantity", "unit", *NUTRIENTS)} for row in items],
            hide_index=True, use_container_width=True,
            column_config={"food_name": "Food", "quantity": "Quantity", "unit": "Unit", "calories": "Calories (kcal)", **{name: f"{name.title()} (g)" for name in NUTRIENTS if name != "calories"}},
        )
        display_image(api, meal.get("image_id"), f"{key}_photo_{mid}")
        if editable and st.checkbox("Edit this meal", key=f"{key}_edit_{mid}"):
            with st.form(f"{key}_edit_form_{mid}"):
                kind = st.selectbox(
                    "Meal type", MEAL_TYPES,
                    index=MEAL_TYPES.index(meal["meal_type"]) if meal.get("meal_type") in MEAL_TYPES else 0,
                    key=f"{key}_kind_{mid}", format_func=str.title,
                )
                timestamp = timestamp_inputs(profile, day, f"{key}_time_{mid}", meal.get("logged_at"))
                edited = item_editor(items, f"{key}_items_{mid}")
                remove_photo = st.checkbox("Remove attached photo", key=f"{key}_remove_photo_{mid}") if meal.get("image_id") else False
                replacement = st.file_uploader("Add or replace photo", type=["jpg", "jpeg", "png", "webp"], key=f"{key}_replace_photo_{mid}")
                if st.form_submit_button("Save meal corrections", type="primary"):
                    cleaned = clean_items(edited)
                    image_id = None if remove_photo else meal.get("image_id")
                    if replacement:
                        image_id = upload_once(api, replacement, f"edit_{mid}")
                    api.put(f"/meals/{mid}", json={"meal_type": kind, "logged_at": timestamp, "image_id": image_id, "items": cleaned})
                    st.session_state.pop(f"{key}_items_{mid}", None)
                    flash("Meal corrections saved.")
        if editable:
            st.divider()
            delete_button(api, f"/meals/{mid}", f"{key}_meal_{mid}", "Delete meal")


def day_records(api, data, profile, day, key, editable=True):
    meals, water, weight, supplement = st.tabs(["Meals", "Water records", "Weight records", "Supplements"])
    with meals:
        if not data.get("meals"):
            st.info("No confirmed meals on this day. Add a manual meal or review an AI draft to get started.")
        for meal in data.get("meals") or []:
            meal_details(api, meal, profile, day, key, editable)
    with water:
        if not data.get("water_logs"):
            st.info("No water logged on this day. Quick water buttons are available on the Dashboard.")
        for record in data.get("water_logs") or []:
            stamp = parse_timestamp(record.get("logged_at"), profile, day)
            with st.expander(f"{nice(record.get('amount_ml'))} ml · {stamp:%H:%M}"):
                st.caption(f"{stamp:%d %b %Y, %H:%M %Z}")
                if editable:
                    delete_button(api, f"/water/{record['id']}", f"{key}_water_{record['id']}", "Delete water entry")
    with weight:
        if not data.get("weight_records"):
            st.info("No new weight measurement on this day. The overview may show the latest earlier measurement with its date.")
        for record in data.get("weight_records") or []:
            stamp = parse_timestamp(record.get("recorded_at"), profile, day)
            with st.expander(f"{nice(record.get('weight_kg'), ' kg', 1)} · {stamp:%H:%M}"):
                st.caption(f"{stamp:%d %b %Y, %H:%M %Z}")
                if editable:
                    delete_button(api, f"/weight/{record['id']}", f"{key}_weight_{record['id']}", "Delete weight entry")
    with supplement:
        if editable:
            supplement_logging(api, data, day, key)
        elif data.get("supplements"):
            st.dataframe(data["supplements"], use_container_width=True, hide_index=True)
        else:
            st.caption("No supplement routine for this day.")
