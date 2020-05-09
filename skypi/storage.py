import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


def get_files_for_pattern(base_path, pattern, placeholders, type):
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
                path=f,
                **{
                    name: wildcard.value(match)
                    for name, wildcard in placeholders.items()
                },
            )


@dataclass
class SkyPiFile:
    path: Path
    timestamp: datetime


@dataclass
class SkyPiFolder:
    path: Path
    mode: str
    date: datetime


class DatetimeWildcard:
    def id(self):
        return f"dtr{id(self)}"

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
        return datetime.strptime(match.group(self.id()), self.format_spec)


class ModeWildcard:
    def id(self):
        return f"mr{id(self)}"

    class GlobWildcard:
        def __format__(self, format_spec):
            return "*"

    def __format__(self, format_spec):
        self.format_spec = format_spec
        return f"(?P<{self.id()}>day|night)"

    def value(self, match):
        return match.group(self.id())


class SkyPiFileManager:
    def __init__(
        self,
        base_path,
        storage_path,
        file_pattern,
        latest_path,
        latest_filename,
        retain_days=None,
    ):
        self.log = logging.getLogger("filemanager")
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

    def link_latest(self, filepath):
        if self.latest_path is None:
            return
        self.latest_filename_tmp.symlink_to(filepath)
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
            type=SkyPiFolder,
        )

    def cleanup(self, retain_days):
        cutoff = datetime.now() - timedelta(days=retain_days)
        self.log.info(f"Removing files older than {retain_days} days ({cutoff}).")
        for file in self.get_existing_folders():
            self.log.debug(f"Folder {file.path} is from {file.date:%Y-%m-%d}.")
            if file.date < cutoff:
                self.get_filestore(date=file.date, mode=file.mode).delete_all()


class SkyPiFileStore:
    def __init__(self, manager, date, mode):
        self.manager = manager
        self.date = date
        self.mode = mode
        self.log = logging.getLogger("filestore")

        self.storage_path = self.manager.base_path / Path(
            self.manager.storage_path.format(date=date, mode=mode)
        )
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def get_filename(self, timestamp):
        return self.storage_path / self.manager.file_pattern.format(timestamp=timestamp)

    def get_existing_files(self):
        placeholders = {
            "timestamp": DatetimeWildcard(),
        }
        return get_files_for_pattern(
            self.storage_path,
            self.manager.file_pattern,
            placeholders=placeholders,
            type=SkyPiFile,
        )

    def link_latest(self, filename):
        self.manager.link_latest(filename)

    @contextmanager
    def tempfile(self, timestamp):
        tmpfile = self.storage_path / (self.manager.file_pattern + "_tmp").format(
            timestamp
        )
        yield tmpfile
        tmpfile.replace(self.get_filename(timestamp))

    def delete_all(self):
        self.log.info(f"Removing files from {self.storage_path}.")
        files = self.get_existing_files()
        for f in files:
            self.log.debug(f" - delete {f}")
            f.path.unlink()
        try:
            self.storage_path.rmdir()
        except OSError as e:
            if e.errno != 39:  # Directory not empty
                raise


def fake_root(path):
    global SkyPiFileManager
    OrigSkyPiFileManager = SkyPiFileManager

    class FakeFileManager:
        def __new__(self, base_path, **kwargs):
            return OrigSkyPiFileManager(base_path=path, **kwargs)

    SkyPiFileManager = FakeFileManager
