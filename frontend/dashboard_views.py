from datetime import date

import streamlit as st

from ai_views import ai_logging
from components import hero, nice, progress_card, trend_chart
from logging_views import day_records, manual_meal, supplement_logging, water_logging, weight_logging
from validation import complete_history_days, history_chart_rows, local_now


def day_overview(data):
    totals = data.get("totals") or {}
    targets = data.get("targets") or {}
    cols = st.columns(3)
    for column, label, field, target_field, unit in zip(
        cols, ("Energy", "Protein", "Hydration"), ("calories", "protein", "water_ml"),
        ("calories_target", "protein_target", "water_target_ml"), ("kcal", "g", "ml"),
    ):
        with column:
            progress_card(label, totals.get(field, 0), targets.get(target_field), unit)
    st.caption("Confirmed entries only. The dark tick marks your target; the scale expands when you go above it.")
    if not targets:
        st.info("No confirmed targets apply to this date. Set an effective date in Settings → Targets to compare your intake.")
    elif targets.get("effective_date"):
        st.caption(f"User-confirmed targets · effective from {targets['effective_date']}. Review assumptions or edit targets in Settings.")
    columns = st.columns(4)
    columns[0].metric("Latest weight", nice(data.get("weight_kg"), " kg", 1))
    weight_date = data.get("weight_date")
    columns[0].caption(f"As of {weight_date}" if weight_date else "No dated measurement")
    columns[1].metric("BMI", nice(data.get("bmi"), decimals=1))
    columns[1].caption("General screening measure, not a diagnosis")
    columns[2].metric("Meals logged", len(data.get("meals") or []))
    supplements = [item for item in data.get("supplements", []) if item.get("active", True)]
    taken = sum(item.get("status") == "taken" for item in supplements)
    columns[3].metric("Supplements taken", f"{taken} / {len(supplements)}" if supplements else "—")
    columns[3].caption("Taken / expected daily doses")
    st.caption(
        f"More nutrition: {nice(totals.get('carbs', 0))} g carbs · "
        f"{nice(totals.get('fat', 0))} g fat · {nice(totals.get('fiber', 0))} g fiber"
    )


def dashboard_page(api, profile):
    hero("Small steps. A clearer picture.", "Keep track of meals, hydration and movement toward your goals — one day at a time.", "Your daily dashboard")
    left, right = st.columns([3, 1])
    day = left.date_input("Your day", value=local_now(profile).date(), key="dashboard_day")
    right.caption(f"Local timezone\n\n{profile.get('timezone', 'Asia/Kolkata')}")
    if right.button("↻ Refresh day", use_container_width=True):
        st.rerun()
    data = api.get("/dashboard", params={"date": day.isoformat()})
    st.subheader(day.strftime("%A, %d %B"))
    day_overview(data)
    st.divider()
    st.subheader("＋ Add a log")
    st.caption("Manual logs save directly. AI logs are always drafts until you review and confirm.")
    tabs = st.tabs(["🍽️ Manual food", "💧 Water", "⚖️ Weight", "💊 Supplement", "✨ AI Log"])
    with tabs[0]:
        manual_meal(api, profile, day)
    with tabs[1]:
        water_logging(api, profile, day)
    with tabs[2]:
        weight_logging(api, profile, day)
    with tabs[3]:
        supplement_logging(api, data, day, "quick")
    with tabs[4]:
        ai_logging(api, profile, day)
    st.divider()
    st.subheader("Your confirmed day")
    st.caption("Review your records, correct a meal, or remove an accidental entry.")
    day_records(api, data, profile, day, "dashboard")


