from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
from typing import List, Dict, Optional
import pandas as pd
import re
from io import StringIO

app = FastAPI()

SPEC_URL = "https://cabletechsupport.southwire.com/en/search_products/?search_field={spec}"


class SpecBatchRequest(BaseModel):
    specs: List[str]


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Southwire Stock Lookup</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            background: #f0f2f5;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh;
        }
        .card {
            background: white; border-radius: 12px; padding: 48px 40px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.10);
            width: 100%; max-width: 560px; text-align: center;
        }
        h1 { font-size: 22px; color: #1a1a2e; margin-bottom: 6px; }
        .sub { font-size: 13px; color: #6b7280; margin-bottom: 28px; }
        textarea {
            width: 100%; padding: 12px 14px; font-size: 15px;
            border: 1.5px solid #d1d5db; border-radius: 8px;
            outline: none; resize: vertical; min-height: 120px;
            font-family: monospace;
        }
        textarea:focus { border-color: #e8141c; }
        button {
            margin-top: 14px; width: 100%; padding: 13px;
            font-size: 16px; font-weight: 600;
            background: #e8141c; color: white;
            border: none; border-radius: 8px;
            cursor: pointer; transition: background 0.15s;
        }
        button:hover { background: #c01118; }
        .hint { font-size: 12px; color: #9ca3af; margin-top: 10px; }

        /* Loading overlay */
        #loading-overlay {
            display: none;
            position: fixed; inset: 0;
            background: rgba(240, 242, 245, 0.92);
            backdrop-filter: blur(4px);
            z-index: 100;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 20px;
        }
        #loading-overlay.visible { display: flex; }
        .spinner {
            width: 44px; height: 44px;
            border: 4px solid #fecaca;
            border-top-color: #e8141c;
            border-radius: 50%;
            animation: spin 0.75s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text {
            font-size: 15px; font-weight: 600;
            color: #1a1a2e; letter-spacing: 0.01em;
        }
        .loading-sub {
            font-size: 12px; color: #6b7280; margin-top: -12px;
        }
    </style>
</head>
<body>
    <!-- Loading overlay -->
    <div id="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text" id="loading-spec-label">Looking up SPECs...</div>
        <div class="loading-sub">This may take a few seconds per SPEC</div>
    </div>

    <div class="card">
        <h1>Southwire Stock Lookup</h1>
        <p class="sub">Enter one or more SPEC numbers</p>
        <form id="search-form" action="/batch" method="get">
            <textarea name="specs" placeholder="60100&#10;60115&#10;45051&#10;10000"></textarea>
            <p class="hint">Separate SPEC numbers with commas, spaces, or new lines</p>
            <button type="submit">Search</button>
        </form>
    </div>

    <script>
        document.getElementById('search-form').addEventListener('submit', function (e) {
            const raw = this.querySelector('textarea[name="specs"]').value.trim();
            if (!raw) return;

            const specs = raw.split(/[\s,]+/).filter(Boolean);
            const label = specs.length === 1
                ? `Looking up SPEC ${specs[0]}...`
                : `Looking up ${specs.length} SPECs...`;

            document.getElementById('loading-spec-label').textContent = label;
            document.getElementById('loading-overlay').classList.add('visible');
        });
    </script>
</body>
</html>"""


def clean(value):
    if value is None or pd.isna(value):
        return ""
    v = str(value).strip()
    return "" if v.lower() in ("nan", "none", "null") else v


def get_value(row, possible_names):
    row_lower = {str(k).lower().strip(): v for k, v in row.items()}
    for name in possible_names:
        val = row_lower.get(name.lower().strip())
        if val is not None:
            return clean(val)
    return ""


def normalize_stock(value: str) -> str:
    v = clean(value)
    return v if re.match(r"^\d{5,}$", v) or v.upper() == "TBA" else ""


def normalize_awg(value: str) -> str:
    v = clean(value)
    if not v:
        return ""
    v = re.sub(r"\s*AWG.*", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s*KCMIL.*", "", v, flags=re.IGNORECASE)
    m = re.match(r"^\s*([\d/]+)", v)
    return m.group(1) if m else v.strip()


ALLOWED_INSULATION_TYPES = [
    "THHN/THWN-2", "THHN/THWN", "TFFN/TFN",
    "XHHW-2", "XHHW", "RHH/RHW-2", "RHH/RHW",
    "USE-2", "THHW", "THW-2", "THW",
    "MTW", "TFFN", "TFN",
]


def normalize_insulation(value: str) -> str:
    text = clean(value).upper()
    if not text:
        return ""
    for item in ALLOWED_INSULATION_TYPES:
        if re.search(rf"\b{re.escape(item.upper())}\b", text):
            return item
    return ""


COLOR_WORDS = {
    "BLACK", "WHITE", "RED", "BLUE", "GREEN", "YELLOW", "BROWN",
    "ORANGE", "PURPLE", "VIOLET", "GRAY", "GREY", "PINK", "TAN",
    "BK", "WH", "RD", "BL", "BU", "GN", "YW", "YL", "BR", "OR",
    "PU", "VT", "GY", "PK", "TN", "GR", "PE"
}


def normalize_color(value: str) -> str:
    v = clean(value)
    if not v:
        return ""
    original = v.strip()
    v = v.upper().replace("\\", "/")
    v = re.sub(r"\s+", "", v)
    if v in {"N/A", "NA", "NONE", "TBA", "-"}:
        return ""
    if re.search(r"\d", v):
        return ""
    parts = re.split(r"[/,-]", v)
    if all(part in COLOR_WORDS for part in parts if part):
        if "/" in v or "-" in v or len(v) <= 3:
            return v
        return original.title()
    if v in COLOR_WORDS:
        if len(v) <= 3:
            return v
        return original.title()
    return original


# OD column name variants to check in table headers
OD_COLUMN_NAMES = [
    "approx. od (in)",
    "approx od (in)",
    "approx. od",
    "approx od",
    "od (in)",
    "od(in)",
    "overall diameter (in)",
    "overall diameter",
    "diameter over insulation (in)",
    "diameter over insulation",
    "diameter over conductor (in)",
    "diameter over conductor",
    "outer diameter (in)",
    "outer diameter",
    "cable od",
    "od",
]


def normalize_od(value: str) -> str:
    """Return a clean numeric OD string or empty string."""
    v = clean(value)
    if not v:
        return ""
    # strip units like "in", "inches", '"'
    v = re.sub(r'["\']', "", v)
    v = re.sub(r"\s*(in|inch|inches)\s*$", "", v, flags=re.IGNORECASE).strip()
    # accept decimals and fractions like 0.123 or .456
    m = re.match(r"^\d*\.?\d+$", v)
    return v if m else ""


def extract_od_from_text(content: str) -> str:
    """
    Fallback: scrape a single OD value from free text on the page.
    Tries several label patterns and returns the first match.
    """
    patterns = [
        r"Approx\.?\s*O\.?D\.?\s*[:\-]?\s*([\d.]+)\s*(?:in|inch|inches|\")?",
        r"Overall\s+Diameter\s*[:\-]?\s*([\d.]+)\s*(?:in|inch|inches|\")?",
        r"Diameter\s+Over\s+(?:Insulation|Conductor|Jacket)\s*[:\-]?\s*([\d.]+)\s*(?:in|inch|inches|\")?",
        r"Outer\s+Diameter\s*[:\-]?\s*([\d.]+)\s*(?:in|inch|inches|\")?",
        r"Cable\s+O\.?D\.?\s*[:\-]?\s*([\d.]+)\s*(?:in|inch|inches|\")?",
        r"O\.?D\.?\s*[:\-]\s*([\d.]+)\s*(?:in|inch|inches|\")?",
    ]
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            v = normalize_od(m.group(1))
            if v:
                return v
    return ""


def extract_page_fields(content) -> Dict:
    lower = content.lower()

    insulation = ""
    m = re.search(r"Insulation:\s*(.*)", content, re.IGNORECASE)
    if m:
        insulation = normalize_insulation(m.group(1).split("\n")[0])

    ground = "TRUE" if re.search(r"Ground:\s*", content, re.IGNORECASE) else ""

    cable_class = ""
    cm = re.search(r"\bClass\s+([A-Z])\b", content, re.IGNORECASE)
    if cm:
        cable_class = cm.group(1).upper()
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
    vm = re.search(r"(\d+)\s*[- ]?[Vv]olt", content, re.IGNORECASE)
    if vm:
        voltage = vm.group(1) + "V"

    # Page-level OD fallback (single value for whole SPEC)
    page_od = extract_od_from_text(content)

    return {
        "Insulation": insulation,
        "Ground": ground,
        "Class": cable_class,
        "Rating": rating,
        "Type": cable_type,
        "Voltage": voltage,
        "OD": page_od,
    }


def parse_visible_web_tables(page) -> pd.DataFrame:
    tables = page.locator("table")
    all_rows = []

    for t in range(tables.count()):
        table = tables.nth(t)
        rows = table.locator("tr")

        headers = []
        current_awg = ""

        for r in range(rows.count()):
            cells = rows.nth(r).locator("th, td")
            values = [cells.nth(i).inner_text().strip() for i in range(cells.count())]

            if not values:
                continue

            row_text = " ".join(values).strip()

            awg_match = re.match(r"^(\d+(?:/\d+)?)\s*AWG$", row_text, re.IGNORECASE)
            if awg_match:
                current_awg = awg_match.group(1)
                continue

            if any("Stock" in v for v in values) and any("Color" in v for v in values):
                headers = values
                continue

            if headers and len(values) == len(headers):
                rec = dict(zip(headers, values))
                if current_awg:
                    rec["AWG"] = current_awg
                all_rows.append(rec)

    return pd.DataFrame(all_rows)


def parse_southwire_text_tables(content: str) -> pd.DataFrame:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    rows = []

    for i, line in enumerate(lines):
        lower_line = line.lower()

        if "table 3" in lower_line and "color" in lower_line:
            if i + 1 >= len(lines):
                continue

            header = lines[i + 1]
            header = header.replace("Size (Strand)", "Size(Strand) ")
            headers = header.split()

            if len(headers) < 2:
                continue

            color_headers = headers[1:]

            for row_line in lines[i + 2:]:
                row_lower = row_line.lower()

                if row_lower.startswith(("table", "notes", "revision", "updated", "all dimensions")):
                    break

                parts = row_line.split()

                if len(parts) < 2:
                    continue

                size = normalize_awg(parts[0])

                if len(parts) > 2 and re.match(r"^\(\d+\)$", parts[1]):
                    stock_values = parts[2:]
                else:
                    stock_values = parts[1:]

                for color, stock in zip(color_headers, stock_values):
                    stock = normalize_stock(stock)
                    color = normalize_color(color)

                    if stock and color:
                        rows.append({
                            "Stock Number": stock,
                            "Cond. Size": size,
                            "Cond. Number": "1",
                            "Color Set": color,
                            "Jacket Color": color,
                            "AWG": size,
                        })

    return pd.DataFrame(rows)


def fetch_southwire(spec_no: str):
    url = SPEC_URL.format(spec=spec_no)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        content = page.inner_text("body")
        current_url = page.url

        web_table = parse_visible_web_tables(page)

        if web_table.empty:
            web_table = parse_southwire_text_tables(content)

        spec_match = re.search(r"SPEC\s+(\d+)", content)
        found_spec_no = spec_match.group(1) if spec_match else spec_no

        browser.close()

    return found_spec_no, content, current_url, web_table


def make_base(spec_no, page_fields, current_url) -> Dict:
    return {
        "Supplier": "Southwire",
        "SPEC NO.": spec_no,
        "Stock NO.": "",
        "QES PN": "",
        "Conductor #": "",
        "Color Set": "",
        "AWG": "",
        "Class": page_fields["Class"],
        "Insulation": page_fields["Insulation"],
        "Voltage": page_fields["Voltage"],
        "Ground": page_fields["Ground"],
        "Rating": page_fields["Rating"],
        "Type": page_fields["Type"],
        "Approx. OD (in)": page_fields["OD"],
        "Link": current_url,
    }


def build_records_from_web_table(web_table, spec_no, page_fields, current_url) -> List[Dict]:
    records = []

    if web_table is None or web_table.empty:
        return records

    for _, row in web_table.iterrows():
        stock = normalize_stock(get_value(row, [
            "Stock Number", "Stock No.", "Stock NO.", "Stock No",
            "Stock #", "Stock"
        ]))

        if not stock:
            continue

        color = normalize_color(get_value(row, [
            "Jacket Color", "Color", "Color Set",
            "Insulation Color", "Conductor Color", "Wire Color"
        ]))

        awg = normalize_awg(get_value(row, [
            "AWG", "Cond. Size", "Cond Size", "Conductor Size", "Size"
        ]))

        cond_num = get_value(row, [
            "Cond. Number", "Conductor Number", "Cond Number",
            "No. of Conductors", "Cond. No", "Number of Conductors",
            "# of Conductors", "Conductors"
        ])

        # Try to get OD from the row first; fall back to page-level OD
        row_od = normalize_od(get_value(row, OD_COLUMN_NAMES))
        od = row_od if row_od else page_fields["OD"]

        rec = make_base(spec_no, page_fields, current_url)
        rec.update({
            "Stock NO.": stock,
            "Conductor #": cond_num,
            "Color Set": color,
            "AWG": awg,
            "Approx. OD (in)": od,
        })

        records.append(rec)

    return records


def get_spec_records(spec_no: str) -> Dict:
    spec_no = spec_no.strip()

    found_spec_no, content, current_url, web_table = fetch_southwire(spec_no)
    page_fields = extract_page_fields(content)

    records = build_records_from_web_table(
        web_table,
        found_spec_no,
        page_fields,
        current_url
    )

    if records:
        return {"data": records}

    return {
        "error": f"No website table found for SPEC {spec_no}",
        "data": []
    }


def parse_spec_list(raw: str) -> List[str]:
    return [s.strip() for s in re.split(r"[\s,]+", raw.strip()) if s.strip()]


def result_html(title: str, records: List[Dict]) -> str:
    df = pd.DataFrame(records)

    if records:
        table_html = df.to_html(index=False, classes="data-table", border=0)
    else:
        table_html = "<p>No records found.</p>"

    count = len(records)
    encoded_specs = title.replace(",", " ")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Segoe UI', Arial, sans-serif;
            background: #f0f2f5;
            padding: 32px 24px;
        }}
        .header {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .header h2 {{
            font-size: 20px;
            font-weight: 700;
            color: #1a1a2e;
        }}
        .header p {{
            font-size: 13px;
            color: #6b7280;
            margin-top: 4px;
        }}
        .actions {{
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }}
        .btn {{
            padding: 9px 18px;
            font-size: 14px;
            font-weight: 600;
            border-radius: 7px;
            text-decoration: none;
            display: inline-block;
            cursor: pointer;
            border: 1.5px solid #d1d5db;
            background: white;
            color: #374151;
        }}
        .btn-copy {{
            background: #e8141c;
            color: white;
            border-color: #e8141c;
        }}
        .btn-copy.copied {{
            background: #16a34a;
            border-color: #16a34a;
        }}
        .table-wrapper {{
            background: white;
            border-radius: 10px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.07);
            overflow-x: auto;
        }}
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .data-table thead th {{
            background: #1a1a2e;
            color: white;
            padding: 11px 14px;
            text-align: left;
            font-weight: 600;
            white-space: nowrap;
        }}
        .data-table tbody tr:nth-child(even) {{
            background: #f8f9fb;
        }}
        .data-table tbody tr:hover {{
            background: #fef2f2;
        }}
        .data-table tbody td {{
            padding: 9px 14px;
            color: #374151;
            border-bottom: 1px solid #f0f0f0;
            white-space: nowrap;
        }}
        .footer {{
            margin-top: 14px;
            font-size: 12px;
            color: #9ca3af;
            text-align: right;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h2>{title} — {count} items</h2>
            <p>Click Copy Table, then paste directly into Excel or SharePoint grid view.</p>
        </div>
        <div class="actions">
            <button class="btn btn-copy" id="copyBtn" onclick="copyTable()">Copy Table</button>
            <a class="btn" href="/download?specs={encoded_specs}">Download CSV</a>
            <a class="btn" href="/">New Search</a>
        </div>
    </div>

    <div class="table-wrapper">{table_html}</div>
    <div class="footer">Data sourced directly from Southwire website tables</div>

    <script>
        function copyTable() {{
            const table = document.querySelector(".data-table");
            if (!table) return;

            let text = "";
            for (const row of table.rows) {{
                const cells = Array.from(row.cells).map(cell => cell.innerText.trim());
                text += cells.join("\\t") + "\\n";
            }}

            navigator.clipboard.writeText(text).then(() => {{
                const btn = document.getElementById("copyBtn");
                btn.innerText = "Copied";
                btn.classList.add("copied");
                setTimeout(() => {{
                    btn.innerText = "Copy Table";
                    btn.classList.remove("copied");
                }}, 1800);
            }});
        }}
    </script>
</body>
</html>"""


@app.get("/batch", response_class=HTMLResponse)
def batch(specs: str):
    spec_list = parse_spec_list(specs)
    all_records = []
    errors = []

    for spec in spec_list:
        result = get_spec_records(spec)

        if result.get("error"):
            errors.append(result["error"])

        all_records.extend(result.get("data", []))

    title = ", ".join(spec_list)

    if errors and not all_records:
        return HTMLResponse(
            "<br>".join(errors) + '<br><br><a href="/">Back</a>',
            status_code=500
        )

    return result_html(title, all_records)


@app.post("/api/batch")
def api_batch(req: SpecBatchRequest):
    all_records = []
    errors = []

    for spec in req.specs:
        result = get_spec_records(spec)

        if result.get("error"):
            errors.append(result["error"])

        all_records.extend(result.get("data", []))

    return {
        "count": len(all_records),
        "errors": errors,
        "data": all_records
    }


@app.get("/download")
def download(specs: str):
    spec_list = parse_spec_list(specs)
    all_records = []

    for spec in spec_list:
        result = get_spec_records(spec)
        all_records.extend(result.get("data", []))

    df = pd.DataFrame(all_records)
    output = StringIO()
    df.to_csv(output, index=False)

    response = StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv"
    )

    response.headers["Content-Disposition"] = "attachment; filename=southwire_results.csv"
    return response