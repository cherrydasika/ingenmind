# Sign-in for the web app on EC2 (app/auth.py, AUTH_MODE=oidc): an Amazon
# Cognito user pool as the OpenID Connect provider. Cognito holds the
# passwords; the app only receives a signed ID token. Accounts are created by
# an administrator (no self sign-up); the app's own invite list (app_users,
# ADMIN_EMAILS) then decides who may use it and with which permissions.
#
# The app reaches it through the SSM tunnel at http://localhost:28000, which
# Cognito accepts as a callback (http is allowed for localhost only). Add a
# domain's https URL to the lists when the app gets one.

locals {
  app_urls = ["http://localhost:28000"]
}

resource "aws_cognito_user_pool" "app" {
  name                = "rag-systems"
  deletion_protection = "ACTIVE"
  username_attributes = ["email"]
  # Cognito verifies the email when the user first signs in with the
  # temporary password it sent there.
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  lifecycle {
    # Cognito cannot change a pool's attribute schema after creation.
    ignore_changes = [schema]
  }
}

# The hosted sign-in pages, e.g. https://rag-systems-<account>.auth.eu-west-2.amazoncognito.com
resource "aws_cognito_user_pool_domain" "app" {
  domain       = "rag-systems-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.app.id
}

resource "aws_cognito_user_pool_client" "app" {
  name                                 = "rag-systems-web"
  user_pool_id                         = aws_cognito_user_pool.app.id
  generate_secret                      = true # the web app keeps it server-side
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for url in local.app_urls : "${url}/auth/callback"]
  logout_urls                          = [for url in local.app_urls : "${url}/"]
  prevent_user_existence_errors        = "ENABLED"
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  enable_token_revocation              = true
  id_token_validity                    = 1
  access_token_validity                = 1
  refresh_token_validity               = 1
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }
}
