# OIDC Authentication Support for Matrix Locust

This document explains how to use OpenID Connect (OIDC) authentication with Matrix Locust for load testing Matrix homeservers that support OIDC login.

## Overview

Matrix Locust now supports OIDC authentication as an alternative to password-based authentication. This is particularly useful for testing Matrix homeservers that are configured to use external identity providers like Keycloak, Auth0, or other OIDC-compliant providers.

## Generating OIDC Users

To generate users for OIDC testing, use the `--oidc` flag with the `generate_users.py` script:

```bash
python3 generate_users.py 100 --oidc --oidc-issuer https://auth.example.com --domains example.com
```

### Parameters:

- `--oidc`: Enable OIDC mode for user generation
- `--oidc-issuer`: The OIDC issuer URL (required when using --oidc)
- `--oidc-client-id`: The OIDC client ID (default: "matrix-locust")
- `--domains`: The Matrix homeserver domain(s)

This will generate a `users.csv` file with OIDC configuration instead of passwords:

```csv
username,oidc_issuer,oidc_client_id,user_id
user.000000:example.com,https://auth.example.com,matrix-locust,@user.000000:example.com
user.000001:example.com,https://auth.example.com,matrix-locust,@user.000001:example.com
```

## Running OIDC Tests

### Using the OIDC Login Script

Use the provided `locust-oidc-login.py` script for basic OIDC authentication testing:

```bash
locust -f locust-oidc-login.py --host https://matrix.example.com
```

### Using Test Suites

You can also use the test suite configuration:

```bash
python3 run.py test-suites/oidc-example-100.json
```

## OIDC Implementation Details

### Mock Token Generation

For load testing purposes, the implementation creates mock OIDC tokens using JWT. In a real scenario, these would be obtained from the actual OIDC provider through the authorization code flow.

### Authentication Flow

1. **OIDC Discovery**: Fetches OIDC configuration from `{issuer}/.well-known/openid_configuration`
2. **Token Generation**: Creates a mock JWT token for testing (in production, this would involve browser redirects)
3. **Matrix Login**: Uses the token with Matrix's `/login` endpoint using `m.login.token` type

### Supported Features

- ✅ OIDC user generation
- ✅ Mock token creation for load testing
- ✅ Integration with existing Matrix Locust framework
- ✅ Support for multiple OIDC providers
- ✅ Backward compatibility with password-based authentication

### Limitations

- Mock tokens are used for load testing (real OIDC flow would require browser interaction)
- Currently supports JWT token format
- Requires OIDC provider configuration to be accessible

## Customizing OIDC Behavior

### Custom OIDC User Classes

You can create custom user classes that inherit from `MatrixUser` and use OIDC authentication:

```python
from matrix_locust.users.matrixuser import MatrixUser
from nio.responses import LoginError

class CustomOIDCUser(MatrixUser):
    def on_start(self):
        # Load user configuration
        user_data = {
            "username": "test.user:example.com",
            "oidc_issuer": "https://auth.example.com",
            "oidc_client_id": "matrix-client"
        }
        
        self.login_from_csv_oidc(user_data)
        
        # Perform OIDC login
        if hasattr(self.matrix_client, 'oidc_issuer'):
            response = self.matrix_client.login_oidc(
                self.matrix_client.oidc_issuer,
                self.matrix_client.oidc_client_id
            )
            
            if isinstance(response, LoginError):
                print(f"OIDC login failed: {response.message}")
```

### Advanced Configuration

For more complex OIDC scenarios, you can extend the `login_oidc` method in `LocustClient`:

```python
# In your custom implementation
def custom_oidc_login(self, issuer, client_id, custom_claims=None):
    # Add custom claims to the mock token
    if custom_claims:
        payload = {
            **self._create_default_payload(issuer, client_id),
            **custom_claims
        }
        token = jwt.encode(payload, "test-secret", algorithm="HS256")
    else:
        token = self._create_mock_oidc_token(issuer, client_id)
    
    # Use the token for Matrix login
    return self.login(token=token)
```

## Production Considerations

When adapting this for production testing with real OIDC providers:

1. **Real Token Exchange**: Replace mock token generation with actual OIDC authorization code flow
2. **Browser Automation**: Consider using headless browsers for the OAuth redirect flow
3. **Token Caching**: Implement proper token caching and refresh mechanisms
4. **Security**: Use proper token validation and secure storage

## Troubleshooting

### Common Issues

1. **"No OIDC issuer configured"**: Ensure the user CSV file contains `oidc_issuer` column
2. **"OIDC authentication failed"**: Check that the OIDC issuer URL is accessible
3. **Token validation errors**: Verify that the Matrix homeserver is configured to accept OIDC tokens

### Debug Mode

Enable debug logging to see detailed OIDC flow information:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Examples

See the `test-suites/oidc-example-100.json` configuration file for a complete example of OIDC testing setup.
