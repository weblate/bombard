from bombard.terminal_colours import red, dark_red, green, gray, GRAY, OFF
from bombard.attr_dict import AttrDict
from urllib.parse import urlparse
import json
import logging
from bombard.weaver_mill import WeaverMill
from bombard.report import Reporter
from bombard.pretty_ns import time_ns
from bombard.pretty_sz import pretty_sz
from bombard.show_descr import markdown_for_terminal
from bombard.http_request import http_request, EXCEPTION_STATUS
from bombard import request_logging
from collections import Mapping


log = logging.getLogger()

DEFAULT_OK = [200]
DEFAULT_OVERLOAD = [502, 504]


def apply_supply(request: dict, supply: dict) -> dict:
    """
    Use supply to substitute all {name} in request strings.
    """
    for name in request:
        if isinstance(request[name], dict):
            request[name] = apply_supply(request[name], supply)
        if isinstance(request[name], str):
            request[name] = request[name].format(**supply)
    return request


class Bombardier(WeaverMill):
    """
    Use horde of threads to make HTTP-requests
    """
    def __init__(self, supply: dict=None, args=None, campaign_book: dict=None, ok_statuses=None,
                 overload_statuses=None):
        """
        :param args.threads: number of threads to use to request
        """
        self.supply = supply if supply is not None else {}
        self.args = args
        self.campaign = campaign_book
        self.ok = ok_statuses if ok_statuses is not None else DEFAULT_OK
        self.overload = overload_statuses if overload_statuses is not None else DEFAULT_OVERLOAD

        self.show_request = {
            1: 'Sent 1st request..'
        }
        self.show_response = {
            1: 'Got 1st response..'
        }
        self.reporter = Reporter(
            time_units=('ms' if args.ms else None),
            time_threshold_ms=int(args.threshold)
        )
        request_logging.pretty_ns = self.reporter.pretty_ns

        super().__init__()

    def status_coloured(self, status: int) -> str:
        if status in self.ok:
            return green(str(status))
        elif status in self.overload:
            return dark_red(str(status))
        else:
            return red(str(status))

    @staticmethod
    def get_headers(request: dict) -> dict:
        """
        Treat special value 'json' as Content-Type: application/json
        """
        predefined = {
            'json': {'Content-Type': 'application/json'},
        }
        if 'headers' not in request:
            return {}
        if isinstance(request['headers'], str):
            for known in predefined:
                if request['headers'].lower() == known:
                    return predefined[known]
        result = {}
        for name, val in request['headers'].items():
            for known in predefined:
                if name.lower() == known:
                    result.update(predefined[known])
                    break
            else:
                result.update({name: val})
        return result

    def process_resp(self, ammo: dict, status: int, resp: str, elapsed: int, size: int):
        request = ammo['request']
        if status in self.ok:
            self.reporter.log(True, elapsed, request.get('name'), size)
            log.debug(f'{status} reply\n{resp}')
            if 'extract' in request:
                #todo: auto fire ammo after prepare if no reload
                # now extract option is not so useful - you extract something but do not add
                # requests that use that. you cannot just have this requests in the same
                # section because of unpredictable requests order
                # so we wait prepare section scripts to finish and fire ammo section IF no reload
                # was registered
                try:
                    data = json.loads(resp)
                    if not hasattr(request['extract'], 'items'):
                        request['extract'] = {request['extract']: request['extract']}
                    for name, extractor in request['extract'].items():
                        if not extractor:
                            extractor = name
                        if '[' in extractor:
                            self.supply[name] = eval('data' + extractor)
                        else:
                            self.supply[name] = data[extractor]
                    if not isinstance(request['reload'], list):
                        request['reload'] = [request['reload']]
                    for ammo in request['reload']:
                        self.reload(self.campaign['ammo'][ammo])
                except Exception as e:
                    log.error(f'Cannot extract {request["extract"]} from {resp}:\n{e}', exc_info=True)
            if 'script' in request:
                try:
                    # Supply immediately repeats all changes in the self.supply so if the script spawns new
                    # requests they already get new values
                    supply = AttrDict(self.supply, **ammo['supply'])
                    context = {
                        'reload': self.reload,
                        'resp': json.loads(resp),
                        'args': self.args,
                        'supply': supply,
                        'ammo': AttrDict(self.campaign['ammo'])
                    }
                    if 'compiled' not in request:
                        request['compiled'] = compile(request['script'], 'script', 'exec')
                    exec(request['compiled'], context)
                except Exception as e:
                    log.error(f'Script fail\n{e}\n\n{request["script"]}\n\n{supply}\n', exc_info=True)
        else:
            self.reporter.log(False, elapsed, request.get('name'), size)

    @staticmethod
    def beautify_url(url, method, body):
        urlparts = urlparse(url)
        path = urlparts.path if len(urlparts.path) < 15 else '...' + urlparts.path[:-15]
        query = '?' + urlparts.query if urlparts.query else ''
        if urlparts.fragment:
            query += '#' + urlparts.fragment
        query = query if len(query) < 15 else '?...' + query[:-15]
        return f"""{method} {urlparts.netloc}{path}{query}"""

    def worker(self, thread_id, ammo):
        """
        Thread callable.
        Strike ammo from queue.
        """
        try:
            # setup logging ASAP and as safe as possible
            if isinstance(ammo, Mapping):
                request = ammo.get('request', {})
                ammo_id = ammo.get('id', '')
                ammo_name = request.get('name', '')
            else:
                request = {}
                ammo_id = None
                ammo_name = None
            request_logging.sending(thread_id, ammo_id, ammo_name)
            pretty_url = ''  # we use it in `except`
            try:
                ammo = apply_supply(ammo, dict(self.supply, **ammo['supply']))

                url = request.get('url', '')
                method = request['method'] if 'method' in request else 'GET'
                body = json.dumps(request['body']) if 'body' in request else None
                headers = self.get_headers(request)
                pretty_url = self.beautify_url(url, method, body)

                log.debug(f'Bomb to drop:\n{pretty_url}' + ('\n{body}' if body is not None else ''))
                if self.args.quiet:
                    if ammo_id in self.show_request:
                        print(f'{self.show_request[ammo_id].format(id=ammo_id):>15}\r', end='')
                log.info(pretty_url)

                start_ns = time_ns()
                if self.args.dry:
                    status, resp = self.ok[0], json.dumps(request.get('dry'))
                else:
                    status, resp = http_request(url, method, headers, body, self.args.timeout)

                request_logging.receiving()

                self.process_resp(ammo, status, resp, time_ns() - start_ns, len(resp))

                if self.args.quiet:
                    if ammo_id in self.show_response:
                        print(f'{self.show_response[ammo_id].format(id=ammo_id):>15}\r', end='')
                log.info(self.status_coloured(status) + f' ({pretty_sz(len(resp))}) ' + pretty_url
                        + ' ' + (red(resp) if status == EXCEPTION_STATUS else '')
                )
            except Exception as e:
                log.info(pretty_url + ' ' + red(str(e)), exc_info=True)
        finally:
            request_logging.main_thread()

    def reload(self, requests, repeat=None, **kwargs):
        """
        Add request(s) to the bombardier queue `repeat`-times (args.repeat if None).
        If `repeat` field exists in the request additionally repeats as defined by it.

        Requests can be one request or list of requests.
        If supply specified it'll be used in addition to self.supply
        """
        if not isinstance(requests, list):
            requests = [requests]
        if repeat is None:
            repeat = self.args.repeat
        for request in requests:
            for _ in range(repeat):
                for __ in range(request.get('repeat', 1)):
                    self.job_count += 1
                    if self.job_count % self.args.threads == 0 \
                            or self.job_count < self.args.threads and self.job_count % 10 == 0:
                        # show each 10th response before queue is full and then each time it's full
                        self.show_response[self.job_count] = f'Got {self.job_count} responses...'
                    self.put({
                        'id': self.job_count,
                        'request': request,
                        'supply': kwargs
                    })

    def bombard(self):
        self.start()  # lock until queue is not empty
        self.stop()  # stop all threads
        log.warning(
            '\n'
            + '='*100
            + '\n'
            + markdown_for_terminal(self.reporter.report())
            + '='*100
            + '\n'
        )
