import argparse

from src.normalize.crsp_delistings import run


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--force",
        action="store_true",
    )

    args = parser.parse_args()

    run(force=args.force)


if __name__ == "__main__":
    main()