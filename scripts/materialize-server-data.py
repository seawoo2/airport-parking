"""Assemble complete analysis CSVs from the local cumulative store."""

import sys

from airport_parking.sync import main


if __name__ == "__main__":
    sys.exit(main(["materialize", *sys.argv[1:]]))
