import shutil
from logging import Logger
from subprocess import PIPE, Popen
from typing import List, Tuple


class SkyPiCommandRunner:
    BUFFER_CMD = ["buffer", "-m", "15000000", "-p", "40"]
    log: Logger

    def check_cmd(self, cmd: List):
        exe = cmd[0]
        if shutil.which(exe) is None:
            exception = f"Executable not found in path: '{exe}'"
            self.log.exception(exception)
            raise Exception(
                exception
            )

    def run_cmd(self, cmd: List[str], pipe: bool = False, stdout_pipe=False, **kwargs):
        self.log.debug(f"run_cmd {cmd}, pipe={pipe}, kwargs={kwargs}")
        return Popen(
            [arg.format(**kwargs) for arg in cmd],
            stdin=PIPE if pipe else None,
            stdout=PIPE if stdout_pipe else None,
            start_new_session=True,
        )

    def run_cmd_buffered(self, cmd: List[str], **kwargs) -> Tuple:
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
