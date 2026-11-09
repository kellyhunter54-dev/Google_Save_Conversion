#!/usr/bin/env python3
"""
Google Maps Coordinate & Address Extractor using Selenium
Handles both CSV and Excel files (.csv, .xlsx, .xls)
"""

import csv
import time
import re
import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from tkinter import Tk, filedialog

# Try to import openpyxl for Excel support
try:
    import openpyxl
    EXCEL_SUPPORT = True
except ImportError:
    EXCEL_SUPPORT = False
    print("Note: Install openpyxl for Excel support: pip install openpyxl")

def setup_driver():
    """Set up Chrome driver with options"""
    chrome_options = Options()
    chrome_options.add_argument('--headless')  # Run in background
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--lang=en-US')
    
    driver = webdriver.Chrome(options=chrome_options)
    return driver

def extract_coordinates_from_url(url):
    """Extract coordinates from Google Maps URL"""
    # Pattern 1: !3d!4d format (MOST ACCURATE - actual pin location)
    pattern1 = r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'
    match = re.search(pattern1, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern 2: @lat,lng format (map viewport - less accurate)
    pattern2 = r'@(-?\d+\.\d+),(-?\d+\.\d+)'
    match = re.search(pattern2, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    # Pattern 3: /place/name/@lat,lng format
    pattern3 = r'/place/[^/]+/@(-?\d+\.\d+),(-?\d+\.\d+)'
    match = re.search(pattern3, url)
    if match:
        return float(match.group(1)), float(match.group(2))
    
    return None, None

def extract_coordinates_from_page_source(driver):
    """Extract coordinates from page source - more accurate than URL"""
    try:
        page_source = driver.page_source
        
        # Look for coordinates in various patterns in the page source
        # Prioritized by accuracy
        patterns = [
            r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',  # Most accurate - actual pin location
            r'"latitude":([0-9.-]+),"longitude":([0-9.-]+)',
            r'\[null,null,([0-9.-]+),([0-9.-]+)\]',
            r'center:\{lat:([0-9.-]+),lng:([0-9.-]+)\}',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, page_source)
            if matches:
                # Get the LAST match for all patterns (most accurate/final value)
                lat, lng = matches[-1]
                print(f"  DEBUG: Found coordinates using pattern: {pattern[:30]}...")
                print(f"  DEBUG: Total matches: {len(matches)}, Using last match")
                return float(lat), float(lng)
        
        return None, None
    except:
        return None, None

def extract_address_from_page(driver):
    """Extract full address from Google Maps page"""
    try:
        # Wait a bit for address to load
        time.sleep(1)
        
        # Method 1: Try to find address button
        try:
            address_elements = driver.find_elements(By.CSS_SELECTOR, '[data-item-id^="address"]')
            if address_elements:
                return address_elements[0].get_attribute('aria-label').replace('Address: ', '')
        except:
            pass
        
        # Method 2: Look for aria-label with "Address:"
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, '[aria-label*="Address:"]')
            for elem in elements:
                label = elem.get_attribute('aria-label')
                if 'Address:' in label:
                    return label.replace('Address: ', '')
        except:
            pass
        
        # Method 3: Try to find address in divs
        try:
            divs = driver.find_elements(By.TAG_NAME, 'div')
            for div in divs:
                text = div.text
                if 'Thailand' in text and len(text) < 200:
                    return text
        except:
            pass
        
        return None
    except Exception as e:
        return None

def extract_data_from_maps(driver, url, location_name):
    """Open Google Maps URL and extract coordinates and address"""
    if not url or url.strip() == '':
        return None, None, None, None
    
    try:
        print(f"  Opening URL...")
        driver.get(url)
        
        # Wait for initial page load
        time.sleep(3)
        
        # Wait for URL to stabilize (check if it stops changing)
        previous_url = ""
        stable_count = 0
        max_attempts = 10
        
        for attempt in range(max_attempts):
            current_url = driver.current_url
            
            if current_url == previous_url:
                stable_count += 1
                if stable_count >= 3:  # URL hasn't changed for 3 checks
                    break
            else:
                stable_count = 0
            
            previous_url = current_url
            time.sleep(1)
        
        # One final wait to ensure everything is loaded
        time.sleep(2)
        
        # Get the final URL after all redirects
        final_url = driver.current_url
        
        # Extract from URL FIRST - it's most reliable for !3d!4d format
        lat, lng = extract_coordinates_from_url(final_url)
        
        # Only try page source if URL extraction failed completely
        if lat is None or lng is None:
            print(f"  DEBUG: URL extraction failed, trying page source...")
            lat, lng = extract_coordinates_from_page_source(driver)
        
        # Extract address from page
        address = extract_address_from_page(driver)
        
        return lat, lng, address, final_url
        
    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return None, None, None, None

def read_excel_file(filepath):
    """Read Excel file and return list of rows"""
    if not EXCEL_SUPPORT:
        print("ERROR: openpyxl not installed. Install with: pip install openpyxl")
        return None, None
    
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    
    # Get headers from first row
    headers = []
    for cell in ws[1]:
        headers.append(cell.value)
    
    # Get data rows
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_dict = {}
        for i, value in enumerate(row):
            if i < len(headers):
                row_dict[headers[i]] = value
        rows.append(row_dict)
    
    return headers, rows

def read_csv_file(filepath):
    """Read CSV file with encoding detection"""
    # Try to detect encoding
    encoding = 'utf-8'
    for enc in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252', 'windows-1252', 'utf-16']:
        try:
            with open(filepath, 'r', encoding=enc) as test:
                test.read()
            encoding = enc
            if encoding != 'utf-8':
                print(f"  Detected encoding: {encoding}")
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    
    with open(filepath, 'r', encoding=encoding) as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)
    
    return headers, rows

