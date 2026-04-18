#!/usr/bin/env python3
"""HFR Forum Stats — Streamlit app pour analyser les stats d'un sujet Hardware.fr."""

import re
import time
from collections import Counter
from datetime import datetime

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="HFR Stats",
    page_icon="📊",
    layout="wide",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://forum.hardware.fr/",
}

DEFAULT_URL = (
    "https://forum.hardware.fr/hfr/Discussions/Societe/"
    "combien-vivre-convenablement-sujet_21512_1.htm"
)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def parse_hfr_url(url: str) -> tuple[str, int]:
    """
    Returns (base, current_page) from an HFR topic URL.
    e.g. '...sujet_21512_30706.htm' → ('...sujet_21512_', 30706)
    """
    m = re.match(r"^(.*_)(\d+)\.htm$", url.strip())
    if not m:
        raise ValueError(
            "URL non reconnue. Format attendu : "
            "https://forum.hardware.fr/hfr/.../nom-sujet_TOPICID_PAGE.htm"
        )
    return m.group(1), int(m.group(2))


def build_url(base: str, page: int) -> str:
    return f"{base}{page}.htm"


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

DATE_RE = re.compile(
    r"le\s+(\d{2}/\d{2}/\d{4})\s+[àa]\s+(\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)


def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup | None:
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "lxml")
    except requests.HTTPError as e:
        st.warning(f"HTTP {e.response.status_code} sur {url}")
    except Exception as e:
        st.warning(f"Erreur réseau : {e}")
    return None


def _find_username_in_left(td) -> str | None:
    """Cherche le pseudo dans la colonne gauche d'un post HFR."""
    # Stratégie 1 : balise <b> directe (structure classique HFR)
    for b in td.find_all("b"):
        text = b.get_text(strip=True)
        if text and 1 < len(text) <= 60:
            return text

    # Stratégie 2 : lien vers profil
    for a in td.find_all("a", href=True):
        if re.search(r"(profil|user|pseudo)", a["href"], re.I):
            text = a.get_text(strip=True)
            if text and 1 < len(text) <= 60:
                return text

    # Stratégie 3 : premier lien dans la cellule (souvent le pseudo)
    a = td.find("a")
    if a:
        text = a.get_text(strip=True)
        if text and 1 < len(text) <= 60:
            return text

    return None


def extract_posts(soup: BeautifulSoup) -> list[dict]:
    """
    Extrait la liste des posts (auteur + date optionnelle) d'une page HFR.

    HFR utilise des <table class="fondForum ..."> pour chaque message.
    La colonne gauche contient le pseudo, la droite le message.
    """
    posts = []

    # Sélecteur principal : tables avec classe fondForum
    post_tables = soup.find_all(
        "table", class_=lambda c: c and "fondForum" in c
    )

    for table in post_tables:
        # La première <tr> contient les deux colonnes
        row = table.find("tr")
        if not row:
            continue
        tds = row.find_all("td", recursive=False)
        if len(tds) < 2:
            continue

        left_td = tds[0]

        # Ignorer les "en-têtes" de section (colspan, pas de pseudo)
        if left_td.get("colspan"):
            continue

        username = _find_username_in_left(left_td)
        if not username:
            continue

        # Date (optionnelle) — chercher dans la colonne droite
        right_td = tds[1] if len(tds) > 1 else None
        post_date = None
        if right_td:
            right_text = right_td.get_text(" ", strip=True)
            m = DATE_RE.search(right_text)
            if m:
                date_str = m.group(1) + " " + m.group(2)
                try:
                    fmt = "%d/%m/%Y %H:%M:%S" if date_str.count(":") == 2 else "%d/%m/%Y %H:%M"
                    post_date = datetime.strptime(date_str, fmt)
                except ValueError:
                    pass

        posts.append({"username": username, "date": post_date})

    # Fallback : si aucune table fondForum, essayer une heuristique plus large
    if not posts:
        for td in soup.find_all("td", class_=re.compile(r"left|gauche|pseudo", re.I)):
            username = _find_username_in_left(td)
            if username:
                posts.append({"username": username, "date": None})

    return posts


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("📊 HFR Forum Stats")
st.caption("Statistiques de participation par utilisateur — Hardware.fr")

with st.form("params"):
    url_raw = st.text_input("URL du sujet (une page quelconque)", value=DEFAULT_URL)

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        page_start = st.number_input("Page de début", min_value=1, value=1, step=1)
    with col2:
        page_end = st.number_input("Page de fin", min_value=1, value=5, step=1)
    with col3:
        delay = st.slider(
            "Délai entre requêtes (s)",
            min_value=0.5,
            max_value=4.0,
            value=1.5,
            step=0.5,
            help="Évite de surcharger le serveur HFR",
        )

    top_n = st.slider("Nombre d'utilisateurs dans le graphique", 5, 50, 20)
    go = st.form_submit_button("▶ Lancer l'analyse", type="primary")

