from datetime import date
from zoneinfo import available_timezones

import streamlit as st

from api_client import ApiError
from components import flash, hero, nice
from validation import local_now, timezone_for


ASSUMPTIONS = (
    "Automatic recommendations are estimates for adults using the Mifflin–St Jeor equation "
    "and a sedentary activity factor of 1.2. This is a maintenance estimate, with no calorie "
    "deficit automatically applied. Protein and water are general weight-based estimates, "
    "not medical prescriptions. For minors or unspecified sex, use clinician-informed manual targets."
)


def profile_form(api, profile=None, onboarding=False):
    profile = profile or {}
    prefix = "onboard" if onboarding else "profile"
    st.subheader("Your starting point" if onboarding else "Personal profile")
    st.caption("Your height, weight, age and timezone help put confirmed logs into context.")
    with st.form(f"{prefix}_form"):
        left, right = st.columns(2)
        dob = left.date_input(
            "Date of birth", value=date.fromisoformat(profile["date_of_birth"]) if profile.get("date_of_birth") else date(2000, 1, 1),
            min_value=date(1900, 1, 1), max_value=date.today(), key=f"{prefix}_dob",
        )
        choices = ["unspecified", "male", "female"]
        sex = right.selectbox("Sex used for recommendation", choices, index=choices.index(profile.get("sex", "unspecified")), key=f"{prefix}_sex", format_func=str.title)
        st.caption("You can choose Unspecified and set targets manually.")
        left, right = st.columns(2)
        height = left.number_input("Height (cm)", min_value=50.0, max_value=275.0, value=float(profile.get("height_cm") or 170), step=0.5)
        weight = right.number_input("Current profile weight (kg)", min_value=0.01, max_value=650.0, value=float(profile.get("current_weight_kg") or 65), step=0.1)
        goal = st.number_input(
            "Goal weight (kg) · optional", min_value=0.01, max_value=650.0,
            value=float(profile["goal_weight_kg"]) if profile.get("goal_weight_kg") is not None else None,
            step=0.1, placeholder="Leave blank if you do not have a weight goal",
        )
        zones = sorted(available_timezones())
        current_zone = profile.get("timezone") or "Asia/Kolkata"
        zone = st.selectbox("Timezone", zones, index=zones.index(current_zone) if current_zone in zones else zones.index("Asia/Kolkata"))
        st.caption("Daily grouping uses your timezone. Profile weight is a baseline; use Weight logging for dated measurements.")
        save = st.form_submit_button("Save profile & continue" if onboarding else "Save profile", type="primary", use_container_width=True)
    if save:
        timezone_for(zone)
        api.put("/profile", json={
            "date_of_birth": dob.isoformat(), "sex": sex, "height_cm": height,
            "current_weight_kg": weight, "goal_weight_kg": goal, "timezone": zone,
        })
        st.session_state.pop("target_recommendation", None)
        flash("Profile saved. Next, confirm your daily targets." if onboarding else "Profile updated.")


def target_form(api, profile, onboarding=False):
    st.subheader("Confirm your daily targets" if onboarding else "Daily targets")
    today = local_now(profile).date()
    effective = today if onboarding else st.date_input("Targets effective from", value=today, key="target_effective")
    existing = api.get("/targets", params={"date": effective.isoformat()})
    if st.session_state.get("_target_form_date") != effective.isoformat():
        for name in ("calories_target", "protein_target", "water_target_ml"):
            st.session_state.pop(f"target_input_{name}", None)
        st.session_state.pop("target_confirm_assumptions", None)
        st.session_state.pop("target_recommendation", None)
        st.session_state["_target_form_date"] = effective.isoformat()
    st.info(ASSUMPTIONS)
    st.caption("Nothing here changes your target until you explicitly save. New effective dates preserve earlier target history.")
    if st.button("Calculate a recommendation", key="recommend_targets"):
        try:
            recommendation = api.get("/targets/recommend")
            st.session_state["target_recommendation"] = recommendation
            for name in ("calories_target", "protein_target", "water_target_ml"):
                st.session_state[f"target_input_{name}"] = float(recommendation[name])
            st.session_state["target_confirm_assumptions"] = False
        except ApiError as exc:
            if exc.status in {400, 422}:
                st.session_state.pop("target_recommendation", None)
                st.warning(f"Automatic recommendation is unavailable: {exc} Use manual targets below.")
            else:
                raise
    recommendation = st.session_state.get("target_recommendation")
    if recommendation:
        st.success("Recommendation prepared — review, adjust and confirm below.")
        st.write(recommendation.get("explanation", ASSUMPTIONS))
    if existing:
        st.caption(
            f"Saved for this date: {nice(existing.get('calories_target'))} kcal · "
            f"{nice(existing.get('protein_target'))} g protein · {nice(existing.get('water_target_ml'))} ml water."
        )
    source = recommendation or existing or {}
    with st.form("daily_targets"):
        cols = st.columns(3)
        calories = cols[0].number_input(
            "Calories (kcal/day)", min_value=1.0, max_value=20000.0, value=float(source["calories_target"]) if source.get("calories_target") else None,
            step=50.0, key="target_input_calories_target", placeholder="Enter a daily target",
        )
        protein = cols[1].number_input(
            "Protein (g/day)", min_value=1.0, max_value=1000.0, value=float(source["protein_target"]) if source.get("protein_target") else None,
            step=5.0, key="target_input_protein_target", placeholder="Enter a daily target",
        )
        water = cols[2].number_input(
            "Water (ml/day)", min_value=1.0, max_value=20000.0, value=float(source["water_target_ml"]) if source.get("water_target_ml") else None,
            step=100.0, key="target_input_water_target_ml", placeholder="Enter a daily target",
        )
        confirmed = st.checkbox(
            "I reviewed these targets and the assumptions above. I choose these amounts for my daily tracking.",
            key="target_confirm_assumptions",
        )
        submitted = st.form_submit_button("Confirm & save targets", type="primary", use_container_width=True)
    if submitted:
        if not confirmed:
            st.warning("Please confirm that you reviewed your target amounts and assumptions.")
        elif any(value is None for value in (calories, protein, water)):
            st.warning("Enter all three targets, or calculate a recommendation first.")
        elif not water.is_integer():
            st.warning("Water target must be a whole number of millilitres.")
        else:
            api.put("/targets", json={
                "effective_date": effective.isoformat(), "calories_target": calories,
                "protein_target": protein, "water_target_ml": int(water),
            })
            st.session_state.pop("target_recommendation", None)
            flash("Daily targets confirmed and saved.")


