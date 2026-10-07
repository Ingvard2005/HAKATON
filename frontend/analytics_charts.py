"""Donuts use disjoint record groups, with the same records available below."""
import streamlit as st
from html import escape


def donut_data(groups):
    total = sum(len(records) for _, records, _ in groups)
    return [dict(category=label, count=len(records), share=len(records) / total,
        color=color) for label, records, color in groups if records] if total else []


def donut(groups, title, definition, key, render_records):
    rows = donut_data(groups)
    st.html('<style>div[class*="st-key-chart_"]{background:#fff;border-radius:14px;padding:18px!important;border:1px solid #e2e6ec!important}div[class*="st-key-chart_"] summary{min-height:44px}</style>')
    with st.container(border=True, key=f"chart_{key}"):
        st.markdown(f"**{title}**")
        st.caption(definition)
        if not rows:
            st.info("Недостаточно данных для диаграммы")
            return
        chart, records = st.columns([1, 2])
        with chart:
            st.vega_lite_chart(spec={
                "data": {"values": rows},
                "height": 220,
                "mark": {"type": "arc", "innerRadius": 62, "outerRadius": 92, "stroke": "white", "strokeWidth": 2},
                "encoding": {
                    "theta": {"field": "count", "type": "quantitative", "stack": True},
                    "color": {"field": "category", "type": "nominal", "legend": None,
                        "scale": {"domain": [g[0] for g in groups], "range": [g[2] for g in groups]}},
                    "tooltip": [{"field": "category", "type": "nominal", "title": "Категория"},
                        {"field": "count", "type": "quantitative", "title": "Записей", "format": "d"},
                        {"field": "share", "type": "quantitative", "title": "Доля", "format": ".1%"}],
                },
                "view": {"stroke": None},
                "config": {"background": "transparent"},
            }, width="stretch", theme=None, key=f"donut_{key}")
        with records:
            total = sum(r["count"] for r in rows)
            st.caption(f"Всего в расчёте: {total}")
            for index, (label, items, color) in enumerate(groups):
                share = len(items) / total
                st.html(f'<div style="color:#182230;font-size:14px"><span aria-hidden="true" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};margin-right:8px"></span>{escape(label)} · {len(items)} ({share:.1%})</div>')
                with st.expander(f"Показать записи · {label}"):
                    render_records(items, f"chart_{key}_{index}")
