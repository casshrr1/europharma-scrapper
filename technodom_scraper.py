import asyncio
import csv
import json
import logging
import re
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import pandas as pd
from playwright.async_api import BrowserContext, Page, async_playwright
from pynput import keyboard

# ============================================================
# SETTINGS
# ============================================================
BASE_URL = "https://www.technodom.kz"
HEADLESS = False
BACKUP_EVERY = 150
PRODUCT_WORKERS = 5
MAX_RETRIES = 3
PAGE_TIMEOUT_MS = 60_000
MAX_CATEGORY_DEPTH = 7
MAX_CATEGORIES = 5000
MAX_PAGES_PER_CATEGORY = 500

OUTPUT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = OUTPUT_DIR / "technodom_browser_profile"
OUTPUT_XLSX = OUTPUT_DIR / "technodom_products.xlsx"
OUTPUT_CSV = OUTPUT_DIR / "technodom_products.csv"
BACKUP_XLSX = OUTPUT_DIR / "technodom_products_backup.xlsx"
ERROR_CSV = OUTPUT_DIR / "technodom_errors.csv"
CHECKPOINT_JSON = OUTPUT_DIR / "technodom_checkpoint.json"

# These are fallback values. The script first tries to read cities from the site.
FALLBACK_CITIES = [
    "Алматы", "Астана", "Шымкент", "Караганда", "Актобе", "Тараз",
    "Павлодар", "Усть-Каменогорск", "Костанай", "Атырау", "Актау",
]

SPACE_RE = re.compile(r"\s+")
PRODUCT_ID_RE = re.compile(r"-(\d+)(?:/)?$")
PRICE_RE = re.compile(r"(\d[\d\s\u00a0]*)\s*₸")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("technodom")


@dataclass
class CategoryInfo:
    main_category: str
    category: str
    subcategory: str
    url: str


@dataclass
class Product:
    scraping_date: str
    link: str
    name: Optional[str]
    main_category: Optional[str]
    category: Optional[str]
    subcategory: Optional[str]
    brand: Optional[str]
    price: Optional[int]
    variants: Optional[str]
    variant_prices: Optional[str]
    availability: str


pause_event = threading.Event()
pause_event.set()
save_requested = threading.Event()


def clean(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_url(url: str, keep_page: bool = False) -> str:
    parts = urlsplit(urljoin(BASE_URL, url))
    path = parts.path.rstrip("/") or "/"
    query = ""
    if keep_page:
        values = parse_qs(parts.query)
        if values.get("page", [""])[0].isdigit():
            query = urlencode({"page": values["page"][0]})
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, query, ""))


def product_id(url: str) -> Optional[str]:
    match = PRODUCT_ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


