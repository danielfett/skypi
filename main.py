#!/usr/bin/env python3
import io
import logging
import os
import re
import shutil
import signal
import sys
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty, SimpleQueue
from subprocess import PIPE, Popen
from threading import Thread
from time import sleep

import paho.mqtt.client as mqtt
import pytz
from yaml import Loader, load

from astral import LocationInfo
from astral.sun import sun

try:
    from picamera import PiCamera
except ModuleNotFoundError:
    PiCamera = object


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


class FakeCamera:
    def __init__(self, camera_settings):
        pass

    def __enter__(self):
        class Cam:
            exposure_speed = 999999
            iso = 900
            awb_gains = ['999', '999']

            def capture_continuous(self, stream, **kwargs):
                with open("test-image.jpg", "rb") as f:
                    image = f.read()
                while True:
                    try:
                        stream.write(image)
                        yield
                    finally:
                        pass

        return Cam()

    def __exit__(self, *args):
        pass


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
            started=started,
        )

        self.watchdog = datetime.now()
        with SkyPiCamera(settings["camera"]) as camera:

            self.watchdog = datetime.now()

            stream = io.BytesIO()

            image_time = datetime.now()
            camera.annotate_text = settings["annotation"].format(timestamp=image_time)
            #sleep(10)
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

                if (
                    now_tz > self.next_switch
                    or self.stop
                    or os.path.exists("/tmp/stop-skypic")
                ):
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


class DatetimeGlobWildcard:
    def __format__(self, format_spec):
        return re.sub(r"%[YmdHMS]", "*", format_spec)


class DatetimeREWildcard:
    def __format__(self, format_spec):
        self.format_spec = format_spec
        return "(" + re.sub(r"%[YmdHMS]", r"[0-9]{2,4}", format_spec) + ")"


class SkyPiFileManager:
    def __init__(
        self, base_path, storage_path, file_pattern, latest_path, latest_filename
    ):
        self.base_path = Path(base_path)
        self.storage_path = storage_path
        self.file_pattern = file_pattern

        if latest_path is not None:
            self.latest_path = self.base_path / latest_path
            self.latest_path.mkdir(parents=True, exist_ok=True)
            self.latest_filename = self.latest_path / latest_filename
            self.latest_filename_tmp = self.latest_path / ("tmp_" + latest_filename)
        else:
            self.latest_path = None

    def link_latest(self, filepath):
        if self.latest_path is None:
            return
        self.latest_filename_tmp.symlink_to(filepath)
        self.latest_filename_tmp.replace(self.latest_filename)

    def get_filestore(self, date, mode):
        return SkyPiFileStore(self, date, mode)


class SkyPiFile:
    def __init__(self, filepath, datetime):
        self.path = filepath
        self.datetime = datetime


class SkyPiFileStore:
    def __init__(self, manager, date, mode):
        self.manager = manager
        self.date = date
        self.mode = mode

        self.storage_path = self.manager.base_path / Path(
            self.manager.storage_path.format(date=date, mode=mode)
        )
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def get_filename(self, timestamp):
        return self.storage_path / self.manager.file_pattern.format(timestamp=timestamp)

    def get_existing_files(self):
        re_wildcard = DatetimeREWildcard()
        glob_string = self.manager.file_pattern.format(timestamp=DatetimeGlobWildcard())
        regex = self.manager.file_pattern.format(timestamp=re_wildcard)
        existing = sorted(self.storage_path.glob(glob_string))

        for f in existing:
            timestamp = re.findall(regex, str(f))[0]
            time = datetime.strptime(timestamp, re_wildcard.format_spec)
            yield SkyPiFile(f, time)

    def link_latest(self, filename):
        self.manager.link_latest(filename)

    @contextmanager
    def tempfile(self, timestamp):
        tmpfile = self.storage_path / (self.manager.file_pattern + "_tmp").format(
            timestamp
        )
        yield tmpfile
        tmpfile.replace(self.get_filename(timestamp))


class SkyPiOutput:
    def __init__(self, filemanagers, processor_settings, date, mode, started):
        self.log = logging.getLogger("output")
        self.filemanagers = filemanagers
        self.filestore = filemanagers["images"].get_filestore(date, mode)
        self.started = started
        self.date = date
        self.mode = mode
        self.init_processors(processor_settings)

    def init_processors(self, processor_settings):
        processor_classes = {
            p.__name__: p for p in [SkyPiOverlay, SkyPiTimelapse, SkyPiThumbnailGif]
        }

        self.processors = []
        try:
            for processor in processor_settings:
                filestore = self.filemanagers[processor["output"]].get_filestore(
                    self.date, self.mode
                )

                p = processor_classes[processor["class"]](
                    filestore=filestore, started=self.started, **processor["options"]
                )
                p.start()
                if processor["add_old_files"]:
                    if type(processor["add_old_files"]) is bool:
                        limit = 0
                    else:
                        limit = -processor["add_old_files"]
                    for f in list(self.filestore.get_existing_files())[limit:]:
                        p.add(f)
                self.processors.append(p)
        finally:
            self.close()

    def add_image(self, stream, timestamp):
        filename = self.filestore.get_filename(timestamp)
        # save image to disk
        with open(filename, "wb") as outfile:
            self.log.debug(f"Writing image to {filename}.")
            shutil.copyfileobj(stream, outfile, length=131072)  # arbitrary buffer size
            # outfile.write(stream.read())

        self.log.debug("...creating symlink")
        self.filestore.link_latest(filename)
        self.log.debug("...done.")

        for processor in self.processors:
            processor.add(SkyPiFile(filename, timestamp))

    def close(self):
        for processor in self.processors:
            processor.stop()


