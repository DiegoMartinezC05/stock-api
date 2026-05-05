from fastapi import FastAPI
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
from typing import List
import pandas as pd
import re
from io import StringIO
from pathlib import Path

app = FastAPI()

BASE_URL = "https://cabletechsupport.southwire.com/en/?country=US#"


class SpecBatchRequest(BaseModel):
    specs: List[str]


@app.get("/")
def home():
    return {"message": "Stock API is running"}


def parse_tables_from_csv(file_path):
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()

    lines = content.splitlines()
    tables = {}
    i = 0

    while i < len(lines):
        line = lines[i].strip().strip('"')

        if line.lower().startswith("table"):
            title = line
            i += 1

            while i < len(lines) and lines[i].strip().strip('"') == "":
                i += 1

            table_lines = []

            while i < len(lines):
                stripped = lines[i].strip().strip('"')

                if stripped == "":
                    j = i + 1
                    while j < len(lines) and lines[j].strip().strip('"') == "":
                        j += 1

                    if j >= len(lines):
                        break

                    next_line = lines[j].strip().strip('"').lower()

                    if next_line.startswith("table") or next_line.startswith("all dim"):
                        break

                    i += 1
                    continue

                table_lines.append(lines[i])
                i += 1

            if table_lines:
                try:
                    df = pd.read_csv(StringIO("\n".join(table_lines)), dtype=str)

                    if not df.empty:
                        df = df.drop(index=0).reset_index(drop=True)

                    df.columns = [str(col).strip().replace('"', "") for col in df.columns]
                    df = df.loc[:, [col for col in df.columns if col and not col.startswith("Unnamed")]]
                    df = df.apply(lambda col: col.str.strip() if col.dtype == object else col)

                    tables[title] = df
                except Exception:
                    pass
        else:
            i += 1

    return tables


def clean(value):
    if pd.isna(value):
        return ""
    value = str(value).strip()
    return "" if value.lower() == "nan" else value


def get_value(row, possible_names):
    for name in possible_names:
        for col in row:
            if col.lower().strip() == name.lower().strip():
                return clean(row.get(col, ""))
    return ""


def extract_page_fields(content):
    lower = content.lower()

    insulation = ""
    insulation_match = re.search(r"Insulation:\s*(.*)", content, re.IGNORECASE)
    if insulation_match:
        txt = insulation_match.group(1).split("\n")[0].strip()
        if "THHN/THWN-2" in txt.upper():
            insulation = "THHN/THWN-2"
        elif "THHN/THWN" in txt.upper():
            insulation = "THHN/THWN"
        else:
            insulation = txt

    ground = "TRUE" if re.search(r"Ground:\s*", content, re.IGNORECASE) else ""

    cable_class = ""
    if "class c" in lower:
        cable_class = "Class C"
    elif "class b" in lower:
        cable_class = "Class B"
    elif "solid copper" in lower:
        cable_class = "Solid"

    rating = ""
    if "tray cable" in lower:
        rating = "Tray Cable"
    elif "mc cable" in lower or "type mc" in lower:
        rating = "MC Cable"
    elif "building wire" in lower:
        rating = "Building Wire"

    cable_type = ""
    if "stranded" in lower or "strand" in lower:
        cable_type = "Stranded"
    elif "solid copper" in lower or "solid conductor" in lower:
        cable_type = "Solid"

    voltage = ""
    voltage_match = re.search(r"(\d+)\s*Volts", content, re.IGNORECASE)
    if voltage_match:
        voltage = voltage_match.group(1) + "V"

    return {
        "Insulation": insulation,
        "Ground": ground,
        "Class": cable_class,
        "Rating": rating,
        "Type": cable_type,
        "Voltage": voltage,
    }


def search_southwire(search_value):
    safe_value = re.sub(r"[^a-zA-Z0-9_-]", "_", search_value)
    download_file = Path(f"southwire_{safe_value}.csv")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(accept_downloads=True)

        page.goto(BASE_URL, wait_until="networkidle", timeout=60000)

        page.evaluate("""
        (search_value) => {
            const input = document.querySelector("#search_field");
            if (!input) throw new Error("Search input not found");

            input.value = search_value;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));

            const form = input.closest("form");
            if (form) form.submit();
        }
        """, search_value)

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        content = page.inner_text("body")
        current_url = page.url

        spec_match = re.search(r"SPEC\s+(\d+)", content)
        spec_no = spec_match.group(1) if spec_match else search_value

        page.wait_for_selector("text=Download Excel Table", timeout=30000)

        with page.expect_download() as download_info:
            page.locator("text=Download Excel Table").first.click()

        download = download_info.value
        download.save_as(str(download_file))

    return spec_no, content, current_url, download_file


def build_record(row, spec_no, page_fields, current_url):
    return {
        "Supplier": "Southwire",
        "SPEC NO.": spec_no,
        "Class": page_fields["Class"] or get_value(row, ["Cond. Strands", "Class", "Conductor Class"]),
        "Insulation": page_fields["Insulation"],
        "Voltage": page_fields["Voltage"] or "600V",
        "Ground": page_fields["Ground"] or get_value(row, ["Ground"]),
        "Rating": page_fields["Rating"],
        "Stock NO.": get_value(row, ["Stock Number", "Stock NO.", "Stock No", "Stock"]),
        "Conductor #": get_value(row, ["Cond. Number", "Conductor Number", "Conductor #"]),
        "Color Set": get_value(row, ["Insulation Color", "Color Set", "Color"]),
        "APPROX. OD (IN)": get_value(row, ["Approx. OD", "Diameter Over Armor", "Overall Diameter", "OD"]),
        "AWG": get_value(row, ["Cond. Size", "AWG", "Size", "Conductor Size"]),
        "Type": page_fields["Type"],
        "Other": "",
        "QESPN": "",
        "Link": current_url,
    }


def get_spec_records(spec_no):
    found_spec_no, content, current_url, file_path = search_southwire(spec_no)
    page_fields = extract_page_fields(content)
    tables = parse_tables_from_csv(file_path)

    table1 = next((df for title, df in tables.items() if "table 1" in title.lower()), None)

    if table1 is None:
        return {
            "SPEC NO.": found_spec_no,
            "count": 0,
            "data": [],
            "error": "Table 1 not found",
            "tables_found": list(tables.keys()),
            "Link": current_url,
        }

    records = [
        build_record(row.to_dict(), found_spec_no, page_fields, current_url)
        for _, row in table1.iterrows()
    ]

    records = [record for record in records if record["Stock NO."]]

    return {
        "SPEC NO.": found_spec_no,
        "count": len(records),
        "data": records,
    }


@app.get("/spec/{spec_no}")
def search_spec(spec_no: str):
    try:
        return get_spec_records(spec_no)
    except Exception as e:
        return {"SPEC NO.": spec_no, "error": str(e), "count": 0, "data": []}


@app.post("/spec/batch")
def search_spec_batch(request: SpecBatchRequest):
    all_records = []
    errors = []

    for spec_no in request.specs:
        spec_no = spec_no.strip()

        if not spec_no:
            continue

        try:
            result = get_spec_records(spec_no)
            all_records.extend(result.get("data", []))

            if result.get("error"):
                errors.append({
                    "SPEC NO.": spec_no,
                    "error": result.get("error")
                })

        except Exception as e:
            errors.append({
                "SPEC NO.": spec_no,
                "error": str(e)
            })

    return {
        "specs_requested": request.specs,
        "count": len(all_records),
        "data": all_records,
        "errors": errors,
    }