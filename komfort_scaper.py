import asyncio
import csv
import logging
import random
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup

BASE_URL = "https://komfort.kz"
CATALOG_URL = f"{BASE_URL}/catalog/"
CONCURRENCY = 12
MAX_RETRIES = 4
REQUEST_TIMEOUT = 45
BACKUP_EVERY = 250

OUTPUT_DIR = Path(__file__).resolve().parent
OUTPUT_CSV = OUTPUT_DIR / "komfort_all_products.csv"
OUTPUT_XLSX = OUTPUT_DIR / "komfort_all_products.xlsx"
BACKUP_CSV = OUTPUT_DIR / "komfort_all_products_backup.csv"
ERROR_CSV = OUTPUT_DIR / "komfort_all_products_errors.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

SPACE_RE = re.compile(r"\s+")
NON_DIGIT_RE = re.compile(r"[^\d]")
PRODUCT_RE = re.compile(r"^/catalog/[^/]+/.+/\d+/?$", re.I)
TOP_CATEGORY_RE = re.compile(r"^/catalog/[^/]+/?$", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("komfort")


@dataclass(frozen=True)
class Category:
    name: str
    slug: str
    url: str


@dataclass
class Product:
    scraping_date: str
    link: str
    name: Optional[str]
    main_category: Optional[str]
    category: Optional[str]
    subcategory: Optional[str]
    sub_subcategory: Optional[str]
    price: Optional[int]
    country: Optional[str]


def clean(value: object) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_price(value: object) -> Optional[int]:
    digits = NON_DIGIT_RE.sub("", clean(value))
    return int(digits) if digits else None


def meta_content(soup: BeautifulSoup, itemprop: str) -> Optional[str]:
    node = soup.select_one(f'meta[itemprop="{itemprop}"][content]')
    value = clean(node.get("content")) if node else ""
    return value or None


def property_value(soup: BeautifulSoup, wanted_name: str) -> Optional[str]:
    wanted = clean(wanted_name).casefold()
    for item in soup.select(".properties__item"):
        title_node = item.select_one(".properties__title")
        value_node = item.select_one(".properties__value")
        title = clean(title_node.get_text(" ") if title_node else "").casefold()
        if wanted in title:
            value = clean(value_node.get_text(" ") if value_node else "")
            return value or None
    return None


def get_price(soup: BeautifulSoup) -> Optional[int]:
    value = meta_content(soup, "price")
    if value:
        try:
            return round(float(value.replace(" ", "").replace(",", ".")))
        except ValueError:
            pass

    node = soup.select_one(".price[data-value]")
    if node:
        try:
            return round(float(clean(node.get("data-value")).replace(" ", "").replace(",", ".")))
        except ValueError:
            pass

    for selector in (".price_value", ".price__value", "[itemprop='price']"):
        node = soup.select_one(selector)
        if node:
            price = parse_price(node.get_text(" "))
            if price is not None:
                return price
    return None


def parse_category_chain(soup: BeautifulSoup) -> list[str]:
    value = meta_content(soup, "category")
    if value:
        return [part for part in (clean(x) for x in value.split("/")) if part]

    for selector in (".breadcrumbs a", ".breadcrumb a", "[itemprop='itemListElement'] a"):
        nodes = soup.select(selector)
        if nodes:
            parts = [clean(node.get_text(" ")) for node in nodes]
            return [x for x in parts if x and x.casefold() not in {"главная", "каталог"}]
    return []


def parse_product(url: str, html: str, main_category: str, scraping_date: str) -> Product:
    soup = BeautifulSoup(html, "html.parser")
    name = meta_content(soup, "name")
    if not name:
        h1 = soup.select_one("h1")
        name = clean(h1.get_text(" ") if h1 else "") or None
    if not name:
        raise ValueError("Название товара не найдено; возможно, CAPTCHA или изменилась верстка")

    chain = parse_category_chain(soup)
    # Не дублируем верхнюю категорию, если она уже первая в цепочке.
    if chain and chain[0].casefold() == main_category.casefold():
        chain = chain[1:]

    return Product(
        scraping_date=scraping_date,
        link=url,
        name=name,
        main_category=main_category,
        category=chain[0] if len(chain) > 0 else None,
        subcategory=chain[1] if len(chain) > 1 else None,
        sub_subcategory=chain[2] if len(chain) > 2 else None,
        price=get_price(soup),
        country=property_value(soup, "Страна производитель"),
    )


async def retry_delay(attempt: int, retry_after: Optional[str] = None) -> None:
    delay = min(2 ** attempt, 20) + random.uniform(0.2, 0.9)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), 60))
        except ValueError:
            pass
    await asyncio.sleep(delay)


