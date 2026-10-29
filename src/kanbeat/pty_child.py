from __future__ import annotations

import fcntl
import os
import sys
import termios

from .launcher import external_environment


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv if argv is None else argv
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(argv[1], argv[1:], external_environment())


if __name__ == "__main__":
    main()
