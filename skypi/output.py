import logging
import shutil
from collections import deque
from queue import Empty, SimpleQueue
from subprocess import PIPE, Popen
from threading import Thread

from .storage import SkyPiFile


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
