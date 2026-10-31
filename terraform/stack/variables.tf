variable "region" {
  description = "OCI region used by Resource Manager and runtime clients."
  type        = string
  default     = "ap-osaka-1"

  # Compute の image（schema.yaml の instance_image_source_id）を用意しているリージョンだけを許す（#660）。
  validation {
    condition     = contains(["ap-osaka-1", "ap-tokyo-1", "us-chicago-1"], var.region)
    error_message = "region は ap-osaka-1、ap-tokyo-1、us-chicago-1 のいずれかにしてください。"
  }
}

variable "availability_domain" {
  description = "Availability domain for the Compute instance."
  type        = string
  default     = ""
}

variable "compartment_ocid" {
  description = "OCI compartment OCID for the Compute instances of the selected products."
  type        = string
  default     = ""
}

variable "adb_compartment_ocid" {
  description = "OCI compartment OCID used to create or select the Autonomous AI Database."
  type        = string
  default     = ""

  validation {
    condition = (
      trimspace(var.adb_compartment_ocid) != ""
      && can(regex("^ocid1\\.compartment\\.", trimspace(var.adb_compartment_ocid)))
    )
    error_message = "ADBのコンパートメントを選択し、有効なCompartment OCIDを指定してください。"
  }
}

variable "vcn_ai_vcn_id" {
  description = "VCN OCID used by the Resource Manager form for subnet filtering."
  type        = string
  default     = ""
}

variable "adb_deployment_mode" {
  description = "Whether to create a new Autonomous AI Database or connect to an existing one."
  type        = string
  default     = "新規 Autonomous AI Database の作成"

  validation {
    condition = contains([
      "CREATE_NEW",
      "USE_EXISTING",
      "新規 Autonomous Database の作成",
      "既存の Autonomous Database を選択",
      "新規 Autonomous AI Database の作成",
      "既存の Autonomous AI Database を選択"
    ], var.adb_deployment_mode)
    error_message = "adb_deployment_mode must be 新規 Autonomous AI Database の作成, 既存の Autonomous AI Database を選択, CREATE_NEW, or USE_EXISTING."
  }
}

# アプリ用の DB ユーザー（#1668・#1675）。新規 / 既存の ADB のどちらでも使える。既定は有効（推奨。名前の既定は ADB_APP_USER。ADMIN を常用しない。
# アプリの不具合・管理 SQL の誤りを DB 全体の管理者の権限で実行させず、ADMIN のパスワードを各製品の .env に置かない）。
# Compute の platform/deploy/suite-init.sh が、製品の配備の前に ADMIN で接続してこのユーザーを作り、権限を付ける。
variable "adb_app_user_enabled" {
  description = "Create an application database user (not ADMIN) on the Compute instance and connect every product with it. The Compute instance connects as ADMIN once per deployment to create the user and grant PDB_DBA plus the Deep Data Security privileges."
  type        = bool
  default     = true
}

variable "existing_adb_ocid" {
  description = "Existing Autonomous AI Database OCID used when adb_deployment_mode selects an existing database."
  type        = string
  default     = ""

  validation {
    condition = (
      var.existing_adb_ocid == ""
      || can(regex("^ocid1\\.autonomousdatabase\\.", var.existing_adb_ocid))
    )
    error_message = "existing_adb_ocid must be an Autonomous AI Database OCID."
  }
}

variable "existing_adb_admin_password" {
  description = "ADMIN password of the existing Autonomous AI Database. Required when adb_app_user_enabled is true; the Compute instance uses it to create the application database user. It is not written to platform/.env or the product .env files."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !can(regex("[\r\n']", var.existing_adb_admin_password))
    error_message = "existing_adb_admin_password must not contain single quotes or line breaks."
  }
}

variable "existing_oracle_user" {
  description = "Oracle database username for an existing Autonomous AI Database."
  type        = string
  default     = "ADMIN"

  validation {
    condition     = !can(regex("[\r\n']", var.existing_oracle_user))
    error_message = "existing_oracle_user must not contain single quotes or line breaks."
  }
}

variable "existing_oracle_password" {
  description = "Oracle database password for an existing Autonomous AI Database."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !can(regex("[\r\n']", var.existing_oracle_password))
    error_message = "existing_oracle_password must not contain single quotes or line breaks."
  }
}