if go:
    # Validation
    if page_end < page_start:
        st.error("La page de fin doit être ≥ la page de début.")
        st.stop()

    try:
        base_url, _ = parse_hfr_url(url_raw)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    total = int(page_end) - int(page_start) + 1
    all_posts: list[dict] = []
    errors = 0

    progress = st.progress(0.0, text="Initialisation…")
    status = st.empty()

    session = requests.Session()

    for i, page_num in enumerate(range(int(page_start), int(page_end) + 1)):
        url = build_url(base_url, page_num)
        status.text(f"Scraping page {page_num}  ({i + 1}/{total})")

        soup = fetch_soup(url, session)
        if soup is None:
            errors += 1
        else:
            posts = extract_posts(soup)
            if not posts:
                st.warning(f"Page {page_num} : aucun post trouvé (structure HTML inattendue ?)")
                errors += 1
            all_posts.extend(posts)

        progress.progress((i + 1) / total, text=f"Page {page_num} ({i + 1}/{total})")

        if i < total - 1:
            time.sleep(delay)

    progress.empty()
    status.empty()

    if not all_posts:
        st.error(
            "Aucun post trouvé sur les pages analysées.\n\n"
            "Causes possibles :\n"
            "- HFR bloque les requêtes automatisées (essayez d'augmenter le délai)\n"
            "- L'URL ou les numéros de page sont incorrects\n"
            "- La structure HTML du forum a changé"
        )
        st.stop()

    # -----------------------------------------------------------------------
    # Calcul des stats
    # -----------------------------------------------------------------------
    df_raw = pd.DataFrame(all_posts)
    counts = Counter(df_raw["username"])
    df = pd.DataFrame(counts.most_common(), columns=["Utilisateur", "Messages"])
    df.index = range(1, len(df) + 1)

    # Dates disponibles ?
    has_dates = df_raw["date"].notna().any()

    # -----------------------------------------------------------------------
    # Métriques résumées
    # -----------------------------------------------------------------------
    st.divider()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Messages scrapés", len(all_posts))
    m2.metric("Participants uniques", len(counts))
    m3.metric("Pages analysées", total - errors)
    m4.metric("Pages en erreur", errors)
    m5.metric("Top posteur", df.iloc[0]["Utilisateur"] if len(df) else "—")

    st.divider()

    # -----------------------------------------------------------------------
    # Graphique + tableau
    # -----------------------------------------------------------------------
    col_chart, col_table = st.columns([3, 2])

    with col_chart:
        st.subheader(f"Top {min(top_n, len(df))} participants")
        top = df.head(top_n).copy()
        fig = px.bar(
            top,
            x="Messages",
            y="Utilisateur",
            orientation="h",
            color="Messages",
            color_continuous_scale="Blues",
            text="Messages",
        )
        fig.update_layout(
            yaxis=dict(autorange="reversed"),
            coloraxis_showscale=False,
            height=max(400, top_n * 28),
            margin=dict(l=0, r=20, t=20, b=20),
        )
        fig.update_traces(textposition="outside")
        st.plotly_chart(fig, use_container_width=True)

    with col_table:
        st.subheader("Classement complet")
        st.dataframe(df, use_container_width=True, height=500)

    # -----------------------------------------------------------------------
    # Timeline (si dates dispo)
    # -----------------------------------------------------------------------
    if has_dates:
        st.divider()
        st.subheader("Activité dans le temps")
        df_dates = df_raw.dropna(subset=["date"]).copy()
        df_dates["jour"] = df_dates["date"].dt.date
        daily = df_dates.groupby("jour").size().reset_index(name="Messages")
        fig2 = px.bar(
            daily,
            x="jour",
            y="Messages",
            labels={"jour": "Date"},
            color_discrete_sequence=["#4C78A8"],
        )
        fig2.update_layout(margin=dict(l=0, r=0, t=20, b=20))
        st.plotly_chart(fig2, use_container_width=True)

    # -----------------------------------------------------------------------
    # Export CSV
    # -----------------------------------------------------------------------
    csv_bytes = df.to_csv(index=True).encode("utf-8")
    st.download_button(
        label="⬇ Télécharger le classement (CSV)",
        data=csv_bytes,
        file_name="hfr_stats.csv",
        mime="text/csv",
    )
