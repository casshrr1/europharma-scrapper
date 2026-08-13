import time
import os
import re
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime


BASE_URL = "https://biosfera.kz"
CATALOG_URL = "https://biosfera.kz/ru/catalog/lekarstvennye-sredstva-i-bady"


def init_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")

    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2
    }
    options.add_experimental_option("prefs", prefs)

    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )


def accept_city_popup(driver):
    """Dismiss the city dialog without relying on a physical browser click."""
    try:
        btn = WebDriverWait(driver, 7).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[normalize-space()='Да']")
            )
        )
        # The dialog overlay can intercept Selenium's normal .click().
        # JavaScript clicks the button directly and does not require manual input.
        driver.execute_script("arguments[0].click();", btn)
        WebDriverWait(driver, 5).until(EC.staleness_of(btn))
        print("City popup accepted")
    except TimeoutException:
        # Product/category hrefs are still readable from the DOM behind the popup,
        # so do not stop the scraper if the dialog stays open.
        print("City popup ignored")


def accept_city_popup_if_present(driver):
    """Fast version for product pages; it never waits when no dialog exists."""
    buttons = driver.find_elements(By.XPATH, "//button[normalize-space()='Да']")
    if buttons:
        driver.execute_script("arguments[0].click();", buttons[0])
        print("City popup accepted")


def wait_for_product_page(driver):
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1.product__title"))
        )
        time.sleep(0.5)
    except TimeoutException:
        pass


def safe_text(driver, by, selector):
    try:
        return driver.find_element(by, selector).text.strip()
    except Exception:
        return None


def get_product_field(driver, label):
    try:
        return driver.find_element(
            By.XPATH,
            f"//*[normalize-space()='{label}:']/following::*[normalize-space()][1]"
        ).text.strip()
    except Exception:
        return None


def get_categories(driver):
    try:
        crumbs = [
            crumb.text.strip()
            for crumb in driver.find_elements(
                By.CSS_SELECTOR, ".breadcrumbs__item"
            )
            if crumb.text.strip() and crumb.text.strip() != "Каталог"
        ]
    except Exception:
        crumbs = []

    # Breadcrumbs are: Catalog > category > subcategory > product.
    cat1 = crumbs[0] if len(crumbs) > 0 else None
    cat2 = crumbs[1] if len(crumbs) > 1 else None
    return cat1, cat2, None


def get_price(driver):
    try:
        prices = driver.find_elements(
            By.XPATH,
            "//h1[contains(@class,'product__title')]"
            "/following::*[contains(normalize-space(), '₸')]"
        )
        for price in prices:
            value = price.text.strip()
            if re.search(r"\d", value) and "Цена действует" not in value:
                return value
    except Exception:
        pass
    return None


def get_product_links(driver):
    # Read href values from the page DOM.  This does not click product cards,
    # so the city popup cannot intercept the operation.
    return set(driver.execute_script("""
        return [...document.querySelectorAll('a.productCard__name[href]')]
            .map(a => a.href)
            .filter(Boolean);
    """))


def get_catalog_page(driver, page_number):
    if page_number == 1:
        WebDriverWait(driver, 30).until(
            lambda d: len(get_product_links(d)) > 0
        )

    if page_number > 1:
        try:
            previous_links = get_product_links(driver)
            next_page = driver.find_element(
                By.CSS_SELECTOR, f'button[aria-label="Go to page {page_number}"]'
            )
            driver.execute_script("arguments[0].click();", next_page)
            WebDriverWait(driver, 15).until(
                lambda d: get_product_links(d) != previous_links
            )
        except Exception:
            return set(), False

    links = get_product_links(driver)
    has_next_page = bool(driver.find_elements(
        By.CSS_SELECTOR, f'button[aria-label="Go to page {page_number + 1}"]'
    ))
    return links, has_next_page


def get_leaf_categories(driver):
    pending = [CATALOG_URL]
    visited = set()
    product_categories = []

    while pending:
        category_url = pending.pop()
        if category_url in visited:
            continue
        visited.add(category_url)

        print(f"Checking category: {category_url}")
        driver.get(category_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        if get_product_links(driver):
            product_categories.append(category_url)
            print("  Product links found")
            continue

        child_categories = driver.execute_script("""
            return [...document.querySelectorAll('a[href*="/ru/catalog/"]')]
                .map(a => a.href)
                .filter(Boolean);
        """)
        new_child_categories = []
        for href in child_categories:
            if (
                href.startswith(BASE_URL + "/ru/catalog/")
                and not href.endswith("/null")
                and not re.search(r"/[0-9a-f-]{36}$", href, re.I)
                and href not in visited
            ):
                new_child_categories.append(href)

        # A category page already exposes its child hrefs.  Continue down the
        # tree immediately instead of waiting for product cards on this page.
        if new_child_categories:
            pending.extend(new_child_categories)
            continue

        # This is a leaf category: wait for its client-rendered product names.
        try:
            WebDriverWait(driver, 8).until(lambda d: get_product_links(d))
            product_categories.append(category_url)
            print("  Product links found")
        except TimeoutException:
            print("  No product-name hrefs appeared after 8 seconds")

    return product_categories


def scrape(driver, max_pages=None):
    product_links = set()
    data = []

    for category_url in get_leaf_categories(driver):
        print(f"Category: {category_url}")
        driver.get(category_url)
        page_number = 1

        while True:
            current_links, has_next_page = get_catalog_page(driver, page_number)
            new_links = current_links - product_links
            print(f"Page {page_number}: {len(new_links)} new products found")

            for idx, link in enumerate(new_links, 1):
                print(f"  [{idx}/{len(new_links)}] Scraping {link}")
                driver.get(link)
                wait_for_product_page(driver)
                accept_city_popup_if_present(driver)

                cat1, cat2, _ = get_categories(driver)
                product = {
                    "category": cat1,
                    "subcategory": cat2,
                    "group": None,
                    "name": safe_text(driver, By.CSS_SELECTOR, "h1.product__title"),
                    "company": get_product_field(driver, "Производитель"),
                    "country": get_product_field(driver, "Страна производитель"),
                    "price": get_price(driver),
                    "url": link,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                data.append(product)

            product_links.update(new_links)

            if max_pages is not None and page_number >= max_pages:
                print("Reached max_pages limit")
                break

            if not has_next_page:
                print("No more pages to load")
                break

            driver.get(category_url)
            page_number += 1
            time.sleep(1)

    print(f"Total product links collected: {len(product_links)}")
    return data


def main():
    driver = init_driver()
    try:
        driver.get(CATALOG_URL)
        accept_city_popup(driver)
        products = scrape(driver)
    finally:
        driver.quit()

    output_path = os.path.join(
        os.path.expanduser("~"), "Downloads", "biosfera_products_list.csv"
    )
    pd.DataFrame(products).to_csv(output_path, index=False, encoding="utf-8-sig")
    print("Saved biosfera_products_list.csv")


if __name__ == "__main__":
    main()
