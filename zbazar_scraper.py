import re
import time
from datetime import datetime

import pandas as pd

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


catalog = pd.read_csv(
    "zbazar_catalog.csv"
)


driver = webdriver.Chrome(
    service=Service(
        ChromeDriverManager().install()
    )
)

results = []


PRODUCT_END_MARKERS = {
    "В корзину",
    "Открыть товар"
}

BAD_NAMES = {
    "Мы на связи!👋",
    "Главная",
    "Каталог",
    "Клиентам",
    "Магазины",
    "Базары",
    "Google Play",
    "App Store",
    "Цена магазина",
    "Гарантия качества",
    "Открыть в приложении",
    "Скопировать ссылку",
    "В корзину",
    "Открыть товар"
}


def clean_price(price):

    if not price:
        return None

    price = (
        str(price)
        .replace("〒", "")
        .replace("₸", "")
        .replace(" ", "")
        .replace(",", ".")
        .strip()
    )

    try:
        return float(price)
    except:
        return None


def is_price(text):

    text = text.strip()

    return bool(
        re.match(
            r"^\d[\d\s.,]*(〒|₸)?$",
            text
        )
    )


def extract_country(product_name):

    match = re.search(
        r"\((.*?)\)",
        str(product_name)
    )

    if not match:
        return ""

    country = match.group(1).strip()

    if len(country) > 30:
        return ""

    return country


for idx, row in catalog.iterrows():

    main_category = row["main_category"]
    subcategory = row["subcategory"]
    category_url = row["url"]

    print(
        f"\n[{idx+1}/{len(catalog)}]"
    )

    print(main_category)
    print(subcategory)

    try:

        driver.get(category_url)

        time.sleep(5)

        while True:

            try:

                button = driver.find_element(
                    By.XPATH,
                    "//button[contains(., 'Показать ещё')]"
                )

                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});",
                    button
                )

                time.sleep(1)

                driver.execute_script(
                    "arguments[0].click();",
                    button
                )

                time.sleep(3)

            except:
                break

        body = driver.find_element(
            By.TAG_NAME,
            "body"
        ).text

        lines = [
            x.strip()
            for x in body.split("\n")
            if x.strip()
        ]


        products = []
        current_block = []

        for line in lines:

            current_block.append(line)

            if line in PRODUCT_END_MARKERS:

                products.append(
                    current_block.copy()
                )

                current_block = []

        print(
            f"Found product blocks: {len(products)}"
        )


        for block in products:

            try:

                discount = ""
                product_name = ""
                current_price = None
                old_price = None
                unit = ""

                for item in block:

                    if (
                        item.startswith("-")
                        and "%" in item
                    ):
                        discount = item
                        break


                prices = []

                for item in block:

                    if is_price(item):

                        cleaned = clean_price(item)

                        if cleaned is not None:
                            prices.append(cleaned)

                if len(prices) >= 1:
                    current_price = prices[0]

                if len(prices) >= 2:
                    old_price = prices[1]

                for item in block:

                    if item.startswith("/"):

                        unit = item
                        break

                for item in block:

                    item = item.strip()

                    if not item:
                        continue

                    if item in BAD_NAMES:
                        continue

                    if item.startswith(
                        "Товары в категории"
                    ):
                        continue

                    if item.startswith(
                        "Категории товаров"
                    ):
                        continue

                    if item.startswith(
                        "Минимальный заказ"
                    ):
                        continue

                    if item.startswith(
                        "Доставка"
                    ):
                        continue

                    if item.startswith(
                        "Экономия"
                    ):
                        continue

                    if item.startswith("/"):
                        continue

                    if is_price(item):
                        continue

                    if (
                        item.startswith("-")
                        and "%" in item
                    ):
                        continue

                    product_name = item
                    break

                if not product_name:
                    continue

                if (
                    "Товары в категории"
                    in product_name
                ):
                    continue

                if (
                    "Категории товаров"
                    in product_name
                ):
                    continue

                if (
                    "Минимальный заказ"
                    in product_name
                ):
                    continue

                if current_price is None:
                    continue


                results.append(
                    {
                        "snapshot_date":
                            datetime.now().date(),

                        "scrape_timestamp":
                            datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),

                        "main_category":
                            main_category,

                        "subcategory":
                            subcategory,

                        "product_name":
                            product_name,

                        "country":
                            extract_country(
                                product_name
                            ),

                        "price_current":
                            current_price,

                        "price_old":
                            old_price,

                        "unit":
                            unit,

                        "discount":
                            discount,

                        "category_url":
                            category_url
                    }
                )

            except:
                continue


        pd.DataFrame(
            results
        ).to_csv(
            "zbazar_products_temp.csv",
            index=False,
            encoding="utf-8-sig"
        )

        print(
            f"Collected: {len(results)}"
        )

    except Exception as e:

        print(
            f"ERROR: {category_url}"
        )

        print(e)


driver.quit()

df = pd.DataFrame(results)

df = df[
    ~df["product_name"].str.startswith(
        "Товары в категории",
        na=False
    )
]

df = df[
    ~df["product_name"].str.startswith(
        "Категории товаров",
        na=False
    )
]

df["country"] = (
    df["country"]
    .fillna("")
    .astype(str)
    .str.strip()
)

df.drop_duplicates(
    subset=[
        "main_category",
        "subcategory",
        "product_name",
        "price_current"
    ],
    inplace=True
)

df.to_csv(
    "zbazar_products.csv",
    index=False,
    encoding="utf-8-sig"
)

df.to_csv(
    f"zbazar_products_{datetime.now().date()}.csv",
    index=False,
    encoding="utf-8-sig"
)

print("\nDONE")
print(
    f"FINAL PRODUCTS: {len(df)}"
)