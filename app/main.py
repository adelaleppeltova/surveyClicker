"""
vpn_clicker.py
Použití: python vpn_clicker.py
"""

import subprocess
import time
import os
import glob
import logging
from contextlib import contextmanager
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- Konfigurace ----------
VPN_CONFIG_DIR = "/Users/jakubsmida/sources/surveyClicker/app/vpns"   # adresář s .ovpn soubory
OPENVPN_BIN = "openvpn"            # cesta k openvpn (nebo jiný VPN klient)
PAGE_URL = "https://nachodsky.denik.cz/zpravy_region/anketa-nejpopularnejsi-dobrovolni-hasici-na-nachodsku-2025.html"
TARGET_TEXT = "SDH Bukovice"  # text pro identifikaci správné sekce
BUTTON_SELECTOR = "button.survey__answer-btn"  # tlačítko pro hlasování
VOTES_SELECTOR = ".survey__progress-text-result"  # selektor pro počet hlasů
COOKIES_DELAY = 5  # čekání po odkliknutí cookies v sekundách
CONNECT_TIMEOUT = 30               # sekundy na navázání VPN
ACTION_TIMEOUT = 15                # sekundy na načtení a kliknutí
WAIT_AFTER_CLICK = 3               # čekání po kliknutí (pokud je potřeba)
LOGFILE = "vpn_clicker.log"
# ----------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOGFILE), logging.StreamHandler()])

def find_ovpn_files(directory):
    files = glob.glob(os.path.join(directory, "*.ovpn"))
    files.sort()
    return files

@contextmanager
def start_openvpn(config_path):
    """
    Spustí openvpn s daným configem, sleduje stdout pro potvrzení spojení.
    Vrací proces. Pokud se nepřipojí do CONNECT_TIMEOUT, vyhodí výjimku.
    """
    cmd = [OPENVPN_BIN, "--config", config_path]
    logging.info(f"Spouštím OpenVPN: {cmd}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    connected = False
    start = time.time()
    try:
        # čteme řádky z stdout, hledáme známý text (OpenVPN hlásí "Initialization Sequence Completed")
        while True:
            if proc.poll() is not None:
                # proces skončil
                raise RuntimeError(f"OpenVPN proces skončil s kódem {proc.returncode}")
            line = proc.stdout.readline()
            if line:
                logging.debug(f"[openvpn] {line.strip()}")
                if "Initialization Sequence Completed" in line or "CONNECTED,SUCCESS" in line:
                    connected = True
                    logging.info("VPN připojena.")
                    break
            if time.time() - start > CONNECT_TIMEOUT:
                raise TimeoutError("Timeout při připojování VPN.")
            time.sleep(0.1)
        yield proc
    finally:
        logging.info("Ukoncovani OpenVPN procesu...")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            logging.exception("Chyba při ukončování OpenVPN: %s", e)

def perform_web_action(url, button_selector, headless=True):
    """
    Otevře stránku a klikne na tlačítko pomocí Playwright (synchronní).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        logging.info(f"Načítám stránku: {url}")
        page.goto(url, timeout=ACTION_TIMEOUT * 1000)
        try:
            # Nejdřív zkusíme odkliknout cookies
            try:
                logging.info("Čekám na cookies dialog...")
                cookie_button = "button#didomi-notice-agree-button"
                page.wait_for_selector(cookie_button, timeout=ACTION_TIMEOUT * 1000)
                page.click(cookie_button)
                logging.info(f"Cookies dialog odkliknut, čekám {COOKIES_DELAY} sekund na načtení ankety...")
                time.sleep(COOKIES_DELAY)
            except PWTimeout:
                logging.info("Cookies dialog se nezobrazil nebo již byl potvrzen.")

            # Najdeme správnou sekci podle textu
            logging.info(f"Hledám sekci s textem '{TARGET_TEXT}'...")
            section_selector = f".survey__progress-text >> text={TARGET_TEXT}"
            page.wait_for_selector(section_selector, timeout=ACTION_TIMEOUT * 1000)
            section = page.locator(section_selector)
            
            # Najdeme tlačítko v rámci správné sekce
            button = section.locator("xpath=../../../..").locator(button_selector)
            votes_element = section.locator("xpath=../../../..").locator(VOTES_SELECTOR)
            
            logging.info("Čekám na tlačítko a počet hlasů...")
            page.wait_for_selector(button_selector, timeout=ACTION_TIMEOUT * 1000)
            page.wait_for_selector(VOTES_SELECTOR, timeout=ACTION_TIMEOUT * 1000)
            
            # Získáme počet hlasů před kliknutím
            votes_before = votes_element.text_content()
            logging.info(f"Počet hlasů před kliknutím: {votes_before}")
            
            # Klikneme na tlačítko
            button.click(timeout=ACTION_TIMEOUT * 1000)
            logging.info("Kliknutí proběhlo, čekám na aktualizaci hlasů...")
            
            # Počkáme na aktualizaci hlasů a získáme nový počet
            time.sleep(WAIT_AFTER_CLICK)
            votes_after = page.text_content(VOTES_SELECTOR)
            logging.info(f"Počet hlasů po kliknutí: {votes_after}")
            time.sleep(WAIT_AFTER_CLICK)
            logging.info("Kliknutí proběhlo.")
        except PWTimeout:
            logging.warning("Nepodařilo se najít nebo kliknout na tlačítko (timeout).")
            raise
        finally:
            context.close()
            browser.close()

def main():
    ovpn_files = find_ovpn_files(VPN_CONFIG_DIR)
    if not ovpn_files:
        logging.error("Nebyly nalezeny .ovpn soubory v adresáři: %s", VPN_CONFIG_DIR)
        return

    logging.info("Nalezeno %d VPN konfigurací.", len(ovpn_files))

    for cfg in ovpn_files:
        logging.info("=== Zpracovávám: %s ===", cfg)
        try:
            with start_openvpn(cfg):
                # drobná pauza pro jistotu routingu
                time.sleep(2)
                try:
                    perform_web_action(PAGE_URL, BUTTON_SELECTOR, headless=True)
                except Exception as e:
                    logging.exception("Chyba při vykonávání akce na webu: %s", e)
        except TimeoutError as te:
            logging.warning("Nepřipojeno k VPN (timeout) pro %s: %s", cfg, te)
        except Exception as e:
            logging.exception("Chyba při práci s VPN configem %s: %s", cfg, e)

    logging.info("Hotovo — zpracovány všechny VPN konfigurace.")

if __name__ == "__main__":
    main()
