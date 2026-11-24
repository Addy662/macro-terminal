# Macro Terminal

One page showing what drives MCX gold, silver, copper and Nifty futures. No broker login.

## How it works
- `fetch_data.py` pulls Yahoo Finance (COMEX metals, FX, VIX, indices), FRED (real yield, Fed funds) and NSE (FII/DII, option chain, FII futures positioning). A failed source keeps its last value and is marked stale.
- Rupee metals figures are estimates, not MCX prices: COMEX x USD/INR x (1 + import duty) x (1 + typical local premium). Duties and the fixed premium live in `config.json`; re-measure the premium now and then.
- `site/index.html` reads `site/data.json`.
- `.github/workflows/update.yml` runs the job and deploys `site/` to GitHub Pages. GitHub's own schedule is only a backup: on 23 Nov 2026 it started 2 of about 48 due runs.
- The page shows data age and a red warning when data is over 45 minutes old, Mon-Fri 09:00-23:55 IST.

## Refresh trigger (cron-job.org)
1. GitHub -> Settings -> Developer settings -> Fine-grained tokens: only this repo, **Actions: Read and write**, nothing else.
2. cron-job.org job: `POST https://api.github.com/repos/Addy662/macro-terminal/actions/workflows/update.yml/dispatches`, body `{"ref":"main"}`, headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `Content-Type: application/json`. Schedule `*/15 * * * 1-6`, timezone Asia/Kolkata. Turn on failure emails.
3. Never put the token in this repo: it is public.

## Deploy (about 5 minutes)
1. Create a GitHub repo, push this folder to `main`.
2. Settings -> Pages -> Source: **GitHub Actions**.
3. Actions tab -> run **update-and-deploy** once. Your page is at `https://<user>.github.io/<repo>/`.

Free GitHub Pages needs a public repo, which makes `data.json` public. For a private repo use Cloudflare Pages instead: point it at `site/` and keep the workflow for the data job only.

## Test locally
    pip install -r requirements.txt
    python fetch_data.py --demo     # synthetic numbers, page shows a DEMO banner
    python fetch_data.py            # live
    cd site && python -m http.server 8000

## Known limits
- NSE often blocks GitHub's servers. Those panels go stale until a fetch works. If it never works, run the job from an Indian IP (a small VPS or your own machine) instead.
- No traded MCX price, GIFT Nifty, Nifty futures basis, LME or Shanghai copper. None has a free no-login feed.
