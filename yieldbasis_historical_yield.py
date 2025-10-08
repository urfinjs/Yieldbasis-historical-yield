import tomllib
import json
from decimal import Decimal
from time import sleep
from datetime import datetime
from pathlib import Path
import logging

from web3 import Web3


logging.basicConfig(
    level=logging.CRITICAL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


MAX_DAYS_TO_PARSE = 0
# https://chainlist.org/chain/1
RPC_PROVIDER = 'https://eth-mainnet.nodereal.io/v1/1659dfb40aa24bbb8153a677b98064d7'
QUERY_COOLDOWN = 0.5


wBTC_pool_addr  = '0x6095a220C5567360d459462A25b1AD5aEAD45204'
cbBTC_pool_addr = '0xD6a1147666f6E4d7161caf436d9923D44d901112'
tBTC_pool_addr  = '0x2B513eBe7070Cff91cf699a0BFe5075020C732FF'
divisors = {
    'wBTC': 10**8,
    'cbBTC': 10**8,
    'tBTC': 10**18,
    'shares': 10**18
}


with open(Path(__file__).parent / 'addresses_to_check.toml', 'rb') as fh:
    addresses_to_check = tomllib.load(fh)

with open(Path(__file__).parent / 'yieldbasis_pool_abi.json', 'r') as fh:
    yieldbasis_pool_abi = json.load(fh)


def timestamp_to_date(timestamp:int) -> str:
    if timestamp == 0:
        return 'unknown'
    return str(datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M'))


def get_web3_error_code(error):
    if hasattr(error, 'code'):
        return error.code
    try:
        json.loads(error)
        return error['code']
    except:
        return None


def get_block_timestamp(w3, block_number) -> int:
    for attempt in range(1, 4):
        try:
            block_data = w3.eth.get_block(block_number)
            return block_data['timestamp']
        except Exception as e:
            logger.info(f"get_block_timestamp error: {e}")
            if 'Too Many Requests' in str(e):
                sleep(attempt*30 + QUERY_COOLDOWN)
            else:
                return 0
    return 0


def get_shares_balance(contract, address:str, block_number:int=None):
    for attempt in range(1, 4):
        try:
            return contract.functions.balanceOf(address).call(block_identifier=block_number)
        except Exception as e:
            logger.info(f"get_shares_balance error: {e}")
            error_str = str(e)
            if 'Too Many Requests' in error_str:
                sleep(attempt*30 + QUERY_COOLDOWN)
            elif "Could not decode contract function" in error_str:
                # probably the contract is not deployed at this block
                return 0
            else:
                eroror_code = get_web3_error_code(e)
                # provider has no data at this block: "historical state is not available" or "state is pruned"
                if eroror_code is not None and eroror_code in [-32000, -32603]:
                    return -1
    return 0


def get_withdraw_amount(contract, shares:int, block_number:int=None):
    for attempt in range(1, 4):
        try:
            return contract.functions.preview_withdraw(shares).call(block_identifier=block_number)
        except Exception as e:
            logger.info(f"get_withdraw_amount error: {e}")
            error_str = str(e)
            if 'Too Many Requests' in error_str:
                sleep(attempt*30 + QUERY_COOLDOWN)
            elif "Could not decode contract function" in error_str:
                # probably the contract is not deployed at this block
                return 0
            else:
                eroror_code = get_web3_error_code(e)
                # provider has no data at this block: "historical state is not available" or "state is pruned"
                if eroror_code is not None and eroror_code in [-32000, -32603]:
                    return -1
    return 0


w3 = Web3(Web3.HTTPProvider(RPC_PROVIDER))
contract_pools = {}
for (pool_name, pool_addr) in [('wBTC', wBTC_pool_addr), ('cbBTC', cbBTC_pool_addr), ('tBTC', tBTC_pool_addr)]:
    contract_pools[pool_name] = w3.eth.contract(address=pool_addr, abi=yieldbasis_pool_abi)


blocks_per_day = 24*60*60 // 12 # eth mainnet has ~12 seconds per block

current_block = w3.eth.get_block_number()
current_block_timestamp = get_block_timestamp(w3, current_block)
print(f'the latest block is {current_block} {timestamp_to_date(current_block_timestamp)}')
sleep(QUERY_COOLDOWN)


for pool_name, depositor_addresses in addresses_to_check.items():
    if pool_name not in contract_pools:
        print(f"{pool_name} not found in contract_pools")
        continue

    for depositor_address, deposited_amount in depositor_addresses.items():
        print(f'[{pool_name}] {depositor_address}')

        deposited_amount = int(Decimal(str(deposited_amount)) * divisors[pool_name])
        pool_contract = contract_pools[pool_name]

        current_value = 0
        oldest_parsed_value = 0
        oldest_parsed_block_timestamp = 0

        for block_number in range(current_block, 0, -blocks_per_day):
            block_timestamp = get_block_timestamp(w3, block_number)
            if MAX_DAYS_TO_PARSE > 0:
                if current_block_timestamp - block_timestamp >= (MAX_DAYS_TO_PARSE+1) * 24 * 60 * 60 + 1000:
                    break

            block_str = f"{block_number} {timestamp_to_date(block_timestamp)}"

            shares_balance = get_shares_balance(pool_contract, depositor_address, block_number)
            if shares_balance <= 0:
                if shares_balance == -1:
                    print(f"\t{block_str} provider has no data for this block")
                break

            withdraw_amount = get_withdraw_amount(pool_contract, shares_balance, block_number)
            if withdraw_amount <= 0:
                if withdraw_amount == -1:
                    print(f"\t{block_str} provider has no data for this block")
                break

            shares_balance_f = shares_balance / divisors['shares']
            withdraw_amount_f = withdraw_amount / divisors[pool_name]
            diff_deposited_f = (withdraw_amount - deposited_amount) / divisors[pool_name]
            if not current_value:
                current_value = withdraw_amount
                diff_current_f = 0.0
            else:
                diff_current_f = (withdraw_amount - current_value) / divisors[pool_name]

            print(
                f"\t{block_str} {shares_balance_f:.8f} shares => {withdraw_amount_f:.8f} {pool_name} "
                f"vs current {diff_current_f:>11.8f} vs deposited {diff_deposited_f:>11.8f} "
            )

            oldest_parsed_block_timestamp = block_timestamp
            oldest_parsed_value = withdraw_amount
            sleep(QUERY_COOLDOWN)


        if oldest_parsed_value > 0 and oldest_parsed_block_timestamp > 0:
            parsed_days = (current_block_timestamp - oldest_parsed_block_timestamp) / (24*60*60)
            parsed_gain = (current_value - oldest_parsed_value)
            parsed_gain_f = parsed_gain / divisors[pool_name]
            parsed_apr = parsed_gain / oldest_parsed_value / parsed_days * 365
            print(f"\tgained {parsed_gain_f:.8f} {pool_name} in {parsed_days:.1f} days for APR: {parsed_apr:.2%}")
        sleep(QUERY_COOLDOWN)