variable "existing_oracle_dsn" {
  description = "Optional Oracle DSN for an existing Autonomous AI Database. Leave blank to use lower(db_name)_high from the selected ADB."
  type        = string
  default     = ""

  validation {
    condition     = !can(regex("[\r\n']", var.existing_oracle_dsn))
    error_message = "existing_oracle_dsn must not contain single quotes or line breaks."
  }
}

variable "existing_oracle_wallet_password" {
  description = "Deprecated compatibility input. Existing ADB wallet generation reuses existing_oracle_password."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !can(regex("[\r\n]", var.existing_oracle_wallet_password))
    error_message = "existing_oracle_wallet_password must not contain line breaks."
  }
}

variable "adb_display_name" {
  description = "Autonomous AI Database display name. Leave blank to use adb_name."
  type        = string
  default     = ""
}

variable "adb_name" {
  description = "Autonomous AI Database database name."
  type        = string
  default     = "SUITEADB"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9]{0,13}$", var.adb_name))
    error_message = "adb_name must start with a letter and contain only letters and digits, up to 14 characters."
  }
}

variable "adb_password" {
  description = "Autonomous AI Database ADMIN password."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = !can(regex("[\r\n']", var.adb_password))
    error_message = "adb_password must not contain single quotes or line breaks."
  }
}

variable "adb_workload" {
  description = "Autonomous AI Database workload type. LH (Lakehouse) is the default."
  type        = string
  default     = "LH"

  validation {
    condition     = contains(["OLTP", "AJD", "LH"], var.adb_workload)
    error_message = "adb_workload は OLTP、AJD、LH のいずれかにしてください。APEX Service は Suite が必要とする SQL*Net 接続を提供しません。"
  }
}

variable "adb_db_version" {
  description = "Autonomous AI Database version."
  type        = string
  default     = "26ai"

  validation {
    condition     = contains(["19c", "23ai", "26ai"], var.adb_db_version)
    error_message = "adb_db_version must be one of 19c, 23ai, or 26ai."
  }
}

variable "adb_compute_model" {
  description = "Autonomous AI Database compute model."
  type        = string
  default     = "ECPU"

  validation {
    condition     = contains(["ECPU", "OCPU"], var.adb_compute_model)
    error_message = "adb_compute_model must be ECPU or OCPU."
  }
}

variable "adb_compute_count" {
  description = "Autonomous AI Database compute count."
  type        = number
  default     = 2

  validation {
    condition     = var.adb_compute_count > 0
    error_message = "adb_compute_count must be greater than 0."
  }
}

variable "adb_is_auto_scaling_enabled" {
  description = "Enable Autonomous AI Database compute auto scaling."
  type        = bool
  default     = false
}

variable "adb_data_storage_size_in_tbs" {
  description = "Autonomous AI Database storage size in TB."
  type        = number
  default     = 1

  validation {
    condition     = var.adb_data_storage_size_in_tbs > 0
    error_message = "adb_data_storage_size_in_tbs must be greater than 0."
  }
}

variable "adb_is_auto_scaling_for_storage_enabled" {
  description = "Enable Autonomous AI Database storage auto scaling."
  type        = bool
  default     = false
}

variable "adb_is_elastic_pool_enabled" {
  description = "Enable Autonomous AI Database elastic pool configuration."
  type        = bool
  default     = false
}

variable "adb_resource_pool_size" {
  description = "Autonomous AI Database elastic pool size. Used when adb_is_elastic_pool_enabled is true."
  type        = number
  default     = 0

  validation {
    condition     = var.adb_resource_pool_size >= 0
    error_message = "adb_resource_pool_size must be 0 or greater."
  }
}

variable "adb_resource_pool_storage_size_in_tbs" {
  description = "Autonomous AI Database elastic pool storage size in TB. Used when adb_is_elastic_pool_enabled is true."
  type        = number
  default     = 0

  validation {
    condition     = var.adb_resource_pool_storage_size_in_tbs >= 0
    error_message = "adb_resource_pool_storage_size_in_tbs must be 0 or greater."
  }
}

