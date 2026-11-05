cat > cloudphoto.py <<'PY'
#!/usr/bin/env python3
"""CLI фотоархива в Yandex Object Storage."""

import argparse
import base64
import hashlib
import html
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote

import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError

ENDPOINT = "https://storage.yandexcloud.net"
REGION = "ru-central1"
ENV_NAMES = ("BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
WEBSITE = {
    "IndexDocument": {"Suffix": "index.html"},
    "ErrorDocument": {"Key": "error.html"},
}


class Error(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise Error("Ошибка в аргументах")


def valid_name(value):
    if not value or "/" in value or "\0" in value:
        raise ValueError("Недопустимое имя")
    value.encode("utf-8")
    return value


def name_argument(value):
    try:
        return valid_name(value)
    except (ValueError, UnicodeError):
        raise argparse.ArgumentTypeError("Недопустимое имя") from None


def arguments(argv):
    parser = Parser(prog="cloudphoto")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list")
    listing.add_argument("--album", type=name_argument)

    for command in ("upload", "download"):
        child = commands.add_parser(command)
        child.add_argument("--album", required=True, type=name_argument)
        child.add_argument("--path", default=".")

    deletion = commands.add_parser("delete")
    deletion.add_argument("--album", required=True, type=name_argument)
    deletion.add_argument("--photo", type=name_argument)

    commands.add_parser("mksite")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        if any(not os.environ.get(name) for name in ENV_NAMES):
            raise Error("Не заданы переменные окружения")

        args = arguments(argv)

        if hasattr(args, "path") and not args.path:
            raise Error("Ошибка в аргументах")

        archive_class = globals().get("Archive")
        method = "albums" if args.command == "list" else args.command

        if archive_class is None or not hasattr(archive_class, method):
            raise Error("Команда пока не реализована")

        client = make_client(
            os.environ["AWS_ACCESS_KEY_ID"],
            os.environ["AWS_SECRET_ACCESS_KEY"],
        )
        archive = archive_class(client, os.environ["BUCKET"])

        if args.command == "list":
            names = (
                archive.albums()
                if args.album is None
                else archive.photos(args.album)
            )
            for name in names:
                print(name)

        elif args.command in ("upload", "download"):
            getattr(archive, args.command)(args.album, args.path)

        elif args.command == "delete":
            archive.delete(args.album, args.photo)

        else:
            archive.mksite()
            print(f"https://{archive.bucket}/")

        return 0

    except (Exception, KeyboardInterrupt) as exc:
        message = (
            "Операция прервана"
            if isinstance(exc, KeyboardInterrupt)
            else str(exc)
        )

        for name in ENV_NAMES[1:]:
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[скрыто]")

        print(message or "Ошибка выполнения программы", file=sys.stderr)
        return 1
PY