import io
import logging
import signal
from datetime import date, datetime, timedelta
from threading import Thread
from time import sleep

import paho.mqtt.client as mqtt
import pytz
from astral import LocationInfo
from astral.sun import sun

from .camera import SkyPiCamera
from .output import SkyPiOutput
from .storage import SkyPiFileManager


class SkyPiControl:
    MODE_RECALC_TIME = 120  # seconds
    WATCHDOG_TIMEOUT = timedelta(seconds=360)
    current_mode = None
    stop = False
    shutdown = False
    watchdog = None
    forced_mode = None

    def __init__(self, settings):
        self.log = logging.getLogger()

        self.settings = settings
        self.mqtt_client = mqtt.Client(self.settings["mqtt"]["client_name"])
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.connect(self.settings["mqtt"]["server"])
        self.mqtt_client.loop_start()

        self.location = LocationInfo(**self.settings["location"])
        self.timer = Thread(target=self.watchdog_thread, daemon=True)
        self.timer.start()
        signal.signal(signal.SIGINT, self.signal_handler)

        self.file_managers = {
            name: SkyPiFileManager(**kwargs)
            for (name, kwargs) in self.settings["files"].items()
        }

    def on_message(self, client, userdata, msg):
        base = self.settings["mqtt"]["topic"]
        topic = msg.topic[len(base) :]
        payload = str(msg.payload).strip()
        self.log.debug(f"Received MQTT message on topic {topic} containing {payload}")
        if topic == "force_mode":
            self.forced_mode = payload
            self.stop = True

    def on_connect(self, client, userdata, flags, rc):
        base = self.settings["mqtt"]["topic"]
        client.subscribe(f"{base}/#")

    def signal_handler(self, sig, frame):
        self.log.warning("Caught Ctrl+C, stopping....")
        self.stop = True
        self.shutdown = True

    def publish(self, ext, message):
        topic = self.settings["mqtt"]["topic"] + ext
        self.log.debug(f"mqtt: {topic}: {message}")
        self.mqtt_client.publish(topic, message)

    def watchdog_thread(self):
        while not self.shutdown:
            sleep(5)
            if self.watchdog is None:
                continue
            if self.watchdog < datetime.now() - self.WATCHDOG_TIMEOUT:
                self.log.warn("Watchdog reset! Shutting down.")
                self.publish("watchdog_timeout", "1")
                sleep(3)
                self.run_cmd(self.settings["watchdog_reset"])

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

    def run_mode(self, mode):
        settings = self.settings["modes"][mode]
        started = datetime.now()
        canonical_date = self.canonical_date

        self.log.info(
            f"Started mode {mode}\n - started: {started}\n - canonical_date: {canonical_date}"
        )

        output = SkyPiOutput(
            self.file_managers,
            settings["processors"],
            date=canonical_date,
            mode=mode,
        )

        self.watchdog = datetime.now()
        with SkyPiCamera(settings["camera"]) as camera:

            self.watchdog = datetime.now()

            stream = io.BytesIO()

            image_time = datetime.now()
            camera.annotate_text = settings["annotation"].format(timestamp=image_time)
            # sleep(10)
            self.log.debug(f"First image: {camera.annotate_text}")

            for _ in camera.capture_continuous(stream, **settings["capture_options"]):
                # Touch the watchdog
                self.watchdog = datetime.now()
                conversion_time = datetime.now()

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

                if now_tz > self.next_switch or self.stop:
                    self.log.info("Finishing recording.")
                    break

                in_period = (now_tz - self.last_switch).total_seconds()
                period_percent = (in_period / self.total_period_time) * 100

                status = {
                    "capture/exposure_speed": camera.exposure_speed / 1000000,
                    "capture/iso": repr(camera.iso),
                    "capture/awb_gains": repr(camera.awb_gains),
                    "capture/exposure_duration": (
                        conversion_time - image_time
                    ).total_seconds(),
                    "timing/period_percent": period_percent,
                }

                for topic, message in status.items():
                    self.publish(topic, message)

                stream.seek(0)
                image_time = datetime.now()
                camera.annotate_text = settings["annotation"].format(
                    timestamp=image_time
                )
                self.log.debug(f"Next image: {camera.annotate_text}")

        stream.close()
        output.close()

        self.watchdog = None
        finished = datetime.now()
        self.log.info(f"Finished at {finished}; watchdog disabled.")
