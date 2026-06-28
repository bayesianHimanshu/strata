variable "subscription_id" {
  type        = string
  description = "Azure subscription id to deploy into."
}

variable "name_prefix" {
  type        = string
  default     = "strata"
  description = "Prefix for resource names (keep short; some resources have length limits)."
}

variable "location" {
  type        = string
  default     = "eastus2"
  description = "Azure region. Must support Azure OpenAI + the GPT-5.x and embeddings models."
}

variable "postgres_admin" {
  type    = string
  default = "strata"
}

variable "openai_model" {
  type        = string
  default     = "gpt-5.5"
  description = "Azure OpenAI chat model name to deploy."
}

variable "openai_model_version" {
  type        = string
  default     = "2026-04-24"
  description = "Chat model version. VERIFY availability: az cognitiveservices model list -l <region>. (gpt-5.5 is 2026-04-24 in eastus2/swedencentral/westus3/eastus.)"
}

variable "embeddings_model" {
  type        = string
  default     = "text-embedding-3-small"
  description = "Azure OpenAI embeddings model (1536-dim; keep ChunkRow.dim matched)."
}

variable "embeddings_model_version" {
  type        = string
  default     = "1"
  description = "Embeddings model version."
}

variable "model_cutoff" {
  type        = string
  default     = "2025-12-01"
  description = "Training cutoff of the deployed chat model; defines the leakage-clean slice. Keep aligned to whatever model is deployed."
}

variable "demo_profile" {
  type        = bool
  default     = true
  description = "Cost-optimised showcase profile: Basic ACR + worker scale-to-zero (~$5-10/mo idle). Set false for production sizing."
}

variable "enable_easy_auth" {
  type        = bool
  default     = false
  description = "Front the API/UI with Container Apps built-in Entra (Easy Auth). Requires Graph app-registration rights in the tenant; see auth.tf. When false, AUTH is off at the app and ingress is the only gate (fine for a private demo)."
}

variable "ncbi_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "NCBI/PubMed E-utilities API key (raises ingestion throughput). Stored in Key Vault."
}

variable "openfda_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "openFDA API key (optional; raises limits). Stored in Key Vault."
}

variable "nice_xlsx_url" {
  type        = string
  default     = "https://a.storyblok.com/f/243782/x/39a98b770a/ta-cancer-recommendations.xlsx"
  description = "Reliable storyblok asset URL for the NICE cancer-recommendations xlsx (the HTML page 502s). Used by the NICE live-horizon connector."
}

variable "container_image" {
  type        = string
  default     = ""
  description = "Fully-qualified API/worker image, e.g. <acr>.azurecr.io/strata-platform:latest. Apply infra first, push, then apply again with this set."
}

variable "tags" {
  type    = map(string)
  default = { project = "strata", component = "ieg-platform" }
}
