import argparse

import utility
import jslogger

import prescrape_agents
import ky_agent
import ky_appt

if __name__ == '__main__':

    utility.set_state('ky')

    parser = argparse.ArgumentParser(description='Scrape KY Agents and Appointments')
    parser.add_argument('-v', dest='verbose', action='store_true', help='Verbose output')
    parser.add_argument('-c', '--continue_run', action='store_true', help='Resume previous run')
    parser.add_argument('-run', dest='run_mode',
                        choices=['all', 'prescrape', 'agents', 'appts'], default='all',
                        help='Which stage(s) to run')
    parser.add_argument('-l', '--limit', type=int,
                        help='Limit number of records per stage (for testing)')
    parser.add_argument('-s', '--start_zip', type=str, default='0',
                        help='Start prescrape from this zipcode')
    parser.add_argument('--tor', action='store_true', help='Route requests through Tor')
    args = parser.parse_args()

    logger = jslogger.custom_logger('ky')
    logger.addCustomConsoleLogging(args.verbose)
    logger.addCustomFileLogging(continue_scrape=args.continue_run)

    logger.info('Starting KY scrape — run mode: %s%s', args.run_mode, ' (Tor)' if args.tor else '')

    files = {
        'agents':       utility.setup_file_output('agents', continue_scrape=args.continue_run),
        'licenses':     utility.setup_file_output('licenses', continue_scrape=args.continue_run),
        'appointments': utility.setup_file_output('appointments', continue_scrape=args.continue_run),
    }

    if args.run_mode in ('prescrape', 'all'):
        prescrape_agents.run_prescrape(
            main_logger=logger,
            limit=args.limit,
            start_zip=args.start_zip,
            continue_scrape=args.continue_run,
            use_tor=args.tor,
        )

    if args.run_mode in ('agents', 'all'):
        ky_agent.run_agents(
            main_logger=logger,
            limit=args.limit,
            use_tor=args.tor,
            files=files,
        )

    if args.run_mode in ('appts', 'all'):
        ky_appt.run_appts(
            main_logger=logger,
            limit=args.limit,
            use_tor=args.tor,
            files=files,
        )

    logger.info('KY scrape complete.')
