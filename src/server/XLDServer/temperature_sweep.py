import sys

from xldserver.database_sqlite import ServerDB
from passkey import DB_FILENAME
from tasklib import pause_for_next_execution

import numpy as np
from time import sleep
from datetime import datetime
from multiprocessing import Event
import logging
# from event_logger import xld_logger

# xld_logger = logging.getLogger('waitress')


class TemperatureSweep:
    def __init__(self, thermalization_time, client_timeout, mode: str, power_array = None, temperature_array = None, return_to_base: bool = False,
                 abort_flag: Event = Event(), is_running: Event = Event(), test_mode: bool = False,
                 skip_first: bool = False, debug: bool = False):
        assert power_array is not None or temperature_array is not None, "Must provide either power or temperature array!"
        self.thermalization_time = float(thermalization_time)
        self.power_array = np.array(power_array)
        self.temperature_array = np.array(temperature_array)
        self.db = ServerDB()
        self.client_timeout = float(client_timeout)
        self.test_mode = bool(test_mode)
        self.abort_flag = abort_flag
        self.is_running = is_running
        self.return_to_base = return_to_base
        self.skip_first = skip_first
        assert mode in self.db.heater_modes, "Choose valid heater mode!"
        self.mode = mode
        self.unit = 'mK' if self.mode == 'setpoint' else 'uW'
        
        log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s] [%(name)s] %(message)s")
        
        level = logging.INFO if not debug else logging.DEBUG
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        console_handler.setLevel(level)
        
        self.logger = logging.getLogger('TEMP-SWEEP')
        self.logger.addHandler(console_handler)
        self.logger.setLevel(level)
        
        self.db.write_heater_mode(index=self.db.mxc_ind, mode=self.mode)
        print(f'reading db heater mode {self.db.read_heater_mode(index=self.db.mxc_ind)}')

    def wait_for_all_clients(self):
        start = datetime.now()
        done = False
        while not done:
            self.logger.info(f'waiting... {self.db.get_all_meas_signals()}')
            self._try_abort()
            done = True
            for signal, crashed in self.db.get_all_meas_signals():
                if signal == 'running' or signal == 'go' and not crashed:
                    done = False

            if (datetime.now() - start).total_seconds() >= self.client_timeout:
                clients = self.db.get_html_meas_dict()
                for meas in clients:
                    if meas['signal'] == 'running' or meas['signal'] == 'go':
                        self.db.set_meas_as_crashed(meas_id=meas['id'])
                done = True

            sleep(1)
            # self.logger.info(f'waiting... {self.db.get_all_meas_signals()}')

    def start_all_client_meas(self):
        self.db.set_all_meas_to_go()

    def exec(self):
        self.is_running.set()
        self.logger.info(f'Making sure that all clients are ready.')
        self.logger.info(f'Waiting... {self.db.get_all_meas_signals()}')
        self.wait_for_all_clients()
        self.logger.info(f'Started sweep.')
        
        sweep_array = self.power_array if self.power_array is not None else self.temperature_array

        for i, value in enumerate(sweep_array):
            self.db.write_heater(index=self.db.mxc_ind, val=float(value), mode=self.mode)
            self.logger.info(f'Set heater {self.mode} to {value} {self.unit}.')
            if i == 0:
                if not self.skip_first:
                    self.logger.info(f'Waiting {self.thermalization_time} s for thermalization.')
                    if self.test_mode:
                        self.logger.info(f'Skipping thermalization in test mode, waiting 10 seconds.')
                        if not pause_for_next_execution(interval=10, abort=self.abort_flag):
                            self._try_abort()
                    else:
                        if not pause_for_next_execution(interval=self.thermalization_time, abort=self.abort_flag):
                            self._try_abort()
                else:
                    self.logger.info(f'Skipping first thermalization.')
            else:
                self.logger.info(f'Waiting {self.thermalization_time} s for thermalization.')
                if self.test_mode:
                        self.logger.info(f'Skipping thermalization in test mode, waiting 10 seconds.')
                        if not pause_for_next_execution(interval=10, abort=self.abort_flag):
                            self._try_abort()
                else:
                        if not pause_for_next_execution(interval=self.thermalization_time, abort=self.abort_flag):
                            self._try_abort()
            self.logger.info(
                f'Thermalization done. Current temperature at mixing chamber: '
                f'{self.db.read_temp(channel=self.db.mxc_ch)} K')
            self.start_all_client_meas()
            self.logger.info(f'Send GO signal to all measurement clients.')
            if not self.test_mode:
                if not pause_for_next_execution(interval=30, abort=self.abort_flag):
                    self._try_abort()
            self.logger.info(
                f'Waiting for all measurements to finish at current '
                f'temperature point ({i + 1}/{len(sweep_array)}).')
            self.wait_for_all_clients()
            self.logger.info(f'All measurements finished at current temperature point.')

        if self.return_to_base:
            self.logger.info(f'Returning to base temperature.')
            self.db.write_heater(index=self.db.mxc_ind, val=0.0, mode='power')

        else:
            self.logger.info(f'Not returning to base temperature.')

        self.is_running.clear()
        if not self.test_mode:
            pass
            # self.db.write_heater(index=self.db.mxc_ind, val=0)


    def _try_abort(self):
        if self.abort_flag.is_set():
            self.abort_flag.clear()
            self.is_running.clear()
            self.logger.info(f'SWEEP ABORTED!')
            sys.exit('Succuessfully aborted temperature sweep. This is a planned error message and is no cause for concern!\n=================================================================================================================\n')

        else:
            return False


