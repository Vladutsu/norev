"""Create the key file for the encrypted Session store.

  python -m norev.adapters.store new-key <path>   write 32 random bytes; never overwrites

Keep the key outside the store's folder and back it up separately: without it the store
cannot be read. Then set NOREV_STORE_KEY_FILE=<path>.
"""
import argparse
import sys

from norev.adapters.store.encrypted import create_key_file


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.adapters.store", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["new-key"])
    parser.add_argument("path", help="where to write the key file")
    args = parser.parse_args(argv)
    try:
        create_key_file(args.path)
    except FileExistsError:
        print(f"{args.path} already exists; not overwritten", file=sys.stderr)
        return 1
    print(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
