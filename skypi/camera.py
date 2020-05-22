import logging
from time import sleep
from simple_pid import PID

try:
    from picamera import PiCamera
except ModuleNotFoundError:
    PiCamera = object


class ySkyPiCamera(PiCamera):
    PRESET = ["resolution", "framerate", "sensor_mode"]
    SLEEP = 20

    def __init__(self, camera_settings, target_brightness=None):
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

    def update_brightness(self, brightness):
        ...


class SkyPiCamera(PiCamera):
    SETTINGS_ORDER = {
        "preset": ["resolution", "framerate", "sensor_mode"],
        "postset": [
            "annotate_text_size",
            "awb_mode",
            "exposure_mode",
            "iso",
            "shutter_speed",
            "awb_gains",
        ],
    }
    SLEEP = 40

    pid: PID = None

    def __init__(self, camera_settings, target_brightness=None):
        self.log = logging.getLogger("skypicamera")
        self.CAPTURE_TIMEOUT = 120

        preset_items = {
            k: v
            for k, v in camera_settings.items()
            if k in self.SETTINGS_ORDER["preset"]
        }
        self.log.info(f" - camera init: {preset_items}")

        super().__init__(**preset_items)
        self.log.debug("Camera activated.")

        for attr in self.SETTINGS_ORDER["postset"]:
            if attr not in camera_settings:
                continue
            self.log.info(
                f" - camera setting: setting {attr} to {camera_settings[attr]}"
            )
            setattr(self, attr, camera_settings[attr])

        for name, value in camera_settings.items():
            if (
                name in self.SETTINGS_ORDER["postset"]
                or name in self.SETTINGS_ORDER["preset"]
            ):
                continue
            self.log.info(f" - camera setting: setting {name} to {value}")
            setattr(self, name, value)

        if target_brightness:
            self.pid = PID(
                Kp=100000,
                Ki=1,
                Kd=1,
                setpoint=target_brightness,
                output_limits=(-1000000, 1000000),
                proportional_on_measurement=True,
            )
            self.last_shutter_speed = camera_settings["shutter_speed"]
            self.max_shutter_speed = self.framerate_to_shutter_speed(
                camera_settings["framerate_range"][0]
            )
            self.min_shutter_speed = self.framerate_to_shutter_speed(
                camera_settings["framerate_range"][1]
            )

        sleep(self.SLEEP)

    @staticmethod
    def framerate_to_shutter_speed(fr):
        return round(1 / fr * 1000 * 1000)

    def update_brightness(self, brightness):
        if self.pid is None:
            return

        correction = self.pid(brightness)
        new_shutter_speed = self.last_shutter_speed + correction

        self.shutter_speed = min(
            max(round(new_shutter_speed), self.min_shutter_speed),
            self.max_shutter_speed,
        )
        self.last_shutter_speed = new_shutter_speed
        self.log.info(f"New shutter speed: {self.shutter_speed} ({correction})")

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
            awb_gains = ["999", "999"]

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


def fake_camera():
    global SkyPiCamera
    SkyPiCamera = FakeCamera
