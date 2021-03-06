import sys
import base64
import argparse
import logging
from functools import partial
from itertools import cycle, repeat
from threading import Thread
from time import sleep
import json
import multiprocessing as mp

import coloredlogs
from websocket import create_connection

import bigchaindb_benchmark

from .utils import ts

from . import utils, bdb


logger = logging.getLogger(__name__)

TRACKER = {}
CSV_WRITER = None
OUT_FILE = None

def run_send(args):
    from bigchaindb_driver.crypto import generate_keypair
    from urllib.parse import urlparse

    ls = bigchaindb_benchmark.config['ls']

    keypair = generate_keypair()

    BDB_ENDPOINT = args.peer[0]
    WS_ENDPOINT = 'ws://{}:26657/websocket'.format(urlparse(BDB_ENDPOINT).hostname)
    #WS_ENDPOINT = 'ws://{}:9985/api/v1/streams/valid_transactions'.format(urlparse(BDB_ENDPOINT).hostname)
    sent_transactions = []

    logger.info('Connecting to WebSocket %s', WS_ENDPOINT)
    ws = create_connection(WS_ENDPOINT)

    # Attach to tm websocket
    payload = {
        'method': 'subscribe',
        'jsonrpc': '2.0',
        'params': ['tm.event=\'NewBlock\''],
        'id': 'bdb_stream'
    }
    ws.send(json.dumps(payload))
    result = ws.recv()

    def ping(ws):
        while True:
            ws.ping()
            sleep(2)

    def listen(ws):
        while True:
            result = ws.recv()
            event = json.loads(result)
            if (event['result']['query'] == 'tm.event=\'NewBlock\''):
                block_txs = event['result']['data']['value']['block']['data']['txs']
                ls['event_id'] = event['id']

                # Only push non empty blocks
                if block_txs:
                    for transaction in block_txs:
                        transaction = json.loads(base64.b64decode(transaction.encode('utf8')).decode('utf8'))
                        transaction_id = transaction['id']
                        if transaction_id in TRACKER:
                            TRACKER[transaction_id]['ts_commit'] = ts()
                            CSV_WRITER.writerow(TRACKER[transaction_id])
                            del TRACKER[transaction_id]
                            ls['commit'] += 1
                            ls['mempool'] = ls['accept'] - ls['commit']
                        if not TRACKER:
                            ls()
                            OUT_FILE.flush()
                            return

    t = Thread(target=listen, args=(ws, ), daemon=False)
    p = Thread(target=ping, args=(ws, ), daemon=True)
    t.start()
    p.start()

    logger.info('Start sending transactions to %s', BDB_ENDPOINT)
    with mp.Pool(args.processes) as pool:
        results = pool.imap_unordered(
                bdb.sendstar,
                zip(repeat(args),
                    cycle(args.peer),
                    bdb.generate(keypair, args.size, args.requests)))
        for peer, txid, size, ts_send, ts_accept, ts_error in results:
            TRACKER[txid] = {
                'txid': txid,
                'size': size,
                'ts_send': ts_send,
                'ts_accept': ts_accept,
                'ts_commit': None,
                'ts_error': ts_error,
            }

            if ts_accept:
                ls['accept'] += 1
                delta = (ts_accept - ts_send)
                status = 'Success'
                ls['mempool'] = ls['accept'] - ls['commit']
            else:
                ls['error'] += 1
                delta = (ts_error - ts_send)
                status = 'Error'
                CSV_WRITER.writerow(TRACKER[txid])
                del TRACKER[txid]

            logger.debug('%s: %s to %s [%ims]', status, txid, peer, delta)

def create_parser():
    parser = argparse.ArgumentParser(
        description='Benchmarking tools for BigchainDB.')

    parser.add_argument('--csv',
                        type=str,
                        default='out.csv')

    parser.add_argument('-l', '--log-level',
                        default='INFO')

    parser.add_argument('-p', '--peer',
                        action='append',
                        help='BigchainDB peer to use. This option can be '
                             'used multiple times.')

    parser.add_argument('-a', '--auth',
                        help='Set authentication tokens, '
                             'format: <app_id>:<app_key>).')

    parser.add_argument('--processes',
                        default=mp.cpu_count(),
                        type=int,
                        help='Number of processes to spawn.')

    # all the commands are contained in the subparsers object,
    # the command selected by the user will be stored in `args.command`
    # that is used by the `main` function to select which other
    # function to call.
    subparsers = parser.add_subparsers(title='Commands',
                                       dest='command')

    send_parser = subparsers.add_parser('send',
                                        help='Send a single create '
                                        'transaction from a random keypair')

    send_parser.add_argument('--size', '-s',
                             help='Asset size in bytes',
                             type=int,
                             default=0)

    send_parser.add_argument('--mode', '-m',
                             help='Sending mode',
                             choices=['sync', 'async', 'commit'],
                             default='sync')

    send_parser.add_argument('--requests', '-r',
                             help='Number of transactions to send to a peer.',
                             type=int,
                             default=1)

    send_parser.add_argument('--unconfirmed_tx_th', '-th',
                             help='Threshold for number of unconfirmed transactions in tendermint mempool',
                             type=int,
                             default=5000)

    return parser

def configure(args):
    global CSV_WRITER
    global OUT_FILE
    coloredlogs.install(level=args.log_level, logger=logger)

    import csv
    OUT_FILE = open(args.csv, 'w')

    CSV_WRITER = csv.DictWriter(
            OUT_FILE,
            # Might be useful to add 'operation' and 'size'
            fieldnames=['txid', 'size', 'ts_send', 'ts_accept', 'ts_commit', 'ts_error'])
    CSV_WRITER.writeheader()

    def emit(stats):
        logger.info('Processing transactions, event_id: %s, '
            'accepted: %s (%s tx/s), committed %s (%s tx/s), errored %s (%s tx/s), mempool %s (%s tx/s)',
            stats['event_id'],
            stats['accept'], stats.get('accept.speed', 0),
            stats['commit'], stats.get('commit.speed', 0),
            stats['error'], stats.get('error.speed', 0),
            stats['mempool'], stats.get('mempool.speed', 0))


    import logstats
    ls = logstats.Logstats(emit_func=emit)
    ls['event_id'] = 'bdb_stream'
    ls['accept'] = 0
    ls['commit'] = 0
    ls['error'] = 0

    logstats.thread.start(ls)
    bigchaindb_benchmark.config = {'ls': ls}


def main():
    utils.start(create_parser(),
                sys.argv[1:],
                globals(),
                callback_before=configure)
