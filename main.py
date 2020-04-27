#!/usr/bin/env python3
import io
import logging
import os
import shutil
import signal
import sys
from datetime import date, datetime, timedelta
from fractions import Fraction
from os.path import join, exists
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

    def get_path(self, date, mode, id, **kwargs):
        return join(
            self.settings["files"]["path"].format(date=date, mode=mode,),
            self.settings["files"][id],
        ).format(**kwargs)

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
            ["buffer", "-m", "15000000", "-p", "40"],
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

    def run_mode(self, mode):
        settings = self.settings["modes"][mode]
        started = datetime.now()
        canonical_date = self.canonical_date
        os.makedirs(
            self.settings["files"]["path"].format(date=canonical_date, mode=mode,),
            exist_ok=True,
        )
        overlay_path = self.get_path(canonical_date, mode, "overlay", started=started)
        timelapse_path = self.get_path(
            canonical_date, mode, "timelapse", started=started,
        )
        latest_path = self.settings["files"]["latest"]
        latest_path_tmp = f"{self.settings['files']['latest']}.tmp"

        self.log.info(
            f"Started mode {mode}\n - started: {started}\n - canonical_date: {canonical_date}\n - overlay_path: {overlay_path}\n - timelapse_path: {timelapse_path}"
        )

        self.watchdog = datetime.now()
        cnt = 0

        with SkyPiCamera(settings["camera"]) as camera:

            if "overlay_cmd" in settings:
                self.log.debug("Running overlay init command")
                proc = self.run_cmd(
                    settings["overlay_cmd"]["init"], filename_out=overlay_path,
                )
                proc.communicate()
                self.log.debug("...done.")

            if "timelapse_cmd" in settings:
                self.log.debug("Starting timelapse command")
                timelapse_buffer, timelapse_proc = self.run_cmd_buffered(
                    settings["timelapse_cmd"], filename_out=timelapse_path,
                )

            self.watchdog = datetime.now()

            current_image_set = []
            stream = io.BytesIO()

            image_time = datetime.now()
            camera.annotate_text = settings["annotation"].format(timestamp=image_time)
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

                # Prepare the image storage
                filename = self.get_path(
                    canonical_date, mode, "image", timestamp=image_time
                )
                current_image_set.append(filename)

                # save image to disk
                stream.truncate()
                stream.seek(0)
                with open(filename, "wb") as outfile:
                    self.log.debug(f"Writing image to {filename}.")
                    shutil.copyfileobj(
                        stream, outfile, length=131072
                    )  # arbitrary buffer size
                    # outfile.write(stream.read())
                    self.log.debug("...creating symlink")
                    os.symlink(filename, latest_path_tmp)
                    os.rename(latest_path_tmp, latest_path)
                    self.log.debug("...done.")

                # add to overlay
                if "overlay_cmd" in settings:
                    self.log.debug(f"Adding to overlay image {overlay_path}.")
                    stream.seek(0)
                    self.log.debug("...seeked")
                    overlay_proc = self.run_cmd(
                        settings["overlay_cmd"]["iterate"],
                        pipe=True,
                        filename_out=overlay_path,
                        filename_in=filename,
                    )
                    self.log.debug("...command started")
                    # overlay_proc.stdin.write(stream.read())
                    shutil.copyfileobj(
                        stream, overlay_proc.stdin, length=131072
                    )  # arbitrary buffer size
                    self.log.debug("...data copied")
                    overlay_proc.stdin.close()
                    self.log.debug("...done.")

                # add to timelapse
                if "timelapse_cmd" in settings:
                    self.log.debug("Adding to timelapse...")
                    stream.seek(0)
                    self.log.debug("...seeked")
                    shutil.copyfileobj(
                        stream, timelapse_buffer.stdin, length=131072
                    )  # arbitrary buffer size
                    # timelapse_proc.stdin.write(stream.read())
                    self.log.debug("...data copied")
                    timelapse_buffer.stdin.flush()
                    self.log.debug("...done.")

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

                camera.annotate_text = settings["annotation"].format(
                    timestamp=image_time
                )
                self.log.debug(f"Next image: {camera.annotate_text} → {filename}")
                self.publish("capture_cnt", cnt)
                self.publish("capture_filename", filename)
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

        stream.close()
        if "timelapse_cmd" in settings:
            timelapse_buffer.communicate()
            timelapse_proc.communicate()

        self.watchdog = None
        finished = datetime.now()
        self.log.info(f"Finished at {finished}; watchdog disabled.")

        # save list of files
        fileslist_path = self.get_path(canonical_date, mode, "fileslist")


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
