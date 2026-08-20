import asyncio
import csv
import json
import logging
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import pandas as pd
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pynput import keyboard

# ============================================================
# SETTINGS
# ============================================================
BASE_URL = "https://arbuz.kz"
HEADLESS = False  # Keep False for first runs; True after selectors/session are stable.
BACKUP_EVERY = 150
MAX_RETRIES = 3
PAGE_TIMEOUT_MS = 60_000
CARD_CONCURRENCY = 4
MAX_SCROLLS = 120
SCROLL_PAUSE_MS = 900

OUTPUT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = OUTPUT_DIR / "arbuz_browser_profile"
OUTPUT_XLSX = OUTPUT_DIR / "arbuz_products.xlsx"
OUTPUT_XML = OUTPUT_DIR / "arbuz_products.xml"
BACKUP_XML = OUTPUT_DIR / "arbuz_products_backup.xml"
OUTPUT_CSV = OUTPUT_DIR / "arbuz_products.csv"
BACKUP_XLSX = OUTPUT_DIR / "arbuz_products_backup.xlsx"
ERROR_CSV = OUTPUT_DIR / "arbuz_products_errors.csv"

# The site may change city/address options. The address can be adjusted here.
CITY_OPTIONS = {
    "1": {"name": "Алматы", "slug": "almaty", "address": "проспект Абая, 100"},
    "2": {"name": "Астана", "slug": "astana", "address": "проспект Республики, 10"},
}

# Food-category keywords. Non-food categories are excluded.
FOOD_KEYWORDS = {
    "овощ", "фрукт", "ягод", "зелень", "молоч", "сыр", "яйц", "мяс", "птиц",
    "колбас", "рыб", "морепродукт", "хлеб", "выпеч", "бакале", "круп", "макарон",
    "масло", "соус", "спец", "напит", "вода", "сок", "чай", "кофе", "слад",
    "шоколад", "конфет", "печень", "снек", "заморож", "морожен", "готов", "кулинар",
    "детское питание", "диет", "здоровое питание", "консерв", "солень", "орех", "сухофрукт",
    "импортные товары", "импортн",
}
NON_FOOD_KEYWORDS = {
    "бытовая хим", "космет", "гигиен", "товары для дома", "зоотовар", "канцеляр",
    "техник", "посуда", "текстиль", "уборк", "ремонт", "одежд", "электрон",
}

