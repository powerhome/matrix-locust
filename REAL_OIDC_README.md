# Real OIDC Authentication with NitroID

This document explains how to use **real** NitroID OIDC authentication (not mock tokens) with matrix-locust for testing Connect v3.

## Overview

The matrix-locust implementation has been enhanced to support actual OIDC authentication flows instead of just mock JWT tokens. This allows for realistic testing of the complete OIDC authentication chain:

1. **Matrix SSO Initiation** - Start SSO flow with Matrix homeserver
2. **NitroID Redirect** - Follow redirects to NitroID login page
3. **Form Submission** - Programmatically submit NitroID credentials
4. **Token Exchange** - Follow callback redirects to obtain login token
5. **Matrix Authentication** - Use login token to authenticate with Matrix

## Requirements

- Connect-server running locally with OIDC enabled
- Valid NitroID credentials (set as environment variables)
- Python dependencies: `beautifulsoup4` (automatically installed)

## Security-First Approach

**No passwords in files!** This implementation uses environment variables for credentials:

- ✅ Credentials stored in environment variables only
- ✅ No plain text passwords in CSV files
- ✅ No passwords in command line arguments
- ✅ No sensitive data in git commits
- ✅ Ready for CI/CD secrets integration

### Setting Credentials

Create a `.env` file (from `.env.example`):
```bash
cp .env.example .env
# Edit .env with your NitroID credentials
source .env
```

Or set environment variables directly:
```bash
export NITROID_USERNAME=your_username
export NITROID_PASSWORD=your_password
```

## Quick Test

Test real OIDC login with a single user:

```bash
# Install dependencies first
poetry install

# Set credentials in environment variables
export NITROID_USERNAME=your_username
export NITROID_PASSWORD=your_password

# Option 1: Automated login (no browser interaction)
python test_real_oidc_login.py

# Option 2: Browser-based login (opens browser, closes automatically)
python test_real_oidc_browser.py

# Verbose output for debugging
python test_real_oidc_login.py --verbose
```

## Generating OIDC Users

Create a users.csv file for OIDC authentication (credentials come from environment variables):

```bash
# Set credentials in environment first
export NITROID_USERNAME=your_username
export NITROID_PASSWORD=your_password

# Generate single user for OIDC (no credentials stored in CSV)
python generate_users.py 1 --oidc \
  --oidc-issuer https://id.powerhrg.com \
  --output real_oidc_users.csv
```

The generated CSV will contain (no passwords stored):
```csv
username,oidc_issuer,oidc_client_id,user_id
user.000000,https://id.powerhrg.com,matrix-locust,@user.000000
```

## Using with Existing Locust Scripts

Update your existing OIDC test scripts to use the real authentication:

```python
class RealOIDCMatrixUser(MatrixUser):
    def on_start(self):
        # Load user data (credentials come from environment variables)
        user_data = {
            "username": "testuser",
            "oidc_issuer": "https://id.powerhrg.com", 
            "oidc_client_id": "matrix-locust",
        }
        
        self.login_from_csv_oidc(user_data)
        
        # Perform real OIDC login (uses NITROID_USERNAME/NITROID_PASSWORD env vars)
        response = self.matrix_client.login_oidc(
            self.matrix_client.oidc_issuer,
            self.matrix_client.oidc_client_id
        )
        
        if isinstance(response, LoginResponse):
            logging.info(f"Successfully logged in via real OIDC: {self.matrix_client.user_id}")
        else:
            logging.error(f"Real OIDC login failed: {response.message}")
```

## Connect-Server Configuration

Ensure your local connect-server has OIDC enabled:

1. **Enable OIDC** in `deploy/environment/development/values.yaml`:
   ```yaml
   synapse:
     oidc:
       enabled: true
       idp_id: nitroid
       idp_name: "Nitro ID"
       issuer: "https://id.powerhrg.com"
   ```

2. **Set Client Credentials** in `deploy/templates/partials/homeserver.yaml.erb`:
   - Update `client_id` and `client_secret` with valid NitroID credentials

3. **Restart Connect-Server**:
   ```bash
   docker compose down && docker compose build && docker compose up
   ```

