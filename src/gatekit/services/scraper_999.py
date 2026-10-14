"""999.md classifieds scraper and parser.

Uses httpx and BeautifulSoup4 to extract new ads from 999.md category and search URLs.
Extracts: Ad ID, Title, Price, URL.
Supports both classic/HTML card layouts (via BeautifulSoup) and modern dynamic endpoints (via GraphQL fallback).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

BASE_URL = "https://999.md"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Matches ad ID in links like /ru/12345678, /ro/12345678, /ru/booster/12345678, /12345678
# Ad IDs on 999.md are at least 5 digits (e.g. 10001, 88123456, 105633915).
AD_ID_RE = re.compile(r"/(?:(?:ru|ro)/)?(?:booster/)?(\d{5,12})(?:[/?#]|$)")
PRICE_RE = re.compile(
    r"(\d+[\s\d]*\s*(?:MDL|EUR|USD|€|\$|lei|лей|евро|долл|y\.e\.|у\.е\.))",
    re.IGNORECASE,
)


def _is_valid_999_host(netloc: str) -> bool:
    """Return True if netloc belongs to 999.md or is relative (empty)."""
    if not netloc:
        return True
    host = netloc.split(":")[0].lower()
    return host == "999.md" or host.endswith(".999.md")


@dataclass(frozen=True, slots=True)
class ScrapedAd:
    """One classified ad parsed from 999.md."""

    id: str
    title: str
    price: str
    url: str


def _clean_text(val: str | None) -> str:
    if not val:
        return ""
    # Normalize unicode non-breaking spaces and redundant whitespace
    cleaned = val.replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", cleaned)


def _extract_ad_id(tag: Tag, href: str = "") -> str | None:
    for attr in ("data-ad-id", "data-id", "item-id"):
        raw_val = tag.get(attr)
        if raw_val and str(raw_val).isdigit() and len(str(raw_val)) >= 5:
            return str(raw_val)

    if href:
        parsed = urlparse(href)
        if _is_valid_999_host(parsed.netloc):
            match = AD_ID_RE.search(parsed.path)
            if match:
                return match.group(1)

    return None


def _format_price_val(raw_price: object) -> str:
    if isinstance(raw_price, dict):
        val = raw_price.get("value")
        unit = raw_price.get("unit") or raw_price.get("measurement") or ""
        unit_str = str(unit).replace("UNIT_", "")
        if val is not None:
            return f"{val} {unit_str}".strip()
    if isinstance(raw_price, (int, float, str)):
        return _clean_text(str(raw_price))
    return "Договорная"


def parse_ads_page(html: str, base_url: str = BASE_URL) -> list[ScrapedAd]:
    """Parse HTML from 999.md into a list of ScrapedAd objects using BeautifulSoup4.

    Robust to multiple layout versions (photo view, detail view, table view,
    card divs, and unstructured links).
    """
    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    ads: list[ScrapedAd] = []
    seen_ids: set[str] = set()

    # Strategy 1: Look for standard 999.md card container elements
    card_selectors = [
        ".ads-list-photo-item",
        ".ads-list-detail-item",
        ".ads-list-table-item",
        ".ads-list-item",
        "[data-ad-id]",
        ".ad-card",
        "li.ad-item",
        "div.ad-item",
    ]
    cards = soup.select(", ".join(card_selectors))

    for card in cards:
        # 1. Find link and ID
        link_elem = (
            card.select_one("a[href*='/ru/'], a[href*='/ro/'], a.ads-list-photo-item-title, a.title")
            or card.find("a")
        )
        href = link_elem.get("href", "") if link_elem else ""
        ad_id = _extract_ad_id(card, href)
        if not ad_id and link_elem:
            ad_id = _extract_ad_id(link_elem, href)

        if not ad_id or ad_id in seen_ids:
            continue

        # 2. Find title
        title_elem = (
            card.select_one(".ads-list-photo-item-title, .ads-list-detail-item-title, .title, h3, h4")
            or link_elem
        )
        title = ""
        if title_elem:
            title = _clean_text(title_elem.get("title") or title_elem.get_text())
        if not title and link_elem:
            title = _clean_text(link_elem.get_text())
        if not title:
            title = f"Объявление #{ad_id}"

        # 3. Find price
        price_elem = card.select_one(
            ".ads-list-photo-item-price, .ads-list-detail-item-price, .price, .item-price, [data-price]"
        )
        price = ""
        if price_elem:
            price = _clean_text(price_elem.get_text())
        if not price:
            match = PRICE_RE.search(card.get_text())
            if match:
                price = _clean_text(match.group(1))
        if not price:
            price = "Договорная"

        # 4. Construct URL
        full_url = urljoin(base_url, href) if href else f"{base_url}/ru/{ad_id}"

        seen_ids.add(ad_id)
        ads.append(ScrapedAd(id=ad_id, title=title, price=price, url=full_url))

    # Strategy 2: If no structured cards matched, scan candidate 999.md links
    if not ads:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            parsed_href = urlparse(href)
            if not _is_valid_999_host(parsed_href.netloc):
                continue
            match = AD_ID_RE.search(parsed_href.path)
            if not match:
                continue
            ad_id = match.group(1)
            if not ad_id or ad_id in seen_ids:
                continue

            title = _clean_text(a.get("title") or a.get_text())
            if not title or len(title) < 3 or title.isdigit():
                parent = a.find_parent(["div", "li", "tr"])
                if parent:
                    title_elem = parent.select_one("h3, h4, .title, .header")
                    if title_elem:
                        title = _clean_text(title_elem.get_text())
            if not title:
                title = f"Объявление #{ad_id}"

            # Look for price in container
            price = "Договорная"
            parent = a.find_parent(["div", "li", "tr"])
            if parent:
                price_elem = parent.select_one(".price, [class*='price']")
                if price_elem:
                    price = _clean_text(price_elem.get_text())
                else:
                    pm = PRICE_RE.search(parent.get_text())
                    if pm:
                        price = _clean_text(pm.group(1))

            full_url = urljoin(base_url, href)
            seen_ids.add(ad_id)
            ads.append(ScrapedAd(id=ad_id, title=title, price=price, url=full_url))

    return ads


def _extract_category_path(url: str) -> str | None:
    """Extract category path from 999.md URL like /ru/list/real-estate/apartments-and-rooms."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    # e.g. ru/list/real-estate/apartments-and-rooms -> real-estate/apartments-and-rooms
    match = re.search(r"(?:ru|ro)/list/(.+)$", path)
    if match:
        return match.group(1)
    match = re.search(r"list/(.+)$", path)
    if match:
        return match.group(1)
    return None


