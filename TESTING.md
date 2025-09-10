# Matrix Load Testing Guide

This guide covers the complete process for running load tests with Matrix Locust, including user generation, test data setup, and test execution.

## Prerequisites

- Poetry installed and dependencies ready: `poetry install`
- Matrix homeserver running and accessible
- OIDC identity provider configured

## Test Execution Process

### 1. Generate Test Users

#### Phase 1 Users (Main Test Pool)
Generate users from the main test user list:

```bash
python generate_users.py --from-file test-user-list.txt --oidc --oidc-issuer https://id.powerhrg.com -o users.csv
```

This creates `users.csv` with OIDC-enabled users for the primary load test.

#### Phase 2 Users (Setup/Admin Users)
Generate administrative users for test setup:

```bash
python generate_users.py --from-file setup-users.txt -o setup-users.csv --oidc --oidc-issuer https://id.powerhrg.com
```

This creates `setup-users.csv` with users for creating test rooms and initial data.

### 2. Create Test Data

Set up test rooms, messages, and reactions using the setup script:

```bash
poetry run python /Users/greg/code/connect-v3/matrix-locust/locust-setup-test-data.py --host https://pr920.connect-server.beta.px.powerapp.cloud --rooms=1 --messages=1 --reactions=1
```

This script creates:
- Test rooms with generated users
- Initial messages in those rooms
- Reactions on messages
- Saves room information to `test_rooms.json`

### 3. Run Load Test

Execute the Connect Apple login load test:

```bash
poetry run locust -f connect-apple-login-test.py
```

After running this command:
1. Open your browser to `http://localhost:8089`
2. Configure test parameters in the Locust web UI:
   - Number of users
   - Spawn rate
   - Host URL (your Matrix homeserver)
   - Test duration
3. Start the test and monitor metrics

## Test Configuration Options

The load test supports several command-line options:

- `--sync-type`: Choose between "standard" or "lazy-loading" sync methods

Example with options:
```bash
poetry run locust -f connect-apple-login-test.py --sync-type=lazy-loading
```

### User List File Format

The `.txt` files containing usernames should be formatted with one username per line:

```
username1
username2
username3
```

Example `test-user-list.txt`:
```
first.last
```

Example `setup-users.txt`:
```
first.last
```

### External User IDs File Format

The `user_external_ids.csv` file contains external user ID mappings for Audiences API integration:

```csv
auth_provider,external_id,user_id
oidc-nitroid,123,@u123:example.com
```