class SkyPiFileProcessor(Thread):
    BUFFER_SHUTIL_COPY = 131072
    BUFFER_CMD = ["buffer", "-m", "15000000", "-p", "40"]

    def __init__(self, filestore, started, name, cmd):
        self.log = logging.getLogger(self.LOG_ID)
        super().__init__()
        self.filestore = filestore
        self.cmd = cmd
        self.name = name
        self.configured = cmd is not None
        self.queue = SimpleQueue()
        self.stop_requested = False
        self.started = started
        self.path = self.filestore.get_filename(self.started)
        self.check_cmd(self.BUFFER_CMD)
        for _, command in self.cmd.items():
            self.check_cmd(command)

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

    def start_cmd(self):
        pass

    def stop_cmd(self):
        pass

    def check_cmd(self, cmd):
        exe = cmd[0]
        if shutil.which(exe) is None:
            raise Exception(
                f"Executable for processor '{self.name}' found in path: '{exe}'"
            )

    def run_cmd(self, cmd, pipe=False, stdout_pipe=False, **kwargs):
        self.log.debug(f"run_cmd {cmd}, pipe={pipe}, kwargs={kwargs}")
        return Popen(
            [arg.format(**kwargs) for arg in cmd],
            stdin=PIPE if pipe else None,
            stdout=PIPE if stdout_pipe else None,
            start_new_session=True,
        )

    def run_cmd_buffered(self, cmd, **kwargs):
        self.log.debug(f"run_cmd_buffered {cmd}, kwargs={kwargs}")
        buffer = Popen(
            self.BUFFER_CMD, stdin=PIPE, stdout=PIPE, start_new_session=True,
        )
        proc = Popen(
            [arg.format(**kwargs) for arg in cmd],
            stdin=buffer.stdout,
            start_new_session=True,
        )
        return (buffer, proc)

    def add(self, file: SkyPiFile):
        if not self.configured:
            return
        self.queue.put(file)

    def stop(self):
        self.stop_requested = True
        self.join()


class SkyPiOverlay(SkyPiFileProcessor):
    LOG_ID = "overlay"

    def start_cmd(self):
        proc = self.run_cmd(self.cmd["init"], filename_out=self.path,)
        proc.communicate()

    def process(self, file: SkyPiFile):
        self.log.debug(f"...adding to overlay image {self.path}.")
        overlay_proc = self.run_cmd(
            self.cmd["iterate"], filename_out=self.path, filename_in=file.path,
        )
        overlay_proc.communicate()

    def stop_cmd(self):
        self.filestore.link_latest(self.path)


class SkyPiTimelapse(SkyPiFileProcessor):
    LOG_ID = "timelapse"

    def start_cmd(self):
        self.timelapse_buffer, self.timelapse_proc = self.run_cmd_buffered(
            self.cmd["encode"], filename_out=self.path,
        )

    def process(self, file: SkyPiFile):
        with open(file.path, "rb") as f:
            shutil.copyfileobj(
                f, self.timelapse_buffer.stdin, length=self.BUFFER_SHUTIL_COPY
            )
            self.timelapse_buffer.stdin.flush()

    def stop_cmd(self):
        self.timelapse_buffer.communicate()
        self.timelapse_proc.communicate()
        self.filestore.link_latest(self.path)


class SkyPiThumbnailGif(SkyPiFileProcessor):
    LOG_ID = "thumbnailgif"

    def __init__(self, length, repeat_last_image, **kwargs):
        super().__init__(**kwargs)
        self.images = deque(maxlen=length)
        self.repeat_last_image = repeat_last_image

    def process(self, file: SkyPiFile):
        proc = self.run_cmd(self.cmd["extract"], stdout_pipe=True, filename_in=file.path)
        self.images.append(proc.communicate()[0])
        if len(self.images) < self.images.maxlen:
            return

        with self.filestore.tempfile(self.started) as tmp_path:
            proc = self.run_cmd(self.cmd["convert"], pipe=True, filename_out=tmp_path)
            for i in self.images:
                proc.stdin.write(i)
            for i in range(self.repeat_last_image):
                proc.stdin.write(self.images[-1])
            proc.communicate()

        self.filestore.link_latest(self.path)


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
    parser.add_argument("--fake-camera", action="store_true", help="Use fake camera")
    parser.add_argument(
        "--fake-root", action="store", help="Override storage paths to this value",
    )
    args = parser.parse_args()
    log_handler = logging.StreamHandler(sys.stdout)
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(name)s  %(levelname)s \t%(message)s")
    )
    log_handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger().addHandler(log_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    if "fake_camera" in args or PiCamera is object:
        logging.info("Using fake camera")
        SkyPiCamera = FakeCamera

    if "fake_root" in args:
        logging.info("Using fake root directory")

        OrigSkyPiFileManager = SkyPiFileManager

        class FakeFileManager:
            def __new__(self, base_path, **kwargs):
                return OrigSkyPiFileManager(base_path=args.fake_root, **kwargs)

        SkyPiFileManager = FakeFileManager

    settings = load(args.settingsfile, Loader=Loader)
    spc = SkyPiRunner(settings)
    spc.run()