def parse_price(value: Any) -> Optional[int]:
    match = PRICE_RE.search(clean(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def on_key_press(key: keyboard.Key | keyboard.KeyCode) -> None:
    char = getattr(key, "char", None)
    if not char:
        return
    if char == "=":
        if pause_event.is_set():
            pause_event.clear()
            print("\n*** ПАУЗА. Нажмите = для продолжения. ***")
        else:
            pause_event.set()
            print("\n*** РАБОТА ПРОДОЛЖЕНА. ***")
    elif char.lower() == "s":
        save_requested.set()
        print("\n*** ЗАПРОШЕНО ПРИНУДИТЕЛЬНОЕ СОХРАНЕНИЕ. ***")


def start_hotkeys() -> keyboard.Listener:
    listener = keyboard.Listener(on_press=on_key_press)
    listener.daemon = True
    listener.start()
    return listener


async def wait_if_paused() -> None:
    while not pause_event.is_set():
        await asyncio.sleep(0.25)


async def safe_goto(page: Page, url: str, label: str) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        await wait_if_paused()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            await page.wait_for_timeout(900)
            return
        except Exception as error:
            last_error = error
            log.warning("%s | попытка %s/%s | %s | %s", label, attempt, MAX_RETRIES, error, url)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Не удалось открыть страницу: {last_error}")


async def click_text(page: Page, pattern: str) -> bool:
    locator = page.get_by_text(re.compile(pattern, re.I)).first
    try:
        if await locator.is_visible(timeout=1200):
            await locator.click(force=True, timeout=2500)
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


async def get_site_cities(page: Page) -> list[str]:
    await safe_goto(page, BASE_URL, "Главная")
    current = await page.locator("body").inner_text()
    current_city = "Алматы" if "Алматы" in current else ""

    for selector in (
        '[class*="city"]', '[aria-label*="город" i]',
        'button:has-text("Алматы")', 'button:has-text("Астана")',
    ):
        try:
            node = page.locator(selector).first
            if await node.is_visible(timeout=600):
                await node.click(force=True, timeout=2000)
                await page.wait_for_timeout(600)
                break
        except Exception:
            continue

    texts = await page.locator("button, a, [role='option'], [role='button'], li").all_inner_texts()
    known = []
    fallback_casefold = {x.casefold(): x for x in FALLBACK_CITIES}
    for text in texts:
        value = clean(text)
        if value.casefold() in fallback_casefold and fallback_casefold[value.casefold()] not in known:
            known.append(fallback_casefold[value.casefold()])

    if current_city and current_city not in known:
        known.insert(0, current_city)
    return known or FALLBACK_CITIES


async def choose_city(page: Page) -> str:
    cities = await get_site_cities(page)
    print("\nВыберите город:")
    for index, city in enumerate(cities, 1):
        print(f"{index} - {city}")
    print(f"{len(cities) + 1} - Выбрать вручную в браузере")

    while True:
        choice = clean(await asyncio.to_thread(input, "Введите номер: "))
        if choice.isdigit() and 1 <= int(choice) <= len(cities):
            city = cities[int(choice) - 1]
            if await click_text(page, rf"^{re.escape(city)}$"):
                await click_text(page, r"выбрать|подтвердить|сохранить")
                await page.wait_for_timeout(1200)
                log.info("Выбран город: %s", city)
                return city
            print(f"Выберите город {city} в открытом Edge.")
            await asyncio.to_thread(input, "После выбора нажмите Enter: ")
            return city
        if choice == str(len(cities) + 1):
            await asyncio.to_thread(input, "Выберите город в Edge и нажмите Enter: ")
            return "Выбранный город"
        print("Неверный номер.")


async def open_catalog_menu(page: Page) -> None:
    for selector in (
        '[aria-label*="каталог" i]', '[class*="catalog-button"]',
        'button:has-text("Каталог")', '[class*="burger"]',
    ):
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=700):
                await button.click(force=True, timeout=2500)
                await page.wait_for_timeout(700)
                return
        except Exception:
            continue


async def discover_seed_categories(page: Page) -> list[CategoryInfo]:
    """Discover every top-level catalog section from /catalog.

    Technodom can render category cards as nested clickable blocks. We inspect
    visible anchors and raw page HTML, then reduce every catalog URL to its first
    path segment after /catalog/.
    """
    await safe_goto(page, f"{BASE_URL}/catalog", "Главная каталога")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1000)
    await expand_all_category_blocks(page)

    pairs = await page.locator('a[href*="/catalog/"]').evaluate_all(
        "els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||a.title||'').trim()}))"
    )
    html = await page.content()
    raw_paths = re.findall(r'["\\\'](/catalog/[a-zA-Z0-9_\\-]+(?:/[a-zA-Z0-9_\\-]+)*)', html)
    for raw in raw_paths:
        pairs.append({"href": urljoin(BASE_URL, raw), "text": ""})

    names_by_slug: dict[str, str] = {}
    all_slugs: set[str] = set()
    for item in pairs:
        url = normalize_url(item.get("href", ""))
        path = urlsplit(url).path
        parts = [x for x in path.split("/") if x]
        if len(parts) < 2 or parts[0] != "catalog":
            continue
        slug = parts[1]
        if slug in {"search", "promo", "f"}:
            continue
        all_slugs.add(slug)
        text = clean(item.get("text"))
        # Prefer labels from exact top-level links.
        if len(parts) == 2 and text and len(text) <= 80:
            names_by_slug[slug] = text

    result = []
    for slug in sorted(all_slugs):
        name = names_by_slug.get(slug) or slug.replace("-", " ").replace("_", " ").title()
        result.append(CategoryInfo(name, "", "", normalize_url(f"/catalog/{slug}")))

    if not result:
        raise RuntimeError("На /catalog не найдена ни одна главная категория")

    log.info("Найдено главных категорий: %s", len(result))
    for item in result:
        log.info("Главная категория | %s | %s", item.main_category, item.url)
    return result


