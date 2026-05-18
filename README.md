# mtg-nomulli

MTG opening hand playability analyzer for Commander decklists.

Paste a decklist, run Monte Carlo simulations, and get turn-3 playability stats backed by real Scryfall card data.

## Live app

Deployed on Vercel — see the repo for the URL.

## Local (Windows)

```bat
run.bat
```

Then open http://127.0.0.1:8000

## How it works

- Downloads Scryfall oracle_cards bulk data on first run (cached in `/tmp/mtg_cache`)
- Simulates 10,000 shuffled opening hands
- Evaluates curve coverage and castable plays through turn 3
- Returns playable-hand %, curve rate, and castable-by-turn-3 rate
