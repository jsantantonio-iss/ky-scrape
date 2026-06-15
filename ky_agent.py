import requests
from bs4 import BeautifulSoup
import pymysql
import pandas as pd
import argparse
import os, time, random, re
from io import StringIO

import logging
import utility
import jslogger

from prescrape_agents import get_latest_prescrape_table, create_db_connection

logger = logging.getLogger(__name__)


def get_session(use_tor=False):

    session = requests.session()
    if use_tor:
        session.proxies = {'http': 'socks5://127.0.0.1:9050',
                           'https': 'socks5://127.0.0.1:9050'}

    return session

def go_to_page(stateid, session):

    good = True

    URL = """
    https://insurance.ky.gov/ppc/Agent/ALDetails.aspx?LookupVal={}&Type=1
    """.format(stateid)

    response = session.get(URL)

    if response.status_code != 200:
        good = False

    return good, response


def get_stateids(db):
    table_name = get_latest_prescrape_table(db)
    with db.cursor() as cursor:
        cursor.execute("""
            SELECT stateid
              FROM INS_StateClean.{table}
             WHERE status = 'Active'
               AND scraped_agent IS NULL
             GROUP BY stateid
        """.format(table=table_name))
        return [row[0] for row in cursor.fetchall()]


def update_scraped(db, stateid):
    table_name = get_latest_prescrape_table(db)
    with db.cursor() as cursor:
        cursor.execute(f"""
            UPDATE INS_StateClean.{table_name}
               SET scraped_agent = 'Y'
             WHERE stateid = %s
        """, (stateid,))


def run_agents(main_logger=None, limit=None, use_tor=False, files=None):
    global logger
    if main_logger is not None:
        logger = main_logger.getChild('agents')

    AGENTS = files['agents'] if files is not None else utility.setup_file_output('agents')
    LICENSES = files['licenses'] if files is not None else utility.setup_file_output('licenses')

    db = create_db_connection()
    stateids = get_stateids(db)
    if limit:
        stateids = stateids[:limit]

    total_ids = len(stateids)
    logger.info(f'Scraping {total_ids} KY agents')

    session = get_session(use_tor=use_tor)

    for idx, stateid in enumerate(stateids):

        good, page_content = go_to_page(stateid, session)

        if good:
            soup = BeautifulSoup(page_content.text, 'html.parser')

            agent_info_span = soup.find("span", id="MainContent_lblInd")
            if agent_info_span is None:
                logger.warning("No agent info span found, stateid: {}".format(stateid))
                continue

            try:
                agent_info_df = pd.read_html(StringIO(str(agent_info_span)))[0]
            except ValueError:
                logger.warning("No table found for Agent info, stateid: {}".format(stateid))
                continue

            cols = agent_info_df.columns.tolist()
            agent_dict = {
                agent_info_df[field].values[0]: agent_info_df[value].values[0]
                for field, value in zip(cols[0::2], cols[1::2])
            }
            agent_df = pd.DataFrame([{
                'stateid':          stateid,
                'Name':             agent_dict.get('Name'),
                'NPN':              agent_dict.get('NAIC NPN'),
                'Residency':        None,
                'addresstype':      None, 
                'unparsed_address': None,
                'phonetype':        None,
                'Phone':            None,
                'emailtype':        None,
                'email':            None,
            }])

            main_content = soup.find("span", id="MainContent_lblData")
            if main_content is not None:

                # LOA
                loa_div = main_content.find('div', string=re.compile('^License - Line'))
                if loa_div is not None:
                    try:
                        loa_df = pd.read_html(StringIO(str(loa_div.next_sibling)))[0]
                        agent_df['Residency'] = 'R' if loa_df['Residency'].values[0] == 'Resident' else 'N'
                        loa_df = loa_df.rename(columns={
                            'Class': 'license_type',
                            'Line of Authority': 'loa_name',
                            'Active Date': 'issue_date',
                            'License Expiration Date': 'expiration_date',
                        })
                        loa_df['stateid'] = stateid
                        loa_df['NPN'] = agent_dict.get('NAIC NPN')
                        utility.write_df_to_file(loa_df, LICENSES)
                    except ValueError:
                        logger.debug("No table found for LOA, stateid: {}".format(stateid))

                # Address
                addr_div = main_content.find('div', string=re.compile('^Address Infor'))
                if addr_div is not None:
                    try:
                        addr_df = pd.read_html(StringIO(str(addr_div.next_sibling)))[0]
                        addr_df = addr_df[addr_df['Type'] != 'Residence'][['Type', 'Address']]
                        addr_df = addr_df.rename(columns={'Type': 'addresstype', 'Address': 'unparsed_address'})
                        if not addr_df.empty:
                            agent_df['addresstype'] = addr_df['addresstype'].values[0]
                            agent_df['unparsed_address'] = addr_df['unparsed_address'].values[0]
                    except ValueError:
                        logger.debug("No table found for Address, stateid: {}".format(stateid))

                # Phone
                phone_div = main_content.find('div', string=re.compile('^Phone Infor'))
                if phone_div is not None:
                    try:
                        phone_df = pd.read_html(StringIO(str(phone_div.next_sibling)))[0]
                        phone_df = phone_df[['Type', 'Phone']].rename(columns={'Type': 'phonetype'})
                        phone_df = phone_df[phone_df['Phone'].str.strip().astype(bool)]
                        if not phone_df.empty:
                            agent_df['phonetype'] = phone_df['phonetype'].values[0]
                            agent_df['Phone'] = phone_df['Phone'].values[0]
                    except ValueError:
                        logger.debug("No table found for Phone, stateid: {}".format(stateid))

                # Email
                email_div = main_content.find('div', string=re.compile('^Internet Infor'))
                if email_div is not None:
                    try:
                        email_df = pd.read_html(StringIO(str(email_div.next_sibling)))[0]
                        email_df = email_df[email_df['Type'] != 'Internet'][['Type', 'Address']]
                        email_df = email_df.rename(columns={'Type': 'emailtype', 'Address': 'email'})
                        email_df = email_df[email_df['email'].str.strip().astype(bool)]
                        if not email_df.empty:
                            agent_df['emailtype'] = email_df['emailtype'].values[0]
                            agent_df['email'] = email_df['email'].values[0]
                    except ValueError:
                        logger.debug("No table found for Email, stateid: {}".format(stateid))

            utility.write_df_to_file(agent_df, AGENTS)
            update_scraped(db, stateid)
            logger.debug(f'({idx+1}/{total_ids}) stateid: {stateid} - {agent_dict.get("Name")}')

    utility.parse_full_addresses(AGENTS, 'unparsed_address')


if __name__ == '__main__':

    utility.set_state('ky')

    parser = argparse.ArgumentParser(description="Scrape KY agent details")
    parser.add_argument('-l', '--limit', type=int, help='limit number of agents (for testing)')
    args = parser.parse_args()

    standalone_logger = jslogger.custom_logger('ky_agent')
    standalone_logger.addCustomConsoleLogging(True)
    standalone_logger.addCustomFileLogging()

    run_agents(main_logger=standalone_logger, limit=args.limit)