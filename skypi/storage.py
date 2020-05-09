import logging
import re
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

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


def fake_root(path):
    global SkyPiFileManager
    OrigSkyPiFileManager = SkyPiFileManager
    class FakeFileManager:
        def __new__(self, base_path, **kwargs):
            return OrigSkyPiFileManager(base_path=path, **kwargs)

    SkyPiFileManager = FakeFileManager