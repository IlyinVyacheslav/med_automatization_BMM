import json
import os
import re
import time
from bs4 import BeautifulSoup
import requests

OUTPUT_DIR = "data_txt"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def extract_clean_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "lxml")

    title_tag = soup.find(id="firstHeading")
    page_title = title_tag.get_text(strip=True) if title_tag else ""

    main_content = soup.find(id="bodyContent")
    if not main_content:
        main_content = soup.find(class_="mw-parser-output")
    if not main_content:
        main_content = soup.body if soup.body else soup

    garbage_selectors = [
        "script",
        "style",
        "table",
        "div.navbox",
        "div.reflist",
        "div.printfooter",
        "span.mw-editsection",
        "sup.reference",
        "div.mw-references-wrap",
        "div.toc",
    ]
    for selector in garbage_selectors:
        for element in main_content.select(selector):
            element.decompose()

    text_blocks = []

    if page_title:
        text_blocks.append(f"# {page_title}\n")

    for element in main_content.find_all(["h1", "h2", "h3", "h4", "p"]):
        text = element.get_text(separator=" ", strip=True)

        if text:
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r"\[\d+\]", "", text)
            text = re.sub(r"\[источник\s*не\s*указан[^\]]*\]", "", text)
            text = re.sub(r"\[нет\s*в\s*источнике\]", "", text)
            text = re.sub(r"\s+([.,;:?!])", r"\1", text)
            text = text.strip()

            if not text:
                continue

            if element.name in ["h1", "h2", "h3", "h4"]:
                stop_words = [
                    "см. также",
                    "примечания",
                    "литература",
                    "ссылки",
                    "источники",
                ]
                if text.lower() in stop_words:
                    continue
                text_blocks.append(f"\n### {text}\n")
            else:
                text_blocks.append(text)

    full_text = "\n".join(text_blocks)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)

    return full_text.strip()


def main():
    with open("config_data.json", "r", encoding="utf-8") as f:
        config = json.load(f)

    headers = {
        "User-Agent": "ClinicBotDataFetcher/1.0 (contacts: admin@myclinic.local)"
    }

    for item in config["wiki_dataset"]:
        title = item["title"]
        url = item["url"]

        print(f"Скачивание: {title}...")

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            clean_text = extract_clean_text(response.text)

            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
            file_path = os.path.join(OUTPUT_DIR, f"{safe_title}.txt")

            with open(file_path, "w", encoding="utf-8") as out_f:
                out_f.write(clean_text)

            print(f"Успешно сохранено -> {file_path}")

        except Exception as e:
            print(f"Ошибка при обработке {title}: {e}")

        time.sleep(1.5)


if __name__ == "__main__":
    main()