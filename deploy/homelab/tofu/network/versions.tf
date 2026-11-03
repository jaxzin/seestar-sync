terraform {
  required_version = ">= 1.5"

  required_providers {
    unifi = {
      source  = "filipowm/unifi"
      version = "~> 1.0"
    }
  }

  # Partial S3 backend (Backblaze B2): bucket/key/endpoint/credentials are
  # supplied at init time by CI from OpenBao secret/infra/b2/tofu-state.
  backend "s3" {
    key                         = "seestar-sync/network.tfstate"
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true
  }
}
