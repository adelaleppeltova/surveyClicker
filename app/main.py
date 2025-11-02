"""
vpn_clicker.py
Použití: python vpn_clicker.py
"""

import subprocess
import time
import os
import glob
import logging
import tempfile
import shutil
import stat
import argparse

from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Načtení .env souboru
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(env_path)

# ---------- Konfigurace ----------
VPN_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "vpns")  # relativní cesta k adresáři s .ovpn soubory
OPENVPN_BIN = os.getenv("OPENVPN_BIN", "/usr/local/opt/openvpn/sbin/openvpn")  # absolutní cesta k openvpn
# Volitelné přihlašovací údaje pro OpenVPN
OPENVPN_USER = os.getenv("OPENVPN_USER")
OPENVPN_PASS = os.getenv("OPENVPN_PASS")
OPENVPN_AUTH_FILE = os.getenv("OPENVPN_AUTH_FILE")  # cesta k souboru s řádky: username\npassword
PAGE_URL = "https://nachodsky.denik.cz/zpravy_region/anketa-nejpopularnejsi-dobrovolni-hasici-na-nachodsku-2025.html"
TARGET_TEXT = "SDH Bukovice"  # text pro identifikaci správné sekce
BUTTON_SELECTOR = "button.survey__answer-btn"  # tlačítko pro hlasování
VOTES_SELECTOR = ".survey__progress-text-result"  # selektor pro počet hlasů
COOKIES_DELAY = int(os.getenv("COOKIES_DELAY", "5"))  # čekání po odkliknutí cookies v sekundách
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", "30"))  # sekundy na navázání VPN
ACTION_TIMEOUT = int(os.getenv("ACTION_TIMEOUT", "60"))  # sekundy na načtení a kliknutí
WAIT_AFTER_CLICK = int(os.getenv("WAIT_AFTER_CLICK", "3"))  # čekání po kliknutí
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))  # maximální počet pokusů o načtení stránky
RETRY_DELAY = int(os.getenv("RETRY_DELAY", "5"))  # čekání mezi pokusy v sekundách
LOGFILE = "vpn_clicker.log"

# Debug mód z .env
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes", "on")
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(level=log_level,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOGFILE), logging.StreamHandler()])
# ----------------------------------

def find_ovpn_files(directory):
    files = glob.glob(os.path.join(directory, "*.ovpn"))
    files.sort()
    return files

