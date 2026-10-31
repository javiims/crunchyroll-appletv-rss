import html as html_module
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


LINKS_FILE = "links.txt"
RSS_FILE = "feed.xml"

REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3

# Set DEBUG_HTML_DIR (e.g. "debug_html") to save every downloaded page.
# The GitHub workflow uploads that folder as an artifact so you can inspect
# exactly what Apple TV returned when something is not detected.
DEBUG_HTML_DIR = os.environ.get("DEBUG_HTML_DIR", "").strip()

# Any provider whose name contains one of these strings is accepted
# (e.g. "Crunchyroll", "Crunchyroll Amazon Channel").
ALLOWED_PROVIDERS = ("crunchyroll",)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# Languages to track
# ---------------------------------------------------------------------------
# Each language is detected through three signals:
#   * audio  -> the dub itself appears in "Audio" / "Audio original"
#   * ad     -> an audio-description track in that language
#   * cc     -> CC / SDH ("SPS" in Spanish UI) subtitles in that language,
#               which on Crunchyroll means the dub is coming soon.
# Plain subtitles are ignored on purpose (they do not imply a dub).

LANGUAGES = [
    {
        "label": "Español (España)",
        "names": [
            r"espa[ñn]ol\s*\(\s*espa[ñn]a\s*\)",
            r"castellano",
            r"spanish\s*\(\s*spain\s*\)",
            r"castilian",
            r"european\s+spanish",
        ],
        "codes": ["es-es", "es_es"],
    },
    {
        "label": "Italiano (Italia)",
        "names": [
            r"italiano\s*\(\s*italia\s*\)",
            r"italian\s*\(\s*italy\s*\)",
        ],
        "codes": ["it-it", "it_it"],
    },
]

for _language in LANGUAGES:
    _language["regex"] = re.compile(
        "|".join(f"(?:{name})" for name in _language["names"]),
        flags=re.IGNORECASE,
    )

CC_MARKER = re.compile(
    r"\bSPS\b|\bSDH\b|\bCC\b|closed\s*caption|subt[ií]tulos?\s+para\s+sordos"
    r"|para\s+personas\s+sordas|hearing\s+impaired|non\s+udenti",
    flags=re.IGNORECASE,
)

AD_MARKER = re.compile(
    r"\bAD\b|audio\s*-?\s*descrip|descripci[oó]n\s+de\s+audio"
    r"|audio\s+description|described\s+video|audiodescrizione",
    flags=re.IGNORECASE,
)

# Apple TV "Idiomas" section labels (Spanish and English storefronts).
FIELD_ALIASES = {
    "audio": {"audio", "idiomas de audio", "audio languages"},
    "original_audio": {
        "audio original",
        "original audio",
        "idioma original",
        "original language",
    },
    "subtitles": {
        "subtítulos",
        "subtitulos",
        "subtitles",
        "subtítulos opcionales",
        "subtitles/cc",
        "closed captions",
    },
    "accessibility": {"accesibilidad", "accessibility"},
}

LABEL_TO_FIELD = {
    alias: field
    for field, aliases in FIELD_ALIASES.items()
    for alias in aliases
}

# Text Apple adds when a list is truncated ("y 7 más… Ver todo").
TRUNCATION_NOISE = re.compile(
    r"(?:\by\s+\d+\s+m[aá]s\b|\band\s+\d+\s+more\b)[.…]*"
    r"|\bver\s+todo\b|\bsee\s+all\b|\bmás\s*…|…",
    flags=re.IGNORECASE,
)

INVISIBLE_CHARS = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def load_links():
    """
    Read Apple TV URLs from links.txt.

    Blank lines and lines beginning with # are ignored.
    Duplicate URLs are removed while preserving their original order.
    """
    if not os.path.exists(LINKS_FILE):
        return []

    urls = []
    seen = set()

    with open(LINKS_FILE, "r", encoding="utf-8-sig") as file:
        for line in file:
            url = clean_text(line)

            if not url or url.startswith("#"):
                continue

            if url not in seen:
                urls.append(url)
                seen.add(url)

    return urls


def save_links(urls):
    """
    Save the links that still need to be checked.
    """
    with open(LINKS_FILE, "w", encoding="utf-8") as file:
        for url in urls:
            file.write(url + "\n")


