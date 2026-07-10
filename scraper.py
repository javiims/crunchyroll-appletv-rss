import html as html_module
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

    with open(LINKS_FILE, "r", encoding="utf-8") as file:
        for line in file:
            url = line.strip()

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


def normalize_whitespace(text):
    """
    Replace repeated spaces, tabs and line breaks with one space.
    """
    return re.sub(r"\s+", " ", text).strip()


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


def fetch_url(url, session):
    """
    Download an Apple TV page and decode it explicitly as UTF-8.

    Apple TV pages declare UTF-8 in their HTML, but requests may sometimes
    infer an incorrect encoding. Using response.content.decode("utf-8")
    prevents text such as "Español" from becoming "EspaÃ±ol".
    """
    try:
        response = session.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        response.raise_for_status()

        try:
            page_html = response.content.decode("utf-8")
        except UnicodeDecodeError:
            page_html = response.content.decode(
                "utf-8",
                errors="replace",
            )

        print(
            f"Downloaded: {url} "
            f"(HTTP {response.status_code}, "
            f"{len(page_html)} characters)"
        )

        return page_html

    except requests.RequestException as error:
        print(f"ERROR downloading {url}: {error}")
        return None


def extract_provider(html):
    """
    Extract the content provider from Apple TV.

    In the attached examples, Apple marks the provider using:
    aria-label="Crunchyroll"
    """
    soup = BeautifulSoup(html, "html.parser")

    provider_element = soup.find(
        attrs={"data-testid": "content-provider-logo-coin"}
    )

    if provider_element:
        provider = provider_element.get("aria-label")

        if provider:
            return normalize_whitespace(provider)

    provider_match = re.search(
        r'data-testid=["\']content-provider-logo-coin["\'][^>]*'
        r'aria-label=[^"\']+["\']',
        html,
        flags=re.IGNORECASE,
    )

    if not provider_match:
        provider_match = re.search(
            r'aria-label=[^"\']+["\'][^>]*'
            r'data-testid=["\']content-provider-logo-coin["\']',
            html,
            flags=re.IGNORECASE,
        )

    if provider_match:
        return normalize_whitespace(
            html_module.unescape(provider_match.group(1))
        )

    return None


def extract_title(html):
    """
    Extract a readable title.

    Preference:
    1. Apple structured-data episode name
    2. Apple title metadata
    3. HTML title
    """
    soup = BeautifulSoup(html, "html.parser")

    schema_element = soup.find(
        "script",
        attrs={"id": "schema:tv-episode"},
    )

    if schema_element and schema_element.string:
        name_match = re.search(
            r'"name"\s*:\s*"((?:\\.|[^"\\])*)"',
            schema_element.string,
            flags=re.IGNORECASE,
        )

        if name_match:
            title = name_match.group(1)
            title = title.replace("\\/", "/")
            title = title.replace('\\"', '"')
            title = title.replace("\\n", " ")
            title = html_module.unescape(title)

            return normalize_whitespace(title)

    apple_title = soup.find("meta", attrs={"name": "apple:title"})

    if apple_title and apple_title.get("content"):
        return normalize_whitespace(
            html_module.unescape(apple_title["content"])
        )

    if soup.title and soup.title.string:
        title = normalize_whitespace(
            html_module.unescape(soup.title.string)
        )

        title = re.sub(
            r"\s*-\s*Apple\s*TV(?:\s*\([^)]*\))?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )

        title = title.lstrip("‎").strip()

        if title:
            return title

    return "Título desconocido"


def extract_audio_text(html):
    """
    Extract only the Apple TV Audio field.

    This intentionally ignores:
    - Audio original
    - Subtítulos
    - Accesibilidad

    This distinction is essential because Spanish and Italian subtitles
    do not prove that Spanish or Italian dubbed audio exists.
    """
    soup = BeautifulSoup(html, "html.parser")

    for definition_title in soup.find_all("dt"):
        field_name = normalize_whitespace(
            definition_title.get_text(" ", strip=True)
        )

        if field_name.casefold() != "audio":
            continue

        definition_content = definition_title.find_next_sibling("dd")

        if definition_content is None:
            continue

        audio_text = normalize_whitespace(
            definition_content.get_text(" ", strip=True)
        )

        if audio_text:
            return audio_text

    fallback_match = re.search(
        r"<dt[^>]*>\s*Audio\s*</dt>\s*"
        r"<dd[^>]*>(.*?)</dd>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if fallback_match:
        fallback_soup = BeautifulSoup(
            fallback_match.group(1),
            "html.parser",
        )

        return normalize_whitespace(
            fallback_soup.get_text(" ", strip=True)
        )

    return ""


def detect_audio_languages(audio_text):
    """
    Detect Spanish (Spain) and Italian (Italy) AAC audio.

    Audio is detected only when the complete language-and-codec expression
    appears in the Audio field.
    """
    detected = []

    spanish_pattern = (
        r"Español\s*\(\s*España\s*\)\s*"
        r"\(\s*AAC\s*\)"
    )
    italian_pattern = (
        r"Italiano\s*\(\s*Italia\s*\)\s*"
        r"\(\s*AAC\s*\)"
    )

    if re.search(spanish_pattern, audio_text, flags=re.IGNORECASE):
        detected.append("Audio en Español (España) (AAC)")

    if re.search(italian_pattern, audio_text, flags=re.IGNORECASE):
        detected.append("Audio en Italiano (Italia) (AAC)")

    return detected


def existing_rss_guids(channel):
    """
    Read identifiers already present in feed.xml.

    This prevents duplicate RSS entries if a link is accidentally added
    to links.txt more than once.
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
            print("This URL is already in feed.xml.")
            print("It will be removed from links.txt.")
            continue

        page_html = fetch_url(url, session)

        if page_html is None:
            print("The page could not be downloaded.")
            print("The URL will remain in links.txt.")
            pending_urls.append(url)
            continue

        provider = extract_provider(page_html)

        if not provider:
            print("The content provider could not be identified.")
            print("The URL will remain in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"Provider: {provider}")

        if provider.casefold() != "crunchyroll":
            print("The provider is not Crunchyroll.")
            print("The URL will remain in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        audio_text = extract_audio_text(page_html)

        if not audio_text:
            print("The Apple TV Audio field was not found.")
            print("The URL will remain in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"Audio field: {audio_text}")

        detected_types = detect_audio_languages(audio_text)

        if not detected_types:
            print(
                "No Spanish (Spain) or Italian (Italy) AAC audio "
                "was detected."
            )
            print("The URL will remain in links.txt.")
            pending_urls.append(url)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        title = extract_title(page_html)

        print(f"Title: {title}")
        print("Detected:")
        for detected_type in detected_types:
            print(f"  - {detected_type}")

        add_rss_item(
            channel=channel,
            title=title,
            url=url,
            provider=provider,
            detected_types=detected_types,
        )

        known_guids.add(stable_url)
        feed_changed = True

        print("Added to feed.xml.")
        print("The URL will be removed from links.txt.")

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
