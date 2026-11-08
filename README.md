# Sleeper H2H Tracker

A Streamlit dashboard that pulls data from the public [Sleeper API](https://docs.sleeper.com/) (no keys needed) and shows:

- **Head-to-Head Madness** – manager H2H cards and same-record tiebreaker standings.
- **Points & Weekly Scoring** – weekly PF / PA grids, point differential, season averages and a PFR leaderboard.

Change the league ID in the sidebar, or edit the constants and `NAME_MAPPING` at the top of `app.py`.
API responses are cached for 1 hour (`@st.cache_data(ttl=3600)`); use **Refresh data** in the sidebar to force a reload.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub:
   ```bash
   git add .
   git commit -m "Sleeper H2H tracker"
   git push origin main
   ```
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. Click **Create app** → **Deploy a public app from GitHub**.
4. Pick this repository, branch `main`, and main file path `app.py`.
5. Click **Deploy**. Dependencies are installed from `requirements.txt` automatically.
