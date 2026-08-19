#!/usr/bin/env python3
"""Erzeugt eine statische Übersicht kommender Änderungen der SR."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
QUERY_DIR = SCRIPT_DIR / "queries"
AS_URI_PATTERN = re.compile(
    r"^https://fedlex\.data\.admin\.ch/eli/oc/(?P<year>\d{4})/(?P<number>[^/]+)$"
)
ARTICLE_URI_PATTERN = re.compile(r"/art_(?P<number>\d+)[a-z0-9]*$", re.IGNORECASE)
LANGUAGE_CODES = ("de", "fr", "it")
LANGUAGE_OUTPUT_SUBDIRS = {"de": "", "fr": "fr", "it": "it"}
LANGUAGE_NAMES = {"de": "Deutsch", "fr": "Français", "it": "Italiano"}
OFFICIAL_COLLECTION_PREFIXES = {"de": "AS", "fr": "RO", "it": "RU"}
LOCALIZED_TEXT = {
    "de": {
        "page_title": "In Kraft tretende Änderungen des Schweizer Bundesrechts",
        "period": "Zeitraum",
        "from": "ab",
        "until": "bis",
        "entries": "Einträge",
        "selection": "Auswahl",
        "download_csv": "CSV herunterladen",
        "source": "Quelle",
        "language": "Sprache",
        "effective_date": "Inkrafttreten",
        "sr_number": "SR-Nummer",
        "title": "Titel (deutsch)",
        "amendment_date": "Änderungsdatum",
        "as_reference": "AS-Fundstelle",
        "authority": "Verantwortliche Stelle",
        "dynamic_years": "{years} Jahre nach dem jeweiligen Abruf",
    },
    "fr": {
        "page_title": "Modifications du droit fédéral suisse entrant en vigueur",
        "period": "Période",
        "from": "du",
        "until": "jusqu’à",
        "entries": "entrées",
        "selection": "Sélection",
        "download_csv": "Télécharger le fichier CSV",
        "source": "Source",
        "language": "Langue",
        "effective_date": "Entrée en vigueur",
        "sr_number": "Numéro RS",
        "title": "Titre (français)",
        "amendment_date": "Date de l’acte modificateur",
        "as_reference": "Référence RO",
        "authority": "Service responsable",
        "dynamic_years": "{years} ans après chaque consultation",
    },
    "it": {
        "page_title": "Modifiche del diritto federale svizzero che entrano in vigore",
        "period": "Periodo",
        "from": "dal",
        "until": "fino a",
        "entries": "voci",
        "selection": "Selezione",
        "download_csv": "Scarica il file CSV",
        "source": "Fonte",
        "language": "Lingua",
        "effective_date": "Entrata in vigore",
        "sr_number": "Numero RS",
        "title": "Titolo (italiano)",
        "amendment_date": "Data dell’atto modificatore",
        "as_reference": "Riferimento RU",
        "authority": "Servizio responsabile",
        "dynamic_years": "{years} anni dopo ogni consultazione",
    },
}


class MonitorError(RuntimeError):
    """Fehler, der ohne Python-Traceback ausgegeben werden kann."""


@dataclass(frozen=True)
class Consolidation:
    effective_date: str
    sr_number: str
    title: str
    abstract_uri: str
    language_code: str = "de"


@dataclass(frozen=True)
class ArticleRange:
    sr_number: str
    first_article: int
    last_article: int


@dataclass(frozen=True)
class Selection:
    description: str
    exact_sr_numbers: frozenset[str]
    sr_number_prefixes: tuple[str, ...]
    article_ranges: tuple[ArticleRange, ...]
    localized_descriptions: tuple[tuple[str, str], ...] = ()

    def description_for(self, language_code: str) -> str:
        descriptions = dict(self.localized_descriptions)
        return descriptions.get(language_code, self.description)


@dataclass(frozen=True)
class Impact:
    effective_date: str
    abstract_uri: str
    amendment_date: str
    publication_date: str
    as_number: str
    act_uri: str
    authorities: tuple[str, ...]
    target_subdivisions: tuple[str, ...]
    language_code: str = "de"


@dataclass(frozen=True)
class Entry:
    effective_date: str
    sr_number: str
    title: str
    amendment_date: str
    as_reference: str
    authority: str
    abstract_uri: str
    act_uri: str
    language_code: str = "de"


def normalized_text(value: str) -> str:
    """Normalisiert nur bedeutungslose Unicode- und Leerraumunterschiede."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def parse_iso_date(value: str, field_name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"Ungültiges {field_name}: {value!r} (erwartet YYYY-MM-DD)") from exc