def load_or_create_rss():
    """
    Load feed.xml if it exists.

    If it does not exist, create a new RSS feed.
    """
    if os.path.exists(RSS_FILE):
        try:
            tree = ET.parse(RSS_FILE)
            rss = tree.getroot()
            channel = rss.find("channel")

            if channel is None:
                raise ValueError("feed.xml does not contain an RSS channel")

            return tree, rss

        except (ET.ParseError, ValueError) as error:
            print(f"WARNING: Existing feed.xml could not be read: {error}")
            print("A new RSS feed will be created.")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = (
        "Crunchyroll Apple TV - Nuevos doblajes"
    )
    ET.SubElement(channel, "link").text = "https://tv.apple.com/"
    ET.SubElement(channel, "description").text = (
        "Detección automática de audio de Crunchyroll en "
        "Español (España) e Italiano (Italia) en Apple TV"
    )
    ET.SubElement(channel, "language").text = "es-ES"

    tree = ET.ElementTree(rss)
    return tree, rss


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean_text(text):
    """
    Remove invisible direction marks (Apple TV inserts U+200E etc.),
    convert non-breaking spaces and collapse whitespace.
    """
    if not text:
        return ""

    text = INVISIBLE_CHARS.sub("", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# Kept for backwards compatibility with the previous version.
normalize_whitespace = clean_text


def normalize_label(text):
    return clean_text(text).rstrip(":").strip().casefold()


def canonical_tracking_url(url):
    """
    Produce a stable version of the URL for duplicate detection.

    The query string is preserved because Apple TV episode URLs may contain
    a showId parameter that is useful when opening the episode.

    The URL fragment is removed.
    """
    parts = urlsplit(url.strip())
    normalized_path = re.sub(r"/+", "/", parts.path)

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            normalized_path,
            parts.query,
            "",
        )
    )


def split_language_list(text):
    """
    Split "Español (España) (AAC), Inglés (Estados Unidos) (SPS)" into items.

    Commas inside parentheses (e.g. "(AAC, AD)") are not split.
    Truncation text such as "y 7 más… Ver todo" is removed.
    """
    text = TRUNCATION_NOISE.sub(" ", clean_text(text))

    items = []
    depth = 0
    current = []

    for character in text:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)

        if character in ",;·|" and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(character)

    items.append("".join(current))

    return [clean_text(item) for item in items if clean_text(item)]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_url(url, session):
    """
    Download an Apple TV page and decode it explicitly as UTF-8.

    Retries transient errors (429 / 5xx / network) with a back-off.
    """
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = session.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )

            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(
                    f"HTTP {response.status_code}",
                    response=response,
                )

            response.raise_for_status()

            page_html = response.content.decode("utf-8", errors="replace")

            print(
                f"Downloaded: {response.url} "
                f"(HTTP {response.status_code}, "
                f"{len(page_html)} characters)"
            )

            save_debug_html(url, page_html)
            return page_html

        except requests.RequestException as error:
            print(
                f"ERROR downloading {url} "
                f"(attempt {attempt}/{REQUEST_RETRIES}): {error}"
            )

            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                return None

            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_DELAY_SECONDS * attempt * 2)

    return None


def save_debug_html(url, page_html):
    if not DEBUG_HTML_DIR:
        return

    os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", urlsplit(url).path).strip("_")
    path = os.path.join(DEBUG_HTML_DIR, f"{slug[:150]}.html")

    with open(path, "w", encoding="utf-8") as file:
        file.write(page_html)


# ---------------------------------------------------------------------------
# Provider and title
# ---------------------------------------------------------------------------

