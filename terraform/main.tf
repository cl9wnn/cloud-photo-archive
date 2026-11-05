terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.243.0"
    }
  }
}

# Провайдер получает IAM-токен из YC_TOKEN.
provider "yandex" {
  folder_id = var.folder_id
}

variable "folder_id" {
  type        = string
  description = "ID существующего учебного каталога"
}

variable "bucket_name" {
  type        = string
  description = "Имя нового бакета: photos.vvot27.itiscl.ru"
}

variable "domain" {
  type        = string
  description = "Домен существующей зоны без завершающей точки: vvot27.itiscl.ru"
}

variable "cert_id" {
  type        = string
  description = "ID существующего сертификата Certificate Manager"
}

variable "dns_zone_id" {
  type        = string
  description = "По буквальному описанию ТЗ: FQDN сайта, НЕ ID зоны"
}

variable "existing_dns_zone_id" {
  type        = string
  description = "Настоящий ID существующей DNS-зоны Yandex Cloud"
}

data "yandex_dns_zone" "existing" {
  dns_zone_id = var.existing_dns_zone_id
}

resource "yandex_iam_service_account" "cloudphoto" {
  folder_id   = var.folder_id
  name        = "cloudphoto-${substr(sha256(var.bucket_name), 0, 12)}"
  description = "CLI фотоархива"
}

resource "yandex_resourcemanager_folder_iam_member" "storage_admin" {
  folder_id = var.folder_id
  role      = "storage.admin"
  member    = "serviceAccount:${yandex_iam_service_account.cloudphoto.id}"
}

resource "yandex_iam_service_account_static_access_key" "cloudphoto" {
  service_account_id = yandex_iam_service_account.cloudphoto.id
  description        = "S3 key for cloudphoto"

  depends_on = [
    yandex_resourcemanager_folder_iam_member.storage_admin
  ]
}

resource "yandex_storage_bucket" "photos" {
  folder_id     = var.folder_id
  bucket        = var.bucket_name
  force_destroy = false

  website {
    index_document = "index.html"
    error_document = "error.html"
  }

  https {
    certificate_id = var.cert_id
  }

  lifecycle {
    precondition {
      condition = (
        var.bucket_name == var.dns_zone_id &&
        var.dns_zone_id == "photos.${var.domain}"
      )

      error_message = "Нужно bucket_name = dns_zone_id = photos.<domain>, без завершающей точки."
    }

    precondition {
      condition = (
        data.yandex_dns_zone.existing.zone == "${var.domain}." &&
        data.yandex_dns_zone.existing.public
      )

      error_message = "existing_dns_zone_id должен указывать на публичную зону <domain>."
    }
  }
}

resource "yandex_storage_bucket_grant" "public_read" {
  bucket = yandex_storage_bucket.photos.bucket
  acl    = "public-read"
}

resource "yandex_dns_recordset" "photos" {
  zone_id = data.yandex_dns_zone.existing.id
  name    = "${var.dns_zone_id}."
  type    = "CNAME"
  ttl     = 300

  data = [
    "${yandex_storage_bucket.photos.bucket}.website.yandexcloud.net."
  ]
}

output "bucket_name" {
  value = yandex_storage_bucket.photos.bucket
}

output "website_url" {
  value = "https://${var.dns_zone_id}/"
}

output "service_account_id" {
  value = yandex_iam_service_account.cloudphoto.id
}

output "access_key_id" {
  value     = yandex_iam_service_account_static_access_key.cloudphoto.access_key
  sensitive = true
}

output "secret_access_key" {
  value     = yandex_iam_service_account_static_access_key.cloudphoto.secret_key
  sensitive = true
}