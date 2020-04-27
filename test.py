from picamera import PiCamera
from datetime import datetime
from time import sleep


class SkyPiCamera(PiCamera):
    def __init__(self, *args, **kwargs):
        # self.CAPTURE_TIMEOUT = 120
        super().__init__(*args, **kwargs)



with SkyPiCamera(resolution=[3280, 2464], sensor_mode=3, framerate=1 / 10) as camera:
    print("opened successfully!")
    camera.shutter_speed = 10000000
    #sleep(0.5)
    camera.iso = 800
    camera.exposure_mode = "sports"
    print("camera configured")

    sleep(20)
    for cnt, _ in enumerate(
        camera.capture_continuous("test{counter}.jpg", format="jpeg", burst=True)
    ):
        print ('click!')
        break

    camera.framerate = 1