def history_page(api, profile):
    hero("See the bigger picture", "Patterns, not perfection. Explore your confirmed tracking over time.", "History & trends")
    a, b = st.columns([1, 2])
    period = a.radio("View", ["Day", "Week", "Month"], horizontal=True, key="history_period")
    day = b.date_input("Date in period", value=local_now(profile).date(), key="history_date")
    data = api.get(f"/history/{period.lower()}", params={"date": day.isoformat()})
    if period == "Day":
        day_overview(data)
        st.subheader("Confirmed records")
        day_records(api, data, profile, day, "history")
        return
    summary = data.get("summary") or {}
    start, end = data["start"], data["end"]
    days = complete_history_days(data.get("days") or [], start, end)
    rows = history_chart_rows(days)
    elapsed = summary.get("eligible_calendar_days", sum(item["date"] <= local_now(profile).date().isoformat() for item in days))
    calorie_days = summary.get("calorie_days_logged", sum(bool(item.get("meals")) for item in days if item["date"] <= local_now(profile).date().isoformat()))
    water_days = summary.get("water_days_logged", sum(bool(item.get("water_logs")) for item in days if item["date"] <= local_now(profile).date().isoformat()))
    st.subheader(f"{date.fromisoformat(start):%d %b} – {date.fromisoformat(end):%d %b %Y}")
    columns = st.columns(4)
    columns[0].metric("Starting weight", nice(summary.get("starting_weight_kg"), " kg", 1))
    columns[1].metric("Latest weight", nice(summary.get("latest_weight_kg"), " kg", 1))
    change = summary.get("weight_change_kg")
    columns[2].metric("Weight change", f"{float(change):+.1f} kg" if change is not None else "—")
    columns[3].metric("Days tracked", f"{summary.get('days_tracked', 0)} / {elapsed}")
    st.caption("Weight change is descriptive, not a score. Baseline/latest summary weights may include earlier measurements; the chart shows actual dated measurements only.")
    columns = st.columns(4)
    columns[0].metric("Average calories", nice(summary.get("average_calories"), " kcal"))
    columns[1].metric("Average protein", nice(summary.get("average_protein"), " g", 1))
    columns[2].metric("Average water", nice(summary.get("average_water_ml"), " ml"))
    columns[3].metric("Meals logged", summary.get("meals_logged", 0))
    columns = st.columns(3)
    columns[0].metric("Supplement adherence", nice(summary.get("supplement_adherence"), "%", 1))
    columns[1].metric("Tracking consistency", nice(summary.get("tracking_consistency_percent"), "%", 1))
    columns[2].metric("Weight goal progress", nice(summary.get("weight_goal_progress_percent"), "%", 1))
    st.info(
        f"Coverage: {summary.get('days_tracked', 0)} of {elapsed} elapsed calendar days have tracking "
        f"({len(days)} calendar days in the full period). "
        f"Calories and protein averages use {calorie_days} days with meal logs; water averages use {water_days} days with water logs. "
        "Unlogged days are excluded from those averages, and future days are excluded from summary metrics. "
        "Charts retain zero-log days; zero logs do not prove zero intake. Blank weight points mean no measurement; "
        "they are not filled with previous weights. Supplement adherence is taken doses / expected active daily doses; unlogged doses do not count as taken."
    )
    if summary.get("supplement_data_coverage_percent") is not None:
        st.caption(
            f"Supplement status coverage: {nice(summary['supplement_data_coverage_percent'], '%', 1)} · "
            f"{summary.get('supplement_logged_days', 0)} logged / {summary.get('supplement_eligible_days', 0)} expected daily doses."
        )
    if not summary.get("days_tracked"):
        st.info("There are no confirmed logs in this period yet. Start with one meal, a glass of water, or a weight check-in.")
    a, b = st.columns(2)
    with a:
        trend_chart(rows, "weight_kg", "Weight measurements", "kg")
        trend_chart(rows, "protein", "Daily protein", "g", "#247cb4", bar=True)
        trend_chart(rows, "adherence", "Supplement adherence", "%", "#677ac9", bar=True)
    with b:
        trend_chart(rows, "calories", "Daily calories", "kcal", "#1d9592", bar=True)
        trend_chart(rows, "water_ml", "Daily water", "ml", "#4799cb", bar=True)
    with st.expander("Daily values & coverage"):
        st.dataframe(
            rows, hide_index=True, use_container_width=True,
            column_config={"date": "Date", "weight_kg": "Measured weight (kg)", "calories": "Calories (kcal)", "protein": "Protein (g)", "water_ml": "Water (ml)", "adherence": "Supplements (%)"},
        )
        st.caption("Choose Day above to view, correct or delete the underlying records.")
