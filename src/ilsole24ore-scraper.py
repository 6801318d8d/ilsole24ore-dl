#!/usr/bin/env python3

# %%
import argparse
import datetime
import logging
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from tqdm import tqdm

from mylogin import MyLogin

# %%
# # Config
page_url = ("https://www.quotidiano.ilsole24ore.com/sfoglio/av"
            "iator.php?newspaper=S24&issue={date}&edition=SOLE&"
            "startpage={page}&displaypages=1")

browser_app = "firefox"
# geckodriver_path = Path(os.environ["HOME"])/".local"/"bin"/"geckodriver"
geckodriver_path = Path("/usr/bin/geckodriver")
assert (geckodriver_path.is_file())
headless_browser = False

datadir = Path("../data").resolve()
downloaddir = datadir/"download"
piecesdir = downloaddir/"pieces"
pagesdir = downloaddir/"pages"

sleep_time = (5, 15)

# %%
datadir.mkdir(parents=True, exist_ok=True)
if downloaddir.is_dir():
    shutil.rmtree(downloaddir)
downloaddir.mkdir(parents=True, exist_ok=True)
piecesdir.mkdir(parents=True, exist_ok=True)
pagesdir.mkdir(parents=True, exist_ok=True)


# %%

def get_browser(
    browser_app="firefox",
    headless_browser=False,
    geckodriver_path=None,
):
    browser = None
    if browser_app == "firefox":
        options = webdriver.FirefoxOptions()
        if headless_browser:
            options.add_argument('--headless')
        if geckodriver_path:
            service = webdriver.FirefoxService(
                executable_path=str(geckodriver_path))
        else:
            service = None
        browser = webdriver.Firefox(options=options, service=service)
    elif browser_app == "chrome":
        options = webdriver.ChromeOptions()
        if headless_browser:
            options.add_argument('--headless')
        # executable_path param is not needed if you updated PATH
        browser = webdriver.Chrome(options=options)
    else:
        raise Exception(f"Unknown value for browser_app={browser_app}")
    browser.maximize_window()
    return browser


# %%
# Main
print(sys.executable)
logging.basicConfig()

# %%
parser = argparse.ArgumentParser(
    prog='ilsole24ore-dl',
    description='Download the edition of Il Sole 24 Ore from the web')

parser.add_argument(
    '-d',
    '--date',
    help=("Date of the newspaper edition to download, in the "
          "format YYYY-MM-DD. Default"
          "s to today's newspaper"),
    default="today"
)
parser.add_argument(
    '-o',
    '--output',
    help="Output directory",
    default="."
)
args = parser.parse_args()

# %%
assert (Path(args.output).is_dir())
if args.date == "today":
    journal_issue = datetime.datetime.now().date()
else:
    journal_issue = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
logging.info(f"Downloading issue of {journal_issue}")

# %%
# Check issue was not already downloaded
journal_issue_str = journal_issue.strftime("%Y-%m-%d")
finalfp = Path(args.output)/f"IlSole24Ore-{journal_issue_str}.pdf"
assert (not finalfp.is_file())

# %%
# Open web browser
browser = get_browser(browser_app, headless_browser)

# %%
# These lines access "Il Sole 24 Ore" through your university portal
# You'll need to code these yourself
mylogin = MyLogin(datadir, browser)
mylogin.selectuni()

# %%
# Get first page
browser.get(page_url.format(date=journal_issue.strftime("%Y%m%d"), page="1"))
time.sleep(5)

# %%
# Accept cookies
id = "onetrust-accept-btn-handler"
accept_btn = browser.find_element(By.CSS_SELECTOR, "#"+id) \
                    .click()
time.sleep(5)

# %%
# Get total number of pages
tot_pages = browser.find_element(
    By.CSS_SELECTOR, "a[title='Elenco pagine']").text
tot_pages = tot_pages.split()[1]
tot_pages = int(tot_pages)
print(f"Total pages={tot_pages}")

# %%
# Loop through pages
print("Getting images link...")
for page in tqdm(range(1, tot_pages+1)):
    browser.get(page_url.format(
        date=journal_issue.strftime("%Y%m%d"), page=page))
    time.sleep(2)
    # Get (low resolution) image link
    img_el = browser.find_element(
        By.CSS_SELECTOR, "img.navigatorPageLow.navigatorPageLeft")
    img_link = img_el.get_attribute("src")
    # Save link to file
    with open(downloaddir/"links.txt", "a") as fh:
        fh.write(img_link+"\n")
    # Sleep (not bot)
    time.sleep(random.uniform(*sleep_time))

# %%
# Close browser
browser.close()

# %%
# Download pieces
piece_url = "https://mobapp2.ilsole24ore.com/_deploy/S24/{date}/SOLE/splitted/{page}/{piece_num}.gif"
with open(downloaddir/"links.txt", "r") as fh:
    pages_url = [x.strip() for x in fh.readlines() if x.strip() != ""]

# %%
print("Downloading image pieces of pages...")
for page_url in tqdm(pages_url):
    for piece_num in range(0, 7+1):
        page = page_url.split("/")[-1].split(".")[0]
        page_num = page.split("_")[0]
        fp = piecesdir/f"{page_num}_{piece_num}.gif"
        if (fp.is_file()):
            continue
        down_url = piece_url.format(
            date=journal_issue.strftime("%Y%m%d"),
            page=page,
            piece_num=piece_num,
        )
        # print(down_url)
        reqres = requests.get(down_url)
        with open(fp, "wb") as fh:
            fh.write(reqres.content)

# %%
# Collate pages together
print("Putting together image pieces...")
for page in tqdm(range(1, tot_pages+1)):
    cmd = ["magick"]
    for round in range(0, 4):
        cmd += ["("]
        for piece_num in range(round*2, round*2+2):
            cmd += [str(piecesdir/f"{page}_{piece_num}.gif")]
        cmd += ["+append", ")"]
    cmd += ["-append", str(pagesdir/f"{page}.png")]
    subprocess.run(cmd)

# %%
# Merge pages in a unique PDF
print("Merging pages in a single PDF...")
cmd = ["magick", "convert"]
for page in range(1, tot_pages+1):
    cmd += [str(pagesdir/f"{page}.png")]
cmd += ["-quality", "100", str(downloaddir/"final.pdf")]
ret = subprocess.run(cmd)
assert (ret.returncode == 0)

# %%
# Crop white margins
print("Cropping white margins...")
cmd = ["pdfcrop", str(downloaddir/"final.pdf"),
       str(downloaddir/"final_cropped.pdf")]
ret = subprocess.run(cmd)
assert (ret.returncode == 0)

# %%
# OCR PDF
print("OCRing PDF...")
cmd = ["ocrmypdf",
       "--force-ocr",
       "--language", "ita",
       "--optimize", "1",
       str(downloaddir/"final_cropped.pdf"),
       str(downloaddir/"final_cropped_ocred.pdf")]
ret = subprocess.run(cmd)
assert (ret.returncode == 0)

# %%
# Copy into "final" folder
shutil.copy(
    downloaddir/"final_cropped_ocred.pdf",
    finalfp
)

# %%
logging.info("Fine!")