async def fetch_html(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, url: str, label: str) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status == 200:
                        html = await response.text(errors="replace")
                        if len(html) < 500:
                            raise ValueError(f"Слишком короткий HTML: {len(html)}")
                        return html
                    if response.status in {429, 500, 502, 503, 504}:
                        last_error = RuntimeError(f"HTTP {response.status}")
                        log.warning("%s | попытка %s/%s | HTTP %s | %s", label, attempt, MAX_RETRIES, response.status, url)
                        await retry_delay(attempt, response.headers.get("Retry-After"))
                        continue
                    response.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            last_error = error
            log.warning("%s | попытка %s/%s | %s | %s", label, attempt, MAX_RETRIES, error, url)
            if attempt < MAX_RETRIES:
                await retry_delay(attempt)
    raise RuntimeError(f"Не удалось загрузить после {MAX_RETRIES} попыток: {last_error}")


async def discover_categories(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> list[Category]:
    html = await fetch_html(session, semaphore, CATALOG_URL, "Главный каталог")
    soup = BeautifulSoup(html, "html.parser")
    categories: dict[str, Category] = {}

    for node in soup.select("a[href]"):
        absolute = normalize_url(urljoin(BASE_URL, clean(node.get("href"))))
        path = urlsplit(absolute).path
        if not TOP_CATEGORY_RE.fullmatch(path) or path == "/catalog/":
            continue
        name = clean(node.get_text(" "))
        if not name:
            continue
        slug = path.strip("/").split("/")[1]
        categories[slug] = Category(name=name, slug=slug, url=absolute)

    if not categories:
        raise RuntimeError("На странице /catalog/ не найдены верхние категории")
    result = sorted(categories.values(), key=lambda x: x.name)
    log.info("Найдено верхних категорий: %s", len(result))
    for item in result:
        log.info("Категория | %s | %s", item.name, item.url)
    return result


def extract_product_links(html: str, category_slug: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for node in soup.select("a[href]"):
        absolute = normalize_url(urljoin(BASE_URL, clean(node.get("href"))))
        path = urlsplit(absolute).path
        if PRODUCT_RE.fullmatch(path) and path.startswith(f"/catalog/{category_slug}/"):
            links.add(absolute)
    return links


def extract_last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = {1}
    for node in soup.select('a[href*="PAGEN_1="]'):
        query = parse_qs(urlsplit(urljoin(BASE_URL, clean(node.get("href")))).query)
        for value in query.get("PAGEN_1", []):
            if value.isdigit():
                pages.add(int(value))
    return max(pages)


async def collect_category_links(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    category: Category,
) -> tuple[Category, set[str], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    first_html = await fetch_html(session, semaphore, category.url, f"Категория {category.name}")
    last_page = extract_last_page(first_html)
    links = extract_product_links(first_html, category.slug)
    log.info("%s | страниц: %s | первая страница: %s товаров", category.name, last_page, len(links))

    async def get_page(page_number: int) -> set[str]:
        url = f"{category.url}?PAGEN_1={page_number}"
        html = await fetch_html(session, semaphore, url, f"{category.name}, стр. {page_number}/{last_page}")
        page_links = extract_product_links(html, category.slug)
        if not page_links:
            raise ValueError("Товары на странице не найдены")
        log.info("%s | страница %s/%s | товаров: %s", category.name, page_number, last_page, len(page_links))
        return page_links

    tasks = {asyncio.create_task(get_page(page)): page for page in range(2, last_page + 1)}
    for task, page in list(tasks.items()):
        try:
            links.update(await task)
        except Exception as error:
            log.error("ОШИБКА КАТАЛОГА | %s | страница %s | %s", category.name, page, error)
            errors.append({"stage": "catalog_page", "category": category.name, "link": f"{category.url}?PAGEN_1={page}", "error": repr(error)})

    log.info("%s | уникальных товаров: %s", category.name, len(links))
    return category, links, errors


async def discover_all_product_links(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    categories: list[Category],
) -> tuple[dict[str, Category], list[dict[str, str]]]:
    product_map: dict[str, Category] = {}
    errors: list[dict[str, str]] = []

    # Категории обрабатываются последовательно, страницы внутри категории — параллельно.
    # Так сайт получает контролируемую нагрузку.
    for index, category in enumerate(categories, 1):
        log.info("=== Категория %s/%s: %s ===", index, len(categories), category.name)
        try:
            _, links, category_errors = await collect_category_links(session, semaphore, category)
            errors.extend(category_errors)
            for link in links:
                product_map.setdefault(link, category)
        except Exception as error:
            log.error("ОШИБКА КАТЕГОРИИ | %s | %s", category.name, error)
            errors.append({"stage": "category", "category": category.name, "link": category.url, "error": repr(error)})

    return product_map, errors


def save_products(products: list[Product], path: Path) -> None:
    pd.DataFrame([asdict(x) for x in products]).to_csv(path, index=False, encoding="utf-8-sig")


async def collect_products(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    product_map: dict[str, Category],
    scraping_date: str,
) -> tuple[list[Product], list[dict[str, str]]]:
    queue: asyncio.Queue[tuple[str, Category]] = asyncio.Queue()
    for item in product_map.items():
        queue.put_nowait(item)

    products: list[Product] = []
    errors: list[dict[str, str]] = []
    lock = asyncio.Lock()
    progress = 0
    total = len(product_map)

    async def worker() -> None:
        nonlocal progress
        while True:
            try:
                url, category = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                html = await fetch_html(session, semaphore, url, "Товар")
                product = parse_product(url, html, category.name, scraping_date)
                async with lock:
                    products.append(product)
                    progress += 1
                    log.info("Товар %s/%s | OK | %s | %s", progress, total, category.name, product.name)
                    if len(products) % BACKUP_EVERY == 0:
                        save_products(products, BACKUP_CSV)
                        log.info("BACKUP | сохранено %s товаров", len(products))
            except Exception as error:
                async with lock:
                    progress += 1
                    log.error("ОШИБКА ТОВАРА %s/%s | %s | %s | %s", progress, total, category.name, url, error)
                    errors.append({"stage": "product", "category": category.name, "link": url, "error": repr(error)})
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(min(CONCURRENCY, total))]
    await queue.join()
    await asyncio.gather(*workers)
    return products, errors


def save_errors(errors: list[dict[str, str]]) -> None:
    with ERROR_CSV.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["stage", "category", "link", "error"])
        writer.writeheader()
        writer.writerows(errors)


def save_final(products: list[Product]) -> None:
    products.sort(key=lambda x: (x.main_category or "", x.category or "", x.subcategory or "", x.name or ""))
    frame = pd.DataFrame([asdict(x) for x in products])
    frame.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Все товары", index=False)
        summary = (
            frame.groupby("main_category", dropna=False)
            .size()
            .reset_index(name="products_count")
            .sort_values("main_category")
        )
        summary.to_excel(writer, sheet_name="Категории", index=False)


async def main() -> None:
    started = datetime.now()
    scraping_date = started.strftime("%Y-%m-%d %H:%M:%S")
    log.info("Старт полного каталога: %s", CATALOG_URL)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15, sock_read=35)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=CONCURRENCY, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout, connector=connector) as session:
        categories = await discover_categories(session, semaphore)
        product_map, catalog_errors = await discover_all_product_links(session, semaphore, categories)
        log.info("Всего уникальных ссылок на товары: %s", len(product_map))
        if not product_map:
            save_errors(catalog_errors)
            raise RuntimeError("Не найдено ни одной ссылки на товар")
        products, product_errors = await collect_products(session, semaphore, product_map, scraping_date)

    errors = catalog_errors + product_errors
    save_final(products)
    save_errors(errors)
    log.info("=" * 70)
    log.info("ГОТОВО | товаров: %s | ошибок: %s | время: %s", len(products), len(errors), datetime.now() - started)
    log.info("Excel: %s", OUTPUT_XLSX)
    log.info("CSV: %s", OUTPUT_CSV)
    log.info("Ошибки: %s", ERROR_CSV)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("Остановлено пользователем")
    except Exception as error:
        log.exception("КРИТИЧЕСКАЯ ОШИБКА: %s", error)
        raise
