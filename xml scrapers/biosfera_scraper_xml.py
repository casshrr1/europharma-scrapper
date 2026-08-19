import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://biosfera.kz"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
CATALOG_PREFIX = f"{BASE_URL}/ru/catalog/"
OUTPUT_PATH = os.path.join(
    os.path.expanduser("~"), "Downloads", "biosfera_products_list.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# In the original scraper UUID-looking /ru/catalog/... URLs were excluded from
# category traversal. That strongly suggests these URLs are product pages.
PRODUCT_URL_RE = re.compile(
    r"^https://biosfera\.kz/ru/catalog/.*/[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?$",
    re.I,
)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def request(session, url, *, timeout=30, retries=3):
    """GET with a few retries for transient failures."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    raise last_error


def xml_local_name(tag):
    """Return 'loc' from '{namespace}loc'."""
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(session, sitemap_url, visited=None):
    """
    Recursively parse sitemap XML.

    Supports both:
      * <sitemapindex> containing links to other sitemap files
      * <urlset> containing final website URLs
    """
    if visited is None:
        visited = set()

    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)

    print(f"Reading XML sitemap: {sitemap_url}")
    response = request(session, sitemap_url)

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid XML returned by {sitemap_url}: {exc}") from exc

    root_name = xml_local_name(root.tag)
    locs = [
        node.text.strip()
        for node in root.iter()
        if xml_local_name(node.tag) == "loc" and node.text
    ]

    if root_name == "sitemapindex":
        urls = []
        for child_sitemap in locs:
            urls.extend(parse_sitemap(session, child_sitemap, visited))
        return urls

    if root_name == "urlset":
        return locs

    raise RuntimeError(
        f"Unsupported sitemap XML root <{root_name}> at {sitemap_url}"
    )


def get_product_urls_from_xml(session):
    """Collect product URLs directly from sitemap XML."""
    all_urls = parse_sitemap(session, SITEMAP_URL)
    print(f"URLs found in sitemap(s): {len(all_urls)}")

    catalog_urls = {
        url.rstrip("/")
        for url in all_urls
        if url.startswith(CATALOG_PREFIX)
    }

    # First use the UUID pattern inferred from the original script.
    product_urls = sorted(url for url in catalog_urls if PRODUCT_URL_RE.match(url + "/"))

    if product_urls:
        print(f"Product URLs identified by UUID pattern: {len(product_urls)}")
        return product_urls

    # Fallback: sitemap structure may differ.  Return catalogue URLs and let
    # product-page validation decide which ones are actual products.
    print(
        "No UUID-style product URLs were found. "
        "Falling back to validating all /ru/catalog/ URLs."
    )
    return sorted(catalog_urls)


def text_or_none(element):
    if element is None:
        return None
    text = element.get_text(" ", strip=True)
    return text or None


def find_field(soup, label):
    """
    Locate a value near a Russian product-property label.

    This intentionally tries several common HTML structures because the old
    Selenium XPath depended on rendered text rather than a stable class name.
    """
    label_re = re.compile(rf"^{re.escape(label)}\s*:?$", re.I)

    label_node = soup.find(string=lambda s: bool(s and label_re.match(s.strip())))
    if not label_node:
        return None

    element = label_node.parent

    # Definition list: <dt>Manufacturer</dt><dd>...</dd>
    if element.name == "dt":
        dd = element.find_next_sibling("dd")
        if dd:
            return text_or_none(dd)

    # Table: <tr><td>Label</td><td>Value</td></tr>
    row = element.find_parent("tr")
    if row:
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            for i, cell in enumerate(cells[:-1]):
                if label_re.match(cell.get_text(" ", strip=True)):
                    return text_or_none(cells[i + 1])

    # Common two-column wrappers.
    parent = element.parent
    if parent:
        children = [c for c in parent.find_all(recursive=False) if getattr(c, "name", None)]
        if len(children) >= 2:
            for i, child in enumerate(children[:-1]):
                if label_re.match(child.get_text(" ", strip=True)):
                    return text_or_none(children[i + 1])

    # Last-resort nearby element, similar in spirit to the original XPath.
    next_element = element.find_next()
    while next_element and next_element is not element:
        value = text_or_none(next_element)
        if value and not label_re.match(value):
            return value
        next_element = next_element.find_next()

    return None


def get_categories(soup):
    crumbs = []

    for element in soup.select(".breadcrumbs__item"):
        text = element.get_text(" ", strip=True)
        if text and text != "Каталог":
            crumbs.append(text)

    # Usually final breadcrumb is the product itself. Keep only the first two
    # hierarchy levels to match the output of the Selenium version.
    cat1 = crumbs[0] if len(crumbs) > 0 else None
    cat2 = crumbs[1] if len(crumbs) > 1 else None
    return cat1, cat2


def get_price(soup):
    # Try likely price elements first.
    candidates = soup.select(
        "[class*='price'], [class*='Price'], .product__price, .product-price"
    )

    # Fall back to elements occurring after the title.
    title = soup.select_one("h1.product__title")
    if title:
        candidates.extend(title.find_all_next(limit=80))

    seen = set()
    for element in candidates:
        if id(element) in seen:
            continue
        seen.add(id(element))

        value = element.get_text(" ", strip=True)
        if (
            "₸" in value
            and re.search(r"\d", value)
            and "Цена действует" not in value
            and len(value) < 150
        ):
            return value

    return None


def scrape_product(session, url):
    response = request(session, url)
    soup = BeautifulSoup(response.text, "html.parser")

    title = soup.select_one("h1.product__title")
    if title is None:
        # In fallback mode this rejects category URLs.
        return None

    cat1, cat2 = get_categories(soup)

    return {
        "category": cat1,
        "subcategory": cat2,
        "group": None,
        "name": text_or_none(title),
        "company": find_field(soup, "Производитель"),
        "country": find_field(soup, "Страна производитель"),
        "price": get_price(soup),
        "url": url,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def scrape(session, max_products=None, delay=0.25):
    product_urls = get_product_urls_from_xml(session)

    if max_products is not None:
        product_urls = product_urls[:max_products]

    data = []
    total = len(product_urls)

    for idx, url in enumerate(product_urls, 1):
        print(f"[{idx}/{total}] Scraping {url}")
        try:
            product = scrape_product(session, url)
            if product:
                data.append(product)
            else:
                print("  Skipped: not recognised as a product page")
        except Exception as exc:
            print(f"  ERROR: {exc}")

        if delay:
            time.sleep(delay)

    return data


def main():
    session = make_session()
    products = scrape(session)

    pd.DataFrame(products).to_csv(
        OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print(f"Saved {len(products)} products to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