def date_years_later(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # Der 29. Februar wird in einem Nicht-Schaltjahr zum 28. Februar.
        return value.replace(year=value.year + years, month=2, day=28)


def resolve_end_date(raw: Any, *, today: date | None = None) -> tuple[str, int | None]:
    if isinstance(raw, str):
        return parse_iso_date(raw, "Enddatum"), None
    if not isinstance(raw, dict):
        raise MonitorError(
            "end_date muss ein festes Datum oder ein Objekt mit years_from_today sein."
        )
    try:
        years = int(raw["years_from_today"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MonitorError("Ungültiger Wert für end_date.years_from_today.") from exc
    if years < 1:
        raise MonitorError("end_date.years_from_today muss mindestens 1 sein.")

    end_date = date_years_later(today or date.today(), years).isoformat()
    return end_date, years


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Konfigurationsdatei nicht gefunden: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Ungültiges JSON in {path}: {exc}") from exc

    required = {"endpoint", "start_date", "end_date", "output_dir"}
    missing = sorted(required - config.keys())
    if missing:
        raise MonitorError(f"Fehlende Konfigurationswerte: {', '.join(missing)}")
    return config


def parse_selection(raw: Any) -> Selection | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MonitorError("selection in config.json muss ein JSON-Objekt sein.")

    exact_values = raw.get("exact_sr_numbers", [])
    prefix_values = raw.get("sr_number_prefixes", [])
    range_values = raw.get("article_ranges", [])
    description_values = raw.get("descriptions", {})
    if not isinstance(exact_values, list) or not all(
        isinstance(value, str) and value.strip() for value in exact_values
    ):
        raise MonitorError("exact_sr_numbers muss eine Liste von SR-Nummern sein.")
    if not isinstance(prefix_values, list) or not all(
        isinstance(value, str) and value.strip() for value in prefix_values
    ):
        raise MonitorError("sr_number_prefixes muss eine Liste von SR-Bereichen sein.")
    if not isinstance(range_values, list):
        raise MonitorError("article_ranges muss eine Liste sein.")
    if not isinstance(description_values, dict) or not all(
        key in LANGUAGE_CODES and isinstance(value, str)
        for key, value in description_values.items()
    ):
        raise MonitorError("selection.descriptions enthält ungültige Sprachtexte.")

    article_ranges = []
    for item in range_values:
        if not isinstance(item, dict):
            raise MonitorError("Jeder Eintrag in article_ranges muss ein Objekt sein.")
        try:
            article_range = ArticleRange(
                sr_number=normalized_text(str(item["sr_number"])),
                first_article=int(item["first_article"]),
                last_article=int(item["last_article"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MonitorError(f"Ungültiger article_ranges-Eintrag: {item!r}") from exc
        if article_range.first_article > article_range.last_article:
            raise MonitorError(f"Umgekehrter Artikelbereich: {item!r}")
        article_ranges.append(article_range)

    selection = Selection(
        description=normalized_text(str(raw.get("description", ""))),
        exact_sr_numbers=frozenset(normalized_text(value) for value in exact_values),
        sr_number_prefixes=tuple(
            sorted({normalized_text(value).rstrip(".") for value in prefix_values})
        ),
        article_ranges=tuple(article_ranges),
        localized_descriptions=tuple(
            sorted(
                (key, normalized_text(value))
                for key, value in description_values.items()
            )
        ),
    )
    if not (
        selection.exact_sr_numbers
        or selection.sr_number_prefixes
        or selection.article_ranges
    ):
        raise MonitorError("selection enthält keine Auswahlregel.")
    return selection


def language_code(binding: dict[str, Any]) -> str:
    value = normalized_text(binding_value(binding, "languageCode"))
    if value not in LANGUAGE_CODES:
        raise MonitorError(f"Unerwarteter Sprachcode von Fedlex: {value!r}")
    return value


def render_query(path: Path, start_date: str, end_date: str) -> str:
    try:
        query = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MonitorError(f"SPARQL-Abfrage nicht gefunden: {path}") from exc

    query = query.replace("{{START_DATE}}", start_date).replace("{{END_DATE}}", end_date)
    if "{{" in query or "}}" in query:
        raise MonitorError(f"Nicht ersetzter Platzhalter in {path}")
    return query


def post_sparql(
    endpoint: str,
    query: str,
    *,
    timeout_seconds: int,
    max_attempts: int,
    user_agent: str,
) -> list[dict[str, Any]]:
    """Sendet die Abfrage form-urlencodiert per POST und wiederholt nur Transientes."""
    payload = urllib.parse.urlencode(
        {"query": query, "format": "application/sparql-results+json"}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": user_agent,
        },
    )

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
            document = json.loads(body.decode("utf-8"))
            bindings = document.get("results", {}).get("bindings")
            if not isinstance(bindings, list):
                raise MonitorError("Fedlex lieferte kein gültiges SPARQL-JSON-Ergebnis.")
            return bindings
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
            retry_after = exc.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 2 ** (attempt - 1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            delay = 2 ** (attempt - 1)

        if attempt < max_attempts:
            time.sleep(min(delay, 30))

    raise MonitorError(
        f"Fedlex-SPARQL-Abfrage nach {max_attempts} Versuch(en) fehlgeschlagen: {last_error}"
    )


def binding_value(binding: dict[str, Any], name: str, *, required: bool = True) -> str:
    raw = binding.get(name)
    if isinstance(raw, dict) and isinstance(raw.get("value"), str):
        return raw["value"]
    if required:
        raise MonitorError(f"Pflichtfeld {name!r} fehlt in einer Fedlex-Zeile.")
    return ""


def parse_consolidations(bindings: Iterable[dict[str, Any]]) -> list[Consolidation]:
    unique: dict[tuple[str, str, str, str, str], Consolidation] = {}
    for binding in bindings:
        item = Consolidation(
            effective_date=parse_iso_date(
                binding_value(binding, "effectiveDate")[:10], "Inkrafttretensdatum"
            ),
            sr_number=normalized_text(binding_value(binding, "srNumber")),
            title=normalized_text(binding_value(binding, "title")),
            abstract_uri=binding_value(binding, "abstract"),
            language_code=language_code(binding),
        )
        key = (
            item.language_code,
            item.effective_date,
            item.sr_number,
            item.title,
            item.abstract_uri,
        )
        unique[key] = item
    return list(unique.values())


def parse_impacts(bindings: Iterable[dict[str, Any]]) -> list[Impact]:
    authorities_by_impact: dict[tuple[str, str, str, str, str, str, str], set[str]] = (
        defaultdict(set)
    )
    targets_by_impact: dict[tuple[str, str, str, str, str, str, str], set[str]] = (
        defaultdict(set)
    )

    for binding in bindings:
        effective_date = parse_iso_date(
            binding_value(binding, "effectiveDate")[:10], "Impact-Inkrafttretensdatum"
        )
        abstract_uri = binding_value(binding, "abstract")
        target_subdivision = binding_value(binding, "targetSubdivision")
        act_uri = binding_value(binding, "act")
        amendment_raw = binding_value(binding, "amendmentDate", required=False)
        publication_raw = binding_value(binding, "publicationDate", required=False)
        as_number = normalized_text(binding_value(binding, "asNumber", required=False))

        if not amendment_raw or not publication_raw or not as_number:
            raise MonitorError(
                "AS-Metadaten unvollständig für "
                f"{act_uri}: dateDocument={amendment_raw!r}, "
                f"publicationDate={publication_raw!r}, AS-Nummer={as_number!r}"
            )

        amendment_date = parse_iso_date(amendment_raw[:10], "Änderungsdatum")
        publication_date = parse_iso_date(publication_raw[:10], "Publikationsdatum")
        key = (
            effective_date,
            abstract_uri,
            amendment_date,
            publication_date,
            as_number,
            act_uri,
            language_code(binding),
        )
        authority = normalized_text(binding_value(binding, "authority", required=False))
        if authority:
            authorities_by_impact[key].add(authority)
        targets_by_impact[key].add(target_subdivision)

    impacts = []
    for key, targets in targets_by_impact.items():
        authorities = authorities_by_impact[key]
        impacts.append(
            Impact(
                effective_date=key[0],
                abstract_uri=key[1],
                amendment_date=key[2],
                publication_date=key[3],
                as_number=key[4],
                act_uri=key[5],
                authorities=tuple(sorted(authorities, key=str.casefold)),
                target_subdivisions=tuple(sorted(targets)),
                language_code=key[6],
            )
        )
    return impacts


def derive_as_reference(impact: Impact) -> str:
    match = AS_URI_PATTERN.fullmatch(impact.act_uri)
    if not match:
        raise MonitorError(f"Unerwartete AS-URI: {impact.act_uri}")

    uri_year = match.group("year")
    uri_number = match.group("number")
    publication_year = impact.publication_date[:4]

    normalized_metadata_number = (
        str(int(impact.as_number)) if impact.as_number.isdigit() else impact.as_number
    )
    normalized_uri_number = str(int(uri_number)) if uri_number.isdigit() else uri_number
    if uri_year != publication_year or normalized_uri_number != normalized_metadata_number:
        raise MonitorError(
            "Widersprüchliche AS-Metadaten für "
            f"{impact.act_uri}: publicationDate={impact.publication_date}, "
            f"sequenceInTheYearOfPublication={impact.as_number}"
        )
    try:
        prefix = OFFICIAL_COLLECTION_PREFIXES[impact.language_code]
    except KeyError as exc:
        raise MonitorError(f"Unbekannte Ausgabesprache: {impact.language_code!r}") from exc
    return f"{prefix} {uri_year} {uri_number}"


def natural_key(value: str) -> tuple[tuple[int, Any], ...]:
    parts = re.findall(r"\d+|\D+", value)
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in parts)


def target_article_number(target_subdivision: str) -> int | None:
    match = ARTICLE_URI_PATTERN.search(target_subdivision)
    return int(match.group("number")) if match else None


def selection_matches(
    consolidation: Consolidation, impact: Impact, selection: Selection | None
) -> bool:
    if selection is None:
        return True
    if consolidation.sr_number in selection.exact_sr_numbers:
        return True
    if any(
        consolidation.sr_number == prefix
        or consolidation.sr_number.startswith(prefix + ".")
        for prefix in selection.sr_number_prefixes
    ):
        return True

    for article_range in selection.article_ranges:
        if consolidation.sr_number != article_range.sr_number:
            continue
        for target in impact.target_subdivisions:
            article_number = target_article_number(target)
            if (
                article_number is not None
                and article_range.first_article
                <= article_number
                <= article_range.last_article
            ):
                return True
    return False


def merge_entries(
    consolidations: Iterable[Consolidation],
    impacts: Iterable[Impact],
    selection: Selection | None = None,
) -> list[Entry]:
    impacts_by_target: dict[tuple[str, str, str], list[Impact]] = defaultdict(list)
    for impact in impacts:
        impacts_by_target[
            (impact.language_code, impact.effective_date, impact.abstract_uri)
        ].append(impact)

    entries: list[Entry] = []
    missing: list[Consolidation] = []
    for consolidation in consolidations:
        matches = impacts_by_target.get(
            (
                consolidation.language_code,
                consolidation.effective_date,
                consolidation.abstract_uri,
            ),
            [],
        )
        if not matches:
            missing.append(consolidation)
            continue

        for impact in matches:
            if not selection_matches(consolidation, impact, selection):
                continue
            entries.append(
                Entry(
                    effective_date=consolidation.effective_date,
                    sr_number=consolidation.sr_number,
                    title=consolidation.title,
                    amendment_date=impact.amendment_date,
                    as_reference=derive_as_reference(impact),
                    authority="; ".join(impact.authorities) if impact.authorities else "–",
                    abstract_uri=consolidation.abstract_uri,
                    act_uri=impact.act_uri,
                    language_code=consolidation.language_code,
                )
            )

    if missing:
        examples = ", ".join(
            f"{item.language_code}: {item.effective_date} / SR {item.sr_number}"
            for item in missing[:5]
        )
        raise MonitorError(
            f"{len(missing)} Konsolidierungszeile(n) konnten keinem AS-Impact "
            f"zugeordnet werden: {examples}"
        )

    unique = {
        (
            item.effective_date,
            item.sr_number,
            item.title,
            item.amendment_date,
            item.as_reference,
            item.authority,
            item.abstract_uri,
            item.act_uri,
            item.language_code,
        ): item
        for item in entries
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            LANGUAGE_CODES.index(item.language_code),
            item.effective_date,
            natural_key(item.sr_number),
            item.sr_number,
            item.amendment_date,
            natural_key(item.as_reference),
            item.act_uri,
        ),
    )


def public_fedlex_url(data_uri: str, language_code: str = "de") -> str:
    prefix = "https://fedlex.data.admin.ch/"
    if not data_uri.startswith(prefix):
        raise MonitorError(f"Unerwartete Fedlex-Daten-URI: {data_uri}")
    if language_code not in LANGUAGE_CODES:
        raise MonitorError(f"Unbekannte Ausgabesprache: {language_code!r}")
    return (
        "https://www.fedlex.admin.ch/"
        + data_uri.removeprefix(prefix)
        + f"/{language_code}"
    )


def validate_entry_language_coverage(entries: Iterable[Entry]) -> None:
    identities_by_language: dict[str, set[tuple[str, str, str, str, str]]] = {
        code: set() for code in LANGUAGE_CODES
    }
    for item in entries:
        identities_by_language[item.language_code].add(
            (
                item.effective_date,
                item.sr_number,
                item.amendment_date,
                item.abstract_uri,
                item.act_uri,
            )
        )

    reference = identities_by_language["de"]
    for code in LANGUAGE_CODES[1:]:
        if identities_by_language[code] == reference:
            continue
        missing = reference - identities_by_language[code]
        additional = identities_by_language[code] - reference
        raise MonitorError(
            f"Fedlex-Sprachabdeckung für die gefilterten Treffer in {code!r} "
            f"weicht von Deutsch ab: {len(missing)} fehlend, "
            f"{len(additional)} zusätzlich."
        )


def csv_document(entries: Iterable[Entry], language_code: str = "de") -> str:
    strings = LOCALIZED_TEXT[language_code]
    output = io.StringIO(newline="")
    fieldnames = [
        strings["effective_date"],
        strings["sr_number"],
        strings["title"],
        strings["amendment_date"],
        strings["as_reference"],
        strings["authority"],
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for item in entries:
        writer.writerow(
            {
                strings["effective_date"]: item.effective_date,
                strings["sr_number"]: item.sr_number,
                strings["title"]: item.title,
                strings["amendment_date"]: item.amendment_date,
                strings["as_reference"]: item.as_reference,
                strings["authority"]: item.authority,
            }
        )
    return output.getvalue()


def html_document(
    entries: list[Entry],
    start_date: str,
    end_date: str,
    selection_description: str = "",
    dynamic_end_years: int | None = None,
    language_code: str = "de",
) -> str:
    strings = LOCALIZED_TEXT[language_code]
    rows = []
    for item in entries:
        rows.append(
            "      <tr>\n"
            f'        <td><time datetime="{item.effective_date}">{item.effective_date}</time></td>\n'
            f'        <td><a href="{html.escape(public_fedlex_url(item.abstract_uri, language_code), quote=True)}">{html.escape(item.sr_number)}</a></td>\n'
            f"        <td>{html.escape(item.title)}</td>\n"
            f'        <td><time datetime="{item.amendment_date}">{item.amendment_date}</time></td>\n'
            f'        <td><a href="{html.escape(public_fedlex_url(item.act_uri, language_code), quote=True)}">{html.escape(item.as_reference)}</a></td>\n'
            f"        <td>{html.escape(item.authority)}</td>\n"
            "      </tr>"
        )

    table_rows = "\n".join(rows)
    selection_paragraph = (
        f'\n    <p><strong>{html.escape(strings["selection"])}:</strong> '
        f"{html.escape(selection_description)}</p>"
        if selection_description
        else ""
    )
    language_links = []
    for code in LANGUAGE_CODES:
        if code == language_code:
            language_links.append(
                f'<strong lang="{code}">{html.escape(LANGUAGE_NAMES[code])}</strong>'
            )
            continue
        href = (
            f"{code}/"
            if language_code == "de"
            else "../" if code == "de" else f"../{code}/"
        )
        language_links.append(
            f'<a lang="{code}" href="{href}">{html.escape(LANGUAGE_NAMES[code])}</a>'
        )
    language_navigation = " · ".join(language_links)

    dynamic_end_label = (
        strings["dynamic_years"].format(years=dynamic_end_years)
        if dynamic_end_years is not None
        else ""
    )
    period_text = (
        f'{html.escape(strings["from"])} <time datetime="{start_date}">{start_date}</time> '
        f'{html.escape(strings["until"])} '
        f"{html.escape(dynamic_end_label)}"
        if dynamic_end_label
        else f'{html.escape(strings["from"])} <time datetime="{start_date}">{start_date}</time> '
        f'{html.escape(strings["until"])} '
        f'<time datetime="{end_date}">{end_date}</time>'
    )
    return f"""<!doctype html>
<html lang="{language_code}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(strings["page_title"])}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 2rem auto; max-width: 100rem; padding: 0 1rem; color: #1b1b1b; }}
    h1 {{ font-size: 1.6rem; margin-bottom: .5rem; }}
    p {{ margin: .4rem 0 1rem; }}
    .languages {{ margin-bottom: 1.2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #bbb; padding: .45rem .55rem; text-align: left; vertical-align: top; }}
    th {{ background: #eee; position: sticky; top: 0; }}
    tbody tr:nth-child(even) {{ background: #f8f8f8; }}
    time, td:nth-child(2), td:nth-child(5) {{ white-space: nowrap; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(strings["page_title"])}</h1>
    <p class="languages"><strong>{html.escape(strings["language"])}:</strong> {language_navigation}</p>
    <p>{html.escape(strings["period"])}: {period_text}. {html.escape(strings["entries"])}: {len(entries)}.</p>{selection_paragraph}
    <p><a href="fedlex-aenderungen.csv">{html.escape(strings["download_csv"])}</a> · {html.escape(strings["source"])}: <a href="https://fedlex.data.admin.ch/">Fedlex</a></p>
    <table>
      <thead>
        <tr>
          <th>{html.escape(strings["effective_date"])}</th>
          <th>{html.escape(strings["sr_number"])}</th>
          <th>{html.escape(strings["title"])}</th>
          <th>{html.escape(strings["amendment_date"])}</th>
          <th>{html.escape(strings["as_reference"])}</th>
          <th>{html.escape(strings["authority"])}</th>
        </tr>
      </thead>
      <tbody>
{table_rows}
      </tbody>
    </table>
  </main>
</body>
</html>
"""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def generate(
    config_path: Path,
    *,
    start_override: str | None = None,
    end_override: str | None = None,
    output_override: Path | None = None,
) -> tuple[dict[str, int], Path]:
    config_path = config_path.resolve()
    config = load_config(config_path)
    selection = parse_selection(config.get("selection"))
    start_date = parse_iso_date(start_override or config["start_date"], "Startdatum")
    if end_override:
        end_date = parse_iso_date(end_override, "Enddatum")
        dynamic_end_years = None
    else:
        end_date, dynamic_end_years = resolve_end_date(config["end_date"])
    if start_date > end_date:
        raise MonitorError("Das Startdatum liegt nach dem Enddatum.")

    output_dir = output_override or Path(config["output_dir"])
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    output_dir = output_dir.resolve()

    query_options = {
        "endpoint": str(config["endpoint"]),
        "timeout_seconds": int(config.get("timeout_seconds", 120)),
        "max_attempts": int(config.get("max_attempts", 4)),
        "user_agent": str(config.get("user_agent", "Fedlex-In-Force-Monitor/1.0")),
    }
    consolidation_bindings = post_sparql(
        query=render_query(QUERY_DIR / "consolidations.sparql", start_date, end_date),
        **query_options,
    )
    impact_bindings = post_sparql(
        query=render_query(QUERY_DIR / "impacts.sparql", start_date, end_date),
        **query_options,
    )

    consolidations = parse_consolidations(consolidation_bindings)
    if not consolidations and not bool(config.get("allow_empty", False)):
        raise MonitorError(
            "Fedlex lieferte keine Konsolidierungen. Falls der Zeitraum bewusst leer ist, "
            "kann allow_empty in config.json auf true gesetzt werden."
        )
    entries = merge_entries(
        consolidations, parse_impacts(impact_bindings), selection=selection
    )
    validate_entry_language_coverage(entries)

    counts: dict[str, int] = {}
    for code in LANGUAGE_CODES:
        localized_entries = [item for item in entries if item.language_code == code]
        localized_output_dir = output_dir / LANGUAGE_OUTPUT_SUBDIRS[code]
        atomic_write(
            localized_output_dir / "fedlex-aenderungen.csv",
            csv_document(localized_entries, code),
        )
        atomic_write(
            localized_output_dir / "index.html",
            html_document(
                localized_entries,
                start_date,
                end_date,
                selection_description=(
                    selection.description_for(code) if selection else ""
                ),
                dynamic_end_years=dynamic_end_years,
                language_code=code,
            ),
        )
        counts[code] = len(localized_entries)
    return counts, output_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Statische Fedlex-Übersicht für WebSite-Watcher erzeugen."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start", help="Startdatum YYYY-MM-DD (überschreibt config.json)")
    parser.add_argument("--end", help="Enddatum YYYY-MM-DD (überschreibt config.json)")
    parser.add_argument("--output-dir", type=Path, help="Ausgabeverzeichnis überschreiben")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        counts, output_dir = generate(
            args.config,
            start_override=args.start,
            end_override=args.end,
            output_override=args.output_dir,
        )
    except MonitorError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1

    count_text = ", ".join(f"{code}: {counts[code]}" for code in LANGUAGE_CODES)
    print(f"Einträge ({count_text}) nach {output_dir} geschrieben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
