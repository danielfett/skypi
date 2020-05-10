from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .upload import SkyPiUploader


def get_files_for_pattern(base_path, pattern, placeholders, type, **type_extra_args):
    # find the number of path segments in the pattern
    p = Path(pattern)
    segments_to_compare = len(p.parts)

    # normalize pattern if it is a path (might end with /)
    pattern_normalized = str(p)

    glob_string = pattern_normalized.format(
        **{name: wildcard.GlobWildcard() for name, wildcard in placeholders.items()}
    )
    regex = pattern_normalized.format(**placeholders)
    existing = sorted(base_path.glob(glob_string))

    for f in existing:
        # normalize filename (compare only x last segments)
        filename_normalized = str(Path(*f.parts[-segments_to_compare:]))
        match = re.match(regex, filename_normalized)
        if match is None:
            logging.getLogger("pattern_matcher").error(
                f"Pattern '{regex}' did not match for path '{filename_normalized}'."
            )
        else:
            yield type(
                **type_extra_args,
                **{
                    name: wildcard.value(match)
                    for name, wildcard in placeholders.items()
                },
            )


class Wildcard:
    format_spec = None

    def id(self):
        return f"regroup{id(self)}"


class DatetimeWildcard(Wildcard):
    class GlobWildcard:
        def __format__(self, format_spec):
            twos = re.sub(r"%[mdHMS]", "??", format_spec)
            fours = re.sub(r"%Y", "????", twos)
            return fours

    def __format__(self, format_spec):
        self.format_spec = format_spec
        twos = re.sub(r"%[mdHMS]", r"[0-9]{2}", format_spec)
        fours = re.sub(r"%Y", r"[0-9]{4}", twos)
        return f"(?P<{self.id()}>{fours})"

    def value(self, match):
        if self.format_spec is None:
            return None
        return datetime.strptime(match.group(self.id()), self.format_spec)


class ModeWildcard(Wildcard):
    class GlobWildcard:
        def __format__(self, format_spec):
            return "*"

    def __format__(self, format_spec):
        self.format_spec = format_spec
        return f"(?P<{self.id()}>day|night)"

    def value(self, match):
        if self.format_spec is None:
            return None
        return match.group(self.id())


class SkyPiFileManager:
    uploader: Optional[SkyPiUploader]

    def __init__(
        self,
        name,
        base_path,
        storage_path,
        file_pattern,
        latest_path,
        latest_filename,
        retain_days=None,
        upload=None,
    ):
        self.log = logging.getLogger(f"filemanager '{name}'")
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

        if retain_days is not None:
            self.cleanup(retain_days)

        if upload:
            self.uploader = SkyPiUploader(self, name, **upload)
            self.uploader.start()

    def link_latest(self, file):
        if self.latest_path is None:
            return
        self.latest_filename_tmp.symlink_to(file.path)
        self.latest_filename_tmp.replace(self.latest_filename)

    def get_filestore(self, date, mode):
        return SkyPiFileStore(self, date, mode)

    def get_existing_folders(self):
        placeholders = {
            "date": DatetimeWildcard(),
            "mode": ModeWildcard(),
        }
        return get_files_for_pattern(
            self.base_path,
            self.storage_path,
            placeholders=placeholders,
            type=SkyPiFileStore,
            manager=self,
        )

    def cleanup(self, retain_days):
        cutoff = datetime.now() - timedelta(days=retain_days)
        self.log.info(f"Removing files older than {retain_days} days ({cutoff}).")
        for file in self.get_existing_folders():
            self.log.debug(f"Folder {file.path} is from {file.date:%Y-%m-%d}.")
            if file.date < cutoff:
                self.get_filestore(date=file.date, mode=file.mode).delete_all()

    def close(self):
        if self.uploader is not None:
            self.uploader.stop()


class SkyPiFileStore:
    manager: SkyPiFileManager
    date: datetime
    mode: str
    path: Path

    def __init__(self, manager, date, mode):
        self.manager = manager
        self.date = date
        self.mode = mode
        self.log = logging.getLogger("filestore")

        self.path = self.manager.base_path / Path(
            self.manager.storage_path.format(date=date, mode=mode)
        )
        self.path.mkdir(parents=True, exist_ok=True)

    def get_file_path(self, timestamp) -> Path:
        return self.path / self.manager.file_pattern.format(timestamp=timestamp)

    def get_temp_file_path(self, timestamp) -> Path:
        return self.path / (self.manager.file_pattern + "_tmp").format(timestamp)

    def get_existing_files(self):
        placeholders = {
            "timestamp": DatetimeWildcard(),
        }
        return get_files_for_pattern(
            self.path,
            self.manager.file_pattern,
            placeholders=placeholders,
            type=SkyPiFile,
            filestore=self,
        )

    def link_latest(self, file: SkyPiFile):
        self.manager.link_latest(file)

    def delete_all(self):
        self.log.info(f"Removing files from {self.path}.")
        files = self.get_existing_files()
        for f in files:
            self.log.debug(f" - delete {f}")
            f.path.unlink()
        try:
            self.path.rmdir()
        except OSError as e:
            if e.errno != 39:  # Directory not empty
                raise


class SkyPiFile:
    filestore: SkyPiFileStore
    path: Path
    timestamp: Optional[datetime]

    def __init__(
        self,
        filestore: SkyPiFileStore,
        timestamp: Optional[datetime] = None,
        use_tempfile=False,
    ):
        self.filestore = filestore
        self.timestamp = timestamp
        if not use_tempfile:
            self.path = filestore.get_file_path(timestamp=timestamp)
            self.orig_path = None
        else:
            self.path = filestore.get_temp_file_path(timestamp=timestamp)
            self.orig_path = filestore.get_file_path(timestamp=timestamp)

    def finish(self):
        self.filestore.link_latest(self)
        if self.orig_path is None:
            return
        if self.path.exists():
            self.path.replace(self.orig_path)


def fake_root(path):
    global SkyPiFileManager
    OrigSkyPiFileManager = SkyPiFileManager

    class FakeFileManager:
        def __new__(self, base_path, **kwargs):
            return OrigSkyPiFileManager(base_path=path, **kwargs)

    SkyPiFileManager = FakeFileManager