@contextmanager
def start_openvpn(config_path):
    """
    Spustí openvpn s daným configem, sleduje stdout pro potvrzení spojení.
    Vrací proces. Pokud se nepřipojí do CONNECT_TIMEOUT, vyhodí výjimku.
    Vyžaduje sudo práva pro spuštění OpenVPN.
    """
    logging.info("OpenVPN vyžaduje sudo práva pro přístup k síťovým rozhraním.")

    # Kontrola přihlašovacích údajů
    if not (OPENVPN_AUTH_FILE or (OPENVPN_USER and OPENVPN_PASS)):
        raise RuntimeError(
            "Chybí přihlašovací údaje pro OpenVPN. "
            "Nastavte buď OPENVPN_AUTH_FILE nebo OPENVPN_USER a OPENVPN_PASS v .env souboru."
        )

    # zkusíme ověřit binárku: pokud není absolutní cesta dostupná, zkusíme ji najít v PATH
    if not os.path.exists(OPENVPN_BIN):
        alt = shutil.which(os.path.basename(OPENVPN_BIN))
        if alt:
            logging.info(f"OpenVPN binárka nalezena v PATH: {alt}")
            openvpn_exec = alt
        else:
            raise FileNotFoundError(
                f"OpenVPN binary not found: {OPENVPN_BIN}.\n"
                "Nainstalujte OpenVPN například přes Homebrew: `brew install openvpn`"
            )
    else:
        openvpn_exec = OPENVPN_BIN

    # připravíme příkaz; pokud máme soubor s přihlašovacími údaji (OPENVPN_AUTH_FILE), použijeme jej,
    # jinak pokud jsou v env OPENVPN_USER/OPENVPN_PASS, vytvoříme temp soubor
    cmd = [openvpn_exec, "--config", config_path]
    cred_temp_path = None
    if OPENVPN_AUTH_FILE:
        if not os.path.exists(OPENVPN_AUTH_FILE):
            raise FileNotFoundError(f"OPENVPN_AUTH_FILE set but file does not exist: {OPENVPN_AUTH_FILE}")
        cmd += ["--auth-user-pass", OPENVPN_AUTH_FILE]
        logging.info(f"Používám existující auth file z OPENVPN_AUTH_FILE: {OPENVPN_AUTH_FILE}")
    elif OPENVPN_USER and OPENVPN_PASS:
        tf = tempfile.NamedTemporaryFile(delete=False, mode="w", prefix="ovpn-creds-", dir=None)
        try:
            tf.write(f"{OPENVPN_USER}\n{OPENVPN_PASS}\n")
            tf.close()
            os.chmod(tf.name, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            cred_temp_path = tf.name
            cmd += ["--auth-user-pass", cred_temp_path]
            logging.info("Přihlašovací údaje pro OpenVPN budou použity z environment proměnných.")
        except Exception:
            # cleanup if something failed
            try:
                tf.close()
            except Exception:
                pass
            if os.path.exists(tf.name):
                os.remove(tf.name)
            raise

    # Přidáme sudo před příkaz
    sudo_cmd = ["sudo"] + cmd
    logging.info(f"Spouštím OpenVPN: {sudo_cmd}")
    proc = subprocess.Popen(sudo_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    connected = False
    start = time.time()
    try:
        # čteme řádky z stdout, hledáme známý text (OpenVPN hlásí "Initialization Sequence Completed")
        while True:
            if proc.poll() is not None:
                # proces skončil předčasně (před připojením)
                if proc.returncode != 0:
                    raise RuntimeError(f"OpenVPN proces selhal s chybovým kódem {proc.returncode}")
                else:
                    raise RuntimeError("OpenVPN proces se ukončil předčasně (před potvrzením připojení)")
            line = proc.stdout.readline()
            if line:
                logging.debug(f"[openvpn] {line.strip()}")
                # Pokud OpenVPN požádá o interaktivní přihlášení a my nemáme credentials, přestaňme
                if ("Enter Auth Username" in line or "AUTH" in line and "Username" in line) and not (OPENVPN_AUTH_FILE or cred_temp_path):
                    raise RuntimeError(
                        "OpenVPN vyžaduje interaktivní zadání uživatelského jména/hesla."
                        " Nastavte OPENVPN_USER/OPENVPN_PASS nebo OPENVPN_AUTH_FILE a spusťte znovu."
                    )
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
        # odebereme dočasný soubor s přihlašovacími údaji, pokud jsme jej vytvořili
        try:
            if cred_temp_path and os.path.exists(cred_temp_path):
                os.remove(cred_temp_path)
                logging.debug("Dočasný soubor s přihlašovacími údaji odstraněn.")
        except Exception:
            logging.exception("Nepodařilo se odstranit dočasný soubor s přihlašovacími údaji")

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
            votes_after = votes_element.text_content()
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
    # CLI argument parsing (přepisitelné nastavení přes environment proměnné)
    parser = argparse.ArgumentParser(description="VPN clicker script")
    parser.add_argument("--openvpn-bin", dest="openvpn_bin", help="cesta k openvpn binárce (přepíše OPENVPN_BIN)")
    parser.add_argument("--auth-file", dest="auth_file", help="cesta k souboru s přihlašovacími údaji (username\\npassword)")
    parser.add_argument("--headed", dest="headed", action="store_true", help="spustit prohlížeč v headful módu (ne headless)")
    parser.add_argument("--limit", dest="limit", type=int, help="zpracovat pouze prvních N VPN konfigurací")
    args = parser.parse_args()

    # přepiš globální proměnné pokud byly předány přes CLI
    global OPENVPN_BIN, OPENVPN_AUTH_FILE
    if args.openvpn_bin:
        OPENVPN_BIN = args.openvpn_bin
    if args.auth_file:
        OPENVPN_AUTH_FILE = args.auth_file
    headless = not args.headed

    ovpn_files = find_ovpn_files(VPN_CONFIG_DIR)
    if args.limit and args.limit > 0:
        ovpn_files = ovpn_files[:args.limit]
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
                    perform_web_action(PAGE_URL, BUTTON_SELECTOR, headless=headless)
                except Exception as e:
                    logging.exception("Chyba při vykonávání akce na webu: %s", e)
        except TimeoutError as te:
            logging.warning("Nepřipojeno k VPN (timeout) pro %s: %s", cfg, te)
        except Exception as e:
            logging.exception("Chyba při práci s VPN configem %s: %s", cfg, e)

    logging.info("Hotovo — zpracovány všechny VPN konfigurace.")

if __name__ == "__main__":
    main()
