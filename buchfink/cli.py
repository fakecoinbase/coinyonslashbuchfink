import logging
import os.path
import shutil
from datetime import date, datetime
from operator import itemgetter
from pathlib import Path

import click
import coloredlogs
from tabulate import tabulate
import yaml

from buchfink.datatypes import Asset, FVal
from buchfink.db import BuchfinkDB
from buchfink.serialization import deserialize_trade, serialize_trades

from .config import ReportConfig
from .report import run_report

logger = logging.getLogger(__name__)

epoch_start_ts = datetime(2011, 1, 1).timestamp()
epoch_end_ts = datetime(2031, 1, 1).timestamp()


@click.group()
@click.option('--log-level', '-l', type=str, default='INFO')
def buchfink(log_level):
    coloredlogs.install(level=log_level, fmt='%(asctime)s %(levelname)s %(message)s')


@buchfink.command()
@click.option('--directory', '-d', type=str, default='.')
def init(directory):
    "Initialize new Buchfink directory"

    target_config = os.path.join(directory, 'buchfink.yaml')

    if os.path.exists(target_config):
        click.echo(click.style('Already initialized (buchfink.yaml exists), aborting.', fg='red'))
        return

    initial_config = os.path.join(os.path.dirname(__file__), 'data', 'buchfink.initial.yaml')
    shutil.copyfile(initial_config, target_config)

    buchfink_db = BuchfinkDB(directory)

    click.echo(click.style('Successfully initialized in {0}.'.format(buchfink_db.data_directory.absolute()), fg='green'))


@buchfink.command()
@click.option('--keyword', '-k', type=str, default=None)
def balances(keyword):
    "Show balances across all accounts"

    buchfink_db = BuchfinkDB()
    balances_sum = {}
    usd_value_sum = {}

    for account in buchfink_db.get_all_accounts():
        if keyword is not None and keyword not in account['name']:
            continue

        if 'exchange' in account:
            exchange = buchfink_db.get_exchange(account['name'])

            api_key_is_valid, error = exchange.validate_api_key()

            if not api_key_is_valid:
                logger.critical('Skipping exchange %s because API key is not valid (%s)', account['name'], error)
                continue

            balances, error = exchange.query_balances()

            if not error:
                logger.info('Fetched balances for %d assets from %s', len(balances.keys()), account['name'])
                for asset, balance in balances.items():
                    amount = balance['amount']
                    balances_sum[asset] = balances_sum.get(asset, FVal(0)) + amount
                    if 'usd_value' in balance:
                        usd_value_sum[asset] = usd_value_sum.get(asset, FVal(0)) + balance['usd_value']

        elif 'ethereum' in account:
            manager = buchfink_db.get_chain_manager(account)
            manager.query_balances()

            for eth_balance in manager.balances.eth.values():
                for asset, balance in eth_balance.asset_balances.items():
                    amount = balance.amount
                    balances_sum[asset] = balances_sum.get(asset, FVal(0)) + amount
                    usd_value_sum[asset] = usd_value_sum.get(asset, FVal(0)) + balance.usd_value

        elif 'bitcoin' in account:
            manager = buchfink_db.get_chain_manager(account)
            manager.query_balances()
            asset = Asset('BTC')

            for balance in manager.balances.btc.values():
                amount = balance.amount
                balances_sum[asset] = balances_sum.get(asset, FVal(0)) + amount
                usd_value_sum[asset] = usd_value_sum.get(asset, FVal(0)) + balance.usd_value

        elif 'file' in account:

            account = yaml.load(open(account['file'], 'r'), Loader=yaml.SafeLoader)
            if 'balances' in account:
                for balance in account['balances']:
                    amount = FVal(balance['amount'])
                    asset = Asset(balance['asset'])
                    usd_value = amount * buchfink_db.inquirer.find_usd_price(asset)
                    balances_sum[asset] = balances_sum.get(asset, FVal(0)) + amount
                    usd_value_sum[asset] = usd_value_sum.get(asset, FVal(0)) + usd_value

    currency = buchfink_db.get_main_currency()
    currency_in_usd = buchfink_db.inquirer.find_usd_price(currency)

    table = []
    assets = [obj[0] for obj in sorted(usd_value_sum.items(), key=itemgetter(1), reverse=True)]
    balance_in_currency_sum = 0

    for asset in assets:
        balance = balances_sum[asset]
        balance_in_currency = usd_value_sum.get(asset, FVal(0)) / currency_in_usd
        balance_in_currency_sum += balance_in_currency
        table.append([asset, balance, asset.symbol, round(float(balance_in_currency), 2)])
    table.append(['Total', None, None, round(float(balance_in_currency_sum), 2)])
    print(tabulate(table, headers=['Asset', 'Amount', 'Symbol', 'Fiat Value (%s)' % currency.symbol]))


