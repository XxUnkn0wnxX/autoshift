# AutoSHiFt: Automatically redeem Gearbox SHiFT Codes

- **Compatibility:** 3.9+.
- **Platform:** Crossplatform.
- **Repo:** https://github.com/XxUnkn0wnxX/autoshift forked from https://github.com/zarmstrong/autoshift forked from https://github.com/ugoogalizer/autoshift forked from https://github.com/Fabbi/autoshift

# Overview

Data provided by Mental Mars' Websites via this [shiftcodes.json](https://raw.githubusercontent.com/XxUnkn0wnxX/autoshift-codes/main/shiftcodes.json) file that is updated reguarly by an instance of [this scraper](https://github.com/zarmstrong/autoshift-scraper).  You don't need to run the scraper as well, only this `autoshift` script/container.  This is to reduce the burden on the Mental Mars website given the great work they're doing to make this possible.<br>

`autoshift` detects and memorizes new games and platforms added to the orcicorn shift key database.

Games currently that are scraped and made available to `autoshift` are: 
- [Borderlands](https://mentalmars.com/game-news/borderlands-golden-keys/)
- [Borderlands 2](https://mentalmars.com/game-news/borderlands-2-golden-keys/)
- [Borderlands 3](https://mentalmars.com/game-news/borderlands-3-golden-keys/)
- Borderlands 4  

  > [mentalmars](https://mentalmars.com/game-news/borderlands-4-shift-codes/) |  [polygon](https://www.polygon.com/borderlands-4-active-shift-codes-redeem/) | [ign](https://www.ign.com/wikis/borderlands-4/Borderlands_4_SHiFT_Codes) | [xsmashx88x](https://xsmashx88x.github.io/Shift-Codes/)
- [Borderlands The Pre-Sequel](https://mentalmars.com/game-news/bltps-golden-keys/)
- [Tiny Tina's Wonderlands](https://mentalmars.com/game-news/tiny-tinas-wonderlands-shift-codes)

To see which games and platforms are supported use the `auto.py --help` command.

*This tool doesn't save your login data anywhere on your machine!*
After your first login your login-cookie (a string of seemingly random characters) is saved to the `data` folder and reused every time you use `autoshift` after that.

`autoshift` tries to prevent being blocked when it redeems too many keys at once.

You can choose to only redeem mods/skins etc, only golden keys or both. There is also a limit parameter so you don't waste your keys (there is a limit on how many bl2 keys your account can hold for example).

## Installation

```sh
git clone git@github.com:XxUnkn0wnxX/autoshift.git
```

or download it as zip

you'll need to install a few dependencies

```sh
cd ./autoshift
python3 -m venv venv
source ./venv/bin/activate
pip install -r requirements.txt
mkdir -p ./data
```

## Usage

# ⚠️ Database Change Notice

**As of the 9/18/2025 version, the way redeemed keys are tracked has changed.**  
Redemption status is now tracked in a separate table for each key and platform combination.  

**On first run after upgrade, all keys will be retried to ensure the database is properly marked. This may take a long time if you have a large key database.**  
This is expected and only happens once; subsequent runs will be fast.

## Usage Instructions

You can now specify exactly which platforms should redeem which games' SHiFT codes using the `--redeem` argument.  
**Recommended:** Use `--redeem` for fine-grained control.  
**Manual:** Provide a single SHiFT code (with an optional platform filter) to trigger the manual redeem flow.  
**Legacy:** You can still use `--games` and `--platforms` together, but a warning will be printed and all games will be redeemed on all platforms.

### New (Recommended) Usage

- Redeem codes for Borderlands 3 on Steam and Epic, and Borderlands 2 on Epic only:
```sh
./auto.py --redeem bl3:steam,epic bl2:epic
```

- Redeem codes for Borderlands 3 on Steam only:
```sh
./auto.py --redeem bl3:steam
```

- You can still use other options, e.g.:
```sh
./auto.py --redeem bl3:steam,epic --golden --limit 10
```

### Manual Single-Code Usage

- Redeem a code across every supported platform:
```sh
./auto.py --redeem J9RT3-RBJ5T-WRTBK-JB3J3-5TB3R --shift-source data/shiftcodes.json --profile myprofile
```

- Redeem a code on specific platforms:
```sh
./auto.py --redeem J9RT3-RBJ5T-WRTBK-JB3J3-5TB3R:psn,xbox --shift-source data/shiftcodes.json --profile myprofile
```

Manual mode treats **already redeemed** responses the same as successes and prints a concise `Manual redeem outcome:` summary. It will skip inserting duplicate rows when the SHiFT source already contains metadata for the code.

> **Manual mode restrictions**: `--schedule`, `--limit`, `--games`, `--platforms`, `--golden`, `--non-golden`, and `--other` are rejected when you supply a single code. Use mapping mode instead if you need those flags.

### Legacy Usage (still supported, but prints a warning)

- Redeem codes for Borderlands 3 and Borderlands 2 on Steam and Epic (all combinations):
```sh
./auto.py --games bl3 bl2 --platforms steam epic
```

## Override SHiFT source

You can override the default SHiFT codes source (the JSON that is normally fetched from mentalmars/autoshift-codes) with either a URL or a local file path.

- CLI: use the --shift-source argument. Example:
```sh
./auto.py --shift-source "https://example.com/my-shift-codes.json" --redeem bl3:steam
```

- Environment variable: set SHIFT_SOURCE. The CLI flag takes precedence over the environment variable.
```sh
export SHIFT_SOURCE="file:///path/to/shiftcodes.json"
# or
export SHIFT_SOURCE="/absolute/path/to/shiftcodes.json"
```

Supported formats:
- HTTP(s) URLs (e.g. https://...)
- Local absolute or relative paths (e.g. ./data/shiftcodes.json)
- file:// URLs (e.g. file:///autoshift/data/shiftcodes.json)

Docker / Kubernetes examples
- Docker run (override source via env):
```sh
docker run \
  -e SHIFT_USER='<username>' \
  -e SHIFT_PASS='<password>' \
  -e SHIFT_SOURCE='https://example.com/shiftcodes.json' \
  -e SHIFT_ARGS='--redeem bl3:steam --schedule -v' \
  -v autoshift:/autoshift/data \
  zacharmstrong/autoshift:latest
```

- Kubernetes (set SHIFT_SOURCE in the manifest):
```yaml
env:
  - name: SHIFT_SOURCE
    value: "https://example.com/shiftcodes.json"
  - name: SHIFT_ARGS
    value: "--redeem bl3:steam --schedule 6 -v"
```

Notes
- CLI --shift-source overrides the SHIFT_SOURCE environment variable.
- If the source is a local path inside the container, ensure the file is present in the container filesystem (mounted volume, image, etc.).
- The tool validates and logs the selected source at startup so you can confirm which file/URL is being used. Manual redeems reuse this metadata when populating the database; if the code is missing from the source, it falls back to a generic record.

## Profiles (per-user data)

You can run autoshift with named profiles so separate runs (or different users) keep their own DB, cookies and other state.

- Default: when no profile is specified autoshift uses the normal data directory:
  c:\Users\slate\Dropbox\code\autoshift\data

- Profile mode: when a profile is specified, autoshift will use a profile-specific data path:
  c:\Users\slate\Dropbox\code\autoshift\data/<profile>/
  That directory holds the profile's keys.db, cookie file and any other state.

How to select a profile:
- CLI (takes precedence):
  ./auto.py --profile myprofile --redeem bl3:steam
- Environment variable:
  export AUTOSHIFT_PROFILE=myprofile
  ./auto.py --redeem bl3:steam

Notes:
- The CLI --profile overrides AUTOSHIFT_PROFILE for that run.
- Each profile has its own DB and cookie file; migrations and initial key processing run per-profile.
- First run for a profile may take longer because keys are re-processed and the DB is initialized/migrated for that profile.
- Use profiles when you want isolated state (e.g. different accounts, test vs production, or per-container environments).

Docker / Kubernetes examples
- Docker (profile via env):
```sh
docker run \
  --restart=always \
  -e AUTOSHIFT_PROFILE='myprofile' \
  -e SHIFT_USER='<username>' \
  -e SHIFT_PASS='<password>' \
  -e SHIFT_ARGS='--redeem bl3:steam --schedule -v' \
  -v autoshift:/autoshift/data \
  zacharmstrong/autoshift:latest
```

- Docker (profile via SHIFT_ARGS):
```sh
docker run \
  -e SHIFT_USER='<username>' \
  -e SHIFT_PASS='<password>' \
  -e SHIFT_ARGS='--profile myprofile --redeem bl3:steam --schedule -v' \
  -v autoshift:/autoshift/data \
  zacharmstrong/autoshift:latest
```

- Kubernetes (set AUTOSHIFT_PROFILE in the manifest):
```yaml
env:
  - name: AUTOSHIFT_PROFILE
    value: "myprofile"
  - name: SHIFT_ARGS
    value: "--redeem bl3:steam --schedule 6 -v"
```

## Code

Core modules and their responsibilities:

### `auto.py`
Entry point for the CLI. Parses arguments, wires profiles/SHIFT sources, and orchestrates either mapping mode or manual single-code redeems. It shares the `redeem()` helper across modes so logging and database updates stay consistent.

### `m_redeem.py`
Manual single-code workflow. Normalises `CODE` / `CODE:platform` arguments, validates unsupported flags, drives the platform probe loop, and writes back metadata to the database without disturbing existing rows.

### `shift.py`
Thin wrapper around Gearbox SHiFT endpoints. Manages login, cookie reuse, and translates response messages into `Status` codes consumed by the rest of the app.

### `query.py`
Database gateway and SHIFT-source parsing logic. Handles migrations on demand, tracks known games/platforms, and exposes helpers for inserting keys, marking redemptions, and loading external JSON.

### `migrations.py`
Versioned schema upgrades plus legacy data clean-ups (e.g., normalising historical code formats). Invoked lazily whenever the database is opened.

### `common.py`
Shared utilities: logger configuration, data-directory helpers, and constants referenced by multiple modules.

# Docker

Available as a docker image based on `python3.10-buster`. Build it locally from your checked-out repository:

```bash
docker build -t autoshift:local .
```

## Docker Usage (local image)

``` bash
docker run \
  --restart=always \
  -e SHIFT_USER='<username>' \
  -e SHIFT_PASS='<password>' \
  -e SHIFT_ARGS='--redeem bl3:steam,epic bl2:epic --schedule -v' \
  -e TZ='America/Chicago' \
  -v autoshift:/autoshift/data \
  autoshift:local
```

## Docker Compose Usage (local image)

``` yaml
---
version: "3.0"
services:
  autoshift:
    image: autoshift:local
    container_name: autoshift_all
    restart: always
    volumes:
      - autoshift:/autoshift/data
    environment:
      - TZ=America/Denver
      - SHIFT_USER=<username>
      - SHIFT_PASS=<password>
      - SHIFT_ARGS="--redeem bl3:steam,epic bl2:epic --schedule -v"
    pull_policy: never
```

> **Note:**  
> The Docker image runs `auto.py --user $SHIFT_USER --pass $SHIFT_PASS $SHIFT_ARGS`. Set `SHIFT_USER`/`SHIFT_PASS` (or include credentials inside `SHIFT_ARGS`) and quote the entire `SHIFT_ARGS` string, e.g. `"--redeem bl3:steam --schedule -v"`.

## Kubernetes Usage (local image)

Load the locally built image into your cluster (for example, `kind load docker-image autoshift:local`). Then deploy with something similar to:
``` yaml
--- # deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app: autoshift
  name: autoshift
#  namespace: autoshift
spec:
  selector:
    matchLabels:
      app: autoshift
  revisionHistoryLimit: 0
  template:
    metadata:
      labels:
        app: autoshift
    spec:
      containers:
        - name: autoshift
          image: autoshift:local
          imagePullPolicy: Never
          env:
            - name: SHIFT_USER
              valueFrom:
                secretKeyRef:
                  name: autoshift-secret
                  key: username
            - name: SHIFT_PASS
              valueFrom:
                secretKeyRef:
                  name: autoshift-secret
                  key: password
            - name: SHIFT_ARGS
              value: "--redeem bl3:steam,epic bl2:epic --schedule 6 -v"
            - name: TZ
              value: "Australia/Sydney"
          volumeMounts:
            - mountPath: /autoshift/data
              name: autoshift-pv
      volumes:
        - name: autoshift-pv
          persistentVolumeClaim:
            claimName: autoshift-pvc
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: autoshift-pvc
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi

```

> **Note:**  
> When using Kubernetes, set the `SHIFT_ARGS` environment variable in your deployment manifest to include your `--redeem ...` options.  
> If you use both `SHIFT_GAMES`/`SHIFT_PLATFORMS` and `--redeem`, the `--redeem` mapping will take precedence and a warning will be printed if legacy options are also present.

## Variables

#### **SHIFT_USER** (required)
The username/email for your SHiFT account

Example: `johndoe123`


#### **SHIFT_PASS** (required)
The password for your SHiFT account

Example: `p@ssw0rd`

Important: shells (especially interactive bash) may perform history expansion on the exclamation mark `!`. If you pass a password directly on the command line and it contains `!` the shell may replace/truncate it before autoshift sees it.

Troubleshooting / recommended ways to provide the password:
- Preferred (safe): set the password via environment variable (avoids shell history issues).
  - Bash:
    ```sh
    export SHIFT_PASS='p@ss!word'    # use single quotes to prevent history expansion
    ./auto.py --user you --pass "${SHIFT_PASS}" --redeem bl3:steam
    ```
  - Docker:
    ```sh
    docker run -e SHIFT_PASS='p@ss!word' ...
    ```
  - Kubernetes: store the password in a Secret and set SHIFT_PASS from the secret (recommended).
- If you must pass the password on the CLI, wrap it in single quotes to avoid history expansion:
  ```sh
  ./auto.py -u you -p 'p@ss!word' --redeem bl3:steam
  ```
- Alternative: escape the `!` character (less recommended than quoting).
- Debugging: to temporarily print the password in logs for debugging, set:
  ```sh
  export AUTOSHIFT_DEBUG_SHOW_PW=1
  ```
  Security warning: this will print your password in cleartext to the logs — use only for short-lived debugging and remove the env var afterwards.

Notes about autoshift behavior:
- The tool contains a heuristic: if the CLI-provided password contains `!` and an environment password (SHIFT_PASS or AUTOSHIFT_PASS_RAW) appears longer, the environment value will be preferred. Still, supplying the full password via SHIFT_PASS (or via a container secret) is the most reliable method.
- Do not commit passwords to command history or scripts. Use environment variables, container secrets, or mounted files / K8s Secrets.

#### **SHIFT_GAMES** (recommended)
The game(s) you want to redeem codes for

Default: `bl4 bl3 blps bl2 bl`

Example: `blps` or `bl bl2 bl3`

|Game|Code|
|---|---|
|Borderlands|`bl1`|
|Borderlands 2|`bl2`|
|Borderlands: The Pre-Sequel|`blps`|
|Borderlands 3|`bl3`|
|Borderlands 4|`bl4`|
|Tiny Tina's Wonderlands|`ttw`|
|Godfall|`gdfll`|


#### **SHIFT_PLATFORM** (recommended)
The platform(s) you want to redeem codes for

Default: `epic steam`

Example: `xbox` or `xbox ps`

|Platform|Code|
|---|---|
|PC (Epic)|`epic`|
|PC (Steam)|`steam`|
|Xbox|`xboxlive`|
|Playstation|`psn`|
|Stadia|`stadia`|
|Nintendo|`nintendo`|


#### **SHIFT_ARGS** (optional)
Additional arguments to pass to the script. Combine them as needed; the examples mirror common workflows. Manual single-code runs reject the flags noted earlier.

Default: `--schedule`

Example: `--schedule --golden --limit 30`

|Arg|Description|Example|
|---|---|---|
|`--redeem target`|Mapping mode (`game:platform[,platform]`) or manual mode (`CODE` or `CODE:platforms`).|`--redeem bl3:steam,epic` · `--redeem J9RT3-...:psn`|
|`--golden`|Only redeem golden keys.|`--redeem bl3:steam --golden`|
|`--non-golden`|Redeem non-golden keys (e.g., diamond).|`--redeem bl3:steam --non-golden`|
|`--other`|Include cosmetics/unknown codes alongside golden/non-golden.|`--redeem bl3:steam --golden --other`|
|`--games list`|Legacy mode: list of games (mutually exclusive with manual single-code mode).|`--games bl3 bl2`|
|`--platforms list`|Legacy mode: list of platforms (mutually exclusive with manual single-code mode).|`--platforms steam epic`|
|`--limit n`|Max golden keys to redeem (defaults to 200).|`--redeem bl3:steam --limit 25`|
|`--schedule [hours]`|Keep checking every N hours (defaults to 2 if omitted). Not supported with manual single-code `--redeem`.|`--redeem bl3:steam --schedule 6`|
|`-v`|Verbose logging (shows per-platform summary).|`--redeem bl3:steam -v`|
|`--dump-csv path`|Dump the database to CSV and exit.|`--dump-csv exports/keys.csv`|
|`--shift-source source`|Override the SHiFT code JSON (URL/path).|`--shift-source data/shiftcodes.json`|
|`--profile name`|Use a named profile (separate DB/cookies).|`--profile alt --redeem bl3:steam`|
|`--user value`|Specify SHiFT username via CLI (or use `SHIFT_USER`).|`--user someone@example.com`|
|`--pass value`|Specify SHiFT password via CLI (prefer env/secret).|`--pass 's3cret'`|

> ℹ️ When setting `SHIFT_ARGS` in Docker or Kubernetes manifests, wrap the entire value in quotes (e.g., `"--redeem bl3:steam --schedule -v"`).

#### **TZ** (optional)

Your timezone

Default: `America/Chicago`

Example: `Europe/London`

## Building Docker Image

``` bash
docker build -t autoshift:latest .

```

## Building Docker Image and Pushing to local Harbor and/or Docker Hub

``` bash

# Once off setup: 
git clone TODO

# Personal parameters
export HARBORURL=harbor.test.com

git pull

#Set Build Parameters
export VERSIONTAG=1.8

#Build the Image
docker build -t autoshift:latest -t autoshift:${VERSIONTAG} . 

#Get the image name, it will be something like 41d81c9c2d99: 
export IMAGE=$(docker images -q autoshift:latest)
echo ${IMAGE}

#Login to local harbor
docker login ${HARBORURL}:443

#Tag and Push the image to local harbor
docker tag ${IMAGE} ${HARBORURL}:443/autoshift/autoshift:latest
docker tag ${IMAGE} ${HARBORURL}:443/autoshift/autoshift:${VERSIONTAG}
docker push ${HARBORURL}:443/autoshift/autoshift:latest
docker push ${HARBORURL}:443/autoshift/autoshift:${VERSIONTAG}

#Tag and Push the image to public docker hub repo
docker login -u zacharmstrong docker.io/zacharmstrong/autoshift
docker tag ${IMAGE} docker.io/zacharmstrong/autoshift:latest
docker tag ${IMAGE} docker.io/zacharmstrong/autoshift:${VERSIONTAG}
docker push docker.io/zacharmstrong/autoshift:latest
docker push docker.io/zacharmstrong/autoshift:${VERSIONTAG}

```
