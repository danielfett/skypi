import logging
from json import dumps, loads
from threading import Thread
from time import sleep
from typing import Any, Dict, List

from skypi.common import SkyPiCommandRunner


class SkyPiUploader(Thread, SkyPiCommandRunner):
    RECHECK_TIME = 120  # seconds
    ERROR_WAIT_TIME = 120
    MIN_UPLOAD_SIZE = 1024 
    stop_requested = False
    upload_status: Dict[str, Any]

    def __init__(self, manager, name, cmd: List):
        super().__init__()
        self.log = logging.getLogger(f"uploader '{name}'")
        self.log.info("Uploader started")
        self.status_file = manager.base_path / f".upload_status_{name}.json"
        self.load_upload_status()
        self.manager = manager
        self.check_cmd(cmd)
        self.cmd = cmd

    def load_upload_status(self):
        if not self.status_file.exists():
            self.upload_status = {"uploaded": []}
        else:
            self.upload_status = loads(self.status_file.read_text())

    def save_upload_status(self):
        self.status_file.write_text(dumps(self.upload_status))

    def canonicalize(self, file) -> str:
        # we use only relative filenames to allow for changing the base path later on
        return str(file.path.relative_to(self.manager.base_path))

    def run(self):
        while not self.stop_requested:
            self.sleep(self.RECHECK_TIME)
            for file in self.get_uploads_todo():
                if file.get_size() < self.MIN_UPLOAD_SIZE:
                    self.log.debug(f"Skipping file which is smaller than {self.MIN_UPLOAD_SIZE}: {file}")
                if self.upload(file):
                    self.upload_status["uploaded"].append(self.canonicalize(file))
                    self.save_upload_status()
                else:
                    self.sleep(self.ERROR_WAIT_TIME)
                if self.stop_requested:
                    return

    def sleep(self, time: int):
        for i in range(time):
            sleep(1)
            if self.stop_requested:
                return

    def upload(self, file) -> bool:
        proc = self.run_cmd(
            self.cmd,
            False,
            False,
            filename=file.path,
            timestamp=file.timestamp,
            mode=file.filestore.mode,
            date=file.filestore.date,
        )
        proc.communicate()
        if proc.returncode != 0:
            self.log.error(
                f"Error uploading file {file.path}; return code={proc.returncode}"
            )
            return False
        return True

    def get_uploads_todo(self) -> List:
        files: List = []
        for folder in self.manager.get_existing_folders():
            for file in folder.get_existing_files():
                if self.canonicalize(file) not in self.upload_status["uploaded"]:
                    files.append(file)

        # Reverse the order of returned files to prevent that a single faulty file
        # (too large? wrong format? empty?) stops the upload queue. Upload latest
        # file first
        files.reverse()
        return files

    def stop(self):
        self.stop_requested = True
        if self.is_alive():
            self.join()
