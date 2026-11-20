import html
import hashlib
from io import BytesIO

import streamlit as st
from PIL import Image, UnidentifiedImageError

from api_client import ApiError
from validation import NUTRIENTS, local_now, local_timestamp, parse_timestamp


def style():
    st.markdown("""
    <style>
    :root { --mint:#0f9488; --ocean:#2375b7; }
    .stApp { background:linear-gradient(145deg,#f3faf9 0%,#f7f9fd 60%,#edf5fc 100%); color:#163a47; }
    .block-container { max-width:1160px; padding-top:2rem; padding-bottom:3rem; }
    [data-testid="stSidebar"] { background:#eaf4f5; border-right:1px solid #d4e8e8; }
    [data-testid="stSidebar"] .block-container { padding-top:1.5rem; }
    h1,h2,h3 { letter-spacing:-.035em; color:#113d4b; }
    h1 { font-weight:800 !important; }
    [data-testid="stMetric"] { background:#fff; padding:1rem 1.2rem; border:1px solid #d9e9ec;
        border-radius:16px; box-shadow:0 4px 16px #14394305; }
    [data-testid="stMetricValue"] { color:#137e80; }
    [data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px; }
    .stButton button, .stFormSubmitButton button { border-radius:10px; min-height:2.7rem; }
    .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {
        background:#087f82; border:1px solid #087f82; color:white; }
    .stButton button:hover { border-color:#087f82; color:#087f82; }
    .stTabs [data-baseweb="tab-list"] { gap:8px; flex-wrap:wrap; }
    .stTabs [data-baseweb="tab"] { border-radius:8px; padding:8px 14px; }
    .stTabs [aria-selected="true"] { color:#087f82; background:#e1f2f1; }
    .eyebrow { color:#087f82; text-transform:uppercase; font-size:.74rem; letter-spacing:.16em; font-weight:750; }
    .hero { padding:1.5rem 1.7rem; border:1px solid #c9e7e7; border-radius:20px;
        background:linear-gradient(110deg,#e0f4ef,#e9f2ff); margin-bottom:1.2rem; }
    .hero h1 { margin:.25rem 0 .4rem; font-size:2.15rem; }
    .hero p { margin:0; color:#44616a; }
    .target-card { border:1px solid #d9e9ec; border-radius:16px; padding:1.05rem; background:white; min-height:164px; }
    .target-label { color:#4f6b74; font-size:.85rem; font-weight:600; }
    .target-value { font-size:1.75rem; font-weight:750; color:#143f4d; margin:4px 0; }
    .target-note { font-size:.78rem; color:#4d6873; margin-top:8px; }
    .track { height:12px; border-radius:10px; background:#e8f1f4; position:relative; margin:10px 0 3px; }
    .fill { height:12px; border-radius:10px; background:linear-gradient(90deg,#0d9488,#3b82c4); }
    .marker { position:absolute; width:2px; height:20px; top:-4px; background:#163a47; }
    .small-pill { display:inline-block; background:#e4f3ef; color:#13736f; padding:3px 9px; border-radius:30px; font-size:.75rem; }
    [data-testid="stCaptionContainer"] { color:#536e78; }
    @media (max-width:640px) {
      .block-container { padding:1rem .8rem 3rem; }
      .hero { padding:1.1rem; } .hero h1 { font-size:1.75rem; }
      [data-testid="stHorizontalBlock"] { flex-wrap:wrap; gap:.6rem; }
      [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width:min(100%,230px); flex:1 1 230px; }
      .target-card { min-height:140px; }
    }
    </style>
    """, unsafe_allow_html=True)


def hero(title, subtitle, eyebrow="Nourish • Personal nutrition & fitness"):
    st.markdown(
        f'<section class="hero"><div class="eyebrow">{html.escape(eyebrow)}</div>'
        f'<h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></section>',
        unsafe_allow_html=True,
    )


def flash(message):
    st.session_state["_flash"] = message
    st.rerun()


def show_flash():
    if message := st.session_state.pop("_flash", None):
        st.success(message)


def nice(value, suffix="", decimals=0):
    if value is None:
        return "—"
    return f"{float(value):,.{decimals}f}{suffix}"


def progress_card(label, actual, target, unit):
    actual = float(actual or 0)
    target = float(target or 0)
    scale = max(actual, target, 1) * 1.10
    fill = 100 * actual / scale
    marker = f'<div class="marker" style="left:{100 * target / scale:.2f}%"></div>' if target else ""
    percent = actual / target * 100 if target else None
    note = f"{percent:,.0f}% of target · target marker at {nice(target)} {unit}" if target else "No confirmed target for this day"
    if target and actual > target:
        note += f" · {nice(actual - target)} {unit} above"
    st.markdown(
        f'<div class="target-card"><div class="target-label">{html.escape(label)}</div>'
        f'<div class="target-value">{nice(actual)} <span style="font-size:.9rem">{html.escape(unit)}</span></div>'
        f'<div class="track"><div class="fill" style="width:{fill:.2f}%"></div>{marker}</div>'
        f'<div class="target-note">{html.escape(note)}</div></div>',
        unsafe_allow_html=True,
    )


