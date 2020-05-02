#!/usr/bin/env python3
import io
import logging
import os
import shutil
import signal
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty, SimpleQueue
from subprocess import PIPE, Popen
from threading import Thread
from time import sleep

import paho.mqtt.client as mqtt
import pytz
from astral import LocationInfo
from astral.sun import sun
from picamera import PiCamera
from yaml import Loader, load


class SkyPiCamera(PiCamera):
    PRESET = ["resolution", "framerate", "sensor_mode"]
    SLEEP = 20

    def __init__(self, camera_settings):
        self.log = logging.getLogger("skypicamera")
        self.CAPTURE_TIMEOUT = 120

        preset_items = {k: v for k, v in camera_settings.items() if k in self.PRESET}
        postset_items = {
            k: v for k, v in camera_settings.items() if k not in self.PRESET
        }

        for name, value in preset_items.items():
            self.log.info(f" - camera init: setting {name} to {value}")

        super().__init__(**preset_items)
        self.log.debug("Camera activated.")

        for name, value in postset_items.items():
            self.log.info(f" - camera setting: setting {name} to {value}")
            setattr(self, name, value)

        sleep(self.SLEEP)

    def close(self):
        # fix for https://github.com/waveform80/picamera/issues/528
        self.framerate = 1
        super().close()


class SkyPiRunner:
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
        events = sun(self.location.observer, date=date.today())
        events_tomorrow = sun(
            self.location.observer, date=date.today() + timedelta(days=1)
        )

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
            self.next_switch = events["dawn"]
        elif events["dawn"] <= now < events["dusk"]:
            self.current_mode = "day"
            self.canonical_date = date.today()
            self.next_switch = events["dusk"]
        else:
            self.current_mode = "night"
            self.canonical_date = date.today()
            self.next_switch = events_tomorrow["dawn"]

        self.log.info(f"new mode: '{self.current_mode}' until {self.next_switch}")
        self.publish("mode", self.current_mode or "none")

    def run(self):
        while not self.shutdown:
            self.calculate_event_times()
            if self.forced_mode is not None:
                self.current_mode = self.forced_mode
                self.forced_mode = None
            if self.current_mode == None:
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

        self.watchdog = datetime.now()
        with SkyPiCamera(settings["camera"]) as camera:
            output = SkyPiOutput(
                self.settings["files"],
                settings["processors"],
                date=canonical_date,
                mode=mode,
                started=started,
            )
            self.watchdog = datetime.now()

            stream = io.BytesIO()

            image_time = datetime.now()
            camera.annotate_text = settings["annotation"].format(timestamp=image_time)
            sleep(10)
            self.log.debug(f"First image: {camera.annotate_text}")

            for cnt, _ in enumerate(
                camera.capture_continuous(stream, **settings["capture_options"])
            ):
                # Touch the watchdog
                self.watchdog = datetime.now()
                conversion_time = datetime.now()

                # Let our subscribers know
                self.log.debug(
                    f"Click {cnt}! (took {(datetime.now()-image_time).seconds}s)"
                )

                stream.truncate()
                stream.seek(0)
                output.add_image(
                    stream, timestamp=image_time,
                )

                self.log.debug(
                    f"Image conversion done (took {(datetime.now()-conversion_time).total_seconds()}s)"
                )
                sleep(settings["wait_between"])

                if (
                    datetime.now(pytz.utc) > self.next_switch
                    or self.stop
                    or os.path.exists("/tmp/stop-skypic")
                ):
                    self.log.info("Finishing recording.")
                    break

                self.publish("capture_cnt", cnt)
                self.publish("capture_exposure_speed", camera.exposure_speed / 1000000)
                self.publish("capture_digital_gain", repr(camera.digital_gain))
                self.publish("capture_analog_gain", repr(camera.analog_gain))
                self.publish("capture_iso", repr(camera.iso))
                self.publish(
                    "capture_exposure_duration",
                    (conversion_time - image_time).total_seconds(),
                )
                stream.seek(0)
                image_time = datetime.now()
                camera.annotate_text = settings["annotation"].format(
                    timestamp=image_time
                )
                self.log.debug(f"Next image: {camera.annotate_text}")

                try:
                    with open('/tmp/awb_gains', 'r') as f:
                        tup = f.read().split(' ')
                        camera.awb_gains = [float(tup[0]), float(tup[1])]
                        print ('-------------- NEW AWB')
                except Exception as e:
                    print (e)


        stream.close()
        output.close()

        self.watchdog = None
        finished = datetime.now()
        self.log.info(f"Finished at {finished}; watchdog disabled.")


class DatetimeWildcard:
    def __format__(value, format_spec):
        return "*"


