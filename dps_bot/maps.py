"""Генерация карт: интерактивная HTML (folium) и картинка PNG (matplotlib).

Тяжёлая отрисовка выполняется в отдельном потоке (asyncio.to_thread),
чтобы бот не «замирал» для остальных пользователей. Файлы на диск не пишутся.
"""
from __future__ import annotations

import html
import math
from datetime import datetime
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import folium  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from folium.plugins import Fullscreen, HeatMap, LocateControl, MarkerCluster  # noqa: E402

from .gazetteer import Place  # noqa: E402
from .kinds import KINDS, kind  # noqa: E402
from .utils import ago, local, maps_links  # noqa: E402

FA_ICONS = {"police": "car", "radar": "tachometer", "camera": "camera", "raid": "shield",
            "accident": "exclamation-triangle", "hazard": "warning"}


def _e(row) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def render_html(events: list, villages: list[Place], history: list, center: tuple[float, float],
                tz, title: str, user: tuple[float, float] | None = None) -> bytes:
    events = [_e(e) for e in events]
    m = folium.Map(location=list(user or center), zoom_start=12 if user else 11,
                   tiles="OpenStreetMap", control_scale=True)
    Fullscreen(title="На весь экран").add_to(m)
    LocateControl(strings={"title": "Где я?"}).add_to(m)

    vg = folium.FeatureGroup(name="🏘 Населённые пункты", show=True)
    for p in villages:
        folium.CircleMarker([p.lat, p.lon], radius=4, color="#27ae60", fill=True, fill_opacity=0.8,
                            tooltip=p.name).add_to(vg)
    vg.add_to(m)

    for code, k in KINDS.items():
        layer = folium.FeatureGroup(name=f"{k.emoji} {k.title}", show=True)
        cluster = MarkerCluster().add_to(layer)
        n = 0
        for e in events:
            if e["kind"] != code or e["lat"] is None:
                continue
            n += 1
            ya, go = maps_links(e["lat"], e["lon"])
            popup = f"""
            <div style="font-family:Arial;font-size:13px;min-width:220px">
              <b>{k.emoji} {k.title} #{e['id']}</b><br>
              📍 {html.escape(e['place'] or '')}<br>
              🕐 {local(e['created_at'], tz, '%H:%M')} ({ago(e['created_at'])})<br>
              {'🚗 ' + html.escape(e['plate']) + '<br>' if e['plate'] else ''}
              {'➡️ в сторону ' + html.escape(e['direction']) + '<br>' if e['direction'] else ''}
              👍 {e['confirms']} · 👋 {e['gone']} · 🚫 {e['denies']}<br>
              <i>{html.escape((e['text'] or '')[:160])}</i><br>
              <a href="{ya}" target="_blank">Яндекс</a> · <a href="{go}" target="_blank">Google</a>
            </div>"""
            folium.Marker(
                [e["lat"], e["lon"]],
                popup=folium.Popup(popup, max_width=320),
                tooltip=f"{k.title} · {ago(e['created_at'])}",
                icon=folium.Icon(color=k.color, icon=FA_ICONS.get(code, "info"), prefix="fa"),
            ).add_to(cluster)
            if (e["accuracy_m"] or 0) > 150:
                folium.Circle([e["lat"], e["lon"]], radius=e["accuracy_m"], color=k.hex,
                              fill=True, fill_opacity=0.08, weight=1).add_to(layer)
        if n:
            layer.add_to(m)

    if history:
        HeatMap([[h["lat"], h["lon"]] for h in history], name="🔥 Где обычно стоят (30 дней)",
                radius=18, blur=15, min_opacity=0.3, show=False).add_to(m)
    if user:
        folium.Marker(list(user), tooltip="Вы здесь",
                      icon=folium.Icon(color="green", icon="user", prefix="fa")).add_to(m)

    folium.LayerControl(collapsed=True).add_to(m)
    legend = "".join(f"<div>{k.emoji} {k.title}</div>" for k in KINDS.values())
    m.get_root().html.add_child(folium.Element(f"""
    <div style="position:fixed;top:10px;left:50px;z-index:1000;background:rgba(255,255,255,.95);
      padding:8px 12px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.2);font:13px Arial">
      <b>{html.escape(title)}</b><br>
      <span style="color:#666">Событий: {len(events)} · {datetime.now(tz).strftime('%d.%m %H:%M')}</span>
    </div>
    <div style="position:fixed;bottom:25px;left:10px;z-index:1000;background:rgba(255,255,255,.95);
      padding:8px 12px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.2);font:12px Arial">{legend}</div>
    """))
    return m.get_root().render().encode("utf-8")


def render_png(events: list, villages: list[Place], bounds, tz, title: str,
               history: list | None = None, user: tuple[float, float] | None = None) -> bytes:
    events = [_e(e) for e in events]
    (s, w), (n, e_) = bounds
    pts = [(e["lat"], e["lon"]) for e in events if e["lat"] is not None] + [(p.lat, p.lon) for p in villages]
    if pts:  # подрезаем рамку по данным, чтобы не было пустоты
        s, n = min(p[0] for p in pts) - 0.03, max(p[0] for p in pts) + 0.03
        w, e_ = min(p[1] for p in pts) - 0.05, max(p[1] for p in pts) + 0.05

    fig, ax = plt.subplots(figsize=(11, 10), dpi=110)
    ax.set_facecolor("#f4f6f8")
    ax.set_xlim(w, e_)
    ax.set_ylim(s, n)
    ax.set_aspect(1 / math.cos(math.radians((s + n) / 2)))
    ax.grid(True, alpha=0.25, linestyle="--")
    ax.set_title(title, fontsize=16, fontweight="bold")
    ax.tick_params(labelsize=8)

    if history:
        hx = [h["lon"] for h in history]
        hy = [h["lat"] for h in history]
        ax.hexbin(hx, hy, gridsize=40, cmap="YlOrRd", mincnt=1, alpha=0.55, extent=(w, e_, s, n))

    for p in villages:
        ax.plot(p.lon, p.lat, "o", color="#27ae60", markersize=4, alpha=0.8)
        ax.annotate(p.name, (p.lon, p.lat), fontsize=7, ha="center", va="bottom", xytext=(0, 4),
                    textcoords="offset points", color="#2d3436", alpha=0.85)

    handles = {}
    for ev in events:
        if ev["lat"] is None:
            continue
        k = kind(ev["kind"])
        h = ax.scatter(ev["lon"], ev["lat"], s=230, c=k.hex, edgecolors="black", linewidths=1.2, zorder=10)
        handles[k.code] = h
        ax.annotate(f"#{ev['id']} {local(ev['created_at'], tz, '%H:%M')}", (ev["lon"], ev["lat"]),
                    fontsize=8, fontweight="bold", xytext=(8, 6), textcoords="offset points", zorder=11,
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#ccc", alpha=0.9))
    if user:
        handles["user"] = ax.scatter(user[1], user[0], s=160, marker="*", c="#00b894", edgecolors="black", zorder=12)

    labels = {c: KINDS[c].title for c in handles if c in KINDS}
    if "user" in handles:
        labels["user"] = "Вы"
    if handles:
        ax.legend([handles[c] for c in labels], list(labels.values()), loc="lower left", fontsize=9, framealpha=0.95)
    ax.text(0.99, 0.01, f"{datetime.now(tz).strftime('%d.%m.%Y %H:%M')} · событий: {len(events)}",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555")
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
