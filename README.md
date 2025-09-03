# Matrix load generation with Locust

This project provides Python classes and scripts for load testing
[Matrix](https://matrix.org/) servers using [Locust](https://locust.io/).

## Getting started

### Prerequisites

We assume that you already have your Matrix homeserver installed and
configured for testing.

* Your homeserver should be configured to allow registering new accounts
  without any kind of email verification or CAPTCHA etc.

* Either turn off rate limiting entirely, or increase your rate limits
  to allow the volume of traffic that you plan to produce, with some
  extra headroom just in case.

If you need help creating a reproducible configuration for your server,
have a look at [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy)
for an Ansible playbook that can be used to install and configure Synapse,
Dendrite, and Conduit, along with any required databases and other
homeserver accessories.

**Installation steps for the machine you will be using to generate load on your
server:**
```console
pip install --user pipx
pipx install poetry
poetry install
```

There is also a Dockerfile should you wish to build a container:
```sudo docker build --tag circles/matrix-locust:latest .```

Make sure to expose port `8089` in the container for access to the web UI.

**BS-SPEKE / Circles setup (optional):**

If you are using [swiclops](https://github.com/circles-project/swiclops) on your
server and want to support the added UIA stages, you can install the
dependencies as follows:
1. Install python3 development:
  * Debian: `sudo apt install python3-dev`
  * RPM: `sudo dnf install python3-devel`
2. Setup the repo and build the module:
```console
git submodule init
git submodule update
poetry install --with circles
cd matrix_locust/bsspeke
make
cd python
poetry run python ./bsspeke_build.py
```

### Generating users and rooms

Before you can use the Locust scripts to load your Matrix server, you
first need to generate usernames and passwords for your Locust users,
as well as the set of rooms where they will chat with each other.

#### Standard Password-based Authentication

First we generate the usernames and passwords.

```console
python generate_users.py
```

This generates 1000 users by default and saves the usernames and passwords to
a file called `users.csv`. You can also pass in a number to specify the number
of users to generate.

#### OIDC Authentication Support

Matrix Locust now supports OIDC (OpenID Connect) authentication for testing
homeservers configured with external identity providers. To generate users
for OIDC testing:

```console
python generate_users.py 100 --oidc --oidc-issuer https://auth.example.com --domains example.com
```

This generates OIDC-enabled users instead of password-based users. The generated
`users.csv` will contain OIDC configuration (issuer URL, client ID) instead of passwords.

For detailed OIDC setup and usage instructions, see [OIDC_README.md](OIDC_README.md).

#### Room Generation

Next we need to decide what the rooms are going to look like in our test.
The `generate_rooms.py` script generates as many rooms as there are users
in `users.csv`.

```console
python generate_rooms.py
```

The script decides how many users should be in each room according to an "80/20"
rule (aka a power law distribution), in an attempt to match real-world
human behavior.
Most rooms will be small -- only 2 or 3 users -- but there is a good
chance that there will be some room so big as to contain every single
user in the test.
Once the script has decided how big each room should be, it selects users
randomly from the population to fill up each room.
It saves the room names and the user-room assignments in the file `rooms.json`.

## Running the tests

The following examples show just a few things that we can do with Locust.

In fact, the user registration script and the room creation script (1 and 2 below)
were not originally intended to stress the server.

After running one of the scripts, you can navigate in your web-browser to
`http://0.0.0.0:8089/` to open the Locust interface. From there, you can set
the amount of users, spawn-rate, host URL, and max duration of the test. After
setting the parameters, you can start the test and view the statistics/graphs
in the web UI.

You may need to play around with the total number of users and the spawn rate
to find a configuration that your homeserver can handle. Note that for scripts
1-3, if you specify a smaller amount of Locust users than the amount you have
generated in `users.csv`, all users/rooms will still be registered/created
(Locust users determine the amount of concurrent open connections to the
server).

1. Registering user accounts

```console
poetry run python run.py matrix_locust/client_server/register.py
```

2. Creating rooms

```console
poetry run python run.py matrix_locust/client_server/create_room.py
```

3. Accepting invites to join rooms

```console
poetry run python run.py matrix_locust/client_server/join.py
```

4. Normal chat activity -- Accepting any pending invites, sending messages, paginating rooms

```console
poetry run python run.py locust-run-users.py
```

You can also directly run Locust without using the helper `run.py` script
if you prefer to have more control of the Locust parameters. See the
[Locust Configuration](https://docs.locust.io/en/stable/configuration.html)
section in the documentation for further details.

## Known issues

**Locust becomes unstable/behaves in an undefined manner:**

Sometimes if you are running a load test that has more than 5,000 users, you
may experience undefined behavior, where you may requests may return error
responses or you may experience highly volatile RPS metrics. For large scale
load testing, our current efforts are on developing
[matrix-goose](https://github.com/circles-project/matrix-goose) for
larger-scale and eventually distributed load testing. You can attempt to run
large-scale load tests with matrix-locust, but be aware you may encounter
potential instability.

**Locust uses a lot of system resources:**

This is another motivating reason why we are focusing on developing
[matrix-goose](https://github.com/circles-project/matrix-goose) for
large-scale load testing. For a more lightweight version of matrix-locust,
you can checkout the `legacy` branch that is more lightweight, but with less
capabilities and features.

**I see warnings of "Failed to increase the resource limit":**

You can ignore this warning if you are running load tests with under 1,000
users. If you are running tests with more than 1,000 users, you need to ensure
your file descriptor limit is high enough so all the locust users can make
their http requests. You can either run the load test with admin privileges
to automatically increase the limit or change the descriptor limit yourself
(e.g. `sudo ulimit -Sn 8192`).

## Federated loadtests

You can generate a set of set of users to be registered to multiple servers
when running the `generate_users.py` script. See the script arguments for more
information on specifying user domains and distributions.

## Running automated tests

This repository supports the ability to run automated tests. You can define
test suites, which are JSON files that describes a series of tests along with
its Locust parameters to run. Examples of test suites are provided in the
`test-suites` directory.

There are also utility scripts (located in the `scripts` directory), but note
that some of these scripts are dependent on a specific server setup.

Example for running a test-suite:

```console
$ python3 run.py --host YOUR_HOMESERVER test-suites/synapse-2k.json
```

Note: For the automation scripts provided in this repository, you should not
prefix the host argument with `https://`.

## Writing your own tests

The base class for interacting with a Matrix homeserver is [MatrixUser](./matrixuser.py).

For an example of a class that extends `MatrixUser` to generate traffic
like a real user, see [MatrixChatUser](./matrixchatuser.py).
