import logging
from time import sleep

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


def fake_camera():
    global SkyPiCamera
    SkyPiCamera = FakeCamera
