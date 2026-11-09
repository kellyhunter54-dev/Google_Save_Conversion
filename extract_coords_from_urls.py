#!/usr/bin/env python3
"""
Google Maps Coordinate & Address Extractor
Built for Google Takeout "Saved places" exports (Title, Note, URL, Tags, Comment),
but works with any CSV/Excel file that has a name column and a Google Maps URL column.

How it works:
  1. URL already has coordinates (!3d..!4d.. or @lat,lng)  -> parsed instantly, no network
  2. Headless Chrome (Selenium) opens the exact place in each link for everything else
     (Takeout links contain no coordinates, so most rows use this step)

Requirements:
  pip install selenium openpyxl
  (optional, only for old .xls files: pip install xlrd)
Chrome must be installed. Selenium 4.6+ downloads the matching driver automatically.
"""

import csv
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote

try:
    import openpyxl
    EXCEL_SUPPORT = True
except ImportError:
    EXCEL_SUPPORT = False

try:
    import xlrd  # only needed for legacy .xls files
    XLS_SUPPORT = True
except ImportError:
    XLS_SUPPORT = False

# ===== SETTINGS =====
SELENIUM_PAGE_TIMEOUT = 20  # max seconds to wait for a page to resolve its coordinates
ADDRESS_WAIT = 6            # max seconds to wait for the address panel after coords are found
SAVE_EVERY = 10             # write partial results to disk every N Selenium rows

OUTPUT_FIELDS = ['Place Name', 'URL', 'Longitude', 'Latitude', 'Full Address', 'Final URL', 'Method']



# ===== HELPERS =====
def clean_text(value):
    """Turn any cell value into a clean single-line string."""
    if value is None:
        return ''
    text = str(value)
    # Remove zero-width characters (Takeout sometimes includes them, e.g. "Koh​ Haa")
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Cf')
    # Collapse newlines and repeated spaces (some titles span two lines)
    return re.sub(r'\s+', ' ', text).strip()


def valid_coords(lat, lng):
    return lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180


def coords_from_url(url):
    """Pull coordinates straight out of a Maps URL if they are there."""
    if not url:
        return None, None
    # !3d!4d = the actual pin location (most accurate)
    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m:
        return float(m.group(1)), float(m.group(2))
    # @lat,lng = map centre (close to the pin for place URLs)
    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', url)
    if m:
        return float(m.group(1)), float(m.group(2))
    # ?q=lat,lng or ll=lat,lng
    m = re.search(r'[?&](?:q|ll|query)=(-?\d+\.\d+),\s*(-?\d+\.\d+)', url)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def coords_from_html(page):
    """Find the place coordinates in raw Maps HTML (no JavaScript needed)."""
    # Preview image meta tag: staticmap?center=LAT%2CLNG
    m = re.search(r'staticmap\?center=(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)', page)
    if m:
        return float(m.group(1)), float(m.group(2))
    # Pin location embedded in the page data
    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', page)
    if m:
        return float(m.group(1)), float(m.group(2))
    # Initial map state: APP_INITIALIZATION_STATE=[[[zoom, LNG, LAT]
    m = re.search(r'APP_INITIALIZATION_STATE=\[\[\[-?\d+(?:\.\d+)?,(-?\d+\.\d+),(-?\d+\.\d+)\]', page)
    if m:
        return float(m.group(2)), float(m.group(1))
    return None, None


# ===== BROWSER LOOKUP =====
def setup_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--lang=en-US')
    # Don't download images: pages load much faster
    opts.add_experimental_option('prefs', {
        'intl.accept_languages': 'en-US,en',
        'profile.managed_default_content_settings.images': 2,
    })
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(40)
    return driver