## Authentication Flow Details

### Option 1: Automated Flow (test_real_oidc_login.py)
Uses BeautifulSoup to parse and submit forms programmatically:

1. **Matrix SSO Initiation** - Calls `/_matrix/client/v3/login/sso/redirect/oidc-nitroid`
2. **Enhanced Redirect Handling** - Automatically follows redirects with proper session management
3. **Intelligent Form Detection** - Multiple strategies to find login forms:
   - Forms with login-related attributes (id, class, action)
   - Forms containing password fields
   - Fallback to first available form
4. **Enhanced Field Detection** - Finds username/password fields using:
   - Field types (email, text, password)
   - Field names and IDs
   - Placeholder text analysis
5. **Robust Token Extraction** - Multiple methods to capture login tokens:
   - URL query parameters (loginToken, token, access_token)
   - Response headers (Authorization, X-Login-Token)
   - Response body regex patterns
   - JavaScript redirects and meta refresh tags
6. **Retry Mechanism** - Automatically retries failed attempts with proper session cleanup

### Option 2: Browser Flow (test_real_oidc_browser.py)
Opens a real browser for manual login with automatic cleanup:

1. **Callback Server** - Starts local HTTP server on localhost:8080
2. **Browser Launch** - Opens NitroID login page in default browser
3. **Manual Login** - User completes login in browser
4. **Auto-Close** - JavaScript automatically closes browser window after 1 second
5. **Token Capture** - Extracts login token from callback URL
6. **Matrix Authentication** - Uses captured token for Matrix login

### Matrix Authentication (Both Options)
- Uses extracted login token with Matrix login API
- Establishes authenticated Matrix session
- Validates with sync request

## Troubleshooting

### Common Issues

1. **"Could not find login form"** (Automated Flow)
   - NitroID page structure may have changed
   - Enable verbose logging to inspect HTML content
   - Try the browser-based flow as an alternative

2. **"Could not extract login token"**
   - Check connect-server OIDC configuration
   - Verify redirect URI handling
   - Check if callback server is accessible on localhost:8080

3. **"Maximum redirects reached"** (Fixed in latest version)
   - Previous issue with manual redirect handling
   - Now uses automatic redirect handling with session persistence

4. **Browser window doesn't close** (Browser Flow)
   - Some browsers block `window.close()` for security
   - Window will redirect to blank page as fallback
   - Script continues running regardless of window state

5. **Authentication failures**
   - Confirm NitroID credentials are valid
   - Check connect-server logs for OIDC errors
   - Verify environment variables are properly set

### Debug Mode

Enable verbose logging for detailed flow information:

```bash
python test_real_oidc_login.py --verbose
```

This will show:
- All HTTP requests and responses
- HTML form parsing details
- Redirect chain information
- Token extraction process

### Manual Testing

Test the flow manually in a browser:
1. Visit `http://localhost:8008/_matrix/client/v3/login/sso/redirect/oidc-nitroid?redirectUrl=http://localhost:8080/callback`
2. Complete NitroID login
3. Observe the callback URL and login token

## Security Notes

- **Environment Variables Only**: Credentials are never stored in files, only in environment variables
- **No Version Control**: `.env` files are git-ignored, preventing credential commits
- **Test Environment Only**: This implementation is designed for testing environments
- **Token Handling**: Login tokens are logged for debugging - remove in production use
- **Network Security**: All OIDC communications use HTTPS with NitroID

## Implementation Details

### Key Files Modified
- `matrix_locust/nio/locust_client.py` - Core OIDC flow implementation
- `matrix_locust/users/matrixuser.py` - OIDC credentials support
- `generate_users.py` - Real credential generation
- `pyproject.toml` - Added beautifulsoup4 dependency

### New Files
- `test_real_oidc_login.py` - Automated single-user testing script (no browser)
- `test_real_oidc_browser.py` - Browser-based testing script (auto-closes after 1 second)
- `real_oidc_users.csv` - Example credentials file
- `REAL_OIDC_README.md` - This documentation

The real OIDC implementation provides a complete, production-like authentication flow for comprehensive testing of Connect v3 OIDC integration.