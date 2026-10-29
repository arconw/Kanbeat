from __future__ import annotations

import fcntl
import os
import sys
import termios


def main() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
