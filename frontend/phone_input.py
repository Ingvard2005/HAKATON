"""Local, bidirectional phone input with a mask applied on every keystroke."""
from pathlib import Path
import streamlit as st

ASSETS = Path(__file__).parent / "phone_input_assets"
masked_phone = st.components.v2.component(
    "callmind_masked_phone",
    html='<label for="phone">Телефон</label><input id="phone" type="tel" inputmode="tel" autocomplete="tel" aria-describedby="phone-help phone-error" placeholder="+375 29 123-45-67"><div id="phone-help">Формат: +XXX XX XXX-XX-XX · код страны и 9 цифр номера</div><div id="phone-error" role="status" aria-live="polite"></div>',
    css=(ASSETS / "style.css").read_text(encoding="utf-8"),
    js=(ASSETS / "mask.js").read_text(encoding="utf-8"),
    isolate_styles=False,
)


def phone_input():
    saved = st.session_state.get("call_phone_mask", {})
    initial = saved.get("phone", {"value": "", "error": ""})
    result = masked_phone(data=initial, default={"phone": initial},
        key="call_phone_mask", on_phone_change=lambda: None, height="content")
    payload = result.phone or initial
    return payload.get("value", ""), payload.get("error", "")
