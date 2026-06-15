import requests
import pymysql
import pandas as pd
import os, time, random, re, sys, grp
import getpass
import argparse

from io import StringIO
from bs4 import BeautifulSoup
from datetime import datetime
from sqlalchemy import create_engine, text

import logging
import utility
import jslogger

logger = logging.getLogger(__name__)

URL = "https://insurance.ky.gov/ppc/Agent/Default.aspx"
MANUAL_CURL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.ky_curl')

_engine = None
def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine

def create_db_connection():
    db = pymysql.connect(
        host='insurancedb.discoveryco.com',
        read_default_file='~/.my.cnf',
        autocommit=True,
    )
    return db

def get_latest_prescrape_table(db):
    with db.cursor() as cursor:
        cursor.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'INS_StateClean'
            AND table_name LIKE 'ky_prescrape_%'
            ORDER BY table_name DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError('No ky_prescrape_* table found to continue from')
        return row[0]

def create_db_table(db):
    with db.cursor() as cursor:
        table_date = datetime.today().strftime('%Y%m%d')
        table_name = 'ky_prescrape_{}'.format(table_date)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS INS_StateClean.{table} (
                stateid int unique,
                name varchar(100),
                address varchar(255),
                status varchar(10),
                zipcode char(5),
                inserted datetime,
                scraped_agent char(1),
                scraped_appt char(1)
            )
        """.format(table=table_name))
    return table_name

def create_db_engine():
    user = getpass.getuser()
    host = 'insurancedb.discoveryco.com'
    engine = create_engine('mysql+pymysql://{}@{}/INS_StateClean'.format(user,host), connect_args={'read_default_file': '~/.my.cnf'})
    return engine


def insert_into_db(df, table_name):
    engine = _get_engine()

    df.to_sql(con=engine, name='ky_temptable', if_exists='replace', index=False)

    with engine.connect() as connection:
        connection.execute(text('insert ignore into INS_StateClean.{final_table} select * from ky_temptable'.format(final_table=table_name)))
        connection.commit()

def create_session(use_tor=False):
    session = requests.Session()
    if use_tor:
        session.proxies = {
            'http': 'socks5://127.0.0.1:9050',
            'https': 'socks5://127.0.0.1:9050',
        }

    return session

def parse_python_format(content):
    """Parse converted Python requests code from curlconverter.com or similar.
    Extracts cookies dict, files/data dict for form values."""
    import ast

    cookies = {}
    headers = {}
    viewstate = ''
    viewstategenerator = ''
    eventvalidation = ''

    # Extract cookies = {...}
    cookie_match = re.search(r'^cookies\s*=\s*(\{[^}]+\})', content, re.MULTILINE | re.DOTALL)
    if cookie_match:
        try:
            cookies = ast.literal_eval(cookie_match.group(1))
        except (ValueError, SyntaxError):
            pass

    # Extract headers = {...}
    headers_match = re.search(r'^headers\s*=\s*(\{.*?\n\})', content, re.MULTILINE | re.DOTALL)
    if headers_match:
        try:
            headers = ast.literal_eval(headers_match.group(1))
        except (ValueError, SyntaxError):
            pass

    # Extract files = {...} (multipart) or data = {...} (url-encoded)
    files_match = re.search(r'^files\s*=\s*(\{.*?\n\})', content, re.MULTILINE | re.DOTALL)
    data_match = re.search(r'^data\s*=\s*(\{.*?\n\})', content, re.MULTILINE | re.DOTALL)

    form_dict = {}
    for match in [files_match, data_match]:
        if match:
            try:
                form_dict = ast.literal_eval(match.group(1))
                break
            except (ValueError, SyntaxError):
                continue

    if form_dict:
        for key, val in form_dict.items():
            # files format: key: (None, 'value')
            if isinstance(val, tuple) and len(val) == 2:
                val = val[1]
            if key == '__VIEWSTATE':
                viewstate = val
            elif key == '__EVENTVALIDATION':
                eventvalidation = val
            elif key == '__VIEWSTATEGENERATOR':
                viewstategenerator = val

    if not viewstate or not eventvalidation:
        raise ValueError('Could not extract __VIEWSTATE and __EVENTVALIDATION from Python format')
    if not cookies:
        raise ValueError('Could not extract cookies from Python format')
    if not headers:
        raise ValueError('Could not extract headers from Python format')

    return viewstate, viewstategenerator, eventvalidation, cookies, headers

def get_manual_form_state():
    """Read Python requests code from .ky_curl file (from curlconverter.com)
    and extract cookies + ASP.NET form values."""

    if not os.path.exists(MANUAL_CURL_PATH) or os.path.getsize(MANUAL_CURL_PATH) == 0:
        raise FileNotFoundError(f'Manual file missing or empty: {MANUAL_CURL_PATH}')

    with open(MANUAL_CURL_PATH, 'r') as f:
        content = f.read().strip()

    form_state = parse_python_format(content)
    logger.debug('Loaded form state from Python requests file.')
    return form_state

def error_email(msg):
    recipient = 'Joseph.Santantonio@iss-stoxx.com'
    subject = 'ERROR - KY Scraper'
    body = 'There was an error, see below\n\n{}'.format(msg)

    os.system('echo "{}" | mutt -s "{}" {}'.format(body, subject, recipient))

def send_email(msg):

    recipient = 'Joseph.Santantonio@iss-stoxx.com'
    subject = 'KY needs site data'
    body = 'Put on your chefs hat, go to ' \
           'https://insurance.ky.gov/ppc/Agent/Default.aspx\n\n' \
           'Search for a zipcode, then in DevTools Network tab:\n' \
           'Right-click the POST request > Copy > Copy as cURL (bash)\n' \
           'Go to curlconverter.com, convert to Python, and paste into:\n\n' \
           f'File: {MANUAL_CURL_PATH}\n\n' \
           'Error: {}'.format(msg)

    os.system('echo "{}" | mutt -s "{}" {}'.format(body, subject, recipient))

def refresh_session_and_state(session, use_tor=False, force_refresh=False):
    """Try to load form state from .ky_curl file.
    If not available or force_refresh, clear file, email for help and wait."""

    session = create_session(use_tor=use_tor)

    if not force_refresh:
        try:
            form_state = get_manual_form_state()
            return session, form_state
        except (FileNotFoundError, Exception) as e:
            logger.debug(f'Manual form state not available: {e}')

    msg = 'KY scraper needs a fresh .ky_curl file.'
    wait_for_manual_files(msg)
    form_state = get_manual_form_state()
    return session, form_state

def wait_for_manual_files(msg):
    """Clear manual files, email for help, and block until they are populated."""

    data_paths = [MANUAL_CURL_PATH]

    send_email(msg)

    for path in data_paths:
        if os.path.exists(path):
            os.remove(path)

        with open(path, 'w') as f:
            pass

        uid = -1
        gid = grp.getgrnam('Discovery').gr_gid
        os.chown(path, uid, gid)

    logger.debug('Waiting for manual session data...')
    for path in data_paths:
        loop_count = 1
        while os.path.getsize(path) == 0:
            if loop_count % 360 == 0:
                send_email(msg)
            loop_count += 1
            time.sleep(60)

def get_data(zipcode, viewstategenerator, BOUNDARY, viewstate, eventvalidation):

    form_fields = {
        '__LASTFOCUS': '',
        '__EVENTTARGET': '',
        '__EVENTARGUMENT': '',
        '__VIEWSTATE': viewstate,
        '__VIEWSTATEGENERATOR': viewstategenerator,
        '__EVENTVALIDATION': eventvalidation,
        'ctl00$MainContent$drpLiczType': '1',
        'ctl00$MainContent$drpSrchBy': '5',
        'ctl00$MainContent$txtVal': '{}'.format(zipcode),
        'ctl00$MainContent$drpState': '',
        'ctl00$MainContent$btnContinue': 'Search'
    }

    boundary_data = "--"+BOUNDARY
    data = ""

    for key, value in form_fields.items():

        data += boundary_data+'\n'+\
                'Content-Disposition: form-data; name="{}"\n\n'.format(key)+\
                value+"\n"

    data += boundary_data+"--\n"          

    return data

def get_unscraped_zipcodes(db, limit, start_zip):
    with db.cursor() as cursor:
        cursor.execute('select zipcode from INS_StateClean.KYZips')
        results = cursor.fetchall()
        scraped_zips = {row[0] for row in results}

        zip_list = utility.get_zipcodes(limit=limit, last_zip=start_zip)
        unscraped_zips = [z for z in zip_list if z not in scraped_zips]
        return unscraped_zips

def get_soup(zipcode, session, form_state):
    viewstate, viewstategenerator, eventvalidation, cookies, headers = form_state

    # Extract boundary from Content-Type header
    ct = headers.get('Content-Type', '')
    boundary_match = re.search(r'boundary=(.+)', ct)
    boundary = boundary_match.group(1).strip() if boundary_match else None
    if not boundary:
        raise ValueError('No boundary found in Content-Type header')

    data = get_data(zipcode, viewstategenerator, boundary, viewstate, eventvalidation)

    response = session.post(URL, cookies=cookies, headers=headers, data=data, timeout=60)

    soup = BeautifulSoup(response.text, 'html.parser')

    return soup

def clear_log_table(db):
    with db.cursor() as cursor:
        cursor.execute("truncate table INS_StateClean.KYZips")

def insert_log(db, zipcode):
    with db.cursor() as cursor:
        query = f"""
            insert ignore
              into INS_StateClean.KYZips (ZipCode)
            select '{zipcode}';
        """
        cursor.execute(query)


def run_prescrape(main_logger=None, limit=None, start_zip='0', continue_scrape=False, use_tor=False):
    global logger
    if main_logger is not None:
        logger = main_logger.getChild('prescrape')

    db = create_db_connection()

    if continue_scrape:
        table_name = get_latest_prescrape_table(db)
    else:
        table_name = create_db_table(db)
        clear_log_table(db)

    zipcodes = get_unscraped_zipcodes(db, limit, start_zip)
    zipcode_count = len(zipcodes)

    if zipcode_count == 0:
        logger.info('No unscraped zipcodes remaining.')
        return

    logger.info(f'Scraping {zipcode_count} zipcodes into {table_name}')

    session = create_session(use_tor=use_tor)
    session, form_state = refresh_session_and_state(session, use_tor=use_tor)

    for idx, zipcode in enumerate(zipcodes):
        while True:
            try:
                soup = get_soup(zipcode, session, form_state)
                rows_found = 0

                result = soup.find('div', class_="DOIformpanel")
                if result is None:
                    raise RuntimeError(f'Expected div.DOIformpanel not found for zip {zipcode}')
                if "No results matched your search." not in result.text:
                    agent_table = pd.read_html(StringIO(str(result)))[0]
                    agent_table.rename(columns={'DOI ID': 'stateid'}, inplace=True)
                    agent_table['zipcode'] = zipcode
                    agent_table['inserted'] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
                    agent_table['scraped_agent'] = None
                    agent_table['scraped_appt'] = None
                    insert_into_db(agent_table, table_name)
                    rows_found = len(agent_table)

                logger.debug(f'({idx+1}/{zipcode_count}) Zipcode: {zipcode} - {rows_found} agents found')
                insert_log(db, zipcode)
                break

            except RuntimeError as e:
                logger.warning(f'{type(e)}\n(Message): {e}')
                try:
                    session, form_state = refresh_session_and_state(session, use_tor=use_tor, force_refresh=True)
                except Exception as refresh_err:
                    error_email(f'KY scraper cannot refresh session: {refresh_err}')
                    raise
                time.sleep(10)

            except Exception as e:
                logger.warning(f'Skipping zip {zipcode} due to unexpected error: {type(e).__name__}: {e}')
                insert_log(db, zipcode)
                break

        time.sleep(random.randint(2, 3))


if __name__ == '__main__':

    utility.set_state('ky')

    parser = argparse.ArgumentParser(description="Scrape KY prescrape")
    parser.add_argument('-c', '--continue_scrape', action='store_true', help='resume previous run')
    parser.add_argument('-l', '--limit', type=int, help='limit number of zipcodes')
    parser.add_argument('-s', '--start_zip', type=str, default='0', help='start from this zipcode')
    args = parser.parse_args()

    standalone_logger = jslogger.custom_logger('prescrape_agents')
    standalone_logger.addCustomConsoleLogging(True)
    standalone_logger.addCustomFileLogging()

    run_prescrape(
        main_logger=standalone_logger,
        limit=args.limit,
        start_zip=args.start_zip,
        continue_scrape=args.continue_scrape,
    )
