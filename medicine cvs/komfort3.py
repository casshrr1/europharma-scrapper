import re
import time
from datetime import datetime
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_URL = "https://komfort.kz"
CATEGORY_URL = "https://komfort.kz/catalog/stroymaterialy/"
TOTAL_PAGES = 137

HEADERS = {"User-Agent": "Mozilla/5.0"}

session = requests.Session()
session.headers.update(HEADERS)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def get_soup(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_price(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def get_all_product_links():
    links = []
    seen_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page_obj = browser.new_page(user_agent=HEADERS["User-Agent"])

        for page_num in range(1, TOTAL_PAGES + 1):
            url = CATEGORY_URL if page_num == 1 else f"{CATEGORY_URL}?PAGEN_1={page_num}"
            print("URL:", url)

            page_obj.goto(url, wait_until="networkidle", timeout=60000)
            page_obj.wait_for_timeout(5000)

            soup = BeautifulSoup(page_obj.content(), "html.parser")

            page_links = []

            for a in soup.select("a[href]"):
                href = urljoin(BASE_URL, a.get("href"))

                if re.search(r"/catalog/.+/\d+/$", href):
                    if href not in seen_links:
                        page_links.append(href)
                        seen_links.add(href)

            print(f"Page {page_num}: {len(page_links)} new products")
            links.extend(page_links)

            time.sleep(0.5)

        browser.close()

    return links


def get_property_value(soup, property_name):
    for item in soup.select(".properties__item"):
        title_tag = item.select_one(".properties__title")
        value_tag = item.select_one(".properties__value")

        title = clean(title_tag.get_text(" ")) if title_tag else ""
        value = clean(value_tag.get_text(" ")) if value_tag else ""

        if property_name.lower() in title.lower():
            return value

    return None


def get_price(soup):
    meta_price = soup.select_one('meta[itemprop="price"]')
    if meta_price and meta_price.get("content"):
        return int(float(meta_price["content"]))

    price_div = soup.select_one(".price[data-value]")
    if price_div and price_div.get("data-value"):
        return int(float(price_div["data-value"]))

    price_value = soup.select_one(".price_value")
    if price_value:
        return parse_price(price_value.get_text())

    return None


def parse_product(url):
    soup = get_soup(url)

    name_tag = soup.select_one('meta[itemprop="name"]')
    name = name_tag.get("content") if name_tag else None

    if not name:
        h1 = soup.select_one("h1")
        name = clean(h1.get_text()) if h1 else None

    price = get_price(soup)
    country = get_property_value(soup, "Страна производитель")

    category = None
    subcategory = None
    sub_subcategory = None

    category_meta = soup.select_one('meta[itemprop="category"]')
    if category_meta and category_meta.get("content"):
        parts = [clean(x) for x in category_meta["content"].split("/")]

        if len(parts) > 0:
            category = parts[0]
        if len(parts) > 1:
            subcategory = parts[1]
        if len(parts) > 2:
            sub_subcategory = parts[2]

    return {
        "scraping_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "link": url,
        "name": name,
        "categorie": category,
        "subcategorie": subcategory,
        "sub_subcategorie": sub_subcategory,
        "price": price,
        "country": country,
    }


def safe_parse_product(link):
    try:
        time.sleep(0.05)
        return parse_product(link)
    except Exception as e:
        print(f"Error: {link} -> {e}")
        return None


def main():
    all_rows = []

    print(f"\nScraping first {TOTAL_PAGES} pages of Стройматериалы")

    product_links = get_all_product_links()
    print(f"\nFound {len(product_links)} unique product links")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(safe_parse_product, link) for link in product_links]

        for i, future in enumerate(as_completed(futures), start=1):
            row = future.result()

            if row:
                all_rows.append(row)

            if i % 20 == 0:
                print(f"{i}/{len(product_links)} products processed")

            if len(all_rows) > 0 and len(all_rows) % 100 == 0:
                pd.DataFrame(all_rows).to_csv(
                    "komfort_stroymaterialy_137_pages_backup.csv",
                    index=False,
                    encoding="utf-8-sig"
                )

    df = pd.DataFrame(all_rows)

    df.to_excel("komfort_stroymaterialy_137_pages.xlsx", index=False)
    df.to_csv("komfort_stroymaterialy_137_pages.csv", index=False, encoding="utf-8-sig")

    print(f"\nDONE. Scraped {len(df)} products.")
    print("Saved: komfort_stroymaterialy_137_pages.xlsx")
    print("Saved: komfort_stroymaterialy_137_pages.csv")


if __name__ == "__main__":
    main()