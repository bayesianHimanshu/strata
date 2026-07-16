###############################################################################
# STRATA IEG platform - Azure infrastructure
# Topology: Container Apps (API + worker) over Postgres(pgvector) + Blob + Queue,
# model backend = Azure OpenAI (GPT-5.x), secrets in Key Vault, auth via Entra,
# all data-plane access via a user-assigned managed identity (no secrets in app).
###############################################################################

locals {
  prefix = var.name_prefix
  tags   = var.tags
}

resource "random_password" "pg" {
  length  = 24
  special = true
}

# Azure OpenAI custom subdomains are GLOBALLY unique - a plain "strata-openai" can
# collide. Suffix the account/subdomain to keep applies idempotent across tenants.
resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_resource_group" "rg" {
  name     = "${local.prefix}-rg"
  location = var.location
  tags     = local.tags
}

# --- identity used by both container apps for all data-plane access ---
resource "azurerm_user_assigned_identity" "app" {
  name                = "${local.prefix}-id"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  tags                = local.tags
}

# --- observability ---
resource "azurerm_log_analytics_workspace" "law" {
  name                = "${local.prefix}-law"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  retention_in_days   = 30
  tags                = local.tags
}

# --- container registry ---
resource "azurerm_container_registry" "acr" {
  name                = "${local.prefix}acr"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = var.demo_profile ? "Basic" : "Standard"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- postgres flexible server + pgvector ---
resource "azurerm_postgresql_flexible_server" "pg" {
  name                   = "${local.prefix}-pg"
  resource_group_name    = azurerm_resource_group.rg.name
  location               = azurerm_resource_group.rg.location
  version                = "16"
  administrator_login    = var.postgres_admin
  administrator_password = random_password.pg.result
  storage_mb             = 32768
  sku_name               = "B_Standard_B1ms"
  zone                   = "1"
  tags                   = local.tags
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = "strata"
  server_id = azurerm_postgresql_flexible_server.pg.id
  collation = "en_US.utf8"
  charset   = "utf8"
}

# allow pgvector
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "VECTOR"
}

# allow Azure services (Container Apps) to reach the DB
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# --- storage: snapshots (blob) + jobs (queue) ---
resource "azurerm_storage_account" "sa" {
  name                     = "${local.prefix}stg"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = local.tags
}

resource "azurerm_storage_container" "snapshots" {
  name                  = "snapshots"
  storage_account_name  = azurerm_storage_account.sa.name
  container_access_type = "private"
}

resource "azurerm_storage_queue" "jobs" {
  name                 = "strata-jobs"
  storage_account_name = azurerm_storage_account.sa.name
}

resource "azurerm_role_assignment" "blob" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "queue" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- key vault (db password + any secrets) ---
data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "kv" {
  name                      = "${local.prefix}-kv"
  resource_group_name       = azurerm_resource_group.rg.name
  location                  = azurerm_resource_group.rg.location
  tenant_id                 = data.azurerm_client_config.current.tenant_id
  sku_name                  = "standard"
  enable_rbac_authorization = true
  tags                      = local.tags
}

resource "azurerm_role_assignment" "kv_reader" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_key_vault_secret" "pg_password" {
  name         = "pg-password"
  value        = random_password.pg.result
  key_vault_id = azurerm_key_vault.kv.id
}

