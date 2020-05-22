import logging
import shutil
from collections import deque
from queue import Empty, SimpleQueue
from threading import Thread
from typing import List, Dict
from datetime import datetime
from PIL import Image, ImageStat
import math

from .storage import SkyPiFile, SkyPiFileManager
from .common import SkyPiCommandRunner


class SkyPiOutput:
    def __init__(
        self,
        filemanagers: Dict[str, SkyPiFileManager],
        processor_settings: List[Dict],
        date: datetime,
        mode: str,
    ):
        self.filemanagers = filemanagers
        self.date = date
        self.mode = mode
        self.image_processor = SkyPiImageProcessor(
            filemanagers["images"].get_filestore(date, mode)
        )
        self.processor_classes = {
            cls.__name__: cls for cls in SkyPiFileProcessor.__subclasses__()
        }
        self.init_processors(processor_settings)

    def init_processors(self, processor_settings: List[Dict]):
        self.processors: List[SkyPiFileProcessor] = []
        try:
            for settings in processor_settings:
                p = self.create_processor(**settings)
                self.processors.append(p)
        except Exception:
            self.close()
            raise

        for processor in self.processors:
            processor.start()

    def create_processor(self, cls, output, options={}):
        filestore = self.filemanagers[output].get_filestore(self.date, self.mode)

        p = self.processor_classes[cls](filestore=filestore, **options)

        if p.ADD_OLD_FILES:
            if type(p.ADD_OLD_FILES) is bool:
                limit = 0
            else:
                limit = -p.ADD_OLD_FILES
            for f in list(self.image_processor.filestore.get_existing_files())[limit:]:
                p.add(f)

        return p

    def add_image(
        self, stream, timestamp: datetime, calc_brightness=False
    ) -> SkyPiFile:
        file = self.image_processor.add_image(stream, timestamp, calc_brightness)
        for processor in self.processors:
            processor.add(file)
        return file

    def close(self):
        for processor in self.processors:
            processor.stop()


class SkyPiProcessor:
    BUFFER_SHUTIL_COPY = 131072


class SkyPiImageProcessor(SkyPiProcessor):
    BRIGHTNESS_BORDER_SIZE = [0.18, 0.24]

    def __init__(self, filestore):
        self.log = logging.getLogger("output")
        self.filestore = filestore

    def add_image(
        self, stream, timestamp: datetime, calc_brightness: bool
    ) -> SkyPiFile:
        file = SkyPiFile(self.filestore, timestamp)
        # save image to disk
        with open(file.path, "wb") as outfile:
            self.log.debug(f"Writing image to {file.path}.")
            shutil.copyfileobj(stream, outfile, length=self.BUFFER_SHUTIL_COPY)

        # calculate brightness?
        if calc_brightness:
            stream.seek(0)
            with Image.open(stream) as im:
                crop_area = [
                    self.BRIGHTNESS_BORDER_SIZE[0] * im.width,
                    self.BRIGHTNESS_BORDER_SIZE[1] * im.height,
                    (1 - self.BRIGHTNESS_BORDER_SIZE[0]) * im.width,
                    (1 - self.BRIGHTNESS_BORDER_SIZE[1]) * im.height,
                ]

                stat = ImageStat.Stat(im.crop(crop_area))
                r, g, b = stat.mean
                brightness = math.sqrt(
                    0.299 * (r ** 2) + 0.587 * (g ** 2) + 0.114 * (b ** 2)
                )
                file.brightness = brightness

        self.log.debug("...creating symlink")
        file.finish()
        self.log.debug("...done.")

        return file


class SkyPiFileProcessor(Thread, SkyPiCommandRunner, SkyPiProcessor):
    USE_TEMPFILE = False

    def __init__(self, filestore, name, cmd):
        self.log = logging.getLogger(f"processor '{name}'")
        super().__init__()
        self.cmd = cmd
        self.configured = cmd is not None
        self.queue = SimpleQueue()
        self.stop_requested = False
        self.output_file = SkyPiFile(filestore, use_tempfile=self.USE_TEMPFILE)

        self.check_cmd(self.BUFFER_CMD)
        for _, command in self.cmd.items():
            self.check_cmd(command)

    def run(self):
        if not self.configured:
            self.log.warning(f"Not starting processor: not configured.")
            return

        self.log.debug(f"Starting")
        self.start_cmd()
        while not self.stop_requested:
            try:
                next_file = self.queue.get(block=True, timeout=1)
            except Empty:
                pass
            else:
                self.log.debug(f"Processing from queue: {next_file}")
                self.process(next_file)
                self.log.debug("...done.")

        self.stop_cmd()

    def start_cmd(self):
        pass

    def stop_cmd(self):
        pass

    def add(self, file: SkyPiFile):
        if not self.configured:
            return
        self.queue.put(file)

    def stop(self):
        self.stop_requested = True
        if self.is_alive():
            self.join()


class SkyPiOverlay(SkyPiFileProcessor):
    ADD_OLD_FILES = True

    def __init__(self, max_brightness=None, **kwargs):
        super().__init__(**kwargs)
        self.max_brightness = max_brightness

    def start_cmd(self):
        proc = self.run_cmd(self.cmd["init"], filename_out=self.output_file.path,)
        proc.communicate()

    def process(self, file: SkyPiFile):
        if (
            self.max_brightness is not None
            and file.brightness is not None
            and file.brightness > self.max_brightness
        ):
            self.log.debug(f"Skipping image: {file.brightness} > {self.max_brightness}")
            return
        self.log.debug(f"...adding to overlay image {self.output_file.path}.")
        overlay_proc = self.run_cmd(
            self.cmd["iterate"],
            filename_out=self.output_file.path,
            filename_in=file.path,
        )
        overlay_proc.communicate()

    def stop_cmd(self):
        self.output_file.finish()


class SkyPiTimelapse(SkyPiFileProcessor):
    ADD_OLD_FILES = True
    USE_TEMPFILE = True

    def start_cmd(self):
        self.timelapse_buffer, self.timelapse_proc = self.run_cmd_buffered(
            self.cmd["encode"], filename_out=self.output_file.path,
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
        self.output_file.finish()


class SkyPiThumbnailGif(SkyPiFileProcessor):
    USE_TEMPFILE = True

    def __init__(self, length, repeat_last_image, **kwargs):
        super().__init__(**kwargs)
        self.images = deque(maxlen=length)
        self.repeat_last_image = repeat_last_image
        self.ADD_OLD_FILES = length

    def process(self, file: SkyPiFile):
        proc = self.run_cmd(
            self.cmd["extract"], stdout_pipe=True, filename_in=file.path
        )
        self.images.append(proc.communicate()[0])
        if len(self.images) < self.images.maxlen:
            return

        proc = self.run_cmd(
            self.cmd["convert"], pipe=True, filename_out=self.output_file.path
        )
        for i in self.images:
            proc.stdin.write(i)
        for i in range(self.repeat_last_image):
            proc.stdin.write(self.images[-1])
        proc.communicate()

        self.output_file.finish()
