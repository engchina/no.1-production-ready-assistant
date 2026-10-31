data "oci_core_subnet" "adb_acl_subnet" {
  count = (
    local.adb_secure_acl_enabled
    && var.adb_acl_notation_type == "VCN"
    && trimspace(local.effective_adb_acl_subnet_id) != ""
  ) ? 1 : 0
  subnet_id = local.effective_adb_acl_subnet_id
}

data "oci_core_subnet" "adb_private_endpoint_subnet" {
  count = (
    local.adb_private_endpoint_enabled
    && trimspace(var.adb_subnet_id) != ""
  ) ? 1 : 0
  subnet_id = var.adb_subnet_id
}

data "oci_database_autonomous_database" "selected_existing_adb" {
  count = local.use_existing_adb && trimspace(var.existing_adb_ocid) != "" ? 1 : 0

  autonomous_database_id = var.existing_adb_ocid

  lifecycle {
    postcondition {
      condition     = self.compartment_id == trimspace(var.adb_compartment_ocid)
      error_message = "選択した既存のAutonomous AI Databaseは、ADBのコンパートメントに属している必要があります。"
    }
  }
}

locals {
  adb_deployment_mode_normalized = trimspace(var.adb_deployment_mode)
  adb_deployment_mode_create_values = [
    "CREATE_NEW",
    "新規 Autonomous Database の作成",
    "新規 Autonomous AI Database の作成"
  ]
  adb_deployment_mode_existing_values = [
    "USE_EXISTING",
    "既存の Autonomous Database を選択",
    "既存の Autonomous AI Database を選択"
  ]
  create_new_adb   = contains(local.adb_deployment_mode_create_values, local.adb_deployment_mode_normalized)
  use_existing_adb = contains(local.adb_deployment_mode_existing_values, local.adb_deployment_mode_normalized)
  adb_display_name = trimspace(var.adb_display_name) != "" ? var.adb_display_name : var.adb_name
  adb_private_endpoint_enabled = (
    local.create_new_adb
    && var.adb_network_access_type == "プライベート・エンドポイント・アクセスのみ"
  )
  adb_secure_acl_enabled = (
    local.create_new_adb
    && var.adb_network_access_type == "許可されたIPおよびVCN限定のセキュア・アクセス"
  )
  adb_public_access_enabled = (
    local.create_new_adb
    && var.adb_network_access_type == "すべての場所からのセキュア・アクセス"
  )
  effective_adb_acl_vcn_id    = trimspace(var.adb_acl_vcn_id)
  effective_adb_acl_subnet_id = trimspace(var.adb_acl_subnet_id)

  adb_acl_cidr_entries = local.adb_secure_acl_enabled && var.adb_acl_notation_type == "IPアドレスまたはCIDRブロック" && trimspace(var.adb_acl_cidr_blocks) != "" ? [
    for cidr in split(",", var.adb_acl_cidr_blocks) : trimspace(cidr)
    if trimspace(cidr) != ""
  ] : []

  adb_acl_vcn_entries = local.adb_secure_acl_enabled && var.adb_acl_notation_type == "VCN" && trimspace(local.effective_adb_acl_vcn_id) != "" ? [
    trimspace(local.effective_adb_acl_subnet_id) != "" ? "${local.effective_adb_acl_vcn_id};${data.oci_core_subnet.adb_acl_subnet[0].cidr_block}" : local.effective_adb_acl_vcn_id
  ] : []

  adb_whitelisted_ips = local.adb_secure_acl_enabled ? concat(local.adb_acl_vcn_entries, local.adb_acl_cidr_entries) : null

  existing_adb_db_name = try(data.oci_database_autonomous_database.selected_existing_adb[0].db_name, "")
  effective_existing_oracle_dsn = trimspace(var.existing_oracle_dsn) != "" ? trimspace(var.existing_oracle_dsn) : (
    trimspace(local.existing_adb_db_name) != "" ? "${lower(local.existing_adb_db_name)}_high" : ""
  )

  effective_adb_ocid   = local.create_new_adb ? oci_database_autonomous_database.generated_database_autonomous_database[0].id : var.existing_adb_ocid
  effective_adb_name   = local.create_new_adb ? var.adb_name : local.existing_adb_db_name
  effective_oracle_dsn = local.create_new_adb ? "${lower(var.adb_name)}_high" : local.effective_existing_oracle_dsn

  # アプリ用の DB ユーザー（#1668）。有効なら 3 製品はこのユーザーで接続し、システムの表もこのユーザーの schema に作る。
  # ユーザーの作成と grant は Compute の上で行う（cloud-init → platform/deploy/suite-init.sh が製品の配備の前に、
  # ADMIN で接続して platform/deploy/suite-db-user.py を実行する）。ADB には Compute からしか接続できない
  # （プライベート・エンドポイント・ACL）ため、Terraform（provider・local-exec）では DB に接続しない。
  # 無効なら今までどおり（新規の ADB は ADMIN、既存の ADB は existing_oracle_user）。
  adb_app_user                 = upper(trimspace(var.adb_app_user_name))
  effective_adb_admin_password = local.create_new_adb ? var.adb_password : var.existing_adb_admin_password
  effective_oracle_user = var.adb_app_user_enabled ? local.adb_app_user : (
    local.create_new_adb ? "ADMIN" : var.existing_oracle_user
  )
  effective_oracle_password = var.adb_app_user_enabled ? var.adb_app_user_password : (
    local.create_new_adb ? var.adb_password : var.existing_oracle_password
  )
  # Wallet のパスワードは接続のユーザーのパスワード（platform/.env の PLATFORM_ORACLE_WALLET_PASSWORD）。
  # アプリ用のユーザーを使うときに ADMIN のパスワードを platform/.env に書かないため、ADMIN のパスワードにしない。
  effective_oracle_wallet_password = local.effective_oracle_password
}