variable "license_model" {
  description = "Autonomous AI Database license model."
  type        = string
  default     = "LICENSE_INCLUDED"

  validation {
    condition     = contains(["BRING_YOUR_OWN_LICENSE", "LICENSE_INCLUDED"], var.license_model)
    error_message = "license_model must be BRING_YOUR_OWN_LICENSE or LICENSE_INCLUDED."
  }
}

variable "adb_backup_retention_period_in_days" {
  description = "Autonomous AI Database automatic backup retention period in days."
  type        = number
  default     = 1

  validation {
    condition     = var.adb_backup_retention_period_in_days >= 1
    error_message = "adb_backup_retention_period_in_days must be greater than or equal to 1."
  }
}

variable "adb_network_access_type" {
  description = "Autonomous AI Database network access mode."
  type        = string
  default     = "プライベート・エンドポイント・アクセスのみ"

  validation {
    condition = contains([
      "すべての場所からのセキュア・アクセス",
      "許可されたIPおよびVCN限定のセキュア・アクセス",
      "プライベート・エンドポイント・アクセスのみ"
    ], var.adb_network_access_type)
    error_message = "adb_network_access_typeにはAutonomous AI Database画面に準拠した日本語のアクセス・タイプを指定してください。"
  }
}

variable "adb_private_endpoint_vcn_compartment_id" {
  description = "Compartment OCID containing the VCN used by the Autonomous AI Database private endpoint."
  type        = string
  default     = ""
}

variable "adb_private_endpoint_vcn_id" {
  description = "VCN OCID used by the Autonomous AI Database private endpoint."
  type        = string
  default     = ""
}

variable "adb_private_endpoint_subnet_compartment_id" {
  description = "Compartment OCID containing the subnet used by the Autonomous AI Database private endpoint."
  type        = string
  default     = ""
}

variable "adb_subnet_id" {
  description = "Private subnet OCID for Autonomous AI Database private endpoint access."
  type        = string
  default     = ""
}

variable "adb_acl_notation_type" {
  description = "Access-control notation for secure access from allowed IPs and VCNs."
  type        = string
  default     = "VCN"

  validation {
    condition     = contains(["VCN", "IPアドレスまたはCIDRブロック"], var.adb_acl_notation_type)
    error_message = "adb_acl_notation_typeにはVCNまたはIPアドレスまたはCIDRブロックを指定してください。"
  }
}

variable "adb_acl_vcn_compartment_id" {
  description = "Compartment OCID containing the VCN allowed by the Autonomous AI Database access-control list."
  type        = string
  default     = ""
}

variable "adb_acl_vcn_id" {
  description = "VCN OCID allowed to access Autonomous AI Database when secure ACL mode is selected."
  type        = string
  default     = ""
}

variable "adb_acl_subnet_compartment_id" {
  description = "Compartment OCID containing the optional subnet allowed by the Autonomous AI Database access-control list."
  type        = string
  default     = ""
}

variable "adb_acl_subnet_id" {
  description = "Optional subnet OCID used to derive the CIDR entry for ADB VCN ACL mode."
  type        = string
  default     = ""
}

variable "adb_acl_cidr_blocks" {
  description = "Comma-separated CIDR blocks allowed to access Autonomous AI Database when CIDR ACL mode is selected."
  type        = string
  default     = ""
}

variable "adb_is_mtls_connection_required" {
  description = "Require mutual TLS (mTLS) connections for Autonomous AI Database."
  type        = bool
  default     = true
}

# アプリ用の DB ユーザーの名前とパスワード（adb_app_user_enabled のとき。フォームでは ADB の新規 / 既存の入力の後の
# 「アプリ用の DB ユーザー」の group）。ADMIN のパスワードは新規なら adb_password、既存なら existing_adb_admin_password。
variable "adb_app_user_name" {
  description = "Application database user (schema) name. Every product connects with it and creates its tables in this schema."
  type        = string
  default     = "ADB_APP_USER"

  validation {
    condition = (
      can(regex("^[A-Za-z][A-Za-z0-9_]{0,29}$", var.adb_app_user_name))
      && !contains(["ADMIN", "SYS", "SYSTEM", "PUBLIC", "DEEPSEC_DATA_USER", "GGADMIN", "ADBSNMP"], upper(var.adb_app_user_name))
      && !can(regex("^(?i)(ORA_|SYS)", var.adb_app_user_name))
    )
    error_message = "adb_app_user_name must start with a letter, contain only letters, digits and underscores (up to 30 characters), and must not be an Oracle-maintained name such as ADMIN, SYS, SYSTEM or DEEPSEC_DATA_USER."
  }
}