async def expand_all_category_blocks(page: Page) -> None:
    """Reveal collapsed category links such as 'Показать все' and 'Все категории'."""
    patterns = (
        r"^Показать все$",
        r"^Все категории$",
        r"^Ещё$",
        r"^Еще$",
        r"^Развернуть$",
    )
    for _ in range(8):
        clicked = 0
        for pattern in patterns:
            nodes = page.get_by_text(re.compile(pattern, re.I))
            count = min(await nodes.count(), 100)
            for index in range(count):
                node = nodes.nth(index)
                try:
                    if await node.is_visible(timeout=250):
                        await node.click(force=True, timeout=1200)
                        clicked += 1
                        await page.wait_for_timeout(120)
                except Exception:
                    try:
                        await node.evaluate("el => (el.closest('a,button,[role=button]') || el).click()")
                        clicked += 1
                    except Exception:
                        pass
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)
        if clicked == 0:
            break


async def extract_catalog_links(page: Page, parent_url: str) -> list[tuple[str, str]]:
    """Collect descendant catalog URLs from visible DOM and serialized HTML."""
    await expand_all_category_blocks(page)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(500)

    items = await page.locator('a[href*="/catalog/"]').evaluate_all(
        "els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||a.title||'').trim()}))"
    )
    html = await page.content()
    raw_paths = re.findall(r'["\\\'](/catalog/[a-zA-Z0-9_\\-]+(?:/[a-zA-Z0-9_\\-]+)+)', html)
    for raw in raw_paths:
        items.append({"href": urljoin(BASE_URL, raw), "text": ""})

    parent_path = urlsplit(parent_url).path.rstrip("/")
    results: dict[str, str] = {}
    for item in items:
        url = normalize_url(item.get("href", ""))
        text = clean(item.get("text"))
        path = urlsplit(url).path.rstrip("/")
        if not path.startswith(parent_path + "/"):
            continue
        if "/f/" in path or path == parent_path:
            continue
        if any(part in path for part in ("/cms/", "/promo", "/search")):
            continue
        if not text:
            text = path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
        results[url] = text
    return sorted(results.items())


async def product_links_on_page(page: Page) -> dict[str, str]:
    output: dict[str, str] = {}
    links = await page.locator('a[href*="/p/"]').evaluate_all("els => els.map(a => a.href)")
    html = await page.content()
    raw_paths = re.findall(r'["\\\'](/p/[a-zA-Z0-9_()%.\\-]+-\\d+)', html)
    links.extend(urljoin(BASE_URL, raw) for raw in raw_paths)

    for raw in links:
        url = normalize_url(raw)
        pid = product_id(url)
        if pid:
            output[pid] = url
    return output


async def last_page_number(page: Page) -> int:
    hrefs = await page.locator('a[href*="page="]').evaluate_all("els => els.map(a => a.href)")
    pages = [1]
    for href in hrefs:
        value = parse_qs(urlsplit(href).query).get("page", [""])[0]
        if value.isdigit():
            pages.append(int(value))
    return max(pages)


