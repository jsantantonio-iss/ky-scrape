import requests
import pymysql
import pandas as pd
import argparse
import os, time, random, re
from io import StringIO
from bs4 import BeautifulSoup

import logging
import utility
import jslogger

from prescrape_agents import create_db_connection, get_latest_prescrape_table

logger = logging.getLogger(__name__)


def get_session(use_tor=False):
    session = requests.session()
    if use_tor:
        session.proxies = {'http': 'socks5://127.0.0.1:9050',
                           'https': 'socks5://127.0.0.1:9050'}
    return session

def go_to_page(stateid, session):
    URL = f"https://insurance.ky.gov/ppc/Agent/ALAffiliations.aspx?Type=1&LookupVal={stateid}"
    response = session.get(URL)
    return response

def get_stateids(db):
    table_name = get_latest_prescrape_table(db)
    with db.cursor() as cursor:
        cursor.execute("""
            SELECT stateid
              FROM INS_StateClean.{table}
             WHERE status = 'Active'
               AND scraped_appt IS NULL
             GROUP BY stateid
        """.format(table=table_name))
        return [row[0] for row in cursor.fetchall()]

def update_scraped_appt(db, stateid, status='Y'):
    table_name = get_latest_prescrape_table(db)
    with db.cursor() as cursor:
        cursor.execute(f"""
            UPDATE INS_StateClean.{table_name}
               SET scraped_appt = %s
             WHERE stateid = %s
        """, (status, stateid))

def run_appts(main_logger=None, limit=None, use_tor=False, files=None):
    global logger
    if main_logger is not None:
        logger = main_logger.getChild('appts')

    db = create_db_connection()
    APPTS = files['appointments'] if files is not None else utility.setup_file_output('appointments')

    stateids = get_stateids(db)
    if limit:
        stateids = stateids[:limit]

    total_ids = len(stateids)
    logger.info(f'Scraping {total_ids} KY appointments')

    session = get_session(use_tor=use_tor)

    for idx, stateid in enumerate(stateids):
        logger.debug(f'({idx + 1}/{total_ids}): {stateid}')

        page_content = go_to_page(stateid, session)
        soup = BeautifulSoup(page_content.text, 'html.parser')

        main_content_data = soup.find("span", id="MainContent_lblData")
        if main_content_data is not None:
            appt_div = main_content_data.find('div', string=re.compile('^Appointments wit'))
            if appt_div is not None:
                appt_df = pd.read_html(StringIO(str(appt_div.next_sibling)))[0]
                appt_df.rename(columns={
                    'DOI Number': 'stateid',
                    'Line of Authority': 'loa_name',
                    'Affiliation Name': 'company_name',
                    'Active Date': 'issue_date',
                    'Inactive Date': 'termination_date',
                }, inplace=True)
                utility.write_df_to_file(appt_df, APPTS)
                update_scraped_appt(db, stateid)
                continue

        update_scraped_appt(db, stateid, status='N')


if __name__ == '__main__':

    utility.set_state('ky')

    parser = argparse.ArgumentParser(description="Scrape KY appointments")
    parser.add_argument('-l', '--limit', type=int, help='limit number of agents (for testing)')
    args = parser.parse_args()

    standalone_logger = jslogger.custom_logger('ky_appt')
    standalone_logger.addCustomConsoleLogging(True)
    standalone_logger.addCustomFileLogging()

    run_appts(main_logger=standalone_logger, limit=args.limit)
