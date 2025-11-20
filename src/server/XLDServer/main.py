import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from multiprocessing import Process, Event

from waitress import serve

from flask import Flask, request, render_template, redirect, flash
import flask_login
from flask_login import LoginManager, UserMixin
import json

import os
from passkey import KEY, USERS, BLUEFORS_CONTROL_IP, SERVER_HOST_IP, DATA_DIR, MXC_HEATER_ID, MXC_CHANNEL_ID, ACTIVE_CHANNELS, PORT, PID_CALIB_FILE

if not os.path.isdir(DATA_DIR):
    try:
        os.mkdir(DATA_DIR)
    except Exception as ex:
        print(f'Log directory creation failed! Cannot create {DATA_DIR}')
        print(ex)
        sys.exit()

from tempcomm import XLDTempHandler
from database_sqlite import ServerDB
from temperature_sweep import TemperatureSweepManager, TemperatureSweep
from event_logger import xld_logger

emulate_temp_controller = True
emulate_sweep = True
debug = False
db = ServerDB()
db.prep_tables()

app = Flask(__name__)
app.secret_key = KEY

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

t_sweep_manager = TemperatureSweepManager()

abort = Event()
sweep_running = Event()
global_crash = Event()

log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s] [%(name)s] %(message)s")
    
level = logging.INFO if not debug else logging.DEBUG

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

global_logger = logging.getLogger('GLOBAL')
global_logger.setLevel(level)
global_logger.addHandler(console_handler)

class User(UserMixin):
    def __init__(self, id):
        self.id = id


def exec_flask():
    app.config['TEMPLATES_AUTO_RELOAD'] = True

    serve(app, host=SERVER_HOST_IP)


@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

# @app.before_request
# def before_request():
#     print('here', global_crash.is_set())
#     if global_crash.is_set():
#         sys.exit('FATAL ERROR')

@app.route("/meas/register", methods=['POST'])
def meas_reg():
    if not t_sweep_manager.started:
        cont = json_request_handler()
        token = db.register_measurement(user=cont['user'], group=cont['group'])

        return json.dumps({'id': token})

    else:
        return json.dumps({'error': 'ERROR! Sweep already started.'})


@app.route("/meas/deregister", methods=['POST'])
def meas_dereg():
    cont = json_request_handler()
    db.deregister_measurement(meas_id=cont['id'])

    return json.dumps({'deregistered': True})


@app.route('/temperature-sweep', methods=['GET'])
@flask_login.login_required
def temperature_sweep():
    if request.method == 'GET':
        return render_template('temperature_sweep.html', sweep=t_sweep_manager.html_dict)


@app.route('/temperature-sweep/generate', methods=['POST'])
@flask_login.login_required
def generate_temp_sweep():
    payload = json_request_handler()

    t_sweep_manager.generate_sweep_array(params=payload)

    return json.dumps(t_sweep_manager.html_dict)


@app.route('/temperature-sweep/broadcast-start', methods=['POST'])
@flask_login.login_required
def broadcast_temp_sweep():
    payload = json_request_handler()

    if payload['broadcast']:
        t_sweep_manager.confirm()
        return json.dumps({'confirmed': True, 'started': False})

    elif payload['start']:
        if not t_sweep_manager.started and not sweep_running.is_set():
            if t_sweep_manager.mode == 'direct-power':
                tmode = 'power'
                
            elif t_sweep_manager.mode == 'pid':
                tmode = 'setpoint'
                
            else:
                raise NotImplementedError('Not a valid sweep mode.')
            t_sweep = TemperatureSweep(thermalization_time=float(t_sweep_manager.therm_time) * 60,
                                       power_array=t_sweep_manager.sweep_array,
                                       client_timeout=float(t_sweep_manager.cl_timeout) * 60,
                                       abort_flag=abort, is_running=sweep_running, test_mode=emulate_sweep,
                                       return_to_base=t_sweep_manager.return_to_base,
                                       skip_first=t_sweep_manager.skip_first,
                                       mode=tmode)
            t_sweep_manager.start_sweep()
            sweep_running.set()
            t_sweep_process = Process(target=t_sweep.exec(), name='Temperature Sweep')
            t_sweep_process.start()

        return json.dumps({'confirmed': True, 'sweep_started': True})


@app.route('/temperature-sweep/abort', methods=['GET'])
@flask_login.login_required
def abort_temp_sweep():
    if t_sweep_manager.started and sweep_running.is_set():
        global_logger.info('Abort sweep initiated.')
        abort.set()
        t_sweep_manager.clear()
        return json.dumps({'aborted': False, 'initiated': True})

    elif not sweep_running.is_set():
        global_logger.info("Abort confirmed.")
        abort.clear()
        return json.dumps({'aborted': True, 'initiated': True})

    else:
        return json.dumps({'aborted': False, 'initiated': False})


