from blueftc.BlueforsController import BlueFTController
from xldserver.database_sqlite import ServerDB
from tasklib import pause_for_next_execution

from time import sleep
from numpy import isclose
import logging
from event_logger import xld_logger
from multiprocessing import Event
import sys

# xld_logger = logging.getLogger('waitress')


class XLDTempHandler:
    def __init__(self, database: ServerDB, update_interval: int, abort: Event, global_crash: Event, active_channels: list[int], api_key: str, ip: str, port: int, 
                 mxc_id: int, heater_id: int, init_mode: str, emulate: bool = False, pid_calib: str = None, debug: bool = False):
        self.db = database
        self.update_interval = update_interval
        self.temp_channels = active_channels
        self.mxc_heater = heater_id
        self.first_exec = True
        self.mxc_id = mxc_id
        self.heater_id = heater_id
        assert init_mode in self.db.heater_modes, "Choose valid heater mode!"
        self.mode = init_mode
        self.pid_calib = pid_calib
        self.use_pid_calib = True
        self.abort = abort
        self.global_crash = global_crash
        
        self.controller = BlueFTController(ip=ip, port=port, key=api_key, mixing_chamber_channel_id=mxc_id, mixing_chamber_heater_id=heater_id, emulate=emulate, debug=debug, pid_calib_path=pid_calib)
        
        self.get_mxc_heater_value = None
        self.set_mxc_heater_value = None
        
        log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s] [%(name)-10.10s] %(message)s")
        
        level = logging.INFO if not debug else logging.DEBUG
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        console_handler.setLevel(level)
        
        self.logger = logging.getLogger('TEMP-HANDLER')
        self.logger.addHandler(console_handler)
        self.logger.setLevel(level)
        
        self._update_mode(mode=init_mode)

    def _update_temps(self):
        for ch in self.temp_channels:
            temp = self.controller.get_channel_temperature(ch)
            self.db.write_temp(channel=ch, val=temp)

    def _update_heaters(self):
        if self.first_exec:
            self.db.write_heater(self.heater_id, self.get_mxc_heater_value(), mode=self.mode)
            self.first_exec = False

        db_mode = self.db.read_heater_mode(index=self.mxc_heater)
        if db_mode != self.mode:
            self._update_mode(mode=db_mode)
            
        actual_val = self.get_mxc_heater_value()
        db_val = self.db.read_heater(index=self.mxc_heater, mode=self.mode)
        self.logger.debug(f'value from device: {actual_val}, value in database: {db_val}')

        if not isclose(actual_val, db_val):
            self.set_mxc_heater_value(db_val)
            
    def _update_mode(self, mode: str):
        assert mode in ['setpoint', 'power'], f"{mode} is not a valid operation mode! Choose'setpoint' for PID control and 'power' for direct power control"
        if mode == 'setpoint' and self.pid_calib is None:
            xld_logger.warning('PID mode chosen but no calibration provided. This is known to be unreliable in wide temperature ranges!')
            self.use_pid_calib = False
            
        if mode == 'setpoint':
            self.get_mxc_heater_value = self.controller.get_mxc_heater_setpoint
            self.set_mxc_heater_value = lambda x: self.controller.set_mxc_heater_setpoint(x, use_pid_calib=self.use_pid_calib)
        
        elif mode == 'power':
            self.get_mxc_heater_value = self.controller.get_mxc_heater_power
            self.set_mxc_heater_value = self.controller.set_mxc_heater_power
            
        self.mode = mode

    def exec(self):
        while True:
            try:
                self._update_temps()
                self._update_heaters()
                xld_logger.info(f"Temperatures and heater {self.mode} updated.")
                pause_for_next_execution(interval=self.update_interval, abort=self.abort)
            except Exception as ex:
                self.global_crash.set()
                sys.exit(f'FATAL ERROR: {ex}')


