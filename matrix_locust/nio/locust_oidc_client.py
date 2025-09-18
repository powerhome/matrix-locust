import re
import time
import urllib.parse
from typing import Optional, Union

import requests
from bs4 import BeautifulSoup
from nio.api import Api
from nio.responses import LoginError, LoginResponse

from .locust_client import LocustClient


class LocustOIDCClient(LocustClient):
    """Matrix no-IO client with OIDC authentication support.

    This client extends the base LocustClient with OIDC authentication
    capabilities, allowing authentication with OIDC providers.
    """

    def login_oidc(
        self,
        oidc_issuer: str,
        client_id: str = "matrix-locust",
        device_name: Optional[str] = "",
        redirect_uri: str = None,
        username: str = None,
        password: str = None,
    ) -> Union[LoginResponse, LoginError]:
        """Login to the homeserver using OIDC authentication.

        This method implements the Matrix SSO/OIDC flow including:
        1. Initiating SSO redirect with the Matrix homeserver
        2. Handling OIDC provider authentication form submission
        3. Following the callback to get login token
        4. Using the login token to authenticate with Matrix

        Args:
            oidc_issuer (str): The OIDC issuer URL (e.g., https://id.powerhrg.com).
            client_id (str): The OIDC client ID.
            device_name (str): A display name for the device.
            redirect_uri (str, optional): The redirect URI for OIDC callback. 
                If not provided, defaults to the Matrix homeserver's OIDC callback endpoint.
            username (str): Username for OIDC provider login.
            password (str): Password for OIDC provider login.

        Returns either a `LoginResponse` if the request was successful or
        a `LoginError` if there was an error with the request.
        """
        try:
            # Get credentials from client attributes if not provided
            if username is None:
                username = getattr(self, "oidc_username", None)
            if password is None:
                password = getattr(self, "oidc_password", None)

            # Construct default redirect_uri from Matrix homeserver URL if not provided
            if redirect_uri is None:
                matrix_base_url = f"{self.locust_user.host}"
                redirect_uri = f"{matrix_base_url}/_synapse/client/oidc/callback"

            if not username or not password:
                return LoginError(
                    "OIDC username/password not provided",
                    status_code="M_OIDC_CREDENTIALS_MISSING",
                )

            # Step 1: Get the Matrix SSO login URL
            login_token = self._perform_oidc_flow(username, password, redirect_uri)

            if not login_token:
                return LoginError(
                    "Failed to obtain login token from OIDC flow",
                    status_code="M_OIDC_TOKEN_MISSING",
                )

            # Step 2: Use the login token to authenticate with Matrix
            method, path, data = self._build_request(
                Api.login(
                    self.user,
                    password=None,
                    device_name=device_name,
                    device_id=self.device_id,
                    token=login_token,
                )
            )

            response = self._send(LoginResponse, method, path, data)

            if isinstance(response, LoginResponse):
                self.matrix_domain = self.user_id.split(":")[-1]

            return response

        except Exception as e:
            return LoginError(
                f"OIDC authentication failed: {str(e)}", status_code="M_OIDC_ERROR"
            )

    def _perform_oidc_flow(
        self, username: str, password: str, redirect_uri: str
    ) -> Optional[str]:
        """Perform the OIDC authentication flow.

        This method implements the Matrix SSO + OIDC flow:
        1. Start SSO redirect with Matrix homeserver
        2. Follow redirects to OIDC provider
        3. Submit login form to OIDC provider
        4. Follow callback redirects to get login token

        Args:
            username: OIDC provider username
            password: OIDC provider password
            redirect_uri: Callback URI for OIDC flow

        Returns:
            Login token from Matrix callback, or None if authentication failed
        """
        max_retries = 3

        for attempt in range(max_retries):
            session = requests.Session()
            session.timeout = 30

            # Configure session with proper headers to avoid bot detection
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                }
            )

            try:
                print(f"OIDC login attempt {attempt + 1}/{max_retries}")

                # Step 1: Get the Matrix SSO login URL
                # Use the Matrix SSO redirect endpoint with OIDC provider hint
                matrix_base_url = f"{self.locust_user.host}"
                sso_redirect_url = f"{matrix_base_url}/_matrix/client/v3/login/sso/redirect/oidc-nitroid"

                print(f"Starting SSO flow to {sso_redirect_url}")
                print(f"Using callback URL: {redirect_uri}")
                sso_params = {"redirectUrl": redirect_uri}
                response = session.get(
                    sso_redirect_url, params=sso_params, allow_redirects=True
                )
                response.raise_for_status()

                print(f"Final page reached: (status: {response.status_code})")

                # Step 3: We should now be at the OIDC provider login page
                # Parse the login form and submit credentials
                login_token = self._handle_oidc_login(
                    session, response, username, password, redirect_uri
                )

                if login_token:
                    return login_token
                else:
                    print(f"Attempt {attempt + 1} failed to get login token")

            except Exception as e:
                print(f"Attempt {attempt + 1} failed with exception: {str(e)}")
                if attempt == max_retries - 1:
                    print(f"All {max_retries} attempts failed")
                    return None
                else:
                    print(f"Retrying in 2 seconds...")
                    time.sleep(2)
            finally:
                session.close()

        return None

    def _handle_oidc_login(
        self,
        session: requests.Session,
        response: requests.Response,
        username: str,
        password: str,
        redirect_uri: str,
    ) -> Optional[str]:
        """Handle the OIDC provider login form submission.

        Args:
            session: requests session maintaining cookies
            response: response from OIDC provider login page
            username: OIDC provider username
            password: OIDC provider password
            redirect_uri: Callback URI

        Returns:
            Login token from callback, or None if failed
        """
        try:

            # Parse the login form from the OIDC provider page
            soup = BeautifulSoup(response.text, "html.parser")

            # Find the login form - try multiple strategies
            login_form = None

            # Strategy 1: Look for forms with login-related attributes
            for form in soup.find_all("form"):
                form_id = form.get("id", "").lower()
                form_class = " ".join(form.get("class", [])).lower()
                form_action = form.get("action", "").lower()

                if any(
                    keyword in form_id + form_class + form_action
                    for keyword in ["login", "signin", "auth", "credential"]
                ):
                    login_form = form
                    break

            # Strategy 2: Look for forms with password fields
            if not login_form:
                for form in soup.find_all("form"):
                    if form.find("input", {"type": "password"}):
                        login_form = form
                        break

            # Strategy 3: Use the first form as fallback
            if not login_form:
                login_form = soup.find("form")

            if not login_form:
                print("Could not find any login form on OIDC provider page")
                print(f"Page title: {soup.title.string if soup.title else 'No title'}")
                print(f"Available forms: {len(soup.find_all('form'))}")
                print(f"Page preview: {response.text[:500]}...")
                return None

            form_action = login_form.get("action")
            if form_action and not form_action.startswith("http"):
                # Relative URL, make it absolute
                base_url = f"{response.url.split('?')[0].rsplit('/', 1)[0]}"
                form_action = f"{base_url}/{form_action.lstrip('/')}"
            elif not form_action:
                # No action attribute, use current URL
                form_action = f"{response.url.split('?')[0]}"

            # Extract any hidden form fields (CSRF tokens, etc.)
            form_data = {}
            for input_field in login_form.find_all("input", {"type": "hidden"}):
                name = input_field.get("name")
                value = input_field.get("value", "")
                if name:
                    form_data[name] = value

            # Add username and password fields
            # Common field names for username/email
            username_fields = ["email", "username", "login", "user"]
            password_fields = ["password", "passwd", "pwd"]

            # Find the actual field names from the form
            username_field_found = False
            password_field_found = False

            for input_field in login_form.find_all("input"):
                field_type = input_field.get("type", "").lower()
                field_name = input_field.get("name", "").lower()
                field_id = input_field.get("id", "").lower()
                field_placeholder = input_field.get("placeholder", "").lower()

                # Enhanced username field detection
                if (
                    field_type in ["email", "text"]
                    or any(uf in field_name for uf in username_fields)
                    or any(uf in field_id for uf in username_fields)
                    or any(
                        uf in field_placeholder for uf in ["email", "username", "user"]
                    )
                ):
                    form_data[input_field.get("name")] = username
                    username_field_found = True

                # Enhanced password field detection
                elif (
                    field_type == "password"
                    or any(pf in field_name for pf in password_fields)
                    or any(pf in field_id for pf in password_fields)
                    or "password" in field_placeholder
                ):
                    form_data[input_field.get("name")] = password
                    password_field_found = True

            if not username_field_found:
                print("WARNING: Could not identify username field, trying fallback...")
                # Fallback: look for any text input that's not hidden
                for input_field in login_form.find_all("input"):
                    if (
                        input_field.get("type", "").lower() in ["text", "email", ""]
                        and input_field.get("type", "").lower() != "hidden"
                    ):
                        form_data[input_field.get("name")] = username
                        break

            if not password_field_found:
                print("WARNING: Could not identify password field")
                # This is more critical - we should see a password field

            # Submit the login form
            login_response = session.post(
                form_action, data=form_data, allow_redirects=True
            )
            login_response.raise_for_status()

            # Step 4: Follow redirects to get back to Matrix with login token
            # The callback should contain a login token in the URL
            final_url = login_response.url

            # Extract login token from the callback URL and response
            parsed_url = urllib.parse.urlparse(final_url)
            query_params = urllib.parse.parse_qs(parsed_url.query)

            login_token = None

            # Strategy 1: Check URL parameters
            if "loginToken" in query_params:
                login_token = query_params["loginToken"][0]

            # Strategy 2: Check for common token parameter variations
            elif "token" in query_params:
                login_token = query_params["token"][0]
            elif "access_token" in query_params:
                login_token = query_params["access_token"][0]

            # Strategy 3: Check response headers
            if not login_token:
                auth_header = login_response.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    login_token = auth_header[7:]
                elif "X-Login-Token" in login_response.headers:
                    login_token = login_response.headers["X-Login-Token"]

            # Strategy 4: Check response body with multiple patterns
            if not login_token:
                patterns = [
                    r'loginToken["\']?\s*[:=]\s*["\']?([^"\'&\s<>]+)',
                    r'token["\']?\s*[:=]\s*["\']?([^"\'&\s<>]+)',
                    r'access_token["\']?\s*[:=]\s*["\']?([^"\'&\s<>]+)',
                    r'["\']loginToken["\']\s*:\s*["\']([^"\']+)',
                    r'window\.location\.href\s*=\s*["\'][^"\']*[?&]loginToken=([^"\'&]+)',
                ]

                for pattern in patterns:
                    token_match = re.search(pattern, login_response.text, re.IGNORECASE)
                    if token_match:
                        login_token = token_match.group(1)
                        break

            # Strategy 5: Look for JavaScript redirects or meta refresh
            if not login_token:
                # Check for meta refresh with token
                meta_match = re.search(
                    r'<meta[^>]+refresh[^>]+url=([^"\'>\s]+)',
                    login_response.text,
                    re.IGNORECASE,
                )
                if meta_match:
                    redirect_url = meta_match.group(1)
                    redirect_params = urllib.parse.parse_qs(
                        urllib.parse.urlparse(redirect_url).query
                    )
                    if "loginToken" in redirect_params:
                        login_token = redirect_params["loginToken"][0]

            if login_token:
                return login_token
            else:
                print("Could not extract login token from callback")
                print(f"Final URL: {final_url}")
                print(f"URL query params: {query_params}")
                print(f"Response headers: {dict(login_response.headers)}")
                print(f"Response text preview: {login_response.text[:1000]}...")
                return None

        except Exception as e:
            print(f"Error handling OIDC provider login: {str(e)}")
            return None
