output "api_url" {
  value       = "https://${azurerm_container_app.api.ingress[0].fqdn}"
  description = "Public URL of the platform API."
}

output "acr_login_server" {
  value       = azurerm_container_registry.acr.login_server
  description = "Push your image here: docker push <login_server>/strata-platform:latest"
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "managed_identity_client_id" {
  value       = azurerm_user_assigned_identity.app.client_id
  description = "Set as AZURE_CLIENT_ID; used for managed-identity auth to OpenAI/Blob/Queue."
}
