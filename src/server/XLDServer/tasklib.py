from datetime import datetime, timedelta
from threading import Event
from time import sleep

def pause_for_next_execution(interval: int, abort: Event, update: float = 1, cleanup: callable = None, kwargs: dict = None):
    next_exec = datetime.now() + timedelta(seconds=interval)

    while datetime.now() < next_exec:
        if abort.is_set():
            if cleanup is not None:
                if kwargs is None:
                    cleanup()
                else:
                    cleanup(**kwargs)
            return False
        
        sleep(update)
    
    return True