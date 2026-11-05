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

cat >> cloudphoto.py <<'PY'


def xml_content_md5(params, **kwargs):
    digest = hashlib.md5(params["body"], usedforsecurity=False).digest()
    params["headers"]["Content-MD5"] = base64.b64encode(digest).decode("ascii")

    checksum = params["context"].get("checksum", {})
    checksum.pop("request_algorithm", None)
    checksum.pop("request_algorithm_header", None)


def make_client(access_key, secret_key):
    core = botocore.session.Session()
    core.set_config_variable("config_file", os.devnull)
    core.set_config_variable("credentials_file", os.devnull)
    core.get_component("config_store").set_config_variable("profile", None)

    session = boto3.session.Session(
        botocore_session=core,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=REGION,
    )

    client = session.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "total_max_attempts": 3},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )

    for operation in ("DeleteObjects", "PutBucketWebsite"):
        client.meta.events.register(
            f"before-call.s3.{operation}",
            xml_content_md5,
        )

    return client


def encode(value):
    return valid_name(value).encode("utf-8").hex()


def decode(value):
    try:
        if not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
            raise ValueError()
        return valid_name(bytes.fromhex(value).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise Error("Повреждён ключ объекта фотоархива") from None


def checked_key(value):
    if len(value.encode("utf-8")) > 1024:
        raise Error("Слишком длинное имя: ключ объекта превышает 1024 байта")
    return value


def album_prefix(album):
    return checked_key(f"albums/{encode(album)}/")


def marker_key(album):
    return checked_key(album_prefix(album) + "marker")


def photo_key(album, photo):
    return checked_key(album_prefix(album) + "photos/" + encode(photo))


def is_photo(name):
    return name.lower().endswith((".jpg", ".jpeg"))


def sorted_names(names):
    return sorted(names, key=lambda value: value.encode("utf-8"))


class Archive:
    def __init__(self, client, bucket):
        self.s3 = client
        self.bucket = bucket

    def keys(self, prefix=""):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in (
                "404", "NoSuchKey", "NotFound"
            ):
                return False
            raise

    def require_album(self, album):
        if not self.exists(marker_key(album)):
            raise Error(f"В фотоархиве нет альбома с именем {album}")

    def albums(self):
        names = []

        for key in self.keys("albums/"):
            parts = key.split("/")
            if len(parts) == 3 and parts[2] == "marker":
                names.append(decode(parts[1]))

        return sorted_names(names)

    def photos(self, album):
        self.require_album(album)
        prefix = album_prefix(album) + "photos/"
        names = []

        for key in self.keys(prefix):
            name = decode(key[len(prefix):])

            if not is_photo(name):
                raise Error(
                    "В разделе фотографий обнаружен некорректный объект"
                )

            names.append(name)

        return sorted_names(names)
PY

cat >> cloudphoto.py <<'PY'

    def upload(self, album, directory):
        directory = Path(directory)

        with os.scandir(directory) as entries:
            photos = [
                item.name
                for item in entries
                if is_photo(item.name) and item.is_file()
            ]

        if not photos:
            raise Error("В каталоге нет фотографий")

        photos = sorted_names(photos)
        pairs = [
            (name, photo_key(album, name))
            for name in photos
        ]

        self.s3.put_object(
            Bucket=self.bucket,
            Key=marker_key(album),
            Body=b"",
        )

        for name, key in pairs:
            self.s3.upload_file(
                str(directory / name),
                self.bucket,
                key,
                ExtraArgs={
                    "ContentType": "image/jpeg",
                    "CacheControl": "no-cache",
                },
            )

    def download(self, album, directory):
        photos = self.photos(album)
        directory = Path(directory)

        with tempfile.TemporaryFile(dir=directory):
            pass

        for name in photos:
            fd, temporary = tempfile.mkstemp(
                prefix=".cloudphoto-",
                dir=directory,
            )
            os.close(fd)

            try:
                self.s3.download_file(
                    self.bucket,
                    photo_key(album, name),
                    temporary,
                )
                os.replace(temporary, directory / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
PY

cat >> cloudphoto.py <<'PY'

    def remove_keys(self, keys):
        for offset in range(0, len(keys), 1000):
            result = self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": [
                        {"Key": key}
                        for key in keys[offset:offset + 1000]
                    ],
                    "Quiet": True,
                },
            )

            if result.get("Errors"):
                first = result["Errors"][0]
                raise Error(
                    "Не удалось удалить объект: "
                    + first.get("Code", "ошибка S3")
                )

    def delete(self, album, photo=None):
        self.require_album(album)

        if photo is not None:
            key = photo_key(album, photo)

            if not is_photo(photo) or not self.exists(key):
                raise Error(
                    f"В альбоме нет фотографии с именем {photo}"
                )

            self.s3.delete_object(Bucket=self.bucket, Key=key)
            return

        marker = marker_key(album)
        keys = [
            key
            for key in self.keys(album_prefix(album))
            if key != marker
        ]

        self.remove_keys(keys)
        self.s3.delete_object(Bucket=self.bucket, Key=marker)