resource "oci_database_autonomous_database" "generated_database_autonomous_database" {
  count                                          = local.create_new_adb ? 1 : 0
  admin_password                                 = var.adb_password
  autonomous_maintenance_schedule_type           = "REGULAR"
  backup_retention_period_in_days                = var.adb_backup_retention_period_in_days
  character_set                                  = "AL32UTF8"
  compartment_id                                 = var.adb_compartment_ocid
  compute_count                                  = var.adb_compute_count
  compute_model                                  = var.adb_compute_model
  data_storage_size_in_tbs                       = var.adb_data_storage_size_in_tbs
  db_name                                        = var.adb_name
  db_version                                     = var.adb_db_version
  db_workload                                    = var.adb_workload
  display_name                                   = local.adb_display_name
  is_auto_scaling_enabled                        = var.adb_is_auto_scaling_enabled
  is_auto_scaling_for_storage_enabled            = var.adb_is_auto_scaling_for_storage_enabled
  is_dedicated                                   = "false"
  is_mtls_connection_required                    = var.adb_is_mtls_connection_required
  is_preview_version_with_service_terms_accepted = "false"
  license_model                                  = var.license_model
  ncharacter_set                                 = "AL16UTF16"
  subnet_id                                      = local.adb_private_endpoint_enabled ? var.adb_subnet_id : null
  whitelisted_ips                                = local.adb_secure_acl_enabled ? local.adb_whitelisted_ips : null

  lifecycle {
    precondition {
      condition     = trimspace(var.adb_compartment_ocid) != ""
      error_message = "新規Autonomous AI Databaseを作成する場合は、ADBのコンパートメントを選択してください。"
    }
  }

  dynamic "resource_pool_summary" {
    for_each = var.adb_is_elastic_pool_enabled ? [1] : []

    content {
      pool_size                = var.adb_resource_pool_size
      pool_storage_size_in_tbs = var.adb_resource_pool_storage_size_in_tbs
    }
  }
}