def extract_providers(soup, page_html):
    """
    Return every content provider shown on the page.

    Apple TV marks providers with data-testid="content-provider-logo-coin"
    and the provider name in aria-label. A page can show several providers.
    """
    providers = []

    for element in soup.find_all(attrs={"data-testid": True}):
        test_id = str(element.get("data-testid", "")).casefold()

        if "provider" not in test_id and "channel" not in test_id:
            continue

        for attribute in ("aria-label", "title", "alt"):
            value = clean_text(element.get(attribute, ""))
            if value:
                providers.append(value)

        image = element.find("img", alt=True)
        if image is not None and clean_text(image.get("alt")):
            providers.append(clean_text(image.get("alt")))

    # Regex fallback (the previous version had no capture group here).
    for pattern in (
        r'data-testid=["\']content-provider-logo-coin["\'][^>]*?'
        r'aria-label=["\']([^"\']+)["\']',
        r'aria-label=["\']([^"\']+)["\'][^>]*?'
        r'data-testid=["\']content-provider-logo-coin["\']',
    ):
        for match in re.finditer(pattern, page_html, flags=re.IGNORECASE):
            providers.append(clean_text(html_module.unescape(match.group(1))))

    unique = []
    for provider in providers:
        if provider and provider not in unique:
            unique.append(provider)

    return unique


def is_allowed_provider(providers):
    return any(
        allowed in provider.casefold()
        for provider in providers
        for allowed in ALLOWED_PROVIDERS
    )