PY

cat >> cloudphoto.py <<'PY'

    def put_html(self, key, document):
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=document.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            CacheControl="no-cache",
        )

    def mksite(self):
        self.s3.put_bucket_acl(
            Bucket=self.bucket,
            ACL="public-read",
        )
        self.s3.put_bucket_website(
            Bucket=self.bucket,
            WebsiteConfiguration=WEBSITE,
        )

        albums = self.albums()

        old_pages = {
            key
            for key in self.keys("album")
            if re.fullmatch(r"album[1-9][0-9]*\.html", key)
        }

        current_pages = set()
        links = []

        for number, album in enumerate(albums, 1):
            page = f"album{number}.html"

            self.put_html(
                page,
                album_html(album, self.photos(album)),
            )

            current_pages.add(page)
            links.append(
                f'<li><a href="{page}">'
                f"{html.escape(album)}</a></li>"
            )

        self.put_html(
            "error.html",
            document(
                "Фотоархив",
                "<h1>Ошибка</h1>"
                "<p>Ошибка при доступе к фотоархиву. "
                'Вернитесь на <a href="index.html">'
                "главную страницу</a> фотоархива.</p>",
            ),
        )

        if albums:
            content = "<ul>\n" + "\n".join(links) + "\n</ul>"
        else:
            content = "<p>Фотоархив пустой.</p>"

        self.put_html(
            "index.html",
            document(
                "Фотоархив",
                "<h1>Фотоархив</h1>\n" + content,
            ),
        )

        self.remove_keys(sorted(old_pages - current_pages))


def document(title, body, extra_head=""):
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"{extra_head}\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def album_html(album, photos):
    title = f'Фотоальбом "{album}"'
    back = (
        '<p>Вернуться на <a href="index.html">'
        "главную страницу</a> фотоархива</p>"
    )
    head = ""

    if photos:
        base = "https://cdnjs.cloudflare.com/ajax/libs/galleria/1.6.1"

        head = (
            f'<link rel="stylesheet" '
            f'href="{base}/themes/classic/galleria.classic.min.css">\n'
            "<style>"
            ".galleria{width:960px;height:540px;background:#000}"
            "</style>\n"
            '<script src="https://ajax.googleapis.com/ajax/libs/'
            'jquery/3.6.0/jquery.min.js"></script>\n'
            f'<script src="{base}/galleria.min.js"></script>\n'
            f'<script src="{base}/themes/classic/'
            'galleria.classic.min.js"></script>'
        )

        images = []

        for photo in photos:
            url = quote(photo_key(album, photo), safe="/")
            images.append(
                f'<img src="{html.escape(url, quote=True)}" '
                f'data-title="{html.escape(photo, quote=True)}">'
            )

        content = (
            '<div class="galleria">\n'
            + "\n".join(images)
            + "\n</div>"
        )
        content += (
            back
            + "\n<script>Galleria.run('.galleria');</script>"
        )
    else:
        content = "<p>Фотоальбом пустой.</p>" + back

    return document(
        title,
        f"<h1>{html.escape(title)}</h1>\n" + content,
        head,
    )


if __name__ == "__main__":
    sys.exit(main())
PY