import pandas as pd
import datetime
import re
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


service = Service(
    ChromeDriverManager().install()
)

options = webdriver.ChromeOptions()

options.add_argument("--start-maximized")

driver = webdriver.Chrome(
    service=service,
    options=options
)


urls = pd.read_csv(
    "product_urls.csv"
)

results = []

total_products = len(urls)



def get_name(driver):

    try:

        h1_elements = driver.find_elements(
            By.TAG_NAME,
            "h1"
        )

        for h1 in h1_elements:

            text = h1.text.strip()

            if text:

                return text

    except:
        pass

    return None


def get_price(driver):

    try:

        price_element = driver.find_element(
            By.XPATH,
            "//*[@price]"
        )

        return price_element.get_attribute(
            "price"
        )

    except:
        return None


def get_meta_description(driver):

    try:

        return driver.find_element(
            By.XPATH,
            "//meta[@property='og:description']"
        ).get_attribute(
            "content"
        )

    except:
        return ""


def get_brand(description):

    match = re.search(
        r"Бренд:\s*([^\.]+)",
        description
    )

    if match:

        return match.group(1).strip()

    return None


def get_country(description):

    match = re.search(
        r"Страна-производитель:\s*([^\.]+)",
        description
    )

    if match:

        return match.group(1).strip()

    return None


def get_breadcrumbs(driver):

    text = driver.find_element(
        By.TAG_NAME,
        "body"
    ).text

    lines = [
        x.strip()
        for x in text.split("\n")
        if x.strip()
    ]

    return lines


for index, row in urls.iterrows():

    product_url = row["product_url"]

    category = row.get(
        "main_category",
        None
    )

    subcategory = row.get(
        "subcategory",
        None
    )

    print(
        f"\n[{index + 1}/{total_products}]"
    )

    try:

        driver.get(
            product_url
        )

        time.sleep(5)

        result = {

            "snapshot_date":

                str(
                    datetime.date.today()
                ),

            "category":

                category,

            "subcategory":

                subcategory,

            "group":

                None,

            "product_name":

                None,

            "brand":

                None,

            "country":

                None,

            "price":

                None,

            "url":

                product_url
        }


        result["product_name"] = (
            get_name(driver)
        )


        result["price"] = (
            get_price(driver)
        )

        try:

            result["price"] = int(
                round(float(result["price"]))
            )
        except:
            pass


        description = (
            get_meta_description(driver)
        )

        result["brand"] = (
            get_brand(description)
        )

        result["country"] = (
            get_country(description)
        )


        try:

            lines = get_breadcrumbs(
                driver
            )

            if (
                category
                and subcategory
            ):

                for i, line in enumerate(lines):

                    if (
                        line == subcategory
                    ):

                        if (
                            i + 1 < len(lines)
                        ):

                            group_candidate = (
                                lines[i + 1]
                            )

                            if (
                                group_candidate
                                != result["product_name"]
                            ):

                                result["group"] = (
                                    group_candidate
                                )

                        break

        except:
            pass

        results.append(
            result
        )

        print(
            f"SUCCESS | "
            f"{result['product_name']}"
        )


        if (
            len(results) % 100 == 0
        ):

            temp_df = pd.DataFrame(
                results
            )

            temp_df.to_csv(
                "arbuz_products_list.csv",
                index=False,
                encoding="utf-8-sig"
            )

            print(
                f"AUTOSAVE: "
                f"{len(results)} products"
            )

    except Exception as e:

        print(
            f"ERROR | {product_url}"
        )

        print(e)

        continue


driver.quit()

df = pd.DataFrame(
    results
)

df.to_csv(
    f"arbuz_products_{datetime.date.today()}.csv",
    index=False,
    encoding="utf-8-sig"
)

print(
    "\n================================"
)

print(
    f"Saved {len(df)} products"
)

print(
    "================================"
)