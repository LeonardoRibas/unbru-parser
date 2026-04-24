"""
UnB RU Menu Parser

Fetches the weekly restaurant menu PDFs from ru.unb.br,
extracts table data, and upserts it into a Supabase table.
"""

import datetime
import json
import logging
import os
import re
import sys
import tempfile

import camelot
import pandas as pd
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RU_BASE_URL = "https://ru.unb.br"
MENU_PAGE_URL = RU_BASE_URL + "/index.php/cardapio-refeitorio"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Each campus has its own Supabase table, named exactly as the campus.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------


def fetch_menu_page() -> BeautifulSoup:
    """Download and parse the menu index page."""
    resp = requests.get(MENU_PAGE_URL, verify=False, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_campus_names(soup: BeautifulSoup) -> list[str]:
    """Return the ordered list of campus names from the page."""
    names = []
    for tag in soup("strong"):
        if isinstance(tag.contents[0], str):
            names.append(tag.string)
    return names


def current_week_date_prefix() -> str:
    """Return a date prefix string (DD/MM) for the Monday of the current week.

    The site labels each menu link with the week start date.  We compute
    the Monday of the current ISO week so the filter works automatically.
    """
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    return monday.strftime("%d/%m")


def download_pdf(url: str, dest: str) -> None:
    """Download a PDF from *url* and write it to *dest*."""
    resp = requests.get(url, verify=False, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)


def fetch_tables(soup: BeautifulSoup, campus_names: list[str], date_prefix: str | None = None):
    """Download PDFs and extract camelot tables keyed by campus name.

    If *date_prefix* is ``None`` every available link is fetched;
    otherwise only links whose text contains the prefix are used.
    """
    links = soup.find("main").find_all("a")
    tables: dict[str, list] = {}
    idx = 0

    for link in links:
        if not link.contents or not link.string or link.string == "I":
            continue
        if date_prefix and date_prefix not in link.string:
            continue
        if idx >= len(campus_names):
            log.warning("More menu links than campus names — skipping extras.")
            break

        campus = campus_names[idx]
        pdf_url = RU_BASE_URL + link.attrs["href"]
        log.info("Downloading menu for %s: %s", campus, pdf_url)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            download_pdf(pdf_url, tmp_path)
            tables[campus] = camelot.read_pdf(
                tmp_path,
                pages="all",
                flavor="lattice",
                line_scale=50,
                strip_text="\n",
                copy_text=["h"],
            )
        finally:
            os.unlink(tmp_path)

        idx += 1

    return tables


# ---------------------------------------------------------------------------
# Table → DataFrame conversion
# ---------------------------------------------------------------------------


def _extract_meal(table_page, remove_items=None):
    """Extract meal data from a single camelot table page into a list of JSON strings."""
    df = table_page.df.T
    columns = [x.capitalize() for x in df.columns[1:] if x]
    meals = []
    for i in range(df.shape[0]):
        values = df.values[i][1:].tolist()
        if remove_items:
            for item in remove_items:
                try:
                    values.remove(item)
                except ValueError:
                    pass
        meals.append(json.dumps(dict(zip(columns, values)), ensure_ascii=False))
    return meals


def table_to_dataframe(table) -> pd.DataFrame:
    """Convert a list of camelot table pages into a tidy DataFrame."""
    for i in range(len(table)):
        table[i].df = table[i].df.drop(0, axis=1)
        table[i].df.index = table[i].df[1]
        table[i].df = table[i].df.drop(1, axis=1)

    raw_dates = re.findall(r"\d\d.\d\d.\d\d\d\d", table[0].df.to_string())
    dates = [
        datetime.datetime.strptime(d.replace("/", ""), "%d%m%Y").strftime("%Y-%m-%d")
        for d in raw_dates
    ]
    num_days = len(dates)

    desjejum = _extract_meal(table[0], remove_items=["Café OU chá", "Café ou chá"]) if len(table) > 0 else []
    almoco = _extract_meal(table[1]) if len(table) > 1 else []
    jantar = _extract_meal(table[2]) if len(table) > 2 else []

    empty = json.dumps({})
    for meal in (desjejum, almoco, jantar):
        if len(meal) < num_days:
            meal.extend([empty] * (num_days - len(meal)))

    desjejum = desjejum[-num_days:]
    almoco = almoco[-num_days:]
    jantar = jantar[-num_days:]

    return pd.DataFrame({
        "date": dates,
        "desjejum": desjejum,
        "almoco": almoco,
        "jantar": jantar,
    })


# ---------------------------------------------------------------------------
# Supabase upload
# ---------------------------------------------------------------------------


def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL and SUPABASE_KEY must be set.")
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def upsert_to_supabase(client: Client, campus: str, df: pd.DataFrame) -> None:
    """Upsert menu rows into the campus-specific Supabase table.

    Uses *date* as the conflict key (id is auto-generated).
    """
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "date": row["date"],
            "desjejum": json.loads(row["desjejum"]),
            "almoco": json.loads(row["almoco"]),
            "jantar": json.loads(row["jantar"]),
        })

    if not rows:
        log.warning("No rows to upsert for campus %s", campus)
        return

    res = client.table(campus).upsert(rows, on_conflict="date").execute()
    log.info("Upserted %d rows into table '%s'", len(rows), campus)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log.info("Starting UnB RU menu parser")

    soup = fetch_menu_page()
    campus_names = extract_campus_names(soup)
    log.info("Found campuses: %s", campus_names)

    date_prefix = current_week_date_prefix()
    log.info("Filtering menus for week starting %s", date_prefix)

    tables = fetch_tables(soup, campus_names, date_prefix=date_prefix)

    if not tables:
        log.warning("No menus found for prefix %s — retrying without date filter", date_prefix)
        tables = fetch_tables(soup, campus_names, date_prefix=None)

    if not tables:
        log.error("No menus could be fetched. Exiting.")
        sys.exit(1)

    sb = get_supabase_client()

    for campus, tbl in tables.items():
        log.info("Processing campus: %s", campus)
        df = table_to_dataframe(tbl)
        upsert_to_supabase(sb, campus, df)

    log.info("Done.")


if __name__ == "__main__":
    main()