# Hardened secrets: the app reads these via Container Apps Key Vault references
# (secretref:), never as inline env values (§0.7). The deployer's own identity needs
# Key Vault Secrets Officer to write them - granted below.
resource "azurerm_role_assignment" "kv_deployer" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "database-url"
  value        = "postgresql+asyncpg://${var.postgres_admin}:${random_password.pg.result}@${azurerm_postgresql_flexible_server.pg.fqdn}:5432/strata"
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "queue_conn" {
  name         = "queue-conn"
  value        = azurerm_storage_account.sa.primary_connection_string
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "blob_conn" {
  name         = "blob-conn"
  value        = azurerm_storage_account.sa.primary_blob_connection_string
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

# API keys: written only when provided (keyless ingestion still works with backoff).
resource "azurerm_key_vault_secret" "ncbi_key" {
  count        = var.ncbi_api_key == "" ? 0 : 1
  name         = "ncbi-api-key"
  value        = var.ncbi_api_key
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "openfda_key" {
  count        = var.openfda_api_key == "" ? 0 : 1
  name         = "openfda-api-key"
  value        = var.openfda_api_key
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

# Role assignments are eventually consistent - give them time to propagate before the
# container apps (which pull from ACR + read Key Vault) start.
resource "time_sleep" "role_propagation" {
  create_duration = "60s"
  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_role_assignment.kv_reader,
    azurerm_role_assignment.blob,
    azurerm_role_assignment.queue,
    azurerm_role_assignment.openai_user,
  ]
}

# --- azure openai + GPT-5.x deployment ---
resource "azurerm_cognitive_account" "openai" {
  name                  = "${local.prefix}-openai-${random_string.suffix.result}"
  resource_group_name   = azurerm_resource_group.rg.name
  location              = azurerm_resource_group.rg.location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "${local.prefix}-openai-${random_string.suffix.result}"
  tags                  = local.tags
}

resource "azurerm_cognitive_deployment" "gpt" {
  name                 = var.openai_model
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }
  scale {
    type     = "GlobalStandard"
    capacity = var.demo_profile ? 40 : 80  # thousands of TPM (subject to account quota)
  }
}

# Embeddings deployment for pgvector retrieval (1536-dim; matches ChunkRow.dim).
# Serialized after the chat deployment - concurrent deployments on one account can race.
resource "azurerm_cognitive_deployment" "embeddings" {
  name                 = var.embeddings_model
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.embeddings_model
    version = var.embeddings_model_version
  }
  scale {
    type     = "GlobalStandard"
    capacity = var.demo_profile ? 50 : 120
  }
  depends_on = [azurerm_cognitive_deployment.gpt]
}

resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- container apps environment ---
resource "azurerm_container_app_environment" "env" {
  name                       = "${local.prefix}-cae"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = azurerm_resource_group.rg.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.law.id
  tags                       = local.tags
}

locals {
  # Non-secret config (plain env values).
  plain_env = [
    { name = "ENVIRONMENT", value = "prod" },
    { name = "QUEUE_NAME", value = azurerm_storage_queue.jobs.name },
    { name = "AZURE_OPENAI_ENDPOINT", value = azurerm_cognitive_account.openai.endpoint },
    { name = "AZURE_OPENAI_DEPLOYMENT", value = azurerm_cognitive_deployment.gpt.name },
    { name = "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", value = azurerm_cognitive_deployment.embeddings.name },
    { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.app.client_id },
    { name = "ENTRA_TENANT_ID", value = data.azurerm_client_config.current.tenant_id },
    { name = "MODEL_CUTOFF", value = var.model_cutoff },
    { name = "JOBS_BACKEND", value = "db" },
    { name = "RETRIEVAL_BACKEND", value = "pgvector" },
    { name = "CONTEXT_NICE_XLSX_URL", value = var.nice_xlsx_url },
    # App-level auth is OFF when Easy Auth fronts the apps (platform validates the token);
    # ON only if you wire the API to validate Entra JWTs itself.
    { name = "AUTH_ENABLED", value = "false" },
  ]

  # Secrets sourced from Key Vault (Container Apps secretref). Conditionally include the
  # API-key secrets only when provided.
  kv_secrets = concat([
    { name = "database-url", id = azurerm_key_vault_secret.database_url.versionless_id },
    { name = "queue-conn", id = azurerm_key_vault_secret.queue_conn.versionless_id },
    { name = "blob-conn", id = azurerm_key_vault_secret.blob_conn.versionless_id },
    ],
    var.ncbi_api_key == "" ? [] : [{ name = "ncbi-api-key", id = azurerm_key_vault_secret.ncbi_key[0].versionless_id }],
    var.openfda_api_key == "" ? [] : [{ name = "openfda-api-key", id = azurerm_key_vault_secret.openfda_key[0].versionless_id }],
    var.enable_easy_auth ? [{ name = "easyauth-client-secret", id = azurerm_key_vault_secret.easyauth_client_secret[0].versionless_id }] : [],
  )

  # Env vars that pull from those secrets.
  secret_env = concat([
    { name = "DATABASE_URL", secret_name = "database-url" },
    { name = "QUEUE_CONNECTION_STRING", secret_name = "queue-conn" },
    { name = "BLOB_CONNECTION_STRING", secret_name = "blob-conn" },
    ],
    var.ncbi_api_key == "" ? [] : [{ name = "NCBI_API_KEY", secret_name = "ncbi-api-key" }],
    var.openfda_api_key == "" ? [] : [{ name = "OPENFDA_API_KEY", secret_name = "openfda-api-key" }],
  )
}

# --- API container app (HTTP ingress) ---
resource "azurerm_container_app" "api" {
  name                         = "${local.prefix}-api"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags
  depends_on                   = [time_sleep.role_propagation]

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }
  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }
  dynamic "secret" {
    for_each = local.kv_secrets
    content {
      name                = secret.value.name
      key_vault_secret_id = secret.value.id
      identity            = azurerm_user_assigned_identity.app.id
    }
  }
  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 3
    container {
      name   = "api"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"
      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name        = env.value.name
          secret_name = env.value.secret_name
        }
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
    }
  }
}

# --- worker container app (queue-scaled, scale-to-zero in the demo profile) ---
resource "azurerm_container_app" "worker" {
  name                         = "${local.prefix}-worker"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags
  depends_on                   = [time_sleep.role_propagation]

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }
  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }
  dynamic "secret" {
    for_each = local.kv_secrets
    content {
      name                = secret.value.name
      key_vault_secret_id = secret.value.id
      identity            = azurerm_user_assigned_identity.app.id
    }
  }
  template {
    min_replicas = var.demo_profile ? 0 : 1
    max_replicas = 5
    container {
      name    = "worker"
      image   = var.container_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "strata_platform.jobs.worker"]
      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name        = env.value.name
          secret_name = env.value.secret_name
        }
      }
    }
    azure_queue_scale_rule {
      name         = "queue-depth"
      queue_name   = azurerm_storage_queue.jobs.name
      queue_length = 5
      authentication {
        secret_name       = "queue-conn"
        trigger_parameter = "connection"
      }
    }
  }
}
