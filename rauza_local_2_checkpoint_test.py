import time
import os
import re
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime
#from cloud_storage import *
#from load_to_bq import *

#os.environ["DISPLAY"] = ":99"


BASE_URL = "https://rauza-ade.kz/products"
CATALOG_URL = "https://rauza-ade.kz/catalog/lekarstvennye-sredstva" 


def save_checkpoint(data, output_path):
    temp_path = output_path + ".tmp"
    pd.DataFrame(data).to_csv(temp_path, index=False, encoding="utf-8-sig")
    os.replace(temp_path, output_path)


def load_checkpoint(output_path):
    if not os.path.exists(output_path):
        return []

    try:
        checkpoint = pd.read_csv(output_path).where(pd.notna, None)
        print(f"Checkpoint loaded: {len(checkpoint)} products already scraped")
        return checkpoint.to_dict("records")
    except Exception as error:
        print(f"Could not load checkpoint: {error}")
        return []

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
                (By.XPATH, "//button[contains(normalize-space(.),'Да')]")
            )
        )
        btn.click()
        time.sleep(2)
        print("City popup accepted")
    except:
        print("City popup not found")

def wait_for_product_page(driver):
    try: 
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located(
                (By.XPATH, 
                 "//span[normalize-space()='Фирма-производитель'] | //span[normalize-space()='Производитель']")
            )
        )
        time.sleep(0.5)
    except TimeoutException:
        pass


def safe_text(driver, by, selector):
    try:
        return driver.find_element(by, selector).text.strip()
    except:
        return None

def get_price(driver):
    try:
        elements = driver.find_elements(
            By.XPATH,
            "//*[contains(.,'₸') and not(.//*[contains(.,'₸')])]"
        )

        for element in elements:
            full_text = element.get_attribute("textContent") or ""
            match = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d+)?\s*₸", full_text)
            if match:
                return re.sub(r"[\s\u00a0]+", " ", match.group(0)).strip()
    except:
        pass

    return safe_text(driver, By.XPATH, "//*[contains(text(),'₸')]/parent::*")
    
def get_country(driver, label):
    try:
        return driver.find_element(By.XPATH, f"//span[normalize-space()='{label}']/following-sibling::span").text.strip()
    except:
        return None


def get_categories(driver):
    try:
        container = driver.find_element(By.XPATH, "//div[contains(@class,'mb-4')][.//button[contains(@class,'el-button--small')]]")

        spans = container.find_elements(By.XPATH, ".//button[contains(@class,'el-button--small')]//span[1]")

        raw_cats = [s.text.strip() for s in spans if s.text.strip()]

        # deduplication
        cats = []
        for c in raw_cats:
            if c not in cats:
                cats.append(c)
    except:
        cats = []

    cat1 = cats[0] if len(cats) > 0 else None
    cat2 = cats[1] if len(cats) > 1 else None
    cat3 = cats[2] if len(cats) > 2 else None

    return cat1, cat2, cat3

def scrape(driver, output_path, data=None, max_pages=None):
    # wait for initial products
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located(
            (By.XPATH, "//a[contains(@href,'/products/')]")
        )
    )

    # === CHANGE 1: track catalog tab + open one reusable product tab ===
    catalog_window = driver.current_window_handle
    driver.execute_script("window.open('about:blank');")
    product_window = driver.window_handles[-1]
    # === END CHANGE 1 ===

    data = data or []
    product_links = {
        item["url"] for item in data
        if item.get("url")
    }
    pages_loaded = 1

    while True:
        # collect product links currently loaded
        cards = driver.find_elements(By.XPATH, "//a[contains(@href,'/products/')]")

        # === CHANGE 3: collect into a page-local set instead of adding straight into product_links ===
        current_links = set()
        for a in cards:
            href = a.get_attribute("href")
            if not href:
                continue
            if href.startswith("http"):
                current_links.add(href)
            else:
                current_links.add("https://rauza-ade.kz" + href)

        new_links = current_links - product_links
        print(f"Page {pages_loaded}: {len(new_links)} new products found")
        # === END CHANGE 3 ===

        # === CHANGE 4: scrape the new links immediately, right here, instead of later ===
        driver.switch_to.window(product_window)

        for idx, link in enumerate(new_links, 1):
            print(f"  [{idx}/{len(new_links)}] Scraping {link}")
            driver.get(link)

            wait_for_product_page(driver)

            cat1, cat2, cat3 = get_categories(driver)

            company = safe_text(driver, By.XPATH, "//span[normalize-space()='Фирма-производитель']/following-sibling::span")
            if not company:
                company = safe_text(driver, By.XPATH, "//span[normalize-space()='Производитель']/following-sibling::span")

            product = {
                "category": cat1,
                "subcategory": cat2,
                "group": cat3,
                "name": safe_text(driver, By.CSS_SELECTOR, "h1"),
                "company": company,
                "country": get_country(driver, "Страна"),
                "price": get_price(driver),
                "url": link,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            data.append(product)
            save_checkpoint(data, output_path)
            product_links.add(link)
            print(f"  Checkpoint saved: {len(data)} products")

        driver.switch_to.window(catalog_window)
        product_links.update(new_links)
        # === END CHANGE 4 ===

        # stop if reached page limit
        if max_pages is not None and pages_loaded >= max_pages:
            print("Reached max_pages limit")
            break

        # try loading more
        try:
            load_more = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'Показать')]"))
            )
            driver.execute_script("arguments[0].click();", load_more)
            pages_loaded += 1
            time.sleep(2)
        except:
            print("No more pages to load")
            break

    print(f"Total product links collected: {len(product_links)}")

    # === CHANGE 5: removed the entire old second loop that used to scrape here ===
    # (it's no longer needed — scraping now happens inside the while loop above)

    return data



def main():
    output_path = os.path.join(os.path.expanduser("~"), "Downloads", "rauza_products_list_checkpoint_test.csv")
    products = load_checkpoint(output_path)
    driver = None

    try:
        driver = init_driver()
        driver.get(CATALOG_URL)

        accept_city_popup(driver)
        products = scrape(driver, output_path, products)
        save_checkpoint(products, output_path)
        print("Saved rauza_products_list_checkpoint_test.csv")
    finally:
        if driver is not None:
            driver.quit()

if __name__ == "__main__":
    main()
