#!/usr/bin/env python3
import logging
import sys

from yaml import Loader, load


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("settingsfile", type=argparse.FileType("r"))
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        help="Increase debug level to show debug messages.",
    )
    parser.add_argument("--fake-camera", action="store_true", help="Use fake camera")
    parser.add_argument(
        "--fake-root", action="store", help="Override storage paths to this value",
    )
    args = parser.parse_args()
    log_handler = logging.StreamHandler(sys.stdout)
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(name)s  %(levelname)s \t%(message)s")
    )
    log_handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger().addHandler(log_handler)
    logging.getLogger().setLevel(logging.DEBUG)

    if args.fake_camera:
        logging.info("Using fake camera")
        import skypi.camera

        skypi.camera.fake_camera()

    if args.fake_root:
        logging.info("Using fake root directory")

        import skypi.storage

        skypi.storage.fake_root(args.fake_root)

    from skypi.control import SkyPiControl

    settings = load(args.settingsfile, Loader=Loader)
    spc = SkyPiControl(settings)
    spc.run()
