"""Run from the project root: python -m streamlit run frontend\\app.py."""

import sys
from pathlib import Path

# Streamlit executes this file as a script, including when launched from the root.
FRONTEND = Path(__file__).resolve().parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

import streamlit as st

from ai_views import coach_page
from api_client import ApiClient, ApiError
from components import hero, show_flash, style
from dashboard_views import dashboard_page, history_page
from settings_views import profile_form, settings_page, target_form
from validation import local_now


def clear_session(message=None):
    for key in list(st.session_state):
        del st.session_state[key]
    if message:
        st.session_state["_auth_notice"] = message


def authenticate():
    hero("Feel good about your everyday.", "A private space for your nutrition, hydration and fitness habits. Make small, informed changes that last.")
    a, b, c = st.columns(3)
    a.metric("Track", "Your day")
    b.metric("Reflect", "Real trends")
    c.metric("Review", "AI, carefully")
    st.write("")
    if notice := st.session_state.pop("_auth_notice", None):
        st.info(notice)
    st.subheader("Welcome to Nourish")
    tabs = st.tabs(["Sign in", "Create account"])
    for tab, register in zip(tabs, (False, True)):
        with tab:
            label = "Create account" if register else "Sign in"
            with st.form("register" if register else "login", clear_on_submit=True):
                email = st.text_input("Email address", placeholder="you@example.com", autocomplete="email")
                password = st.text_input("Password", type="password", autocomplete="new-password" if register else "current-password")
                confirm = st.text_input("Confirm password", type="password") if register else None
                if register:
                    st.caption("Use a strong, unique password of at least 8 characters. Your next step is profile and target setup.")
                submit = st.form_submit_button(label, type="primary", use_container_width=True)
            if submit:
                if "@" not in email or not password:
                    st.warning("Enter a valid email address and password.")
                elif register and (password != confirm or len(password) < 8):
                    st.warning("Passwords must match and contain at least 8 characters.")
                else:
                    client = ApiClient()
                    result = client.post("/auth/register" if register else "/auth/login", json={"email": email.strip(), "password": password})
                    clear_session()
                    st.session_state["access_token"] = result["access_token"]
                    st.rerun()
    st.divider()
    st.caption("Your manual tracking does not use AI. Optional AI tools disclose external processing and require explicit confirmation before saving any draft.")


def signed_in():
    # Preserve draft widgets across navigation without a shared cache. Streamlit
    # otherwise deletes widget state when the page containing it is not rendered.
    for key in list(st.session_state):
        keep_draft = key.startswith("ui_draft_") and not key.endswith("_items")
        keep_input = key.startswith("ai_input_") and not key.startswith("ai_input_image_")
        keep_coach = key in {"coach_start", "coach_end", "coach_question", "coach_provider", "coach_consent"}
        if keep_draft or keep_input or keep_coach:
            st.session_state[key] = st.session_state[key]
    api = ApiClient(st.session_state["access_token"])
    user = api.get("/auth/me")
    with st.sidebar:
        st.markdown("## 🌿 Nourish")
        st.caption("Nutrition & fitness, thoughtfully tracked.")
        st.divider()
        nav = st.radio("Your workspace", ["Dashboard", "History", "AI Coach", "Settings"], key="navigation")
        st.divider()
        st.caption("Signed in as")
        st.write(user.get("email", "Your account"))
        if st.button("Sign out", use_container_width=True):
            clear_session("You have signed out. Your session data has been cleared.")
            st.rerun()
        st.caption("Wellbeing information only.\n\nNot medical advice.")
    if nav == "Dashboard" and st.session_state.get("_last_navigation", "Dashboard") != "Dashboard":
        snapshots = st.session_state.get("ai_draft_snapshots", {})
        for entry in st.session_state.get("ai_draft_events", []):
            if entry["key"] in snapshots:
                entry["event"] = snapshots[entry["key"]]
    st.session_state["_last_navigation"] = nav
    show_flash()
    profile = api.get("/profile", missing_ok=True)
    if profile is None:
        hero("Let's start with you", "Two short steps: your profile, then daily targets you explicitly choose.", "Setup · Step 1 of 2")
        profile_form(api, onboarding=True)
        return
    today_targets = api.get("/targets", params={"date": local_now(profile).date().isoformat()})
    if today_targets is None:
        hero("Set a helpful direction", "Review a recommendation or enter your own targets. Nothing is applied without your confirmation.", "Setup · Step 2 of 2")
        target_form(api, profile, onboarding=True)
        with st.expander("Need to correct your profile?"):
            profile_form(api, profile)
        return
    routes = {"Dashboard": dashboard_page, "History": history_page, "AI Coach": coach_page, "Settings": settings_page}
    routes[nav](api, profile)


def main():
    st.set_page_config(page_title="Nourish · Nutrition & Fitness", page_icon="🌿", layout="wide", initial_sidebar_state="auto")
    style()
    try:
        if st.session_state.get("access_token"):
            signed_in()
        else:
            authenticate()
    except ApiError as exc:
        if exc.status == 401 and st.session_state.get("access_token"):
            clear_session("Your session expired. Please sign in again.")
            st.rerun()
        st.error(str(exc))
        st.caption("Your unsubmitted input is retained. If a save timed out, check your records before retrying.")
    except ValueError as exc:
        st.error(str(exc) or "Please check your input and try again.")


if __name__ == "__main__":
    main()