variable "adb_app_user_password" {
  description = "Password of the application database user (also used as the wallet password). 12 to 30 characters with at least one uppercase letter, one lowercase letter and one digit; must not contain double quotes, single quotes or the user name."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition = var.adb_app_user_password == "" || (
      length(var.adb_app_user_password) >= 12
      && length(var.adb_app_user_password) <= 30
      && can(regex("[A-Z]", var.adb_app_user_password))
      && can(regex("[a-z]", var.adb_app_user_password))
      && can(regex("[0-9]", var.adb_app_user_password))
      && !can(regex("[\"'\r\n]", var.adb_app_user_password))
    )
    error_message = "adb_app_user_password must be 12 to 30 characters, include an uppercase letter, a lowercase letter and a digit, and must not contain double quotes, single quotes or line breaks."
  }
}

variable "instance_display_name" {
  description = "Display name of the Compute instance that runs every selected product."
  type        = string
  default     = "PRODUCTION_READY_SUITE"
}

variable "instance_shape" {
  description = "Shape of the Compute instance that runs every selected product."
  type        = string
  default     = "VM.Standard.E5.Flex"

  validation {
    condition     = contains(["VM.Standard.E4.Flex", "VM.Standard.E5.Flex"], var.instance_shape)
    error_message = "instance_shape must be VM.Standard.E4.Flex or VM.Standard.E5.Flex."
  }
}

# 選んだ製品は 1 台の Compute に入れる（#1316）。既定は 4 OCPU / 32 GB / boot volume 200 GB（利用者の決定。
# メモリは 2026-10-20、#2668）。RAG の CPU の parser（Docling）が最も多く使うので、負荷に合わせて上げる。
variable "instance_flex_shape_ocpus" {
  description = "OCPUs of the Compute instance that runs every selected product."
  type        = number
  default     = 4

  validation {
    condition     = var.instance_flex_shape_ocpus > 0
    error_message = "instance_flex_shape_ocpus must be greater than 0."
  }
}

variable "instance_flex_shape_memory" {
  description = "Memory in GB of the Compute instance that runs every selected product."
  type        = number
  default     = 32

  validation {
    condition     = var.instance_flex_shape_memory > 0
    error_message = "instance_flex_shape_memory must be greater than 0."
  }
}

variable "instance_boot_volume_size" {
  description = "Boot volume size in GB of the Compute instance. The per-service Python environments and models of the RAG parsers are large."
  type        = number
  default     = 200

  validation {
    condition     = var.instance_boot_volume_size >= 50 && var.instance_boot_volume_size <= 32768
    error_message = "instance_boot_volume_size must be between 50 and 32768 GB."
  }
}

variable "instance_boot_volume_vpus" {
  description = "Boot volume VPUs/GB of the Compute instance."
  type        = number
  default     = 10

  validation {
    condition     = contains(concat([10, 20], range(30, 121)), var.instance_boot_volume_vpus)
    error_message = "instance_boot_volume_vpus must be 10, 20, or a value from 30 through 120."
  }
}

# Compute の OS は Oracle Linux 9（platform image の Oracle-Linux-9.8-2026.09.18-0）。OCID は東京・大阪・Chicago で固定する（#1611）。
# 配備のスクリプト（platform/deploy・各製品の init_script.sh）は Oracle Linux 9 の dnf・firewalld・SELinux を前提にする。
variable "instance_image_source_id" {
  description = "Oracle Linux 9 image OCID of the Compute instance."
  type        = string
  default     = "ocid1.image.oc1.ap-osaka-1.aaaaaaaabdp5adn6fy2h63s5iizgqv77wz4fqrjzosackszdo2gn5rn436ha"
}

variable "subnet_ai_subnet_id" {
  description = "Subnet OCID of the Compute instance."
  type        = string
  default     = ""
}

variable "ssh_authorized_keys" {
  description = "SSH public keys authorized for the opc user."
  type        = string
  default     = ""
}

