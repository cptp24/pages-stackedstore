# Z21 client pages

Password-protected data pages for clients, at `https://client.z21studio.com/<client>/`. They rebuild from live Shopify and GA4 data every morning at 07:00 SGT.

| Client | Page | Brand slug |
|---|---|---|
| Stacked Store | `/stackedstore/` (inventory, sales and site search) | `stackedstore-my` |

## How it works

1. GitHub Actions (`.github/workflows/refresh.yml`) runs daily, and on demand.
2. For each client, `build_site.sh` does four things:
   - pulls Shopify inventory and sales (`tools/shopify_inventory.py`)
   - pulls GA4 site search (`tools/ga4_site_search.py`, China excluded)
   - builds one HTML file with the data inlined (`build.py`)
   - encrypts that file with the client's password ([StaticCrypt](https://github.com/robinmoisson/staticrypt))
3. The encrypted files are deployed to GitHub Pages.

The published page is ciphertext. Without the password a visitor sees only the password box, and the data is unreadable even if someone downloads the file. Client data is created inside the Actions run and never committed: `.gitignore` blocks `*.json`, `exports/`, `build/` and `dist/`.

If a pull fails, the run stops before deploy, so the live page keeps the previous day's data. GitHub emails the repo admins about failed runs.

## Refresh on request

GitHub, then **Actions**, then **Refresh client pages**, then **Run workflow** on `main`. The live page updates in about 2 minutes. "Data as of" at the top shows when the data was pulled.

To trigger it from n8n or another tool, send one HTTP request with a fine-grained token that has Actions read and write on this repo:

```
POST https://api.github.com/repos/cptp24/pages-stackedstore/actions/workflows/refresh.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json

{"ref": "main"}
```

## One-time setup

1. **Repo.** This repo is public: `github.com/cptp24/pages-stackedstore`. Pages from a private repo needs GitHub Pro or Team. Because it is public:
   - The code is readable by anyone. It holds no secrets and no client data, only the store's Shopify address and GA4 property ID in `config/brands/`. Keep account notes out of that file.
   - Actions logs are public. `build_site.sh` prints no figures when `CI=true`. Never add a `print` of data to a step.
   - The deployed artifact contains only the encrypted page.
   - GitHub turns off scheduled workflows after 60 days without a commit. The `keepalive` job re-enables the workflow on each scheduled run. If the page ever stops updating, check Actions for a "disabled" banner and click **Enable workflow**.
2. **Secrets** (Settings, then Secrets and variables, then Actions):
   - `SHOPIFY_STACKEDSTORE_MY_OFFLINE_TOKEN`: the Shopify app's offline token for the store.
   - `GA4_SA_KEY_JSON`: the whole JSON of the GA4 service-account key. Its domain-wide delegation must be limited to `analytics.readonly`.
   - `STACKEDSTORE_PASSWORD`: the page password. Use four or five random words, because the encrypted file is public.
3. **Pages** (Settings, then Pages): set Source to **GitHub Actions**, set the custom domain to `client.z21studio.com`, and tick **Enforce HTTPS** once the certificate is issued.
4. **DNS** (GoDaddy, z21studio.com): add a CNAME `client` pointing to `<owner>.github.io`. Then verify the domain under the account's Settings, Pages, so no other repo can claim it.
5. Run the workflow once by hand and check the page.

## Add a client

1. Copy `config/brands/<slug>.yaml` from the workspace. It needs `shopify.store`, `shopify.token_ref`, `ga4.property_id`, `ga4.subject` and `site_domain`.
2. Copy `sites/stackedstore/` to `sites/<client>/`, change the page, and make a new salt: `python3 -c "import secrets;print(secrets.token_hex(16))" > sites/<client>/salt.txt`.
3. Add the client's secrets (Shopify token and `<CLIENT>_PASSWORD`). Then add a step to the workflow that writes the token into `.env` and runs `./build_site.sh <slug> <client>`.

## Change a password

Update the `<CLIENT>_PASSWORD` secret and run the workflow. Anyone who ticked "Remember me" has to enter the new password. To also sign out everyone with the old one, replace `sites/<client>/salt.txt`.

## Run locally

```
python3 -m pip install -r requirements.txt
# .env holds GA4_SA_KEY_PATH=<path to key file> and the Shopify token line(s)
STACKEDSTORE_PASSWORD='...' ./build_site.sh stackedstore-my stackedstore
cp public/* dist/ && python3 -m http.server 8766 --directory dist   # open http://localhost:8766/stackedstore/
```

## Keep in sync

`tools/` holds copies of the same files in Cheng Pin's Claude Code workspace. Each one says so in its second line. A fix made in one place must be copied to the other.