def fetch_via_selenium(driver, url):
    """Load the page and wait only as long as needed (no fixed sleeps)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException

    sep = '&' if '?' in url else '?'
    driver.get(url + sep + 'hl=en')

    # Wait until Maps rewrites the URL with the pin location
    try:
        WebDriverWait(driver, SELENIUM_PAGE_TIMEOUT, poll_frequency=0.25).until(
            lambda d: '!3d' in d.current_url
        )
    except TimeoutException:
        pass

    final_url = driver.current_url
    lat, lng = coords_from_url(final_url)
    if not valid_coords(lat, lng):
        lat, lng = coords_from_html(driver.page_source)
    if not valid_coords(lat, lng):
        lat, lng = None, None

    # Address button in the side panel
    address = None
    try:
        el = WebDriverWait(driver, ADDRESS_WAIT, poll_frequency=0.25).until(
            lambda d: d.find_element(By.CSS_SELECTOR, 'button[data-item-id="address"]')
        )
        label = el.get_attribute('aria-label') or el.text
        address = re.sub(r'^Address:\s*', '', label or '').strip() or None
    except TimeoutException:
        pass
    except Exception:
        pass

    return lat, lng, address, final_url


# ===== FILE READING =====
def read_csv_file(filepath):
    raw = Path(filepath).read_bytes()
    # utf-8-sig first so an Excel BOM doesn't corrupt the first header
    for enc in ('utf-8-sig', 'utf-16', 'cp1252', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if enc != 'utf-8-sig':
        print(f"  Detected encoding: {enc}")
    reader = csv.DictReader(text.splitlines(keepends=True))
    return reader.fieldnames, list(reader)


def read_excel_file(filepath):
    ext = Path(filepath).suffix.lower()
    if ext == '.xls':
        if not XLS_SUPPORT:
            print("ERROR: Reading .xls needs xlrd. Run: pip install xlrd  (or re-save the file as .xlsx)")
            return None, None
        book = xlrd.open_workbook(filepath)
        sheet = book.sheet_by_index(0)
        headers = [clean_text(v) for v in sheet.row_values(0)]
        rows = [dict(zip(headers, sheet.row_values(r))) for r in range(1, sheet.nrows)]
        return headers, rows

    if not EXCEL_SUPPORT:
        print("ERROR: Reading .xlsx needs openpyxl. Run: pip install openpyxl")
        return None, None
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not all_rows:
        return None, None
    headers = [clean_text(v) for v in all_rows[0]]
    rows = [dict(zip(headers, r)) for r in all_rows[1:]]
    return headers, rows


def detect_columns(headers):
    names = [h for h in headers if h]
    lower = {h: h.lower() for h in names}

    url_col = next((h for h in names if lower[h] in ('url', 'link', 'maps url', 'google maps url')), None)
    if not url_col:
        url_col = next((h for h in names if 'url' in lower[h] or 'link' in lower[h]), None)

    others = [h for h in names if h != url_col]
    title_col = next((h for h in others if lower[h] in ('title', 'name', 'place name', 'place')), None)
    if not title_col:
        title_col = next((h for h in others if any(k in lower[h] for k in ('title', 'name', 'place'))), None)
    return title_col, url_col


def write_results(output_file, results):
    # utf-8-sig so Excel on Windows shows Thai (and other non-English) text correctly
    with open(output_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(results)


# ===== FILE DIALOGS =====
def pick_input_file():
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    path = filedialog.askopenfilename(
        title="Select your data file",
        filetypes=[("All Supported Files", "*.csv *.xlsx *.xls"), ("CSV Files", "*.csv"),
                   ("Excel Files", "*.xlsx *.xls"), ("All Files", "*.*")],
        initialdir=str(Path.home() / 'Documents'),
    )
    root.destroy()
    return path


def pick_output_file(default_name):
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    path = filedialog.asksaveasfilename(
        title="Save results as",
        defaultextension=".csv",
        filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        initialdir=str(Path.home() / 'Documents'),
        initialfile=default_name,
    )
    root.destroy()
    return path


# ===== MAIN =====
def main():
    print("=" * 70)
    print("GOOGLE MAPS DATA EXTRACTOR")
    print("=" * 70)

    # Optional: pass the input file on the command line to skip the dialog
    input_file = sys.argv[1] if len(sys.argv) > 1 else pick_input_file()
    if not input_file:
        print("No file selected. Exiting.")
        return
    print(f"\n✓ Input: {input_file}")

    ext = Path(input_file).suffix.lower()
    if ext == '.csv':
        headers, rows = read_csv_file(input_file)
    elif ext in ('.xlsx', '.xls'):
        headers, rows = read_excel_file(input_file)
    else:
        print(f"ERROR: Unsupported file type: {ext}")
        return
    if not headers or not rows:
        print("ERROR: Could not read file or file is empty")
        return

    print(f"Detected columns: {', '.join(h for h in headers if h)}")
    title_col, url_col = detect_columns(headers)
    if not title_col or not url_col:
        print("\n⚠ Could not auto-detect columns")
        title_col = input("Enter column name for place names: ").strip()
        url_col = input("Enter column name for URLs: ").strip()
    print(f"✓ Place names: {title_col}   URLs: {url_col}")

    # Build the location list (skips blank rows like Takeout's empty second line)
    locations = []
    for row in rows:
        title = clean_text(row.get(title_col))
        url = clean_text(row.get(url_col))
        if url.startswith('http'):
            locations.append({'title': title or '(no name)', 'url': url})

    unique_urls = list(dict.fromkeys(loc['url'] for loc in locations))
    print(f"✓ Found {len(locations)} locations ({len(unique_urls)} unique URLs)")
    if not locations:
        print("ERROR: No valid Google Maps URLs found.")
        return

    default_out = Path(input_file).stem + "_with_coords.csv"
    output_file = sys.argv[2] if len(sys.argv) > 2 else pick_output_file(default_out)
    if not output_file:
        output_file = str(Path(input_file).with_name(default_out))
    print(f"✓ Output: {output_file}")

    title_for = {}
    for loc in locations:
        title_for.setdefault(loc['url'], loc['title'])

    # data[url] = dict(lat, lng, address, final_url, method)
    data = {}

    # ---- Step 1: coordinates already in the URL ----
    for url in unique_urls:
        lat, lng = coords_from_url(url)
        if valid_coords(lat, lng):
            data[url] = {'lat': lat, 'lng': lng, 'address': None, 'final_url': url, 'method': 'url'}
    print(f"\n[Step 1] Coordinates already in URL: {len(data)}")

    def needs_work(url):
        d = data.get(url)
        return not d or not valid_coords(d['lat'], d['lng']) or not d['address']

    def build_results():
        out = []
        for loc in locations:
            d = data.get(loc['url'], {})
            out.append({
                'Place Name': loc['title'],
                'URL': loc['url'],
                'Longitude': d.get('lng') if d.get('lng') is not None else '',
                'Latitude': d.get('lat') if d.get('lat') is not None else '',
                'Full Address': d.get('address') or '',
                'Final URL': d.get('final_url') or '',
                'Method': d.get('method', 'failed'),
            })
        return out

    todo = [u for u in unique_urls if needs_work(u)]
    print(f"[Step 2] Browser lookup for {len(todo)} places (about 3 to 5 seconds each)...")

    if todo:
        try:
            driver = setup_driver()
        except Exception as e:
            print(f"  ✗ Could not start Chrome: {e}")
            driver = None

        if driver:
            try:
                for i, url in enumerate(todo, 1):
                    print(f"  [{i}/{len(todo)}] {title_for[url][:60]}")
                    try:
                        lat, lng, address, final_url = fetch_via_selenium(driver, url)
                    except Exception as e:
                        print(f"    ✗ Error: {type(e).__name__}: {str(e)[:80]}")
                        continue
                    prev = data.get(url, {})
                    ok = valid_coords(lat, lng)
                    data[url] = {
                        'lat': lat if ok else prev.get('lat'),
                        'lng': lng if ok else prev.get('lng'),
                        'address': address or prev.get('address'),
                        'final_url': final_url,
                        'method': 'browser' if (ok or address) else prev.get('method', 'failed'),
                    }
                    d = data[url]
                    print(f"    {'✓' if d['lat'] is not None else '✗'} {d['lat']}, {d['lng']}"
                          f"   {'✓ address' if d['address'] else '✗ no address'}")
                    if i % SAVE_EVERY == 0:
                        write_results(output_file, build_results())
            finally:
                driver.quit()

    # ---- Write + summary ----
    results = build_results()
    write_results(output_file, results)

    total = len(results)
    got_coords = sum(1 for r in results if r['Latitude'] != '')
    got_addr = sum(1 for r in results if r['Full Address'])
    print("\n" + "=" * 70)
    print("EXTRACTION COMPLETE")
    print("=" * 70)
    print(f"Total locations:   {total}")
    print(f"Coordinates found: {got_coords} ({got_coords / total * 100:.1f}%)")
    print(f"Addresses found:   {got_addr} ({got_addr / total * 100:.1f}%)")
    missing = [r['Place Name'] for r in results if r['Latitude'] == '']
    if missing:
        print("\nStill missing coordinates:")
        for name in missing:
            print(f"  - {name}")
    print(f"\n✓ Saved to: {output_file}")


if __name__ == "__main__":
    main()
