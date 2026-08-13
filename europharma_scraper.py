import time
import os
import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime


BASE_URL = "https://europharma.kz"
CATALOG_URL = "https://europharma.kz/catalog/lekarstvennye-sredstva?segment=available"


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
    try:
        btn = WebDriverWait(driver, 7).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(normalize-space(.),'Да, спасибо')]")
            )
        )
        btn.click()
        time.sleep(1)
        print("City popup accepted")
    except TimeoutException:
        print("City popup not found")


def wait_for_product_page(driver):
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".characteristic__list"))
        )
        time.sleep(0.5)
    except TimeoutException:
        pass


def safe_text(driver, by, selector):
    try:
        return driver.find_element(by, selector).text.strip()
    except Exception:
        return None


def get_country(driver, label):
    try:
        return driver.find_element(
            By.XPATH,
            f"//dt[normalize-space()='{label}']/following-sibling::dd[1]"
        ).text.strip()
    except Exception:
        return None


def get_categories(driver):
    try:
        crumbs = [
            crumb.text.strip()
            for crumb in driver.find_elements(
                By.CSS_SELECTOR, ".breadcrumb .breadcrumb__item"
            )
            if crumb.text.strip()
        ]
    except Exception:
        crumbs = []

    # Breadcrumbs are: Главная > Каталог > category > subcategory.
    cat1 = crumbs[2] if len(crumbs) > 2 else None
    cat2 = crumbs[3] if len(crumbs) > 3 else None
    return cat1, cat2, None


def get_pharmacotherapeutic_group(driver):
    try:
        return driver.find_element(
            By.XPATH,
            "//*[normalize-space()='Фармакотерапевтическая группа']"
            "/following::*[normalize-space()][1]"
        ).text.strip()
    except Exception:
        return None


def get_catalog_page(page_number):
    url = CATALOG_URL if page_number == 1 else f"{CATALOG_URL}&page={page_number}"
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    links = {
        BASE_URL + a["href"]
        for a in soup.select("a.card-product__link[href]")
        if a["href"].startswith("/")
    }
    has_next_page = any(
        a.get_text(strip=True) == "Вперед" and a.get("href")
        for a in soup.select("a.pagination__link")
    )
    return links, has_next_page


def scrape(driver, max_pages=None):
    product_links = set()
    data = []
    page_number = 1

    while True:
        current_links, has_next_page = get_catalog_page(page_number)
        new_links = current_links - product_links
        print(f"Page {page_number}: {len(new_links)} new products found")

        for idx, link in enumerate(new_links, 1):
            print(f"  [{idx}/{len(new_links)}] Scraping {link}")
            driver.get(link)
            wait_for_product_page(driver)

            cat1, cat2, _ = get_categories(driver)
            product = {
                "category": cat1,
                "subcategory": cat2,
                "group": get_pharmacotherapeutic_group(driver),
                "name": safe_text(driver, By.CSS_SELECTOR, "h1.product__title"),
                "company": get_country(driver, "Производитель"),
                "country": get_country(driver, "Страна"),
                "price": safe_text(driver, By.CSS_SELECTOR, ".product__price"),
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

    output_path = os.path.join(os.path.expanduser("~"), "Downloads", "europharma_products_list.csv")
    pd.DataFrame(products).to_csv(output_path, index=False, encoding="utf-8-sig")
    print("Saved europharma_products_list.csv")


if __name__ == "__main__":
    main()