async def discover_leaf_categories(
    page: Page,
    seeds: list[CategoryInfo],
) -> tuple[list[CategoryInfo], list[dict[str, str]]]:
    """Walk the complete category tree.

    Important: a Technodom category may contain products AND deeper categories at
    the same time. Therefore such a page is saved as a product category and its
    descendants are still queued for traversal.
    """
    queue: asyncio.Queue[tuple[CategoryInfo, int]] = asyncio.Queue()
    for seed in seeds:
        queue.put_nowait((seed, 1))

    visited: set[str] = set()
    product_categories: dict[str, CategoryInfo] = {}
    errors: list[dict[str, str]] = []

    while not queue.empty() and len(visited) < MAX_CATEGORIES:
        info, depth = await queue.get()
        url = normalize_url(info.url)
        if url in visited:
            queue.task_done()
            continue
        visited.add(url)
        try:
            await safe_goto(page, url, "Категория")
            await expand_all_category_blocks(page)
            products = await product_links_on_page(page)
            children = await extract_catalog_links(page, url)
            crumbs = await breadcrumb_names(page)

            main = crumbs[0] if crumbs else info.main_category
            category = crumbs[1] if len(crumbs) > 1 else (info.category or "")
            try:
                h1 = clean(await page.locator("h1").first.inner_text(timeout=1500))
            except Exception:
                h1 = ""
            subcategory = crumbs[-1] if len(crumbs) > 2 else (h1 or info.subcategory)

            if products:
                product_categories[url] = CategoryInfo(main, category, subcategory, url)
                log.info(
                    "Товарная категория | %s > %s > %s | товаров на стр. 1: %s | дочерних: %s",
                    main, category, subcategory, len(products), len(children)
                )

            # Do not stop traversal merely because products were found.
            if depth < MAX_CATEGORY_DEPTH:
                for child_url, child_name in children:
                    if child_url in visited:
                        continue
                    child_path = [x for x in urlsplit(child_url).path.split("/") if x]
                    # catalog + up to four category levels
                    if len(child_path) > MAX_CATEGORY_DEPTH + 1:
                        continue
                    child = CategoryInfo(
                        main_category=main or info.main_category,
                        category=category or child_name,
                        subcategory=child_name,
                        url=child_url,
                    )
                    queue.put_nowait((child, depth + 1))

            if not products and not children:
                log.warning("Пустая конечная категория | %s", url)
        except Exception as error:
            log.error("ОШИБКА КАТЕГОРИИ | %s | %s", url, error)
            errors.append({
                "stage": "category",
                "category": info.subcategory or info.category or info.main_category,
                "link": url,
                "error": repr(error),
            })
        finally:
            queue.task_done()

    if len(visited) >= MAX_CATEGORIES:
        log.warning("Достигнут защитный лимит MAX_CATEGORIES=%s", MAX_CATEGORIES)

    result = sorted(
        product_categories.values(),
        key=lambda x: (x.main_category, x.category, x.subcategory, x.url),
    )
    log.info("Проверено страниц категорий: %s", len(visited))
    log.info("Найдено товарных категорий: %s", len(result))
    return result, errors


async def breadcrumb_names(page: Page) -> list[str]:
    selectors = [
        '[class*="breadcrumb"] a', '[aria-label*="breadcrumb" i] a',
        'nav a[href*="/catalog/"]',
    ]
    for selector in selectors:
        try:
            values = [clean(x) for x in await page.locator(selector).all_inner_texts()]
            values = [x for x in values if x and x.casefold() not in {"главная", "каталог"}]
            if values:
                return values
        except Exception:
            continue
    return []


def set_page(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"page": page_number}), ""))


async def advertised_product_count(page: Page) -> Optional[int]:
    """Read counters such as 'Найдено 2 288 товаров'."""
    try:
        text = clean(await page.locator("body").inner_text())
    except Exception:
        return None
    match = re.search(r"Найдено\s+([\d\s\u00a0]+)\s+товар", text, re.I)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


async def next_page_url(page: Page, category_url: str, current_page: int) -> str:
    """Prefer the site's actual link for the next page; fall back to ?page=N."""
    expected = current_page + 1
    hrefs = await page.locator('a[href*="page="]').evaluate_all("els => els.map(a => a.href)")
    for href in hrefs:
        value = parse_qs(urlsplit(href).query).get("page", [""])[0]
        if value.isdigit() and int(value) == expected:
            return normalize_url(href, keep_page=True)
    return set_page(category_url, expected)


