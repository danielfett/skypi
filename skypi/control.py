import io
import logging
from logging.handlers import TimedRotatingFileHandler
import signal
import sys
from datetime import date, datetime, timedelta
from threading import Thread
from time import sleep
from typing import Any, Dict, Optional, List

import paho.mqtt.client as mqtt
import pytz
from astral import LocationInfo
from astral.sun import sun

from .camera import SkyPiCamera
from .output import SkyPiOutput
from .storage import SkyPiFileManager


class SkyPiWatchdog(Thread):
    def __init__(self, timeout_seconds: Optional[int], reset_cmd: Optional[List]):
        super().__init__(daemon=True)
        self.log = logging.getLogger("watchdog")
        self.timeout = (
            timedelta(seconds=timeout_seconds) if timeout_seconds is not None else None
        )
        self.reset_cmd = reset_cmd
        self.last_ping = None

    def run(self):
        if self.timeout is None:
            return
        while True:
            sleep(5)
            self.check()

    def check(self):
        if self.last_ping is None:
            return

        elapsed = datetime.now() - self.last_ping

        if elapsed < self.timeout:
            return

        self.log.warn(
            f"Watchdog reset after {elapsed.total_seconds()}s! Shutting down."
        )
        if self.reset_cmd is not None:
            self.publish("watchdog_timeout", elapsed.total_seconds())
            sleep(3)
            self.run_cmd(self.reset_cmd)
        sys.exit()

    def ping(self):
        self.last_ping = datetime.now()

    def pause(self):
        self.last_ping = None