@app.route('/temperature-sweep/info', methods=['GET'])
# @flask_login.login_required
def info_temp_sweep():
    # print('confirmed:', t_sweep_manager.confirmed, '-- started: ', t_sweep_manager.started)
    # print('abort event: ', abort.is_set(), '-- running event: ', sweep_running.is_set())
    if not sweep_running.is_set() and t_sweep_manager.started:
        t_sweep_manager.clear()
        return json.dumps({'abort_in_progress': False, 'confirmed': False, 'sweep_started': False})

    elif t_sweep_manager.confirmed:
        return json.dumps(t_sweep_manager.client_dict)

    elif t_sweep_manager.started:
        return json.dumps({'abort_in_progress': False, 'confirmed': True, 'sweep_started': True})

    elif abort.is_set() and sweep_running.is_set():
        return json.dumps({'abort_in_progress': True})

    else:
        return json.dumps({'abort_in_progress': False, 'confirmed': False, 'sweep_started': False})


@app.route('/temperature-sweep/params', methods=['GET'])
@flask_login.login_required
def params_temp_sweep():
    if t_sweep_manager.confirmed:
        return json.dumps(t_sweep_manager.html_dict)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user_id = request.form.get("username")
        entered_password = request.form.get("password")
        if user_id in USERS and USERS[user_id]['password'] == entered_password:
            user = User(user_id)
            flask_login.login_user(user)
            flash('Login successful', 'Success!')
            return redirect('/')

        flash('Login failed!')

    return render_template("login.html")


@app.route('/meas/status', methods=['GET', 'POST'])
@flask_login.login_required
def meas_status_get():
    if request.method == 'GET':
        return render_template('measurements.html', measurements=db.get_html_meas_dict())

    else:
        payload = json_request_handler()
        if 'start' in payload.keys() and payload['start']:
            db.set_all_meas_to_go()
            flash("All signal flags set to GO")
            return redirect('/meas/status')

        elif 'delete' in payload.keys() and payload['delete']:
            meas_info = db.get_single_meas_dict(meas_id=payload['meas_id'])
            db.deregister_measurement(meas_id=meas_info['id'])
            flash(f"Deleted measurement client")  # run by {meas_info['user']} ({meas_info['group']})")
            return redirect('/meas/status')


@app.route('/meas/status/set', methods=['POST'])
def meas_status_set_post():
    payload = json_request_handler()
    if not db.get_single_meas_dict(meas_id=payload['id'])['crashed']:
        db.set_meas_status(meas_id=payload['id'], status=payload['running'])
    else:
        pass

    return json.dumps({'running': True})


@app.route('/logout')
@flask_login.login_required
def logout():
    flask_login.logout_user()
    flash('Logged out!')
    return redirect('/')


@app.route("/control", methods=["GET", "POST"])
@flask_login.login_required
def control():
    if sweep_running.is_set():
        return render_template("not_available.html")

    if request.method == "POST":
        mode = request.form.get('mode select')
        new_value = request.form.get("value")
        assert mode in db.heater_modes, "Not a valid heater mode"
        db.write_heater(index=db.mxc_ind, val=float(new_value), mode=mode)
        return redirect('/control')

    else:
        return render_template("control.html", powers=get_all_powers(), temps=get_all_temps())


@app.route('/temps/mxc', methods=['GET'])
def get_base_temp():
    return json.dumps({'mxc_temp': db.read_temp(channel=db.mxc_ch)})


@app.route('/meas/signal', methods=['POST'])
def get_meas_signal():
    payload = json_request_handler()

    return json.dumps({'signal': db.get_meas_signal(payload['id'])})


@app.route('/', methods=['GET'])
def index():
    return render_template('home.html', temps=get_all_temps(), powers=get_all_powers())


def json_request_handler():
    if request.is_json:
        return request.json

    else:
        return


def get_all_temps():
    temps = {'mxc': f'{db.read_temp(db.mxc_ch) * 1e3:4.2f}',
             'still': f'{db.read_temp(db.still_ch) * 1e3:4.2f}',
             'four_k': f'{db.read_temp(db.fourk_ch):2.0f}',
             'fifty_k': f'{db.read_temp(db.fiftyk_ch):2.0f}'}

    return temps


def get_all_powers():
    mode = db.read_heater_mode(db.mxc_ind)
    mode_str = 'ERROR'
    if mode == 'setpoint':
        mode_str = 'PID Control'
    elif mode == 'power':
        mode_str = 'Direct Power Control'
    powers = {'mxc': f'{db.read_heater(db.mxc_ind, mode='power'):4.3f}',
              'mxc_setpoint': f'{db.read_heater(db.mxc_ind, mode='setpoint'):4.2f}',
              'mxc_mode': mode_str}

    return powers


def exec_tcontrol():
    tccontrol = XLDTempHandler(database=db, ip=BLUEFORS_CONTROL_IP, update_interval=5, emulate=emulate_temp_controller, 
                               active_channels=ACTIVE_CHANNELS, api_key=KEY, port=PORT, mxc_id=MXC_CHANNEL_ID, heater_id=MXC_HEATER_ID, 
                               init_mode='setpoint', debug=debug, abort=abort, pid_calib=PID_CALIB_FILE, global_crash=global_crash)
    tccontrol.exec()


flask_server = Process(target=exec_flask, name='XLD Server')
tc_process = Process(target=exec_tcontrol, name='Temperature Controller')

if __name__ == "__main__":
    flask_server.start()
    tc_process.start()
    # app.run(debug=True)
    # exec_flask()