class TemperatureSweepManager:
    def __init__(self, debug: bool = False):
        self.modes = ['pid', 'direct-power']
        self.interpolations = ['linear', 'quadratic', 'manual']

        self.mode = ''
        self.interpolation = ''
        self.cl_timeout = 0
        self.therm_time = 0
        self.sweep_array = np.zeros(1)
        self.html_dict = {}
        self.return_to_base = False
        self.skip_first = False
        self.params = {}
        self.confirmed = False
        self.started = False
        self.client_dict = {}
        
        log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s] [%(name)s] %(message)s")
        
        level = logging.INFO if not debug else logging.DEBUG
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        console_handler.setLevel(level)
        
        self.logger = logging.getLogger('TEMP-SWEEP-MAN')
        self.logger.addHandler(console_handler)
        self.logger.setLevel(level)

        self.generate_html_dict()

    def generate_sweep_array(self, params: dict):
        self.confirmed = False
        assert 'sweep_mode' in params.keys() and 'interpolation' in params.keys()
        assert 'client_timeout' in params.keys()
        assert 'ret_base' in params.keys()
        assert 'skip_first' in params.keys()

        sweep_mode = params['sweep_mode']
        interpolation = params['interpolation']
        self.cl_timeout = params['client_timeout']
        self.return_to_base = bool(params['ret_base'])
        self.skip_first = bool(params['skip_first'])

        assert sweep_mode in self.modes, "Wrong sweep mode."
        assert interpolation in self.interpolations, "Wrong interpolation."
        if sweep_mode == 'pid':
            assert interpolation in ['linear', 'manual'], "Wrong interpolation for PID mode."

        if sweep_mode == 'direct-power':
            assert 'therm_time' in params.keys()

        if sweep_mode == 'direct-power' and interpolation in ['linear', 'quadratic']:
            assert 'min_pow' in params.keys()
            assert 'max_pow' in params.keys()
            assert 'n_pow' in params.keys()
            

        self.params = params
        self.mode = sweep_mode
        self.interpolation = interpolation
        assert 'therm_time' in self.params.keys()
        self.therm_time = self.params['therm_time']
        
        if self.mode == 'pid':
            
            if self.interpolation == 'linear':
                self.sweep_array = np.linspace(float(self.params['min_temp']), float(self.params['max_temp']), int(self.params['n_steps']))
                
            elif self.interpolation == 'manual':
                lines = self.params['values'].split('\n')
                lines = [float(i) for i in lines]
                self.sweep_array = np.array(lines)

        elif self.mode == 'direct-power':

            if self.interpolation == 'linear':
                self.sweep_array = np.linspace(float(self.params['min_pow']), float(self.params['max_pow']),
                                               int(self.params['n_pow']))

            elif self.interpolation == 'quadratic':
                minp, maxp, n = float(self.params['min_pow']), float(self.params['max_pow']), int(self.params['n_pow']) 
                self.sweep_array = minp + (maxp - minp) / n ** 2 * np.square(np.linspace(0, n, n))

                # self.sweep_array = np.square(
                #     np.linspace(float(self.params['min_pow']), np.sqrt(float(self.params['max_pow'])),
                #                 int(self.params['n_pow'])))

            elif self.interpolation == 'manual':
                lines = self.params['values'].split('\n')
                lines = [float(i) for i in lines]
                self.sweep_array = np.array(lines)
                
        self.sweep_array = np.round(self.sweep_array, 1)

        self.generate_html_dict()

    def generate_html_dict(self):
        assert not self.confirmed
        ret = 'RETURN' if self.return_to_base else 'NOT RETURN'
        skip = 'SKIP' if self.skip_first else 'NOT SKIP'
        html_dict = {'sweep_mode': self.mode, 
                     'interpolation': self.interpolation,
                     'vals': [f' {v:.0f}' for v in self.sweep_array],
                     'cl_timeout': self.cl_timeout, 
                     'ret_base': ret, 
                     'skip_first': skip,
                     'therm_time': self.therm_time}

        self.html_dict = html_dict

    def confirm(self):
        assert not self.confirmed
        self.confirmed = True
        self.client_dict = {'abort_in_progress': False, 'confirmed': True, 'sweep_points': len(self.sweep_array),
                            'client_timeout': self.cl_timeout}
        self.logger.info("Parameters set to broadcasted.")

    def start_sweep(self):
        assert not self.started and self.confirmed
        self.started = True
        self.client_dict = {'abort_in_progress': False, 'sweep_started': True, 'confirmed': True,
                            'sweep_points': len(self.sweep_array),
                            'client_timeout': self.cl_timeout}
        self.logger.info("Sweep set to started.")

    def clear(self):
        self.mode = ''
        self.interpolation = ''
        self.cl_timeout = 0
        self.therm_time = 0
        self.sweep_array = np.zeros(1)
        self.html_dict = {}
        self.return_to_base = False
        self.params = {}
        self.confirmed = False
        self.started = False
        self.client_dict = {}

        self.generate_html_dict()
