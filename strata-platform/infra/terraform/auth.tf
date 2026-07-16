###############################################################################
# Easy Auth (Container Apps built-in Entra) - platform-level token validation in
# front of the API and UI. Gated by var.enable_easy_auth (default false) so the
# demo apply never blocks on Graph app-registration rights. Flip the variable on
# once the deployer can create an Entra app registration in the tenant:
#
#   terraform apply -var enable_easy_auth=true
#
# When on: an Entra app registration is created with the API/UI redirect URIs, its
# client secret is stored as a Container Apps secret, and an authConfig requires a
# valid tenant token before any request reaches the container.
###############################################################################

data "azuread_client_config" "current" {
  count = var.enable_easy_auth ? 1 : 0
}

resource "azuread_application" "easyauth" {
  count            = var.enable_easy_auth ? 1 : 0
  display_name     = "${local.prefix}-easyauth"
  owners           = [data.azuread_client_config.current[0].object_id]
  sign_in_audience = "AzureADMyOrg"

  # Construct redirect URIs from the environment default domain (not the app resources)
  # so the registration does not depend on the apps - the apps depend on this secret.
  web {
    redirect_uris = [
      "https://${local.prefix}-api.${azurerm_container_app_environment.env.default_domain}/.auth/login/aad/callback",
      "https://${local.prefix}-ui.${azurerm_container_app_environment.env.default_domain}/.auth/login/aad/callback",
    ]
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }
}

resource "azuread_application_password" "easyauth" {
  count          = var.enable_easy_auth ? 1 : 0
  application_id = azuread_application.easyauth[0].id
}

# The Easy Auth client secret, surfaced to the apps as a Key Vault-referenced secret
# named "easyauth-client-secret" (matches clientSecretSettingName below; wired into the
# apps' kv_secrets in main.tf when enabled).
resource "azurerm_key_vault_secret" "easyauth_client_secret" {
  count        = var.enable_easy_auth ? 1 : 0
  name         = "easyauth-client-secret"
  value        = azuread_application_password.easyauth[0].value
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

locals {
  easyauth_issuer = var.enable_easy_auth ? "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0" : ""
}

# Attach an authConfig to each app (azapi - azurerm has no native resource). The client
# secret is referenced by name; the apps already carry a secret of that name (see below).
resource "azapi_resource" "api_auth" {
  count     = var.enable_easy_auth ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.api.id
  body = jsonencode({
    properties = {
      platform         = { enabled = true }
      globalValidation = { unauthenticatedClientAction = "Return401" }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            openIdIssuer            = local.easyauth_issuer
            clientId                = azuread_application.easyauth[0].client_id
            clientSecretSettingName = "easyauth-client-secret"
          }
          validation = { allowedAudiences = [azuread_application.easyauth[0].client_id] }
        }
      }
    }
  })
}

resource "azapi_resource" "ui_auth" {
  count     = var.enable_easy_auth ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.frontend.id
  body = jsonencode({
    properties = {
      platform         = { enabled = true }
      globalValidation = { unauthenticatedClientAction = "RedirectToLoginPage" }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            openIdIssuer            = local.easyauth_issuer
            clientId                = azuread_application.easyauth[0].client_id
            clientSecretSettingName = "easyauth-client-secret"
          }
          validation = { allowedAudiences = [azuread_application.easyauth[0].client_id] }
        }
      }
    }
  })
}

output "easy_auth_enabled" {
  value = var.enable_easy_auth
}