class SkyPiOutput:
    def __init__(self, path_settings, processor_settings, **params):

        self.log = logging.getLogger("output")

        self.base_path = Path(path_settings["base_path"].format(**params))
        self.image_path = self.base_path / path_settings["image_path"]
        self.image_path.mkdir(parents=True, exist_ok=True)

        self.latest_path = Path(path_settings["latest"])
        self.latest_path_tmp = Path(path_settings["latest"] + ".tmp")

        self.params = params
        self.current_image_set = []
        self.image_name_pattern = path_settings["image_name"]

        self.existing_images = sorted(
            self.image_path.glob(
                self.image_name_pattern.format(timestamp=DatetimeWildcard())
            )
        )

        self.init_processors(processor_settings)

    def init_processors(self, processor_settings):
        processor_classes = {p.__name__: p for p in [SkyPiOverlay, SkyPiTimelapse]}

        self.processors = []
        for processor in processor_settings:
            output_path = self.base_path / processor["output"].format(**self.params)
            p = processor_classes[processor["class"]](processor["cmd"], output_path)
            p.start()
            if processor["add_old_files"]:
                for f in self.existing_images:
                    p.add(f)
            self.processors.append(p)

    def add_image(self, stream, timestamp):
        filename = self.image_path / self.image_name_pattern.format(timestamp=timestamp)
        # save image to disk
        with open(filename, "wb") as outfile:
            self.log.debug(f"Writing image to {filename}.")
            shutil.copyfileobj(stream, outfile, length=131072)  # arbitrary buffer size
            # outfile.write(stream.read())

        self.log.debug("...creating symlink")
        self.latest_path_tmp.symlink_to(filename)
        self.latest_path_tmp.replace(self.latest_path)
        self.log.debug("...done.")
        self.current_image_set.append(str(filename))

        for processor in self.processors:
            processor.add(filename)

    def close(self):
        for processor in self.processors:
            processor.stop()


class SkyPiFileProcessor(Thread):
    BUFFER_SHUTIL_COPY = 131072
    BUFFER_PIPE = 15000000

    def __init__(self, cmd, output_path, *args, **kwargs):
        self.log = logging.getLogger(self.LOG_ID)
        super().__init__(*args, **kwargs)
        self.cmd = cmd
        self.configured = cmd is not None
        self.path = output_path
        self.queue = SimpleQueue()
        self.stop_requested = False

    def run(self):
        if not self.configured:
            self.log.warning(f"Not starting {self.LOG_ID} command: not configured.")
            return

        self.log.debug(f"Starting {self.LOG_ID} command")
        self.start_cmd()
        while not self.stop_requested:
            try:
                next_file = self.queue.get(block=True, timeout=1)
            except Empty:
                pass
            else:
                self.log.debug(f"Processing from {self.LOG_ID} queue: {next_file}")
                self.process(next_file)
                self.log.debug("...done.")

        self.stop_cmd()

    def run_cmd(self, cmd, pipe=False, **kwargs):
        self.log.debug(f"run_cmd {cmd}, pipe={pipe}, kwargs={kwargs}")
        return Popen(
            [arg.format(**kwargs) for arg in cmd],
            stdin=PIPE if pipe else None,
            start_new_session=True,
        )

    def run_cmd_buffered(self, cmd, **kwargs):
        self.log.debug(f"run_cmd_buffered {cmd}, kwargs={kwargs}")
        buffer = Popen(
            ["buffer", "-m", str(self.BUFFER_PIPE), "-p", "40"],
            stdin=PIPE,
            stdout=PIPE,
            start_new_session=True,
        )
        proc = Popen(
            [arg.format(**kwargs) for arg in cmd],
            stdin=buffer.stdout,
            start_new_session=True,
        )
        return (buffer, proc)

    def add(self, filename):
        if not self.configured:
            return
        self.log.debug(f"Adding to {self.LOG_ID} queue: {filename}")
        self.queue.put(filename)

    def stop(self):
        self.stop_requested = True
        self.join()


class SkyPiOverlay(SkyPiFileProcessor):
    LOG_ID = "overlay"

    def start_cmd(self):
        proc = self.run_cmd(self.cmd["init"], filename_out=self.path,)
        proc.communicate()

    def process(self, filename):
        self.log.debug(f"...adding to overlay image {self.path}.")
        overlay_proc = self.run_cmd(
            self.cmd["iterate"], filename_out=self.path, filename_in=filename,
        )
        overlay_proc.communicate()

    def stop_cmd(self):
        pass


class SkyPiTimelapse(SkyPiFileProcessor):
    LOG_ID = "timelapse"

    def start_cmd(self):
        self.timelapse_buffer, self.timelapse_proc = self.run_cmd_buffered(
            self.cmd, filename_out=self.path,
        )

    def process(self, filename):
        with open(filename, "rb") as f:
            shutil.copyfileobj(
                f, self.timelapse_buffer.stdin, length=self.BUFFER_SHUTIL_COPY
            )
            self.timelapse_buffer.stdin.flush()

    def stop_cmd(self):
        self.timelapse_buffer.communicate()
        self.timelapse_proc.communicate()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("settingsfile", type=argparse.FileType("r"))
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        help="Increase debug level to show debug messages.",
    )
    args = parser.parse_args()
    log_handler = logging.StreamHandler(sys.stdout)
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s \t %(levelname)s \t %(message)s")
    )
    log_handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger().addHandler(log_handler)
    logging.getLogger().setLevel(logging.DEBUG)

    settings = load(args.settingsfile, Loader=Loader)
    spc = SkyPiRunner(settings)
    spc.run()

#
