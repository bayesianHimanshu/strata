###############################################################################
# Frontend — Next.js UI as its own Container App.
# Build & push a separate image (frontend/Dockerfile) to the same ACR, then set
# var.frontend_image. It is given the API's internal/ingress URL at runtime.
###############################################################################

variable "frontend_image" {
  type        = string
  default     = ""
  description = "Fully-qualified frontend image, e.g. <acr>.azurecr.io/strata-frontend:latest"
}

resource "azurerm_container_app" "frontend" {
  name                         = "${local.prefix}-ui"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }
  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }
  ingress {
    external_enabled = true
    target_port      = 3000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 2
    container {
      name   = "ui"
      image  = var.frontend_image
      cpu    = 0.25
      memory = "0.5Gi"
      # The browser calls the API directly, so this must be the API's PUBLIC url.
      env {
        name  = "NEXT_PUBLIC_API_BASE"
        value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
      }
    }
  }
}

output "frontend_url" {
  value       = "https://${azurerm_container_app.frontend.ingress[0].fqdn}"
  description = "Public URL of the STRATA UI."
}