resource "oci_database_autonomous_database_wallet" "generated_autonomous_database_wallet" {
  autonomous_database_id = local.effective_adb_ocid
  password               = local.effective_oracle_wallet_password
  base64_encode_content  = "true"
  generate_type          = "SINGLE"

  # ADB の入力と製品の選択は、Compute の台数に関係なく1回だけ検証したいので、常に1つ作る Wallet に置く。
  lifecycle {
    precondition {
      condition     = length(local.selected_products) > 0
      error_message = "配備する製品を1つ以上選択してください（deploy_rag / deploy_nl2sql / deploy_agent）。"
    }
    precondition {
      condition     = local.create_new_adb ? trimspace(var.adb_password) != "" : true
      error_message = "adb_password must be configured."
    }
    precondition {
      condition = (
        local.create_new_adb && local.adb_private_endpoint_enabled
      ) ? trimspace(var.adb_private_endpoint_vcn_compartment_id) != "" : true
      error_message = "プライベート・エンドポイントを作成する場合は、VCNのコンパートメントを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb && local.adb_private_endpoint_enabled
      ) ? trimspace(var.adb_private_endpoint_vcn_id) != "" : true
      error_message = "プライベート・エンドポイントを作成する場合は、仮想クラウド・ネットワークを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb && local.adb_private_endpoint_enabled
      ) ? trimspace(var.adb_private_endpoint_subnet_compartment_id) != "" : true
      error_message = "プライベート・エンドポイントを作成する場合は、サブネットのコンパートメントを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb && local.adb_private_endpoint_enabled
      ) ? trimspace(var.adb_subnet_id) != "" : true
      error_message = "プライベート・エンドポイントを作成する場合は、サブネットを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb
        && local.adb_private_endpoint_enabled
        && trimspace(var.adb_subnet_id) != ""
        && trimspace(var.adb_private_endpoint_vcn_id) != ""
      ) ? data.oci_core_subnet.adb_private_endpoint_subnet[0].vcn_id == trimspace(var.adb_private_endpoint_vcn_id) : true
      error_message = "選択したサブネットは、プライベート・エンドポイント用VCNに属している必要があります。"
    }
    precondition {
      condition = (
        local.create_new_adb
        && local.adb_secure_acl_enabled
        && var.adb_acl_notation_type == "VCN"
      ) ? trimspace(var.adb_acl_vcn_compartment_id) != "" : true
      error_message = "VCNアクセス制御を使用する場合は、VCNのコンパートメントを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb
        && local.adb_secure_acl_enabled
        && var.adb_acl_notation_type == "VCN"
      ) ? trimspace(local.effective_adb_acl_vcn_id) != "" : true
      error_message = "VCNアクセス制御を使用する場合は、許可する仮想クラウド・ネットワークを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb
        && local.adb_secure_acl_enabled
        && var.adb_acl_notation_type == "VCN"
        && trimspace(var.adb_acl_subnet_id) != ""
      ) ? trimspace(var.adb_acl_subnet_compartment_id) != "" : true
      error_message = "許可するサブネットを指定する場合は、サブネットのコンパートメントを選択してください。"
    }
    precondition {
      condition = (
        local.create_new_adb
        && local.adb_secure_acl_enabled
        && var.adb_acl_notation_type == "IPアドレスまたはCIDRブロック"
      ) ? length(local.adb_acl_cidr_entries) > 0 : true
      error_message = "IPアドレスまたはCIDRブロックによるアクセス制御を使用する場合は、許可する値を入力してください。"
    }
    precondition {
      condition     = local.create_new_adb || trimspace(var.existing_adb_ocid) != ""
      error_message = "existing_adb_ocid must be configured when adb_deployment_mode selects an existing Autonomous AI Database."
    }
    precondition {
      condition = local.create_new_adb || var.adb_app_user_enabled || (
        trimspace(var.existing_oracle_user) != ""
        && trimspace(var.existing_oracle_password) != ""
      )
      error_message = "existing_adb_ocid, existing_oracle_user, and existing_oracle_password must be configured when adb_deployment_mode selects an existing Autonomous AI Database and adb_app_user_enabled is false."
    }
    # アプリ用の DB ユーザー（#1668）。名前とパスワードの形は variables.tf の validation、ここでは入力どうしの関係を見る。
    precondition {
      condition     = !var.adb_app_user_enabled || trimspace(var.adb_app_user_password) != ""
      error_message = "adb_app_user_password must be configured when adb_app_user_enabled is true."
    }
    precondition {
      condition     = !var.adb_app_user_enabled || local.create_new_adb || trimspace(var.existing_adb_admin_password) != ""
      error_message = "existing_adb_admin_password (the ADMIN password of the existing Autonomous AI Database) must be configured when adb_app_user_enabled is true. The Compute instance connects as ADMIN to create the application database user."
    }
    precondition {
      condition     = !var.adb_app_user_enabled || !strcontains(lower(var.adb_app_user_password), lower(local.adb_app_user))
      error_message = "adb_app_user_password must not contain adb_app_user_name (Autonomous AI Database password rule)."
    }
    precondition {
      condition     = trimspace(local.effective_adb_ocid) != ""
      error_message = "An Autonomous AI Database OCID is required to generate the wallet."
    }
    precondition {
      condition     = trimspace(local.effective_oracle_wallet_password) != ""
      error_message = "A wallet password is required to generate the Autonomous AI Database wallet."
    }
  }
}

# Save the generated wallet ZIP as a local binary file.
resource "local_file" "wallet_zip" {
  content_base64 = oci_database_autonomous_database_wallet.generated_autonomous_database_wallet.content
  filename       = "${path.module}/wallet_full.zip"
}

# Shrink the wallet ZIP before injecting it into cloud-init.
data "external" "wallet_files" {
  depends_on = [local_file.wallet_zip]
  program    = ["bash", "${path.module}/extract_wallet.sh"]
}
