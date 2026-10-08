"""Download only rows added since the previous successful synchronization."""

import sys

from airport_parking.sync import main


if __name__ == "__main__":
    sys.exit(main(["sync", *sys.argv[1:]]))