def supplement_settings(api):
    st.subheader("Supplement routine")
    st.caption("Each active supplement expects one daily dose. Put any timing instructions in the name. This is a tracking routine, not a dosage recommendation.")
    supplements = api.get("/supplements") or []
    if not supplements:
        st.info("No supplements configured. Add one if supplements are part of your routine.")
    for supplement in supplements:
        sid = supplement["id"]
        label = f"{supplement['name']} · {supplement['dosage']} {supplement['unit']}"
        with st.expander(label + ("" if supplement.get("active", True) else " · inactive")):
            with st.form(f"edit_supplement_{sid}"):
                name = st.text_input("Name", value=supplement["name"], key=f"supp_name_{sid}")
                a, b = st.columns(2)
                dosage = a.number_input("Daily dose", min_value=0.01, value=float(supplement["dosage"]), step=0.5, key=f"supp_dose_{sid}")
                unit = b.text_input("Unit", value=supplement["unit"], key=f"supp_unit_{sid}")
                active = st.checkbox("Active daily routine", value=supplement.get("active", True), key=f"supp_active_{sid}")
                if st.form_submit_button("Save supplement"):
                    if not name.strip() or not unit.strip():
                        st.warning("Name and dose unit are required.")
                    else:
                        api.put(f"/supplements/{sid}", json={"name": name.strip(), "dosage": dosage, "unit": unit.strip(), "active": active})
                        flash("Supplement routine updated.")
    with st.expander("＋ Add a supplement", expanded=not supplements):
        with st.form("add_supplement"):
            name = st.text_input("Supplement name", placeholder="e.g. Vitamin D · with breakfast")
            a, b = st.columns(2)
            dosage = a.number_input("Daily dose", min_value=0.01, value=1.0, step=0.5)
            unit = b.text_input("Dose unit", value="tablet", placeholder="tablet, capsule, mg…")
            if st.form_submit_button("Add to routine", type="primary"):
                if not name.strip() or not unit.strip():
                    st.warning("Enter a supplement name and unit.")
                else:
                    api.post("/supplements", json={"name": name.strip(), "dosage": dosage, "unit": unit.strip()})
                    flash("Supplement added to your daily routine.")


def settings_page(api, profile):
    hero("Make it yours", "Keep your profile, daily targets and supplement routine up to date.", "Settings")
    tabs = st.tabs(["Profile", "Targets", "Supplements", "Privacy & account"])
    with tabs[0]:
        profile_form(api, profile)
    with tabs[1]:
        target_form(api, profile)
    with tabs[2]:
        supplement_settings(api)
    with tabs[3]:
        st.subheader("Your data, clearly separated")
        st.write("Manual entries and confirmed AI drafts make up your tracking history. An AI draft never counts until you review and confirm it.")
        st.write("Images are private and retrieved through your authenticated account. Your login token is held only in this browser session, not in shared caches.")
        st.write("AI requests send selected inputs and relevant tracked facts to an external provider. Credentials for those providers stay on the backend.")
        st.caption("For shared computers, always sign out when finished. The sidebar sign-out button clears this session, including unsaved drafts and photos.")
        st.warning("Nutrition targets, BMI and AI coaching are general information, not medical advice. Consult a qualified clinician for individual needs.")