def item_editor(items, key):
    rows = [{column: item.get(column) for column in ("food_name", "quantity", "unit", *NUTRIENTS)} for item in items]
    if not rows:
        rows = [{"food_name": "", "quantity": 1.0, "unit": "serving", **{name: None for name in NUTRIENTS}}]
    st.caption("Nutrition is the TOTAL for each entered portion — not per 100 g. Add/remove rows using the table controls.")
    return st.data_editor(
        rows, key=key, num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "food_name": st.column_config.TextColumn("Food / drink", required=True, width="medium"),
            "quantity": st.column_config.NumberColumn("Quantity", min_value=0.01, step=0.1, required=True),
            "unit": st.column_config.TextColumn("Unit", required=True),
            "calories": st.column_config.NumberColumn("Calories (kcal)", min_value=0.0, step=1.0, required=True),
            **{name: st.column_config.NumberColumn(f"{name.title()} (g)", min_value=0.0, step=0.1, required=True) for name in NUTRIENTS if name != "calories"},
        },
    )


def timestamp_inputs(profile, day, key, value=None):
    current = parse_timestamp(value, profile, day) if value else local_now(profile)
    selection = f"{day.isoformat()}:{profile.get('timezone', 'Asia/Kolkata')}"
    if value is None and st.session_state.get(f"_{key}_selected_day") != selection:
        st.session_state[f"{key}_date"] = day
        st.session_state[f"{key}_time"] = current.time().replace(tzinfo=None, second=0, microsecond=0)
        st.session_state[f"_{key}_selected_day"] = selection
    left, right = st.columns(2)
    picked_day = left.date_input("Date", value=current.date() if value else day, key=f"{key}_date")
    picked_time = right.time_input("Local time", value=current.time().replace(tzinfo=None, second=0, microsecond=0), key=f"{key}_time")
    zone = profile.get("timezone", "Asia/Kolkata")
    st.caption(f"Timezone: {zone}. During a repeated daylight-saving hour, the first occurrence is used.")
    return local_timestamp(picked_day, picked_time, zone)


def display_image(api, image_id, key):
    if image_id and st.checkbox("Show attached meal photo", key=key):
        try:
            content = api.get(f"/images/{int(image_id)}", binary=True)
            with Image.open(BytesIO(content)) as image:
                image.thumbnail((1100, 900))
                st.image(image.copy(), caption="Your private meal photo", use_container_width=True)
        except ApiError as exc:
            if exc.status == 401:
                raise
            st.warning(str(exc))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            st.warning("This meal image could not be displayed.")


def upload_once(api, image, slot):
    if image is None:
        return None
    fingerprint = hashlib.sha256(image.getvalue()).hexdigest()
    saved = st.session_state.get(f"uploaded_{slot}", {})
    if saved.get("fingerprint") == fingerprint:
        return saved["id"]
    image_id = api.upload(image)
    st.session_state[f"uploaded_{slot}"] = {"fingerprint": fingerprint, "id": image_id}
    return image_id


def delete_button(api, endpoint, key, label="Delete record"):
    confirmed = st.checkbox("I want to permanently delete this record", key=f"{key}_check")
    if st.button(label, key=f"{key}_delete", disabled=not confirmed):
        api.delete(endpoint)
        flash("Record deleted.")


def provider_select(key):
    return st.selectbox(
        "AI provider", ["auto", "nvidia", "groq"], key=key,
        format_func=lambda value: {"auto": "Automatic · NVIDIA with Groq fallback", "nvidia": "NVIDIA", "groq": "Groq"}[value],
    )


def ai_disclosure():
    st.info(
        "AI privacy: your selected text/images or coaching question and tracked facts are sent "
        "to an external model provider (NVIDIA or Groq). Use AI only if you are comfortable sharing "
        "these inputs. Manual logging never uses an AI model."
    )


def trend_chart(rows, field, title, unit, color="#0f9488", *, bar=False):
    st.markdown(f"**{title}**")
    if not any(row.get(field) is not None for row in rows):
        st.caption(f"No {title.lower()} observations in this period.")
        return
    plotted = rows
    detail = {}
    if not bar:
        plotted = []
        segment = 0
        for row in rows:
            if row.get(field) is None:
                segment += 1
            else:
                plotted.append({**row, "_segment": segment})
        detail = {"detail": {"field": "_segment", "type": "nominal"}}
    st.vega_lite_chart(
        spec={
            "data": {"values": plotted},
            "mark": {"type": "bar" if bar else "line", "color": color, **({} if bar else {"point": True})},
            "encoding": {
                **detail,
                "x": {"field": "date", "type": "temporal", "title": None, "axis": {"format": "%d %b", "labelAngle": -35}},
                "y": {"field": field, "type": "quantitative", "title": unit, "scale": {"zero": field != "weight_kg"}},
                "tooltip": [{"field": "date", "type": "temporal", "title": "Date"}, {"field": field, "type": "quantitative", "title": unit, "format": ".1f"}],
            },
            "height": 230,
            "config": {"view": {"stroke": None}, "axis": {"gridColor": "#e3ecef", "labelColor": "#45626c", "titleColor": "#45626c"}},
        },
        use_container_width=True,
    )