# 時間帯（#3255・#3274）。3 製品の共通の設定（platform/.env の PLATFORM_TIMEZONE）と Compute の OS の時間帯（cloud-init の
# timezone と platform/deploy/suite-init.sh の timedatectl）に使う。選べるのは UTC（既定。OCI の Database の Time Zone の既定）と
# Asia/Tokyo（日本時間: 東京・大阪など）の 2 つだけ（利用者の決定。値は OCI と同じ IANA の地域名）。
# 配備した後は platform/.env の PLATFORM_TIMEZONE に任意の IANA の地域名を書ける（suite-init.sh と backend が確かめる）。
# 時刻の保存は UTC のまま（DB の session の時間帯は '+00:00'）。ADB の時間帯（DBTIMEZONE）は stack では変えない（README）。
variable "timezone" {
  description = "Time zone of the Compute OS clock and the products' common setting PLATFORM_TIMEZONE: UTC (default) or Asia/Tokyo (Japan time: Tokyo, Osaka and so on). Timestamps are still stored in UTC."
  type        = string
  default     = "UTC"

  validation {
    condition     = contains(["UTC", "Asia/Tokyo"], var.timezone)
    error_message = "timezone must be UTC or Asia/Tokyo."
  }
}

# 任意の bastion の Compute（bastion.tf。#2670）。既定は作らない。作るときの既定は 2 OCPU / 16 GB /
# boot volume 100 GB / 10 VPUs/GB（利用者の決定。2026-10-20）。shape・image・SSH キーは Suite の Compute と同じ。
variable "bastion_enabled" {
  description = "Create a bastion Compute instance with a VNC desktop (GNOME, TigerVNC on TCP 5901) and Google Chrome."
  type        = bool
  default     = false
}

variable "bastion_display_name" {
  description = "Display name of the bastion Compute instance."
  type        = string
  default     = "PRODUCTION_READY_BASTION"
}

variable "bastion_compartment_ocid" {
  description = "Compartment OCID of the independent bastion Compute instance."
  type        = string
  default     = ""
}

variable "bastion_availability_domain" {
  description = "Availability domain of the independent bastion Compute instance."
  type        = string
  default     = ""
}

variable "bastion_vcn_id" {
  description = "VCN OCID used to filter the bastion subnet in Resource Manager."
  type        = string
  default     = ""
}

variable "bastion_shape" {
  description = "Shape of the independent bastion Compute instance."
  type        = string
  default     = "VM.Standard.E5.Flex"

  validation {
    condition     = contains(["VM.Standard.E4.Flex", "VM.Standard.E5.Flex"], var.bastion_shape)
    error_message = "bastion_shape はVM.Standard.E4.Flex、VM.Standard.E5.Flex のいずれかにしてください。"
  }
}

variable "bastion_image_source_id" {
  description = "Oracle Linux 9 x86 image OCID of the independent bastion Compute instance."
  type        = string
  default     = "ocid1.image.oc1.ap-osaka-1.aaaaaaaabdp5adn6fy2h63s5iizgqv77wz4fqrjzosackszdo2gn5rn436ha"
}

variable "bastion_ssh_authorized_keys" {
  description = "SSH public keys authorized only for the bastion opc user. Required when bastion_enabled is true."
  type        = string
  default     = ""
}

variable "bastion_flex_shape_ocpus" {
  description = "OCPUs of the bastion Compute instance."
  type        = number
  default     = 2

  validation {
    condition     = var.bastion_flex_shape_ocpus > 0
    error_message = "bastion_flex_shape_ocpus must be greater than 0."
  }
}

variable "bastion_flex_shape_memory" {
  description = "Memory in GB of the bastion Compute instance."
  type        = number
  default     = 16

  validation {
    condition     = var.bastion_flex_shape_memory > 0
    error_message = "bastion_flex_shape_memory must be greater than 0."
  }
}

variable "bastion_boot_volume_size" {
  description = "Boot volume size in GB of the bastion Compute instance."
  type        = number
  default     = 100

  validation {
    condition     = var.bastion_boot_volume_size >= 50 && var.bastion_boot_volume_size <= 32768
    error_message = "bastion_boot_volume_size must be between 50 and 32768 GB."
  }
}

