from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json,text/plain,*/*",
}


def yyyymmdd(trade_date: date) -> str:
    return trade_date.strftime("%Y%m%d")


def fetched_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def session(timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.request_timeout = timeout  # type: ignore[attr-defined]
    return s


def get_text(s: requests.Session, url: str, encoding: str | None = None) -> str:
    timeout = getattr(s, "request_timeout", 20)
    resp = s.get(url, timeout=timeout)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    elif not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def clean_number(value: Any) -> int | float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "--", "nan", "None"}:
        return None
    text = text.replace(",", "").replace("，", "")
    text = re.sub(r"[^\d\.\-]", "", text)
    if text in {"", "-", "."}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def flatten_columns(df):
    df = df.copy()
    df.columns = [
        "_".join(clean_text(part) for part in col if clean_text(part) and "Unnamed" not in clean_text(part))
        if isinstance(col, tuple)
        else clean_text(col)
        for col in df.columns
    ]
    return df