def select_input_file():
    """Interactive file selection with GUI dialog"""
    print("\n" + "=" * 70)
    print("SELECT INPUT FILE")
    print("=" * 70)
    print("\nOpening file browser...")
    
    # Hide the root tkinter window
    root = Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    
    # Open file dialog - default to Documents
    file_path = filedialog.askopenfilename(
        title="Select your data file",
        filetypes=[
            ("All Supported Files", "*.csv *.xlsx *.xls"),
            ("CSV Files", "*.csv"),
            ("Excel Files", "*.xlsx *.xls"),
            ("All Files", "*.*")
        ],
        initialdir=str(Path.home() / 'Documents')
    )
    
    # Properly destroy the root window
    try:
        root.quit()
    except:
        pass
    try:
        root.destroy()
    except:
        pass
    
    if not file_path:
        print("\nNo file selected. Exiting.")
        exit()
    
    return file_path

def main():
    print("=" * 70)
    print("GOOGLE MAPS DATA EXTRACTOR")
    print("Extracts coordinates and addresses from Google Maps URLs")
    print("=" * 70)
    
    # Select input file
    input_file = select_input_file()
    print(f"\n✓ Selected: {input_file}")
    
    # Determine file type and read data
    print("\n" + "=" * 70)
    print("READING INPUT FILE")
    print("=" * 70)
    
    file_ext = Path(input_file).suffix.lower()
    
    if file_ext in ['.xlsx', '.xls']:
        if not EXCEL_SUPPORT:
            print("\nERROR: Excel support not available!")
            print("Install openpyxl with: pip install openpyxl")
            return
        print(f"Reading Excel file...")
        headers, rows = read_excel_file(input_file)
    elif file_ext == '.csv':
        print(f"Reading CSV file...")
        headers, rows = read_csv_file(input_file)
    else:
        print(f"ERROR: Unsupported file type: {file_ext}")
        print("Supported types: .csv, .xlsx, .xls")
        return
    
    if not headers or not rows:
        print("ERROR: Could not read file or file is empty")
        return
    
    print(f"\nDetected columns: {', '.join(headers)}")
    
    # Try to auto-detect title and URL columns
    title_col = None
    url_col = None
    
    for field in headers:
        if field:  # Skip None values
            field_lower = field.lower()
            if 'title' in field_lower or 'name' in field_lower or 'place' in field_lower:
                title_col = field
            if 'url' in field_lower or 'link' in field_lower:
                url_col = field
    
    if not title_col or not url_col:
        print("\n⚠ Could not auto-detect columns")
        print(f"Available columns: {', '.join([c for c in headers if c])}")
        title_col = input("Enter column name for place names: ").strip()
        url_col = input("Enter column name for URLs: ").strip()
    
    print(f"\n✓ Using columns:")
    print(f"  Place names: {title_col}")
    print(f"  URLs: {url_col}")
    
    # Build locations list
    locations = []
    for row in rows:
        title = row.get(title_col, '').strip() if row.get(title_col) else ''
        url = row.get(url_col, '').strip() if row.get(url_col) else ''
        
        if title and url:
            locations.append({
                'title': title,
                'url': url
            })
    
    print(f"✓ Found {len(locations)} locations")
    
    if len(locations) == 0:
        print("\nERROR: No valid locations found!")
        print("Make sure your file has data and the column names are correct.")
        return
    
    # Estimate time
    seconds_per_location = 10
    total_seconds = len(locations) * seconds_per_location
    minutes = total_seconds / 60
    print(f"  Estimated time: {minutes:.1f} minutes")
    
    # Ask for output location with save dialog
    print("\n" + "=" * 70)
    print("SELECT OUTPUT LOCATION")
    print("=" * 70)
    print("\nOpening save dialog...")
    
    root = Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    
    output_file = filedialog.asksaveasfilename(
        title="Save results as",
        defaultextension=".csv",
        filetypes=[
            ("CSV Files", "*.csv"),
            ("All Files", "*.*")
        ],
        initialdir=str(Path.home() / 'Documents'),
        initialfile="thailand_with_coords.csv"
    )
    
    # Properly destroy the root window
    try:
        root.quit()
    except:
        pass
    try:
        root.destroy()
    except:
        pass
    
    if not output_file:
        print("\nNo output file selected. Using default: thailand_with_coords.csv")
        output_file = str(Path.cwd() / "thailand_with_coords.csv")
    
    print(f"\n✓ Output will be saved to: {output_file}")
    
    # Confirm before starting
    confirm = input("\nStart extraction? (yes/no): ").strip().lower()
    if confirm not in ['yes', 'y']:
        print("Cancelled.")
        return
    
    # Start extraction
    print("\n" + "=" * 70)
    print("STARTING EXTRACTION")
    print("=" * 70)
    
    print("Setting up Chrome browser...")
    driver = setup_driver()
    print("✓ Browser ready! (running in background)")
    
    results = []
    successful_coords = 0
    successful_addresses = 0
    
    print("-" * 70)
    for i, location in enumerate(locations, 1):
        print(f"[{i}/{len(locations)}] {location['title']}")
        
        lat, lng, address, final_url = extract_data_from_maps(driver, location['url'], location['title'])
        
        if lat and lng:
            print(f"  ✓ Coordinates: {lat}, {lng}")
            successful_coords += 1
        else:
            print(f"  ✗ Could not extract coordinates")
        
        if address:
            # Truncate long addresses for display
            display_addr = address if len(address) <= 60 else address[:57] + "..."
            print(f"  ✓ Address: {display_addr}")
            successful_addresses += 1
        else:
            print(f"  ✗ Could not extract address")
        
        results.append({
            'Place Name': location['title'],
            'URL': location['url'],
            'Longitude': lng if lng else '',
            'Latitude': lat if lat else '',
            'Full Address': address if address else '',
            'Final URL': final_url if final_url else ''
        })
        print("-" * 70)
    
    # Close browser
    driver.quit()
    
    # Write results
    print("Writing results...")
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['Place Name', 'URL', 'Longitude', 'Latitude', 'Full Address', 'Final URL'])
        writer.writeheader()
        writer.writerows(results)
    
    # Summary
    print("\n" + "=" * 70)
    print("EXTRACTION COMPLETE!")
    print("=" * 70)
    print(f"Total locations:       {len(locations)}")
    print(f"Coordinates found:     {successful_coords} ({successful_coords/len(locations)*100:.1f}%)")
    print(f"Addresses found:       {successful_addresses} ({successful_addresses/len(locations)*100:.1f}%)")
    print(f"\n✓ Results saved to: {output_file}")

if __name__ == "__main__":
    main()