variable "bastion_boot_volume_vpus" {
  description = "Boot volume VPUs/GB of the bastion Compute instance."
  type        = number
  default     = 10

  validation {
    condition     = contains(concat([10, 20], range(30, 121)), var.bastion_boot_volume_vpus)
    error_message = "bastion_boot_volume_vpus must be 10, 20, or a value from 30 through 120."
  }
}

variable "bastion_subnet_id" {
  description = "Subnet OCID used only by the bastion Compute instance. Required when bastion_enabled is true."
  type        = string
  default     = ""
}

# TigerVNC は先頭の 8 文字だけを使うため、ちょうど 8 文字に限る（#2673）。metadata で渡すため、値は state と instance metadata に残る。
variable "bastion_vnc_password" {
  description = "VNC password of the opc user on the bastion Compute instance (exactly 8 characters; TigerVNC uses only the first 8)."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.bastion_vnc_password == "" || (length(var.bastion_vnc_password) == 8 && !can(regex("[\r\n]", var.bastion_vnc_password)))
    error_message = "bastion_vnc_password must be exactly 8 characters and must not contain line breaks."
  }
}

# HTTPS（#1316）。on なら Nginx が 443 で TLS 1.2 / 1.3 を受け、80 は開けない・listen しない（#1638）。
# 証明書は Compute の中で作る自作の Root CA と、公開 IP・private IP の証明書。port と証明書の発行者は変数にしない
# （platform/deploy/suite-tls.sh の固定値）。on のとき共通 .env の PLATFORM_AUTH_COOKIE_SECURE を true にする。
variable "https_enabled" {
  description = "Serve the products over HTTPS on port 443 with a private root CA and an IP address certificate generated on the Compute instance (port 80 is neither opened nor listened on). When false, the products are served over HTTP on port 80."
  type        = bool
  default     = true
}

# 公開の port（#1316）。HTTPS が on なら https_port だけで HTTPS を受け、http_port は開けない・listen しない（#1638）。
# HTTPS が off なら http_port で HTTP をそのまま配信する。SSH・よく使われる service・Compute の中で使う port は
# locals.tf の reserved_ports / reserved_port_ranges で拒む（compute.tf の precondition。配備のスクリプトも同じ一覧で確かめる）。
variable "http_port" {
  description = "Public HTTP port of Nginx, used only when https_enabled is false. When https_enabled is true, this port is neither opened in the firewall nor listened on."
  type        = number
  default     = 80

  validation {
    condition     = floor(var.http_port) == var.http_port && var.http_port >= 1 && var.http_port <= 65535
    error_message = "http_port must be an integer between 1 and 65535."
  }
}

variable "https_port" {
  description = "Public HTTPS port of Nginx when https_enabled is true."
  type        = number
  default     = 443

  validation {
    condition     = floor(var.https_port) == var.https_port && var.https_port >= 1 && var.https_port <= 65535
    error_message = "https_port must be an integer between 1 and 65535."
  }
}

variable "application_git_url" {
  description = "Git repository URL for the Production Ready suite monorepo (rag/, nl2sql/, agent/, and platform/)."
  type        = string
  default     = "https://github.com/engchina/no.1-production-ready-suite.git"

  validation {
    condition     = trimspace(var.application_git_url) != ""
    error_message = "application_git_url must be a non-empty Git URL."
  }
}

variable "application_git_ref" {
  description = "Git branch or tag of the Production Ready suite deployed to the Compute instance."
  type        = string
  default     = "main"

  validation {
    condition     = trimspace(var.application_git_ref) != ""
    error_message = "application_git_ref must be a non-empty Git ref."
  }
}

# ---------------------------------------------------------------- 配備する製品
# 選んだ製品は 1 台の Compute に入れる（#1316）。最低1つは選ぶ（adb.tf の precondition で検証する）。
# ADB と Wallet は1つだけ作り、選んだ製品すべてで共有する。

variable "deploy_rag" {
  description = "Deploy Production Ready RAG (served under /rag/) on the shared Compute instance."
  type        = bool
  default     = true
}

variable "deploy_nl2sql" {
  description = "Deploy Production Ready NL2SQL (served under /nl2sql/) on the shared Compute instance."
  type        = bool
  default     = true
}

