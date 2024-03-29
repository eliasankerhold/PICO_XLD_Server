import json
import sys

import requests
from time import sleep


class XLDMeasClient:
    def __init__(self, server_ip: str, user: str, group: str, server_port: int = 8080, update_interval: int = 30):
        self.user = user
        self.group = group
        self.id = None
        self.server_ip = server_ip
        self.server_port = server_port
        self.http_ip_port = f'http://{server_ip}:{self.server_port}'
        self.listen_delay = 10
        self.running = False
        self.update_interval = update_interval

    @staticmethod
    def _generic_request(path: str, payload: dict = None):
        try:
            if payload is None:
                response = requests.get(path)
            else:
                headers = {'Content-Type': 'application/json'}
                response = requests.post(path, data=json.dumps(payload), headers=headers)
            response.raise_for_status()

            return response.json()

        except Exception as ex:
            print(ex)
            return

    def _make_endpoint(self, *args):
        return self.http_ip_port + '/' + '/'.join(args)

    def _register(self):
        payload = {'user': self.user, 'group': self.group}
        response = self._generic_request(path=self._make_endpoint('meas', 'register'), payload=payload)
        if 'error' in response.keys():
            print(response['error'])
            sys.exit("Failed to register at server.")

        self.id = response['id']

    def _deregister(self):
        payload = {'id': self.id}
        response = self._generic_request(path=self._make_endpoint('meas', 'deregister'), payload=payload)
        return response['deregistered']

    def listen(self, autostart=True):
        while True:
            sleep(self.update_interval)
            payload = {'id': self.id}
            response = self._generic_request(path=self._make_endpoint('meas', 'signal'), payload=payload)
            print(f'Pinged server. Response: {response}')
            try:
                if response['signal'] == 'go':
                    if autostart:
                        self.started()
                    return True
            except TypeError as ex:
                print("Error on server side. Wrong response received.")

    def _running_update(self, running):
        payload = {'id': self.id, 'running': running}
        response = self._generic_request(path=self._make_endpoint('meas', 'status', 'set'), payload=payload)
        self.running = bool(response['running'])

    def started(self):
        self._running_update(running=True)

    def stopped(self):
        self._running_update(running=False)

    def open_session(self):
        self._register()
        print(f"Registered at {self.server_ip}. API ID: {self.id}")
        print("Waiting for sweep info broadcast.")
        return self._wait_for_sweep_info()

    def close_session(self):
        if self._deregister():
            print("Deregistered successfully.")

    def _wait_for_sweep_info(self):
        while True:
            sleep(self.update_interval)
            response = self._generic_request(path=self._make_endpoint('temperature-sweep', 'info'))
            print(f'Pinged server. Response: {response}')
            try:
                if response['confirmed']:
                    n_sweep = response['sweep_points']
                    timeout = response['client_timeout']
                    return int(n_sweep), float(timeout)

            except TypeError or KeyError as ex:
                print("Error on server side. Wrong response received.")

    def get_mxc_temp(self):
        response = self._generic_request(path= self._make_endpoint('temps', 'mxc'))

        return response['mxc_temp']