@buchfink.command()
@click.option('--keyword', '-k', type=str, default=None)
def fetch(keyword):
    "Fetch trades for configured accounts"

    buchfink_db = BuchfinkDB()

    for account in buchfink_db.get_all_accounts():
        if keyword is not None and keyword not in account['name']:
            continue

        if 'exchange' not in account:
            continue

        click.echo('Fetching trades for ' + account['name'])

        exchange = buchfink_db.get_exchange(account['name'])

        api_key_is_valid, error = exchange.validate_api_key()

        if not api_key_is_valid:
            logger.critical('Skipping exchange %s because API key is not valid (%s)', account['name'], error)
            continue

        name = account.get('name')

        trades = exchange.query_online_trade_history(
            start_ts=epoch_start_ts,
            end_ts=epoch_end_ts
        )

        logger.info('Fetched %d trades from %s', len(trades), name)

        with open("trades/" + name + ".yaml", "w") as f:
            yaml.dump({"trades": serialize_trades(trades)}, stream=f)


@buchfink.command()
@click.option('--name', '-n', type=str, required=True)
@click.option('--from', '-f', 'from_', type=str, required=True)
@click.option('--to', '-t', type=str, required=True)
def report(name, from_, to):
    "Run an ad-hoc report on your data"

    start_ts = datetime.fromisoformat(from_).timestamp()
    end_ts = datetime.fromisoformat(to).timestamp()

    buchfink_db = BuchfinkDB()

    result = run_report(buchfink_db, ReportConfig(
        name=name,
        from_dt=datetime.fromisoformat(from_),
        to_dt=datetime.fromisoformat(to)
    ))

    logger.info("Overview: %s", result['overview'])


@buchfink.command()
@click.option('--keyword', '-k', type=str, default=None)
def run(keyword):
    "Generate reports for all report definition and output overview table"

    buchfink_db = BuchfinkDB()

    num_matched_reports = 0
    results = {}

    for report in buchfink_db.get_all_reports():
        name = str(report.name)

        if keyword is not None and keyword not in name:
            continue
        num_matched_reports += 1

        results[name] = run_report(buchfink_db, report)

    table = []
    for report_name, result in results.items():
        table.append([
            report_name,
            result['overview']['total_profit_loss'],
            result['overview']['total_taxable_profit_loss']
        ])
    print(tabulate(table, headers=['Report', 'Profit/Loss', 'Taxable P/L']))


@buchfink.command()
def allowances():
    "Show the amount of each asset that you could sell tax-free"

    buchfink_db = BuchfinkDB()

    num_matched_accounts = 0
    all_trades = []

    for account in buchfink_db.get_all_accounts():
        num_matched_accounts += 1
        all_trades.extend(buchfink_db.get_local_trades_for_account(account['name']))

    logger.info('Collected %d trades from %d exchange account(s)',
            len(all_trades), num_matched_accounts)

    accountant = buchfink_db.get_accountant()
    result = accountant.process_history(epoch_start_ts, epoch_end_ts, all_trades, [], [], [], [])
    table = []
    for (symbol, (_allowance, buy_price)) in accountant.events.details.items():
        table.append([
            symbol,
            _allowance,
            'TBD',
            buy_price
        ])
    print(tabulate(table, headers=['Asset', 'Tax-free allowance', 'Tax-free amount', 'Average buy price']))


if __name__ == '__main__':
    buchfink()