variable "deploy_agent" {
  description = "Deploy the Production Ready Agent Control Plane (served under /agent/; / redirects to it) on the shared Compute instance."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- RAG

# 文書解析（parser）は CPU のマイクロサービスだけを配備する。parser-docling（既定の解析エンジン。
# RAG_PARSER_ADAPTER_BACKEND=docling。PDF と画像）は常に配備し、ここでは任意の parser だけを選ぶ。
# 以前の Docling を配備するかを選ぶ入力は廃止した（Docling は常に配備する。#286）。
variable "rag_enable_parser_unstructured" {
  description = "Install the Unstructured parser (parser-unstructured) as a systemd service (default on). The Docling parser (CPU, PDF and images) is always installed as the default document parser. Use Unstructured to ingest formats that Docling cannot parse, such as text, HTML, Office, and e-mail; select Unstructured in the document's processing recipe."
  type        = bool
  default     = true
}


# ---------------------------------------------------------------- NL2SQL

variable "platform_environment" {
  description = "3製品共通の実行環境（PLATFORM_ENVIRONMENT）。staging / production は HTTPS とログイン認証を必須にする。"
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["local", "staging", "production"], var.platform_environment)
    error_message = "platform_environment must be local, staging, or production."
  }
}

variable "app_admin_login_user_id" {
  description = "Fixed login user ID of the configuration administrator (SYSTEM_ADMIN) shared by RAG and NL2SQL (PLATFORM_ADMIN_LOGIN_USER_ID)."
  type        = string
  default     = "system_admin"

  validation {
    condition     = var.app_admin_login_user_id == "system_admin"
    error_message = "app_admin_login_user_id is fixed and must be exactly system_admin."
  }
}

variable "app_admin_login_user_password" {
  description = "Login password of the configuration administrator (system_admin) shared by RAG, NL2SQL, and the Agent Control Plane (PLATFORM_ADMIN_LOGIN_USER_PASSWORD). Required when any product is deployed."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition = (
      var.app_admin_login_user_password == ""
      || (
        length(var.app_admin_login_user_password) >= 12
        && length(var.app_admin_login_user_password) <= 30
        && !can(regex("[\r\n]", var.app_admin_login_user_password))
        && !can(regex("\"", var.app_admin_login_user_password))
        && !can(regex("admin", var.app_admin_login_user_password))
        && can(regex("[0-9]", var.app_admin_login_user_password))
        && can(regex("[a-z]", var.app_admin_login_user_password))
        && can(regex("[A-Z]", var.app_admin_login_user_password))
      )
    )
    error_message = "app_admin_login_user_password must be 12-30 characters, include uppercase, lowercase, and digits, not include admin or double quotes, and not contain line breaks."
  }
}

variable "nl2sql_oracle_deepsec_enabled" {
  description = "Enable Deep Data Security in the NL2SQL backend/.env. Disabled by default; enable it only where it is used."
  type        = bool
  default     = false
}

variable "nl2sql_oracle_deepsec_data_user_password" {
  description = "Password for the shared DEEPSEC_DATA_USER Deep Data Security DATA USER."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition = (
      trimspace(var.nl2sql_oracle_deepsec_data_user_password) == ""
      || (
        length(var.nl2sql_oracle_deepsec_data_user_password) >= 12
        && length(var.nl2sql_oracle_deepsec_data_user_password) <= 256
        && !can(regex("[\r\n]", var.nl2sql_oracle_deepsec_data_user_password))
        && !can(regex("\"", var.nl2sql_oracle_deepsec_data_user_password))
      )
    )
    error_message = "nl2sql_oracle_deepsec_data_user_password must be empty or 12-256 characters without double quotes or line breaks."
  }
}

# ---------------------------------------------------------------- Agent

variable "agent_runtime_repository_backend" {
  description = "Oracle repository for Agent runs, business agents and screen-edited definitions (AGENT_RUNTIME_REPOSITORY_BACKEND). The tables are created by the Agent system tables (init_script.sh runs agent_system_schema --initialize); the application does not run DDL."
  type        = string
  default     = "oracle_checkpoint"

  validation {
    condition     = contains(["oracle_checkpoint", "oracle_normalized"], var.agent_runtime_repository_backend)
    error_message = "agent_runtime_repository_backend must be oracle_checkpoint or oracle_normalized."
  }
}