async def fetch_ads_graphql(
    client: httpx.AsyncClient,
    url: str,
    graphql_url: str = "https://999.md/graphql",
    limit: int = 20,
) -> list[ScrapedAd]:
    """Fallback fetcher for 999.md Next.js/React endpoints via public GraphQL API."""
    cat_path = _extract_category_path(url)
    query_str: str | None = None
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    if "query" in query_params:
        query_str = query_params["query"][0]

    cat_id: int | None = None
    if cat_path:
        # Resolve category ID
        cat_query = """
        query GetCat($input: GetCategoryRequestInput!) {
          category(input: $input) {
            id
            url
          }
        }
        """
        try:
            r = await client.post(
                graphql_url,
                json={"query": cat_query, "variables": {"input": {"url": cat_path}}},
                timeout=10.0,
            )
            if r.status_code == 200:
                data = r.json()
                cid = data.get("data", {}).get("category", {}).get("id")
                try:
                    cid_int = int(cid) if cid is not None else None
                except (ValueError, TypeError):
                    cid_int = None
                if cid_int and cid_int > 0:
                    cat_id = cid_int
        except Exception as exc:
            log.warning("GraphQL category resolution failed for %s: %s", cat_path, exc)

    # Search query
    search_query = """
    query SearchAds($input: Ads_SearchInput!) {
      searchAds(input: $input) {
        count
        ads {
          id
          title
          price: feature(id: 2) {
            id
            value
          }
        }
      }
    }
    """
    search_input: dict[str, object] = {
        "sort": "SORT_ADS_DATE_DESC",
        "pagination": {"skip": 0, "limit": limit},
    }
    if cat_id is not None:
        search_input["subCategoryId"] = cat_id
    if query_str:
        search_input["query"] = query_str

    try:
        r = await client.post(
            graphql_url,
            json={"query": search_query, "variables": {"input": search_input}},
            timeout=10.0,
        )
        if r.status_code != 200:
            log.warning("GraphQL searchAds returned status %s: %s", r.status_code, r.text[:200])
            return []

        data = r.json()
        raw_ads = data.get("data", {}).get("searchAds", {}).get("ads", [])
        ads: list[ScrapedAd] = []
        for item in raw_ads:
            ad_id = str(item.get("id"))
            title = _clean_text(item.get("title")) or f"Объявление #{ad_id}"
            price_val = item.get("price", {}).get("value") if item.get("price") else None
            price_str = _format_price_val(price_val)
            ad_url = f"{BASE_URL}/ru/{ad_id}"
            ads.append(ScrapedAd(id=ad_id, title=title, price=price_str, url=ad_url))
        return ads
    except Exception as exc:
        log.warning("GraphQL searchAds failed: %s", exc)
        return []


def ensure_sort_by_date(url: str) -> str:
    """Ensure category URL has sort_type=date_desc to guarantee monitoring newest ads."""
    parsed = urlparse(url)
    if not parsed.netloc or not _is_valid_999_host(parsed.netloc):
        return url
    from urllib.parse import parse_qsl, urlencode, urlunparse

    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "sort_type" not in query_params:
        query_params["sort_type"] = "date_desc"
        if "applied" not in query_params:
            query_params["applied"] = "1"
        new_query = urlencode(query_params)
        return urlunparse(parsed._replace(query=new_query))
    return url


async def fetch_latest_ads(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
) -> list[ScrapedAd]:
    """Fetch the given 999.md category or search URL and return parsed ads.

    First attempts HTML parsing with BeautifulSoup4. If no cards are found
    (due to client-side hydration), seamlessly falls back to GraphQL.
    """
    target_url = ensure_sort_by_date(url)
    req_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,ro;q=0.8,en;q=0.7",
    }
    if headers:
        req_headers.update(headers)

    try:
        response = await client.get(target_url, headers=req_headers, follow_redirects=True, timeout=15.0)
    except httpx.HTTPError as exc:
        log.warning("Failed to fetch 999.md URL %s: %s", target_url, exc)
        return []

    if response.status_code != 200:
        log.warning("999.md returned HTTP %s for %s", response.status_code, target_url)
        return []

    ads = parse_ads_page(response.text, base_url=str(response.url))
    if ads:
        return ads

    # If HTML returned empty card list and the host is 999.md, use GraphQL
    parsed = urlparse(target_url)
    if "999.md" in parsed.netloc:
        return await fetch_ads_graphql(client, target_url)

    return []