def extract_title(soup):
    """
    Extract a readable title.

    Preference:
    1. Apple structured-data episode name (with show name)
    2. Apple title metadata
    3. HTML title
    """
    for script_id in ("schema:tv-episode", "schema:tv-season", "schema:tv-series"):
        schema_element = soup.find("script", attrs={"id": script_id})

        if not schema_element or not schema_element.string:
            continue

        try:
            data = json.loads(schema_element.string)
        except ValueError:
            data = None

        if isinstance(data, dict) and data.get("name"):
            name = clean_text(html_module.unescape(str(data["name"])))
            series = data.get("partOfSeries") or data.get("partOfTVSeries")
            series_name = ""

            if isinstance(series, dict):
                series_name = clean_text(str(series.get("name", "")))

            if series_name and series_name.casefold() not in name.casefold():
                return f"{series_name} - {name}"

            return name

    apple_title = soup.find("meta", attrs={"name": "apple:title"})
    if apple_title and apple_title.get("content"):
        return clean_text(html_module.unescape(apple_title["content"]))

    if soup.title and soup.title.string:
        title = clean_text(html_module.unescape(soup.title.string))
        title = re.sub(
            r"\s*-\s*Apple\s*TV(?:\s*\([^)]*\))?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )

        if title:
            return title

    return "Título desconocido"


# ---------------------------------------------------------------------------
# Language fields
# ---------------------------------------------------------------------------

def extract_language_fields(soup):
    """
    Collect the items of the "Idiomas" section:
    Audio original, Audio, Subtítulos and Accesibilidad.

    Returns {"audio": [...], "original_audio": [...],
             "subtitles": [...], "accessibility": [...]}

    Sources (merged, de-duplicated):
    1. <dt>/<dd> pairs (visible "Ficha técnica")
    2. Any other element whose text is exactly a field label, followed by
       a sibling element (covers the "Ver todo" modal / layout changes)
    3. JSON embedded in <script> tags (full lists, not truncated)
    """
    fields = {field: [] for field in FIELD_ALIASES}

    def add(field, text):
        for item in split_language_list(text):
            if item not in fields[field]:
                fields[field].append(item)

    def value_text(element):
        # Join list children with commas so items never merge together.
        children = element.find_all("li")
        if children:
            return ", ".join(child.get_text(" ", strip=True) for child in children)
        return element.get_text(" ", strip=True)

    # 1. Definition lists.
    for definition_title in soup.find_all("dt"):
        field = LABEL_TO_FIELD.get(normalize_label(definition_title.get_text(" ")))
        if not field:
            continue

        definition_content = definition_title.find_next_sibling("dd")
        if definition_content is not None:
            add(field, value_text(definition_content))

    # 2. Generic "label + next sibling" layouts.
    for element in soup.find_all(["h2", "h3", "h4", "h5", "span", "div", "p", "strong"]):
        if element.find(True) is not None and element.name in ("div", "p"):
            # Only leaf-like containers can be labels.
            if len(element.find_all(True)) > 1:
                continue

        field = LABEL_TO_FIELD.get(normalize_label(element.get_text(" ")))
        if not field:
            continue

        sibling = element.find_next_sibling()
        if sibling is not None and not LABEL_TO_FIELD.get(
            normalize_label(sibling.get_text(" "))
        ):
            add(field, value_text(sibling))

    # 3. Embedded JSON.
    for field, text in extract_json_language_strings(soup):
        add(field, text)

    return fields


def classify_path(path):
    """
    Decide which field a JSON value belongs to, based on its key path.
    """
    joined = " ".join(str(part) for part in path).casefold()
    labels = [normalize_label(str(part)) for part in path]

    for label in reversed(labels):
        if label in LABEL_TO_FIELD:
            return LABEL_TO_FIELD[label]

    if re.search(r"subtit|caption|\bcc\b|sdh", joined):
        return "subtitles"
    if re.search(r"accessib|accesib", joined):
        return "accessibility"
    if "original" in joined and "audio" in joined:
        return "original_audio"
    if "audio" in joined:
        return "audio"

    return None


def extract_json_language_strings(soup):
    """
    Walk every JSON blob in <script> tags and yield (field, text) for values
    that mention a tracked language and live under an audio/subtitle/
    accessibility key (or next to a label such as "Subtítulos").

    Language codes (es-ES) inside such objects are converted to readable
    items, keeping CC/SDH/AD flags.
    """
    results = []

    def walk(node, path):
        if isinstance(node, dict):
            # A sibling string equal to a field label gives context,
            # e.g. {"title": "Subtítulos", "items": [...]}
            context = list(path)
            for value in node.values():
                if isinstance(value, str) and normalize_label(value) in LABEL_TO_FIELD:
                    context.append(value)
                    break

            code_item = json_code_item(node)
            if code_item:
                field = classify_path(context)
                if field:
                    results.append((field, code_item))

            for key, value in node.items():
                walk(value, context + [key])

        elif isinstance(node, list):
            for value in node:
                walk(value, path)

        elif isinstance(node, str):
            text = clean_text(node)
            if len(text) > 2000:
                return
            if not any(language["regex"].search(text) for language in LANGUAGES):
                return

            field = classify_path(path)
            if field:
                results.append((field, text))

    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        content = (content or "").strip()

        if not content or content[0] not in "[{":
            continue

        try:
            data = json.loads(content)
        except ValueError:
            continue

        walk(data, [])

    return results


def json_code_item(node):
    """
    Turn {"language": "es-ES", "isSDH": true} into "Español (España) (SDH)".
    """
    code = None

    for key in ("language", "languageCode", "locale", "lang", "languageTag", "code"):
        value = node.get(key)
        if isinstance(value, str):
            code = value.strip().casefold()
            break

    if not code:
        return None

    for language in LANGUAGES:
        if code not in language["codes"]:
            continue

        flags = []
        for key, value in node.items():
            key_folded = str(key).casefold()
            truthy = value is True or (
                isinstance(value, str)
                and (CC_MARKER.search(value) or AD_MARKER.search(value))
            )

            if not truthy:
                continue

            if re.search(r"sdh|closedcaption|\bcc\b|hearing", key_folded) or (
                isinstance(value, str) and CC_MARKER.search(value)
            ):
                flags.append("SDH")
            elif re.search(r"audiodescri|describ|^(?:is|has)?ad$", key_folded) or (
                isinstance(value, str) and AD_MARKER.search(value)
            ):
                flags.append("AD")

        suffix = "".join(f" ({flag})" for flag in sorted(set(flags)))
        return f"{language['label']}{suffix}"

    return None


def detect_languages(fields):
    """
    Return the human-readable detections for the RSS description.
    """
    detected = []

    audio_items = fields["audio"] + fields["original_audio"]

    for language in LANGUAGES:
        regex = language["regex"]
        label = language["label"]

        has_audio = False
        has_ad = False
        has_cc = False

        for item in audio_items:
            if not regex.search(item):
                continue
            remainder = regex.sub("", item)
            if AD_MARKER.search(remainder):
                has_ad = True
            else:
                has_audio = True

        for item in fields["subtitles"]:
            if regex.search(item) and CC_MARKER.search(regex.sub("", item)):
                has_cc = True

        for item in fields["accessibility"]:
            if not regex.search(item):
                continue
            remainder = regex.sub("", item)
            if AD_MARKER.search(remainder):
                has_ad = True
            if CC_MARKER.search(remainder):
                has_cc = True

        if has_audio:
            detected.append(f"Audio en {label}")
        if has_ad:
            detected.append(f"Audiodescripción en {label}")
        if has_cc:
            detected.append(f"Subtítulos CC/SDH en {label} (doblaje próximo)")

    return detected


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def existing_rss_guids(channel):
    """
    Read identifiers already present in feed.xml.
    """
    guids = set()

    for item in channel.findall("item"):
        guid_element = item.find("guid")

        if guid_element is not None and guid_element.text:
            guids.add(guid_element.text.strip())
            continue

        link_element = item.find("link")

        if link_element is not None and link_element.text:
            guids.add(canonical_tracking_url(link_element.text))

    return guids


def add_rss_item(channel, title, url, provider, detected_types):
    """
    Add one new item to the RSS feed.
    """
    stable_url = canonical_tracking_url(url)

    item = ET.SubElement(channel, "item")

    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = url

    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = stable_url

    ET.SubElement(item, "description").text = (
        f"Proveedor: {provider}. Detectado: "
        + " | ".join(detected_types)
    )

    ET.SubElement(item, "pubDate").text = (
        datetime.now(timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
    )


def write_rss(tree):
    """
    Write feed.xml using UTF-8.
    """
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass

    tree.write(
        RSS_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_page(page_html):
    """
    Analyse one page. Returns (providers, title, fields, detected).
    """
    soup = BeautifulSoup(page_html, "html.parser")

    providers = extract_providers(soup, page_html)
    title = extract_title(soup)
    fields = extract_language_fields(soup)
    detected = detect_languages(fields)

    return providers, title, fields, detected


def main():
    urls = load_links()

    if not urls:
        print("There are no URLs in links.txt.")
        return

    tree, rss = load_or_create_rss()
    channel = rss.find("channel")

    if channel is None:
        raise RuntimeError("The RSS channel could not be created or loaded.")

    known_guids = existing_rss_guids(channel)
    pending_urls = []
    feed_changed = False

    session = requests.Session()

    print(f"Checking {len(urls)} Apple TV URL(s).")

    for position, url in enumerate(urls, start=1):
        print()
        print(f"[{position}/{len(urls)}] Checking: {url}")

        if "tv.apple.com" not in url.lower():
            print("Not an Apple TV URL. It will remain in links.txt.")
            pending_urls.append(url)
            continue

        stable_url = canonical_tracking_url(url)

        if stable_url in known_guids:
            print("This URL is already in feed.xml. Removed from links.txt.")
            continue

        page_html = fetch_url(url, session)

        if page_html is None:
            print("The page could not be downloaded. Kept in links.txt.")
            pending_urls.append(url)
            continue

        providers, title, fields, detected_types = check_page(page_html)

        print(f"Title: {title}")
        print(f"Providers: {', '.join(providers) or '(none found)'}")
        for field, items in fields.items():
            print(f"  {field}: {' | '.join(items) or '-'}")

        if providers and not is_allowed_provider(providers):
            print("The provider is not Crunchyroll. Kept in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not providers:
            print(
                "WARNING: provider not found on the page; "
                "continuing with language detection."
            )

        if not any(fields.values()):
            print("No language information was found. Kept in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not detected_types:
            print("Nothing new detected. Kept in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print("Detected:")
        for detected_type in detected_types:
            print(f"  - {detected_type}")

        add_rss_item(
            channel=channel,
            title=title,
            url=url,
            provider=", ".join(providers) or "Crunchyroll",
            detected_types=detected_types,
        )

        known_guids.add(stable_url)
        feed_changed = True

        print("Added to feed.xml and removed from links.txt.")
        time.sleep(REQUEST_DELAY_SECONDS)

    save_links(pending_urls)

    if feed_changed or not os.path.exists(RSS_FILE):
        write_rss(tree)
        print()
        print("feed.xml was updated.")
    else:
        print()
        print("No new RSS entries were created.")

    print(f"{len(pending_urls)} URL(s) remain in links.txt.")


if __name__ == "__main__":
    main()
