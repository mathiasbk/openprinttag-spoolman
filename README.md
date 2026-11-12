# OpenPrintTag Spoolman for Prusa Connect

[![Tests](https://github.com/mathiasbk/openprinttag-spoolman/actions/workflows/tests.yml/badge.svg)](https://github.com/mathiasbk/openprinttag-spoolman/actions/workflows/tests.yml)
[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](LICENSE)

Sync OpenPrintTag spool data and filament usage from Prusa Connect to Spoolman.

The bridge reads each loaded spool's OpenPrintTag data from Prusa Connect, links it to a [Spoolman](https://github.com/Donkie/Spoolman) spool and records what the printer consumes.

**This is an unofficial project, not affiliated with Prusa Research or Spoolman.** It uses the same undocumented API as the Prusa Connect web app, which can change or stop working at any time.

![The dashboard](docs/screenshots/dashboard.png)

## How it works

1. Every minute, the bridge asks Prusa Connect's GraphQL API what each printer has loaded. Connect reports the OpenPrintTag data read from the spool's NFC tag, including its instance UUID.
2. It looks that UUID up in the `opt_instance_uuid` extra field in Spoolman. You choose what happens to a spool it does not know: link it to an existing one, or create a new one.
3. When Connect reports higher consumption for a linked spool, the bridge deducts the difference in Spoolman. If something else already deducts filament in Spoolman, such as a print tracker, turn off `TRACK_USAGE` so the same grams are not deducted twice.

The bridge starts in dry-run mode. It reads everything and shows what it would do, but writes nothing to Spoolman until you turn that off.

## Requirements

- A Prusa printer with an OpenPrintTag NFC reader, connected to a Prusa Connect account. Tested with a Core One on firmware 8.0.0.
- Spools with OpenPrintTag NFC tags.
- A running [Spoolman](https://github.com/Donkie/Spoolman) server. Tested with version 0.27.0.
- Docker with Compose 2.24 or newer.

## Quick start

The bridge runs from the image published with each [release](https://github.com/mathiasbk/openprinttag-spoolman/releases):

```
mkdir openprinttag-spoolman && cd openprinttag-spoolman
curl -fsSLO https://raw.githubusercontent.com/mathiasbk/openprinttag-spoolman/main/docker-compose.yml
docker compose up -d
```

To update to the newest release later, run `docker compose pull && docker compose up -d` in the same folder. Your settings stay in `data/`. To stay on one version, put `BRIDGE_TAG=0.1.0` (or another version) in `.env`.

To build from source instead, clone the repository and run `./setup.sh --build`.

Open `http://your-server:7913` and follow the steps:

1. **Password:** choose an admin password. Do this right away, because whoever opens the page first sets it.
2. **Spoolman:** enter the address and test the connection. If you use HTTPS with your own CA, upload the root certificate here.
3. **Prusa Connect:** drag the button to your bookmarks bar, log in on connect.prusa3d.com in a private window, and click the bookmark. The setup page notices the login and continues by itself. On a phone, copy the bookmark code from the page and paste it as the address of a new bookmark. Prusa has no login for tools like this, so the bookmark copies it from your browser once. The bridge renews it after that. You can paste the login by hand instead.
4. **Printers:** choose your printers and the location each one gets in Spoolman.
5. **Operation:** choose dry run, whether new spools are created automatically, and how often to poll.

Watch your first print in dry-run mode, then turn dry run off in the settings once the consumption looks right.

## Linking spools

If the bridge does not know a loaded spool, it checks Spoolman and suggests the most likely matches by brand, material, name, color, weight and OpenPrintTag material. Each suggestion shows a confidence level and the reasons. If nothing looks likely, it suggests creating a new spool.

Nothing is written until you confirm. The review page shows each change before and after, and you choose which ones to apply. Filament changes are off by default if other spools share the filament. If Spoolman has recorded more usage than the tag, for example from another printer, its value is kept by default. Other extra fields are kept.

In a dry run you can go through the same steps, and confirming shows the exact API calls without making them. Linked spools can be unlinked from the dashboard.

Set `AUTO_CREATE` or turn it on in the settings to create unknown spools without asking. Before creating one, the bridge checks Spoolman for an unlinked spool that is very likely the same, for example one Spoolman 0.27 created when its own reader scanned the tag. If it finds one, it creates nothing and the dashboard asks you to link it instead. The dashboard also warns if more than one spool is linked to a loaded tag.

## Adding new spools

Connect's Warehouse has a catalog of materials, so the easiest way to avoid entering a spool twice is to start there:

1. Add the spool to Warehouse in the Prusa app or in Connect, preferably from the catalog.
2. Write the tag from that warehouse spool in the Prusa app.
3. The spool shows up under Warehouse on the dashboard, even before it is loaded. Link it to a Spoolman spool or create a new one, and review the changes as above.

When creating a spool, the bridge reuses a filament with the same OpenPrintTag material, then one with the same brand and name, and otherwise creates a new filament. When linking, it offers to add the material to a filament that does not have one yet.

The Warehouse page lists every spool in the Connect warehouse, plus spools loaded in a printer that are not in it: material, color, filament left, where it is and which Spoolman spool it is linked to, with Spoolman's own weight next to it. The totals at the top count the filament left per material. It is a compact table like the spool list in Spoolman: click a column header to sort, search, filter by printer, shelf, link state or material, and link a spool that is not linked yet. Empty spools are listed at the bottom.

The bridge only reads the warehouse. It never changes anything in Connect. `AUTO_CREATE` still applies only to spools loaded in a printer, so a spool that is only in the warehouse waits for your choice. Archived spools are left out, and empty spools are only listed on the Warehouse page.

### Writing your own tags

A tag written from the material form in the Prusa app can make a Core One say "The OpenPrintTag does not allow filament usage tracking". To avoid this, add the spool to Warehouse in the Prusa app and write that warehouse spool to the tag.

## Consumption

| Consumption from Connect | What the bridge does |
|---|---|
| First time a linked spool is seen | Stores the value as a baseline and deducts nothing |
| Higher than last time | Adds the difference to the spool's used weight |
| More than a whole spool higher in one step | Deducts nothing, stores the new value and logs an error |
| Lower than last time | Stores the new value and logs a warning |

The consumption a spool already accounts for is stored on the spool itself, in the extra field `opt_consumed_weight` ("OpenPrintTag consumed"). The used weight and this field are changed in the same request, after the bridge has read the spool again. A second bridge, a reinstall or a lost `data` folder therefore cannot deduct the same grams twice. Do not edit the field by hand unless you mean to change what gets deducted next.

The dashboard shows what Spoolman has left next to Connect's number for each linked spool, and points it out when they differ by 10 g or more, for example after use on another printer. The bridge never corrects that difference itself.

`data/state.json` keeps a local copy and is used for spools linked before the field existed. It is updated only after Spoolman has answered. A dry run stores only its first baseline, so what is used during the dry run is deducted when you turn writing on.

## Configuration

Everything can be set in the browser and is saved in `data/config.json`. Environment variables and `.env` are defaults for anything not saved there. See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `WEB_PORT` | `7913` | Port of the web interface |
| `PUID`, `PGID` | `1000` | User that owns `data/` |
| `SPOOLMAN_URL` | None | Spoolman address |
| `SPOOLMAN_CA_BUNDLE` | System CAs | Root certificate for HTTPS |
| `SPOOLMAN_VERIFY_TLS` | `true` | `false` turns off the certificate check |
| `CONNECT_PRINTER_UUID`, `CONNECT_ORGANIZATION_ID` | None | One printer, if you do not choose in the browser |
| `LOCATION_NAME` | `Core One` | Location for the printer above |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `WAREHOUSE_INTERVAL` | `600` | Seconds between reading the Connect warehouse |
| `DRY_RUN` | `true` | Dry-run mode |
| `AUTO_CREATE` | `false` | Create unknown spools without asking |
| `TRACK_USAGE` | `true` | Deduct what the printer consumes in Spoolman. Turn off to only link spools and set their location |

## Security

- **Your Prusa account:** `data/` holds a login that gives access to your Prusa Connect account. Files with secrets have mode `600` and are never shown in the browser. Keep the folder private.
- **Plain HTTP:** the password and login travel unencrypted over your network. Use the bridge only on a network you trust, or put it behind a reverse proxy with HTTPS. Do not expose it to the internet.
- **Login bookmark:** it holds a one-time code that expires after 15 minutes. It sends your login to the bridge in the URL fragment, which stays out of server logs.
- **First visitor:** whoever opens the page first chooses the password.
- **Forgotten password:** stop the bridge, delete `data/auth.json` and start it again.

## Limitations

- **One bridge per printer is still best:** two bridges no longer deduct the same grams twice, but they can still move a spool's location back and forth.
- **Whole grams:** Connect reports consumption in whole grams, so small uses can show up a little late.
- **Token renewal can still fail:** if Prusa rejects it, the dashboard says so and you need to log in again.
- **Spools without a confirmed NFC reading:** Connect can report a spool, such as one with a custom tag, without marking it as read from NFC or including its name and brand. You can link it yourself, but it is never created automatically. Whether Connect keeps its ID and reports its consumption has not been verified.
- **Warehouse spools:** a spool from the Warehouse in the Prusa app has little data on the printer, so the bridge reads the rest from the Connect warehouse. Connect has no measured weight for it, so the nominal weight is used, and it is not applied over Spoolman's weight unless you choose it on the review page.

## Screenshots

The screenshots use made-up demo data.

| | |
|---|---|
| ![The Warehouse page, a table of every spool Connect knows about](docs/screenshots/warehouse-page.png) | ![The Warehouse page on a phone](docs/screenshots/warehouse-page-mobile.png) |
| ![Suggested matches for an unlinked spool](docs/screenshots/link-suggestion.png) | ![Reviewing a link before confirming](docs/screenshots/link-confirm.png) |
| ![Warehouse spools waiting for a choice on the dashboard](docs/screenshots/warehouse.png) | ![Warehouse spools on a phone](docs/screenshots/warehouse-mobile.png) |
| ![The dashboard in dark mode](docs/screenshots/dashboard-dark.png) | ![The dashboard on a phone](docs/screenshots/dashboard-mobile.png) |
| ![The dashboard on a phone in dark mode](docs/screenshots/dashboard-mobile-dark.png) | ![Connecting to Spoolman](docs/screenshots/setup-spoolman.png) |
| ![Setting up the Connect login](docs/screenshots/setup-connect.png) | ![Choosing printers](docs/screenshots/setup-printers.png) |
| ![Choosing how the bridge runs](docs/screenshots/setup-operation.png) | |

## Development

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`python -m bridge once` runs one poll and exits. `python -m bridge bootstrap` stores the login from the terminal.

## License

Copyright (c) 2026 mathiasbk.

This project is licensed under [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/) (CC BY-NC-SA 4.0). See [LICENSE](LICENSE) for the full text.

You may use, share and adapt it for non-commercial purposes, as long as you give credit and share your changes under the same license. Commercial use requires permission from the author.

Releases up to and including v0.2.0 were published under the MIT license, and copies obtained under those releases keep those terms.