async def collect_all_product_links(
    page: Page,
    leaves: list[CategoryInfo],
) -> tuple[dict[str, tuple[str, CategoryInfo]], list[dict[str, str]]]:
    products: dict[str, tuple[str, CategoryInfo]] = {}
    errors: list[dict[str, str]] = []

    for index, category in enumerate(leaves, 1):
        category_seen: set[str] = set()
        page_signatures: set[tuple[str, ...]] = set()
        repeat_pages = 0
        page_number = 1
        page_url = set_page(category.url, 1)
        expected_count: Optional[int] = None

        try:
            while page_number <= MAX_PAGES_PER_CATEGORY:
                await wait_if_paused()
                await safe_goto(
                    page,
                    page_url,
                    f"{category.subcategory or category.category}, стр. {page_number}",
                )

                if page_number == 1:
                    expected_count = await advertised_product_count(page)
                    log.info(
                        "КОНТРОЛЬ КАТЕГОРИИ | %s | сайт заявляет товаров: %s",
                        category.subcategory or category.category,
                        expected_count if expected_count is not None else "не найдено",
                    )

                found = await product_links_on_page(page)
                signature = tuple(sorted(found))
                repeated_html_page = bool(signature) and signature in page_signatures
                page_signatures.add(signature)

                new_in_category = 0
                new_global = 0
                for pid, url in found.items():
                    if pid not in category_seen:
                        category_seen.add(pid)
                        new_in_category += 1
                    if pid not in products:
                        products[pid] = (url, category)
                        new_global += 1

                log.info(
                    "Категория %s/%s | %s | страница %s | сайт: %s | собрано в категории: %s | найдено на странице: %s | новых глобально: %s | всего: %s",
                    index,
                    len(leaves),
                    category.subcategory or category.category,
                    page_number,
                    expected_count if expected_count is not None else "?",
                    len(category_seen),
                    len(found),
                    new_global,
                    len(products),
                )

                # Stop successfully as soon as the advertised total has been met.
                if expected_count is not None and len(category_seen) >= expected_count:
                    break

                if not found or repeated_html_page or new_in_category == 0:
                    repeat_pages += 1
                else:
                    repeat_pages = 0

                if repeat_pages >= 3:
                    if expected_count is not None and len(category_seen) < expected_count:
                        message = (
                            f"Пагинация остановилась раньше счетчика сайта: "
                            f"собрано {len(category_seen)} из {expected_count}"
                        )
                        log.error("НЕПОЛНАЯ КАТЕГОРИЯ | %s | %s", category.url, message)
                        errors.append({
                            "stage": "incomplete_category",
                            "category": category.subcategory or category.category,
                            "link": category.url,
                            "error": message,
                        })
                    break

                page_url = await next_page_url(page, category.url, page_number)
                page_number += 1

            if page_number >= MAX_PAGES_PER_CATEGORY:
                log.warning("Достигнут MAX_PAGES_PER_CATEGORY=%s | %s", MAX_PAGES_PER_CATEGORY, category.url)
        except Exception as error:
            log.error("ОШИБКА СТРАНИЦ КАТЕГОРИИ | %s | page=%s | %s", category.url, page_number, error)
            errors.append({
                "stage": "catalog_page",
                "category": category.subcategory or category.category,
                "link": page_url,
                "error": repr(error),
            })

    return products, errors


