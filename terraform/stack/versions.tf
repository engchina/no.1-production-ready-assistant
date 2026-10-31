terraform {
  required_version = "~> 1.5.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5.0"
    }
    oci = {
      source  = "oracle/oci"
      version = "= 8.24.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6.0"
    }
  }
}
