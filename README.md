# Macro Terminal

One page showing what drives MCX gold, silver, copper and Nifty futures. No broker login.

## How it works
- `fetch_data.py` pulls Yahoo Finance (COMEX metals, FX, VIX, indices), FRED (real yield, Fed funds) and NSE (FII/DII, option chain, FII futures positioning). A failed source keeps its last value and is marked stale.
- MCX prices are a computed fair value: COMEX x USD/INR x (1 + import duty) + premium. Duties live in `config.json` and are assumptions: check them.
- `site/index.html` reads `site/data.json`.
- `.github/workflows/update.yml` runs the job every 30 minutes and deploys `site/` to GitHub Pages.

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