async def jsonld_product(page: Page) -> Optional[dict[str, Any]]:
    scripts = page.locator('script[type="application/ld+json"]')
    for i in range(await scripts.count()):
        try:
            data = json.loads(await scripts.nth(i).text_content() or "null")
            stack = data if isinstance(data, list) else [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    node_type = node.get("@type")
                    if node_type == "Product" or (isinstance(node_type, list) and "Product" in node_type):
                        return node
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
        except Exception:
            continue
    return None


async def first_text(page: Page, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        try:
            node = page.locator(selector).first
            if await node.count():
                value = clean(await node.inner_text(timeout=1200))
                if value:
                    return value
        except Exception:
            continue
    return None


async def extract_variants(page: Page, current_id: str) -> tuple[Optional[str], Optional[str]]:
    selectors = [
        '[class*="variant"] a', '[class*="variant"] button',
        '[class*="option"] a', '[class*="option"] button',
        '[class*="configuration"] a', '[class*="color"] a',
        '[class*="memory"] a', '[class*="model"] a',
    ]
    variants: list[str] = []
    priced: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        nodes = page.locator(selector)
        for i in range(min(await nodes.count(), 80)):
            try:
                node = nodes.nth(i)
                text = clean(await node.inner_text()) or clean(await node.get_attribute("title"))
                href = clean(await node.get_attribute("href"))
                if not text or len(text) > 180:
                    continue
                key = f"{text}|{href}"
                if key in seen:
                    continue
                seen.add(key)
                variants.append(text)
                value = parse_price(text)
                if value is not None:
                    priced.append(f"{text} - {value} ₸")
            except Exception:
                continue
    return "; ".join(variants) or None, "; ".join(priced) or None


async def parse_product(page: Page, url: str, category: CategoryInfo, scraping_date: str) -> Product:
    await safe_goto(page, url, "Товар")
    data = await jsonld_product(page) or {}

    name = clean(data.get("name")) or await first_text(page, ["h1", '[itemprop="name"]'])
    if not name:
        raise ValueError("Название товара не найдено")

    brand_data = data.get("brand")
    brand = clean(brand_data.get("name")) if isinstance(brand_data, dict) else clean(brand_data)
    if not brand:
        brand = await first_text(page, ['[itemprop="brand"]', '[class*="brand"]'])

    offers = data.get("offers") or {}
    offer_list = offers if isinstance(offers, list) else [offers]
    price: Optional[int] = None
    status_values: list[str] = []
    for offer in offer_list:
        if not isinstance(offer, dict):
            continue
        raw_price = offer.get("price") or offer.get("lowPrice")
        try:
            if raw_price is not None and price is None:
                price = round(float(str(raw_price).replace(" ", "").replace(",", ".")))
        except ValueError:
            pass
        status_values.append(clean(offer.get("availability")))

    body = clean(await page.locator("body").inner_text())
    if price is None:
        price = parse_price(body)

    pid = product_id(url) or ""
    variants, variant_prices = await extract_variants(page, pid)

    lowered = (" ".join(status_values) + " " + body).casefold()
    unavailable_markers = ("нет в наличии", "товар закончился", "недоступен", "outofstock", "soldout")
    # 'На витрине', delivery and pickup all count as available.
    availability = "Нет в наличии" if any(x in lowered for x in unavailable_markers) else "В наличии"

    crumbs = await breadcrumb_names(page)
    main = crumbs[0] if crumbs else category.main_category
    cat = crumbs[1] if len(crumbs) > 1 else category.category
    sub = crumbs[-1] if len(crumbs) > 2 else category.subcategory

    return Product(
        scraping_date=scraping_date,
        link=normalize_url(url),
        name=name,
        main_category=main or None,
        category=cat or None,
        subcategory=sub or None,
        brand=brand or None,
        price=price,
        variants=variants,
        variant_prices=variant_prices,
        availability=availability,
    )


def product_columns() -> list[str]:
    return list(Product.__dataclass_fields__)


def write_outputs(products: list[Product], errors: list[dict[str, str]], excel_path: Path) -> None:
    frame = pd.DataFrame([asdict(x) for x in products], columns=product_columns())
    if not frame.empty:
        frame = frame.drop_duplicates(subset=["link"], keep="first")
        available = frame[frame["availability"] == "В наличии"]
        unavailable = frame[frame["availability"] == "Нет в наличии"]
        summary = (
            frame.groupby(["main_category", "category", "subcategory"], dropna=False)
            .agg(products_count=("link", "count"), unavailable_count=("availability", lambda x: (x == "Нет в наличии").sum()))
            .reset_index()
        )
    else:
        available = unavailable = frame
        summary = pd.DataFrame(columns=["main_category", "category", "subcategory", "products_count", "unavailable_count"])

    error_frame = pd.DataFrame(errors, columns=["stage", "category", "link", "error"])
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        available.to_excel(writer, sheet_name="Все товары", index=False)
        unavailable.to_excel(writer, sheet_name="Нет в наличии", index=False)
        summary.to_excel(writer, sheet_name="Категории", index=False)
        error_frame.to_excel(writer, sheet_name="Ошибки", index=False)


def save_checkpoint(products: list[Product], errors: list[dict[str, str]]) -> None:
    payload = {
        "products": [asdict(x) for x in products],
        "errors": errors,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    CHECKPOINT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_checkpoint() -> tuple[list[Product], list[dict[str, str]]]:
    if not CHECKPOINT_JSON.exists():
        return [], []
    try:
        data = json.loads(CHECKPOINT_JSON.read_text(encoding="utf-8"))
        products = [Product(**row) for row in data.get("products", [])]
        errors = data.get("errors", [])
        log.info("Checkpoint загружен | товаров: %s", len(products))
        return products, errors
    except Exception as error:
        log.warning("Checkpoint не загружен: %s", error)
        return [], []


async def collect_products(
    context: BrowserContext,
    product_map: dict[str, tuple[str, CategoryInfo]],
    scraping_date: str,
    initial_products: list[Product],
    initial_errors: list[dict[str, str]],
) -> tuple[list[Product], list[dict[str, str]]]:
    completed_ids = {product_id(x.link) for x in initial_products if product_id(x.link)}
    queue: asyncio.Queue[tuple[str, str, CategoryInfo]] = asyncio.Queue()
    for pid, (url, category) in product_map.items():
        if pid not in completed_ids:
            queue.put_nowait((pid, url, category))

    products = list(initial_products)
    errors = list(initial_errors)
    lock = asyncio.Lock()
    processed = len(completed_ids)
    total = len(product_map)

    async def worker(worker_number: int) -> None:
        nonlocal processed
        page = await context.new_page()
        try:
            while True:
                try:
                    pid, url, category = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await wait_if_paused()
                try:
                    product = await parse_product(page, url, category, scraping_date)
                    async with lock:
                        products.append(product)
                        processed += 1
                        log.info("Товар %s/%s | %s | %s", processed, total, product.availability, product.name)
                        should_backup = len(products) % BACKUP_EVERY == 0
                        should_force = save_requested.is_set()
                        if should_backup or should_force:
                            write_outputs(products, errors, BACKUP_XLSX)
                            save_checkpoint(products, errors)
                            save_requested.clear()
                            log.info("СОХРАНЕНО | товаров: %s | %s", len(products), BACKUP_XLSX.name)
                except Exception as error:
                    async with lock:
                        processed += 1
                        errors.append({"stage": "product", "category": category.subcategory, "link": url, "error": repr(error)})
                        log.error("ОШИБКА ТОВАРА %s/%s | %s | %s", processed, total, url, error)
                finally:
                    queue.task_done()
        finally:
            await page.close()

    count = min(PRODUCT_WORKERS, max(1, queue.qsize()))
    workers = [asyncio.create_task(worker(i)) for i in range(count)]
    await queue.join()
    await asyncio.gather(*workers)
    return products, errors


async def main() -> None:
    started = datetime.now()
    scraping_date = started.strftime("%Y-%m-%d %H:%M:%S")
    listener = start_hotkeys()
    PROFILE_DIR.mkdir(exist_ok=True)
    products, checkpoint_errors = load_checkpoint()

    log.info("Старт | = пауза/продолжение | S сохранить | Ctrl+C остановка")

    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=HEADLESS,
                locale="ru-RU",
                viewport={"width": 1440, "height": 950},
                args=["--disable-blink-features=AutomationControlled"],
            )
            context.set_default_timeout(15_000)
            page = context.pages[0] if context.pages else await context.new_page()

            try:
                await safe_goto(page, BASE_URL, "Главная")
                await choose_city(page)
                seeds = await discover_seed_categories(page)
                leaves, category_errors = await discover_leaf_categories(page, seeds)
                log.info("Товарных категорий для обхода: %s", len(leaves))
                product_map, catalog_errors = await collect_all_product_links(page, leaves)
                log.info("Уникальных товаров по ID: %s", len(product_map))

                all_initial_errors = checkpoint_errors + category_errors + catalog_errors
                products, errors = await collect_products(
                    context, product_map, scraping_date, products, all_initial_errors
                )
            finally:
                await context.close()
    finally:
        listener.stop()

    products.sort(key=lambda x: (x.main_category or "", x.category or "", x.subcategory or "", x.name or ""))
    write_outputs(products, errors, OUTPUT_XLSX)
    pd.DataFrame([asdict(x) for x in products], columns=product_columns()).drop_duplicates(subset=["link"]).to_csv(
        OUTPUT_CSV, index=False, encoding="utf-8-sig"
    )
    with ERROR_CSV.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["stage", "category", "link", "error"])
        writer.writeheader()
        writer.writerows(errors)
    save_checkpoint(products, errors)

    log.info("=" * 70)
    log.info("ГОТОВО | товаров: %s | ошибок: %s | время: %s", len(products), len(errors), datetime.now() - started)
    log.info("Excel: %s", OUTPUT_XLSX)
    log.info("CSV: %s", OUTPUT_CSV)
    log.info("Ошибки: %s", ERROR_CSV)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("Работа остановлена пользователем. Последний checkpoint сохранен при backup/S.")
    except Exception as error:
        log.exception("КРИТИЧЕСКАЯ ОШИБКА: %s", error)
        raise
