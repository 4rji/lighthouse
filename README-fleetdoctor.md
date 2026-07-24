# OOB Fleet Doctor

Fleet Doctor queries the Lighthouse REST API in read-only mode and summarizes
node health, VPN state, model information, and detected warnings.

## Run

```sh
python3 fleetdoctor.py https://192.168.88.10
```

Use `--insecure` only for a lab with a self-signed certificate. Add
`--json-output fleet-report.json` to save the node response.

For the live terminal dashboard:

```sh
python3 fleetdoctor_dashboard.py https://192.168.88.10 --insecure
```

Dashboard controls: `q` quits, `r` refreshes, and `n`/`p` or the arrow keys
move between node pages.

## Environment configuration

The scripts can read these values from a `.env` file:

```dotenv
FLEETDOCTOR_URL=https://192.168.88.10
FLEETDOCTOR_USERNAME=fleetdoctor
FLEETDOCTOR_PASSWORD=REPLACE_WITH_A_NEW_PASSWORD
FLEETDOCTOR_API_VERSION=v3.7
FLEETDOCTOR_INSECURE=true
FLEETDOCTOR_REFRESH=10
```

Protect the file with `chmod 600 .env` and never commit it. Use a dedicated
read-only Lighthouse account and rotate any password previously shared in
plain text.