SPACE_RE = re.compile(r"\s+")
PRICE_RE = re.compile(r"(\d[\d\s\u00a0]*)(?:[,.]\d+)?\s*(?:₸|тг|тенге)", re.I)
WEIGHT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:кг|г|гр|мг|л|мл|шт|уп|пач|бут|бан|таб|капс)\b",
    re.I,
)
PRODUCT_URL_RE = re.compile(r"/(?:product|products|catalog)/.+(?:\d|-[a-z0-9]{4,})/?$", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("arbuz")


@dataclass
class CategoryInfo:
    main_category: str
    category: str
    subcategory: str
    url: str


@dataclass
class Product:
    scraping_date: str
    city: str
    link: str
    name: Optional[str]
    main_category: Optional[str]
    category: Optional[str]
    subcategory: Optional[str]
    price: Optional[int]
    brand: Optional[str]
    weight_volume: Optional[str]
    variants: Optional[str]
    variant_prices: Optional[str]
    availability: str


pause_event = threading.Event()
pause_event.set()
stop_listener = threading.Event()


def clean(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def parse_price(text: Any) -> Optional[int]:
    match = PRICE_RE.search(clean(text))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def first_weight(text: Any) -> Optional[str]:
    match = WEIGHT_RE.search(clean(text))
    return clean(match.group(0)) if match else None


def choose_city() -> dict[str, str]:
    print("\nВыберите город:")
    for key, value in CITY_OPTIONS.items():
        print(f"{key} - {value['name']}")
    print("3 - Другой город (адрес и город выберете вручную в браузере)")

    while True:
        choice = input("Введите номер: ").strip()
        if choice in CITY_OPTIONS:
            return CITY_OPTIONS[choice]
        if choice == "3":
            name = clean(input("Название города: ")) or "Другой город"
            slug = clean(input("Slug города в URL, если знаете (можно оставить пустым): "))
            return {"name": name, "slug": slug, "address": ""}
        print("Введите 1, 2 или 3.")


def on_key_press(key: keyboard.Key | keyboard.KeyCode) -> None:
    try:
        if getattr(key, "char", None) == "=":
            if pause_event.is_set():
                pause_event.clear()
                print("\n*** ПАУЗА. Нажмите = для продолжения. ***")
            else:
                pause_event.set()
                print("\n*** РАБОТА ПРОДОЛЖЕНА. ***")
    except Exception:
        pass


def start_hotkey_listener() -> keyboard.Listener:
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
            await page.wait_for_timeout(1200)
            return
        except Exception as error:
            last_error = error
            log.warning("%s | попытка %s/%s | %s | %s", label, attempt, MAX_RETRIES, error, url)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Не удалось открыть страницу: {last_error}")


async def click_text_if_visible(page: Page, patterns: list[str]) -> bool:
    for pattern in patterns:
        locator = page.get_by_text(re.compile(pattern, re.I)).first
        try:
            if await locator.is_visible(timeout=1000):
                await locator.click(timeout=3000)
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


async def try_set_location(page: Page, city: dict[str, str]) -> bool:
    """Best-effort automatic location selection; falls back to manual setup."""
    try:
        await click_text_if_visible(page, [r"укажите адрес", r"выбрать адрес", r"адрес доставки"])
        await page.wait_for_timeout(800)

        # Choose city if a city control is visible.
        await click_text_if_visible(page, [r"город", re.escape(city["name"])])
        await click_text_if_visible(page, [rf"^{re.escape(city['name'])}$"])

        address = city.get("address", "")
        if address:
            inputs = page.locator("input")
            count = await inputs.count()
            for i in range(count):
                node = inputs.nth(i)
                placeholder = clean(await node.get_attribute("placeholder"))
                aria = clean(await node.get_attribute("aria-label"))
                combined = f"{placeholder} {aria}".casefold()
                if any(word in combined for word in ("адрес", "улиц", "дом", "достав")):
                    await node.fill(address)
                    await page.wait_for_timeout(1500)
                    # Select first autocomplete suggestion if present.
                    suggestions = page.locator('[role="option"], [class*="suggest"], [class*="autocomplete"] li')
                    if await suggestions.count() > 0:
                        await suggestions.first.click(timeout=3000)
                    await click_text_if_visible(page, [r"подтверд", r"сохран", r"готово"])
                    await page.wait_for_timeout(1800)
                    return True
    except Exception as error:
        log.warning("Автовыбор адреса не удался: %s", error)
    return False


async def ensure_location(page: Page, city: dict[str, str]) -> None:
    profile_has_session = False
    try:
        body = clean(await page.locator("body").inner_text(timeout=5000)).casefold()
        profile_has_session = "укажите адрес доставки" not in body and "подтвердить адрес" not in body
    except Exception:
        pass

    if profile_has_session:
        log.info("Сохраненная сессия/адрес уже присутствуют")
        return

    if await try_set_location(page, city):
        log.info("Адрес выбран автоматически: %s", city.get("address"))
        return

    print("\nНе удалось надежно задать адрес автоматически.")
    print("В открытом Edge выберите город и любой подходящий адрес доставки.")
    await asyncio.to_thread(input, "После загрузки ассортимента нажмите Enter здесь: ")


async def extract_jsonld(page: Page) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    scripts = page.locator('script[type="application/ld+json"]')
    for i in range(await scripts.count()):
        try:
            raw = await scripts.nth(i).text_content()
            data = json.loads(raw or "null")
            if isinstance(data, dict):
                results.append(data)
            elif isinstance(data, list):
                results.extend(x for x in data if isinstance(x, dict))
        except Exception:
            continue
    return results


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


async def force_click_category(page: Page, category_name: str) -> None:
    """Click a menu category even when a transparent/sticky layer intercepts pointer events."""
    locator = page.get_by_text(
        re.compile(rf"^{re.escape(category_name)}$", re.I)
    ).first
    await locator.wait_for(state="attached", timeout=7000)

    # First try a normal click. It works when no overlay is present.
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
        await locator.click(timeout=2500)
        return
    except Exception:
        pass

    # Playwright force click skips hit-target checks.
    try:
        await locator.click(force=True, timeout=2500)
        return
    except Exception:
        pass

    # Final fallback: invoke click on the nearest clickable parent in the page DOM.
    await locator.evaluate(
        """el => {
            const target = el.closest('a,button,[role="button"],li,.menu-item') || el.parentElement || el;
            target.scrollIntoView({block:'center', inline:'nearest'});
            target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            target.click();
        }"""
    )


async def ensure_category_menu_open(page: Page) -> None:
    """Reopen the category menu if navigation or an overlay closed it."""
    vegetables = page.get_by_text(re.compile(r"^Овощи,\s*фрукты,\s*зелень$", re.I)).first
    try:
        if await vegetables.is_visible(timeout=800):
            return
    except Exception:
        pass

    for selector in (
        '[aria-label*="меню" i]',
        '[aria-label*="каталог" i]',
        '[class*="burger"]',
        '[class*="menu-button"]',
        'button:has(svg)',
    ):
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=500):
                await button.click(force=True, timeout=2000)
                await page.wait_for_timeout(700)
                if await vegetables.is_visible(timeout=1000):
                    return
        except Exception:
            continue

async def discover_food_categories(page: Page, city: dict[str, str]) -> list[CategoryInfo]:
    """Open the category menu, click each top-level food category, then collect leaf /catalog/cat/ links."""
    slug = city.get("slug") or "almaty"
    start_urls = [
        f"{BASE_URL}/ru/{slug}/catalog",
        f"{BASE_URL}/ru/{slug}",
        BASE_URL,
    ]

    opened = False
    for url in start_urls:
        try:
            await safe_goto(page, url, "Открытие каталога")
            # Open hamburger/catalog menu when it is collapsed.
            for selector in (
                'button:has(svg)',
                '[aria-label*="меню" i]',
                '[aria-label*="каталог" i]',
                '[class*="burger"]',
                '[class*="menu-button"]',
            ):
                try:
                    node = page.locator(selector).first
                    if await node.is_visible(timeout=700):
                        await node.click(timeout=2000)
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            if await page.get_by_text(re.compile(r"Овощи,\s*фрукты,\s*зелень", re.I)).count():
                opened = True
                break
        except Exception as error:
            log.warning("Не удалось открыть меню через %s: %s", url, error)

    if not opened:
        raise RuntimeError("Боковое меню категорий не найдено")

    # The left column contains top-level categories (about 28 on the current site).
    raw_nodes = await page.locator("a, button, [role='button']").evaluate_all(
        """els => els.map((e, i) => ({
            i,
            text: (e.innerText || e.textContent || '').trim(),
            href: e.href || '',
            rect: (() => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })()
        })).filter(x => x.text && x.rect.w > 20 && x.rect.h > 15 && x.rect.x < window.innerWidth * 0.45)"""
    )

    top_names: list[str] = []
    for item in raw_nodes:
        name = clean(item.get("text"))
        lowered = name.casefold()
        if len(name) > 70 or any(x in lowered for x in NON_FOOD_KEYWORDS):
            continue
        if any(x in lowered for x in FOOD_KEYWORDS) and name not in top_names:
            top_names.append(name)

    if not top_names:
        raise RuntimeError("В левом меню не найдены продуктовые верхние категории")

    categories: dict[str, CategoryInfo] = {}
    for index, main_name in enumerate(top_names, 1):
        await wait_if_paused()
        try:
            await ensure_category_menu_open(page)
            await force_click_category(page, main_name)
            await page.wait_for_timeout(1100)

            # Right-side tiles are leaf category links such as
            # /ru/almaty/catalog/cat/225178-ovoshi#/ .
            links = await page.locator('a[href*="/catalog/cat/"]').evaluate_all(
                "els => els.map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))"
            )
            added = 0
            for item in links:
                url = normalize_url(clean(item.get("href")))
                category_name = clean(item.get("text"))
                if not category_name or not re.search(r"/catalog/cat/\d+-", url, re.I):
                    continue
                categories[url] = CategoryInfo(
                    main_category=main_name,
                    category=category_name,
                    subcategory="",
                    url=url,
                )
                added += 1
            log.info("Главная категория %s/%s | %s | подкатегорий: %s", index, len(top_names), main_name, added)
        except Exception as error:
            log.warning("ПОВТОР ГЛАВНОЙ КАТЕГОРИИ | %s | %s", main_name, error)
            try:
                await ensure_category_menu_open(page)
                await force_click_category(page, main_name)
                await page.wait_for_timeout(1200)
                links = await page.locator('a[href*="/catalog/cat/"]').evaluate_all(
                    "els => els.map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))"
                )
                added = 0
                for item in links:
                    url = normalize_url(clean(item.get("href")))
                    category_name = clean(item.get("text"))
                    if category_name and re.search(r"/catalog/cat/\d+-", url, re.I):
                        categories[url] = CategoryInfo(main_name, category_name, "", url)
                        added += 1
                log.info("ПОВТОР УСПЕШЕН | %s | подкатегорий: %s", main_name, added)
            except Exception as retry_error:
                log.error("ПРОПУЩЕНА ГЛАВНАЯ КАТЕГОРИЯ | %s | %s", main_name, retry_error)

    if not categories:
        raise RuntimeError("Не найдены ссылки вида /catalog/cat/ID-название")

    result = sorted(categories.values(), key=lambda x: (x.main_category, x.category))
    log.info("Найдено конечных продуктовых категорий: %s", len(result))
    return result


async def auto_scroll(page: Page, max_scrolls: int = MAX_SCROLLS) -> None:
    stable = 0
    previous_height = 0
    for _ in range(max_scrolls):
        await wait_if_paused()
        try:
            await click_text_if_visible(page, [r"показать еще", r"показать ещё", r"загрузить еще", r"загрузить ещё"])
            height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(SCROLL_PAUSE_MS)
            if height == previous_height:
                stable += 1
                if stable >= 4:
                    break
            else:
                stable = 0
                previous_height = height
        except Exception:
            break


async def collect_product_links(page: Page, categories: list[CategoryInfo]) -> dict[str, CategoryInfo]:
    """Visit every /catalog/cat/ leaf page and collect actual product-card links."""
    product_map: dict[str, CategoryInfo] = {}
    for index, category in enumerate(categories, 1):
        await wait_if_paused()
        try:
            await safe_goto(page, category.url, f"Подкатегория {category.category}")
            await auto_scroll(page)

            anchors = await page.locator("a[href]").evaluate_all(
                "els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||'').trim()}))"
            )
            added = 0
            for item in anchors:
                url = normalize_url(clean(item.get("href")))
                path = urlsplit(url).path
                if not url.startswith(BASE_URL):
                    continue
                # Never treat category links as products.
                if "/catalog/cat/" in path:
                    continue
                # Arbuz product links can change shape; require product-like URL
                # plus visible card text to avoid navigation/menu links.
                product_like = (
                    "/product/" in path
                    or "/products/" in path
                    or "/catalog/product/" in path
                    or bool(re.search(r"/(?:item|good|goods)/", path, re.I))
                )
                if not product_like:
                    continue
                if url not in product_map:
                    product_map[url] = category
                    added += 1

            # Fallback: product cards may carry URLs in data attributes.
            data_urls = await page.locator(
                '[data-product-url], [data-url*="product"], [class*="product-card"]'
            ).evaluate_all(
                """els => els.map(e => e.dataset.productUrl || e.dataset.url ||
                (e.querySelector('a[href]') || {}).href || '').filter(Boolean)"""
            )
            for raw_url in data_urls:
                url = normalize_url(urljoin(BASE_URL, raw_url))
                if "/catalog/cat/" not in urlsplit(url).path and url not in product_map:
                    product_map[url] = category
                    added += 1

            log.info(
                "Подкатегория %s/%s | %s > %s | новых товаров: %s | всего: %s",
                index, len(categories), category.main_category, category.category, added, len(product_map)
            )
        except Exception as error:
            log.error("ОШИБКА ПОДКАТЕГОРИИ | %s | %s | %s", category.category, category.url, error)

    if not product_map:
        raise RuntimeError(
            "Страницы /catalog/cat/ найдены, но ссылки карточек товаров не распознаны. "
            "Нужно посмотреть href одной карточки товара в DevTools."
        )
    return product_map


async def text_from_selectors(page: Page, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=500):
                value = clean(await locator.inner_text())
                if value:
                    return value
        except Exception:
            continue
    return None


async def extract_variants(page: Page) -> tuple[Optional[str], Optional[str]]:
    entries: list[str] = []
    priced_entries: list[str] = []
    selectors = [
        '[class*="variant"]', '[class*="option"]', '[class*="sku"]',
        '[role="radiogroup"] label', 'button[class*="size"]', 'button[class*="volume"]',
    ]
    seen: set[str] = set()
    for selector in selectors:
        nodes = page.locator(selector)
        for i in range(min(await nodes.count(), 80)):
            try:
                text = clean(await nodes.nth(i).inner_text())
                if not text or len(text) > 160 or text in seen:
                    continue
                if WEIGHT_RE.search(text) or PRICE_RE.search(text):
                    seen.add(text)
                    entries.append(text)
                    if PRICE_RE.search(text):
                        priced_entries.append(text)
            except Exception:
                continue
    return ("; ".join(entries) or None, "; ".join(priced_entries) or None)


async def parse_product_page(page: Page, url: str, category: CategoryInfo, city: str, scraping_date: str) -> Product:
    await safe_goto(page, url, "Товар")
    await wait_if_paused()
    jsonld = await extract_jsonld(page)

    product_json: Optional[dict[str, Any]] = None
    for root in jsonld:
        for node in walk_json(root):
            node_type = node.get("@type")
            if node_type == "Product" or (isinstance(node_type, list) and "Product" in node_type):
                product_json = node
                break
        if product_json:
            break

    name = clean((product_json or {}).get("name")) or await text_from_selectors(page, ["h1", '[itemprop="name"]', '[class*="product"][class*="title"]'])
    if not name:
        raise ValueError("Название товара не найдено")

    brand_value = (product_json or {}).get("brand")
    if isinstance(brand_value, dict):
        brand = clean(brand_value.get("name")) or None
    else:
        brand = clean(brand_value) or None
    if not brand:
        brand = await text_from_selectors(page, ['[itemprop="brand"]', '[class*="brand"]'])

    offers = (product_json or {}).get("offers") or {}
    offer_list = offers if isinstance(offers, list) else [offers]
    prices: list[int] = []
    availability_values: list[str] = []
    for offer in offer_list:
        if not isinstance(offer, dict):
            continue
        raw_price = offer.get("price") or offer.get("lowPrice")
        try:
            if raw_price is not None:
                prices.append(round(float(str(raw_price).replace(" ", "").replace(",", "."))))
        except ValueError:
            pass
        availability_values.append(clean(offer.get("availability")))

    body_text = clean(await page.locator("body").inner_text())
    price = prices[0] if prices else parse_price(body_text)
    weight_volume = first_weight(name) or first_weight(body_text[:3000])
    variants, variant_prices = await extract_variants(page)

    unavailable_markers = ("нет в наличии", "распродано", "недоступен", "outofstock")
    combined_availability = " ".join(availability_values).casefold() + " " + body_text.casefold()
    availability = "Нет в наличии" if any(x in combined_availability for x in unavailable_markers) else "В наличии"

    return Product(
        scraping_date=scraping_date,
        city=city,
        link=url,
        name=name,
        main_category=category.main_category or None,
        category=category.category or None,
        subcategory=category.subcategory or None,
        price=price,
        brand=brand,
        weight_volume=weight_volume,
        variants=variants,
        variant_prices=variant_prices,
        availability=availability,
    )


def xml_safe(value: object) -> str:
    return "" if value is None else str(value)


def write_products_xml(products: list[Product], path: Path) -> None:
    root = ET.Element("catalog", {"source": "arbuz.kz", "generated_at": datetime.now().isoformat(timespec="seconds")})
    available_node = ET.SubElement(root, "available_products")
    unavailable_node = ET.SubElement(root, "unavailable_products")
    for product in products:
        parent = unavailable_node if product.availability == "Нет в наличии" else available_node
        node = ET.SubElement(parent, "product")
        for key, value in asdict(product).items():
            ET.SubElement(node, key).text = xml_safe(value)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_excel(products: list[Product], errors: list[dict[str, str]], path: Path) -> None:
    rows = [asdict(x) for x in products]
    frame = pd.DataFrame(rows, columns=[field for field in Product.__dataclass_fields__])
    available = frame[frame["availability"] != "Нет в наличии"] if not frame.empty else frame
    unavailable = frame[frame["availability"] == "Нет в наличии"] if not frame.empty else frame
    errors_frame = pd.DataFrame(errors, columns=["stage", "category", "link", "error"])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        available.to_excel(writer, sheet_name="Все товары", index=False)
        unavailable.to_excel(writer, sheet_name="Нет в наличии", index=False)
        if not frame.empty:
            summary = (
                frame.groupby(["main_category", "category", "subcategory"], dropna=False)
                .agg(products_count=("link", "count"), unavailable_count=("availability", lambda x: (x == "Нет в наличии").sum()))
                .reset_index()
            )
        else:
            summary = pd.DataFrame(columns=["main_category", "category", "subcategory", "products_count", "unavailable_count"])
        summary.to_excel(writer, sheet_name="Категории", index=False)
        errors_frame.to_excel(writer, sheet_name="Ошибки", index=False)


def write_backup(products: list[Product], errors: list[dict[str, str]]) -> None:
    write_excel(products, errors, BACKUP_XLSX)
    write_products_xml(products, BACKUP_XML)
    log.info("BACKUP | сохранено %s товаров | %s", len(products), BACKUP_XLSX.name)


async def collect_products(
    context: BrowserContext,
    product_map: dict[str, CategoryInfo],
    city: str,
    scraping_date: str,
) -> tuple[list[Product], list[dict[str, str]]]:
    queue: asyncio.Queue[tuple[str, CategoryInfo]] = asyncio.Queue()
    for item in product_map.items():
        queue.put_nowait(item)

    products: list[Product] = []
    errors: list[dict[str, str]] = []
    lock = asyncio.Lock()
    total = len(product_map)
    processed = 0

    async def worker(worker_id: int) -> None:
        nonlocal processed
        page = await context.new_page()
        try:
            while True:
                try:
                    url, category = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await wait_if_paused()
                try:
                    product = await parse_product_page(page, url, category, city, scraping_date)
                    async with lock:
                        products.append(product)
                        processed += 1
                        log.info("Товар %s/%s | %s | %s", processed, total, product.availability, product.name)
                        if len(products) % BACKUP_EVERY == 0:
                            write_backup(products, errors)
                except Exception as error:
                    async with lock:
                        processed += 1
                        errors.append({"stage": "product", "category": category.category, "link": url, "error": repr(error)})
                        log.error("ОШИБКА ТОВАРА %s/%s | %s | %s", processed, total, url, error)
                finally:
                    queue.task_done()
        finally:
            await page.close()

    workers = [asyncio.create_task(worker(i)) for i in range(min(CARD_CONCURRENCY, total))]
    await queue.join()
    await asyncio.gather(*workers)
    return products, errors


async def main() -> None:
    city = choose_city()
    scraping_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    started = datetime.now()
    listener = start_hotkey_listener()

    log.info("Старт | город: %s | пауза/продолжение: клавиша =", city["name"])
    PROFILE_DIR.mkdir(exist_ok=True)

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
            await ensure_location(page, city)
            categories = await discover_food_categories(page, city)
            product_map = await collect_product_links(page, categories)
            log.info("Всего уникальных ссылок на товары: %s", len(product_map))
            products, errors = await collect_products(context, product_map, city["name"], scraping_date)
        finally:
            await context.close()
            listener.stop()

    products.sort(key=lambda x: (x.main_category or "", x.category or "", x.subcategory or "", x.name or ""))
    write_excel(products, errors, OUTPUT_XLSX)
    write_products_xml(products, OUTPUT_XML)
    pd.DataFrame([asdict(x) for x in products]).to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    with ERROR_CSV.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["stage", "category", "link", "error"])
        writer.writeheader()
        writer.writerows(errors)

    log.info("=" * 70)
    log.info("ГОТОВО | товаров: %s | ошибок: %s | время: %s", len(products), len(errors), datetime.now() - started)
    log.info("Excel: %s", OUTPUT_XLSX)
    log.info("CSV: %s", OUTPUT_CSV)
    log.info("XML: %s", OUTPUT_XML)
    log.info("Ошибки: %s", ERROR_CSV)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("Работа остановлена пользователем")
    except Exception as error:
        log.exception("КРИТИЧЕСКАЯ ОШИБКА: %s", error)
        raise
