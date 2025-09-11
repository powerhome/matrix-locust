#!/bin/env python3

import argparse
import csv
import os

parser = argparse.ArgumentParser(
    description="Generates a list of matrix users with OIDC authentication from a file to store in a .csv file"
)
parser.add_argument(
    "-o", "--output", type=str, default="users.csv", help="Output .csv file path"
)
parser.add_argument(
    "--oidc-issuer", type=str, required=True, help="OIDC issuer URL for authentication"
)
parser.add_argument(
    "--oidc-client-id",
    type=str,
    default="matrix-locust",
    help="OIDC client ID for authentication",
)
parser.add_argument(
    "--from-file",
    type=str,
    required=True,
    help="Read usernames from file (one per line)",
)

args = parser.parse_args()

if not os.path.exists(args.from_file):
    raise FileNotFoundError(f"Input file {args.from_file} not found")

with open(args.from_file, "r", encoding="utf-8") as f:
    usernames_from_file = [line.strip() for line in f if line.strip()]

if not usernames_from_file:
    raise ValueError(f"No usernames found in file {args.from_file}")

print(f"Read {len(usernames_from_file)} usernames from {args.from_file}")

password = os.getenv("PASSWORD")

if not password:
    raise ValueError("PASSWORD environment variable is required")

with open(args.output, "w", encoding="utf-8") as csvfile:
    fieldnames = ["username", "password", "oidc_issuer", "oidc_client_id", "user_id"]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

    for username in usernames_from_file:
        user_id = f"@{username}"
        pwd = password

        print(
            f"username = [{username}]\tpassword = [{pwd}]\toidc_issuer = [{args.oidc_issuer}]\tuser_id = [{user_id}]"
        )
        writer.writerow(
            {
                "username": username,
                "password": pwd,
                "oidc_issuer": args.oidc_issuer,
                "oidc_client_id": args.oidc_client_id,
                "user_id": user_id,
            }
        )