class SkyPiControl:
    MODE_RECALC_TIME: int = 120  # seconds
    WATCHDOG_TIMEOUT = timedelta(seconds=360)

    stop = False
    shutdown = False

    current_mode: Optional[str] = None
    forced_mode: Optional[str] = None

    watchdog: SkyPiWatchdog
    file_managers: Dict[str, SkyPiFileManager]

    log: logging.Logger
    mqtt_client: Optional[mqtt.Client]
    location: LocationInfo

    update_camera_settings: Dict[str, Any] = {}

    def __init__(self, settings: Dict[str, Any]):
        self.log = logging.getLogger()
        signal.signal(signal.SIGINT, self.signal_handler)

        self.configure_logging(settings["log"])

        self.settings = settings
        if 'mqtt' in self.settings:
            self.mqtt_client = mqtt.Client(self.settings["mqtt"]["client_name"])
            self.mqtt_client.on_connect = self.on_connect
            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(self.settings["mqtt"]["server"])
            self.mqtt_client.loop_start()
        else:
            self.mqtt_client = None

        self.location = LocationInfo(**self.settings["location"])
        self.watchdog = SkyPiWatchdog(**self.settings["watchdog"])
        self.watchdog.start()

        self.file_managers: Dict[str, SkyPiFileManager] = {
            name: SkyPiFileManager(name=name, **kwargs)
            for (name, kwargs) in self.settings["files"].items()
        }

    def configure_logging(self, filename):
        handler = TimedRotatingFileHandler(filename, when="D", backupCount=7)
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(name)s  %(levelname)s \t%(message)s")
        )
        handler.setLevel(logging.DEBUG)
        self.log.addHandler(handler)

    def on_message(self, client, userdata, msg):
        base = self.settings["mqtt"]["topic"]
        topic = msg.topic[len(base) :]
        payload = msg.payload.decode("utf-8").strip()
        self.log.debug(f"Received MQTT message on topic {topic} containing {payload}")
        if topic == "force_mode":
            self.forced_mode = payload
            self.stop = True
        if topic.startswith("set/"):
            setting = topic[len("set/") :]
            try:
                value = eval(payload)
            except Exception as e:
                self.log.exception(e)
            else:
                self.update_camera_settings[setting] = value

    def on_connect(self, client, userdata, flags, rc):
        base = self.settings["mqtt"]["topic"]
        client.subscribe(f"{base}#")

    def signal_handler(self, sig, frame):
        self.log.warning("Caught Ctrl+C, stopping....")
        self.stop = True
        self.shutdown = True

    def publish(self, ext, message):
        if self.mqtt_client is None:
            return
        topic = self.settings["mqtt"]["topic"] + ext
        self.log.debug(f"mqtt: {topic}: {message}")
        self.mqtt_client.publish(topic, message)

    def calculate_event_times(self):
        events_yesterday = sun(
            self.location.observer, date=date.today() - timedelta(days=1)
        )
        events = sun(self.location.observer, date=date.today())
        events_tomorrow = sun(
            self.location.observer, date=date.today() + timedelta(days=1)
        )

        self.log.info(f"Astral events yesterday")
        for event, time in events_yesterday.items():
            self.log.info(f" * {event} at {time}")

        self.log.info(f"Astral events today")
        for event, time in events.items():
            self.log.info(f" * {event} at {time}")

        self.log.info(f"Astral events tomorrow")
        for event, time in events_tomorrow.items():
            self.log.info(f" * {event} at {time}")

        now = datetime.now(pytz.utc)

        if now < events["dawn"]:
            self.current_mode = "night"
            self.canonical_date = date.today() - timedelta(days=1)
            self.last_switch = events_yesterday["dusk"]
            self.next_switch = events["dawn"]
        elif events["dawn"] <= now < events["dusk"]:
            self.current_mode = "day"
            self.canonical_date = date.today()
            self.last_switch = events["dawn"]
            self.next_switch = events["dusk"]
        else:
            self.current_mode = "night"
            self.canonical_date = date.today()
            self.last_switch = events["dusk"]
            self.next_switch = events_tomorrow["dawn"]

        self.log.info(f"new mode: '{self.current_mode}' until {self.next_switch}")
        self.publish("mode", self.current_mode or "none")

        self.total_period_time = (self.next_switch - self.last_switch).total_seconds()

    def run(self):
        while not self.shutdown:
            self.calculate_event_times()
            if self.forced_mode is not None:
                self.current_mode = self.forced_mode
                self.forced_mode = None
            if self.current_mode is None:
                self.log.debug("Nothing to do...")
                sleep(60)
            elif self.current_mode in self.settings["modes"]:
                self.log.debug(f"Starting mode {self.current_mode}")
                self.run_mode(self.current_mode)
            self.stop = False

        for _, filemanager in self.file_managers.items():
            filemanager.close()

    def run_mode(self, mode):
        settings = self.settings["modes"][mode]
        started = datetime.now()
        canonical_date = self.canonical_date

        self.log.info(
            f"Started mode {mode}\n - started: {started}\n - canonical_date: {canonical_date}"
        )

        output = SkyPiOutput(
            self.file_managers, settings["processors"], date=canonical_date, mode=mode,
        )

        self.watchdog.ping()

        with SkyPiCamera(settings["camera"]) as camera:

            self.watchdog.ping()

            stream = io.BytesIO()

            image_time = datetime.now()
            camera.annotate_text = settings["annotation"].format(timestamp=image_time)
            # sleep(10)
            self.log.debug(f"First image: {camera.annotate_text}")

            for _ in camera.capture_continuous(stream, **settings["capture_options"]):
                # Touch the watchdog
                self.watchdog.ping()
                conversion_time = datetime.now()

                # Check if we need to update the camera settings
                for key, value in self.update_camera_settings.items():
                    try:
                        setattr(camera, key, value)
                    except Exception as e:
                        self.log.exception(e)
                    else:
                        self.log.info(f"Setting camera setting {key} to {repr(value)}")
                self.update_camera_settings = {}

                # Let our subscribers know
                self.log.debug(f"Click! (took {(datetime.now()-image_time).seconds}s)")

                stream.truncate()
                stream.seek(0)
                output.add_image(
                    stream, timestamp=image_time,
                )

                self.log.debug(
                    f"Image conversion done (took {(datetime.now()-conversion_time).total_seconds()}s)"
                )
                sleep(settings["wait_between"])

                now_tz = datetime.now(pytz.utc)

                in_period = (now_tz - self.last_switch).total_seconds()
                period_percent = (in_period / self.total_period_time) * 100

                awb_red, awb_blue = camera.awb_gains
                status = {
                    "capture/exposure_speed": camera.exposure_speed,
                    "capture/iso": camera.iso,
                    "capture/awb_gains/red": float(round(awb_red, 2)),
                    "capture/awb_gains/blue": float(round(awb_blue, 2)),
                    "capture/gains/digital": float(round(camera.digital_gain, 2)),
                    "capture/gains/analog": float(round(camera.analog_gain, 2)),
                    "capture/exposure_duration": (
                        conversion_time - image_time
                    ).total_seconds(),
                    "timing/period_percent": period_percent,
                }

                for topic, message in status.items():
                    self.publish(topic, message)

                if now_tz > self.next_switch or self.stop:
                    self.log.info("Finishing recording.")
                    break

                stream.seek(0)
                image_time = datetime.now()
                camera.annotate_text = settings["annotation"].format(
                    timestamp=image_time
                )
                self.log.debug(f"Next image: {camera.annotate_text}")

        self.watchdog.pause()
        stream.close()
        output.close()

        finished = datetime.now()
        self.log.info(f"Finished at {finished}; watchdog disabled.")
