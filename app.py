DEFAULT_LEAGUE_ID = "1389341473178140672"
DEFAULT_LEAGUE_NAME = "The League"
LEAGUE_URL = "https://sleeper.com/leagues/1389341473178140672/league"

# Map Sleeper username (or user_id) -> Clean Display Name
NAME_MAPPING = {
    "ankit-patel-32": "Ankit",
    "TheNameIsAtlas": "Carlos",
    "Fyomama": "Charlie",
    "OnlyDilfs": "Diller",
    "ironkaiser": "Eric",
    "wollpie": "Evan",
    "SisterJean": "Joe",
    "siesel4": "Matt",
}

import html
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

API_BASE = "https://api.sleeper.app/v1"
CACHE_TTL = 3600  # seconds
DEFAULT_PLAYOFF_WEEK_START = 15

st.set_page_config(
    page_title="Sleeper H2H Tracker",
    page_icon="🏈",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Sleeper API client
# ---------------------------------------------------------------------------
class SleeperAPIError(Exception):
    """Raised when the Sleeper API cannot be reached or returns bad data."""


def _get_json(url: str, retries: int = 3):
    """GET a URL and return parsed JSON, retrying on transient failures."""
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise SleeperAPIError(f"Sleeper API returned HTTP {resp.status_code} for {url}")
            return resp.json()
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(1.5 * (attempt + 1))
        except ValueError as exc:  # invalid JSON
            raise SleeperAPIError(f"Invalid JSON received from {url}: {exc}") from exc
    raise SleeperAPIError(f"Could not reach Sleeper API ({last_error}) for {url}")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_league(league_id: str) -> dict:
    data = _get_json(f"{API_BASE}/league/{league_id}")
    if not isinstance(data, dict) or not data.get("league_id"):
        raise SleeperAPIError(f"League '{league_id}' was not found on Sleeper.")
    return data


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_users(league_id: str) -> list:
    data = _get_json(f"{API_BASE}/league/{league_id}/users")
    return data if isinstance(data, list) else []


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_rosters(league_id: str) -> list:
    data = _get_json(f"{API_BASE}/league/{league_id}/rosters")
    return data if isinstance(data, list) else []


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_matchups(league_id: str, last_week: int) -> dict:
    """Fetch matchups for weeks 1..last_week concurrently. Returns {week: [matchup, ...]}."""
    weeks = list(range(1, last_week + 1))

    def one(week):
        data = _get_json(f"{API_BASE}/league/{league_id}/matchups/{week}")
        return week, (data if isinstance(data, list) else [])

    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, weeks))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_nfl_state() -> dict:
    """Sleeper's current NFL state (season, week, season_type, ...)."""
    data = _get_json(f"{API_BASE}/state/nfl")
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Current-week / in-progress detection
# ---------------------------------------------------------------------------
def now_eastern() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # tzdata unavailable: approximate with a fixed EST offset
        return datetime.now(timezone(timedelta(hours=-5)))


def is_week_in_progress(now: datetime | None = None) -> bool:
    """True from Thursday through Monday, and Tuesday before 6 AM ET.

    Games run Thursday night through Monday night; Sleeper finalizes scores early Tuesday.
    """
    now = now or now_eastern()
    weekday = now.weekday()  # Monday = 0 ... Sunday = 6
    if weekday in (3, 4, 5, 6, 0):
        return True
    if weekday == 1:
        return now.hour < 6
    return False


def get_active_week(league: dict, state: dict):
    """The league's current regular-season NFL week, or None if the league isn't live."""
    try:
        if str(state.get("season")) != str(league.get("season")):
            return None
        if state.get("season_type") != "regular":
            return None
        week = int(state.get("week") or 0)
        return week if week >= 1 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data processing
# ---------------------------------------------------------------------------
def build_names(users: list, rosters: list) -> dict:
    """Return {roster_id: clean display name}."""
    mapping_lower = {k.lower(): v for k, v in NAME_MAPPING.items()}
    user_names = {}
    for u in users:
        uid = str(u.get("user_id", ""))
        username = u.get("username") or ""
        display = u.get("display_name") or ""
        # The league /users endpoint usually omits `username`, so also try display_name
        # (which for these accounts equals the Sleeper username) before falling back.
        name = (
            NAME_MAPPING.get(username)
            or NAME_MAPPING.get(uid)
            or NAME_MAPPING.get(display)
            or mapping_lower.get(username.lower())
            or mapping_lower.get(display.lower())
            or display
            or username
            or f"User {uid}"
        )
        user_names[uid] = name

    names = {}
    for r in rosters:
        rid = r["roster_id"]
        names[rid] = user_names.get(str(r.get("owner_id")), f"Team {rid}")

    # Guarantee unique names so they can be used as dictionary keys.
    seen = {}
    for rid in sorted(names):
        base = names[rid]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            names[rid] = f"{base} ({rid})"
    return names


def build_games(matchups_by_week: dict, names: dict, live_week: int | None = None) -> list:
    """Pair up teams by matchup_id and build one game record per team per week.

    Games in `live_week` (the in-progress week) are flagged `live=True`.
    """
    games = []
    for week, entries in sorted(matchups_by_week.items()):
        by_id = {}
        for e in entries:
            mid = e.get("matchup_id")
            if mid is None or e.get("roster_id") not in names:
                continue
            by_id.setdefault(mid, []).append(e)
        for pair in by_id.values():
            if len(pair) != 2:
                continue  # bye or malformed matchup
            a, b = pair
            pa_pts = round(float(a.get("points") or 0), 2)
            pb_pts = round(float(b.get("points") or 0), 2)
            if pa_pts == 0 and pb_pts == 0:
                continue  # unplayed
            for me, opp, pf, pa in ((a, b, pa_pts, pb_pts), (b, a, pb_pts, pa_pts)):
                result = "W" if pf > pa else "L" if pf < pa else "T"
                games.append(
                    {
                        "week": week,
                        "manager": names[me["roster_id"]],
                        "opponent": names[opp["roster_id"]],
                        "pf": pf,
                        "pa": pa,
                        "result": result,
                        "live": week == live_week,
                    }
                )
    return games


def empty_record():
    return {"W": 0, "L": 0, "T": 0}


def compute_records(games: list, managers: list):
    overall = {m: empty_record() for m in managers}
    h2h = {m: {o: empty_record() for o in managers if o != m} for m in managers}
    for g in games:
        overall[g["manager"]][g["result"]] += 1
        h2h[g["manager"]][g["opponent"]][g["result"]] += 1
    return overall, h2h


def win_pct(rec: dict) -> float:
    n = rec["W"] + rec["L"] + rec["T"]
    return (rec["W"] + 0.5 * rec["T"]) / n if n else 0.0


def fmt_record(rec: dict) -> str:
    s = f"{rec['W']}-{rec['L']}"
    return s + f"-{rec['T']}" if rec["T"] else s


def compute_standings(managers, overall, h2h, total_pf):
    """Group managers by record; break ties by H2H among tied teams, then total PF.

    Returns a list of groups: (record_label, [ {name, rank, h2h_label|None}, ... ]).
    """
    groups = {}
    for m in managers:
        o = overall[m]
        groups.setdefault((o["W"], o["L"], o["T"]), []).append(m)

    def group_key(k):
        rec = {"W": k[0], "L": k[1], "T": k[2]}
        return (-win_pct(rec), -k[0], k[1])

    result = []
    rank = 1
    for key in sorted(groups, key=group_key):
        members = groups[key]
        tied = len(members) > 1
        sub = {}
        for m in members:
            agg = empty_record()
            for o in members:
                if o != m:
                    for r in "WLT":
                        agg[r] += h2h[m][o][r]
            sub[m] = agg
        members = sorted(
            members,
            key=lambda m: (-win_pct(sub[m]), -total_pf.get(m, 0.0), m.lower()),
        )
        entries = []
        for m in members:
            entries.append(
                {
                    "name": m,
                    "rank": rank,
                    "h2h": fmt_record(sub[m]) if tied else None,
                }
            )
            rank += 1
        result.append((fmt_record({"W": key[0], "L": key[1], "T": key[2]}), entries))
    return result


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------
# Theme palette: name -> (light value, dark value).
# Streamlit sets `color-scheme: light|dark` on its app container whenever the theme changes
# (Settings > Theme, or the OS preference), so CSS `light-dark()` follows it live with no rerun.
# Browsers without `light-dark()` fall back to prefers-color-scheme / [data-theme="dark"].
THEME_VARS = {
    # Streamlit-style base variables (mirror Streamlit's default light/dark themes)
    "--text-color": ("#31333f", "#fafafa"),
    "--background-color": ("#ffffff", "#0e1117"),
    "--secondary-background-color": ("#f0f2f6", "#262730"),
    # Banner / pill
    "--banner-bg": ("#1f5fbf", "#1f5fbf"),
    "--banner-text": ("#ffffff", "#ffffff"),
    "--banner-border": ("#14407f", "#3b6fd0"),
    "--pill-wrap-bg": ("#e8f0fb", "#0f1b33"),
    "--pill-bg": ("#0d2f63", "#1e3a8a"),
    "--pill-text": ("#ffffff", "#ffffff"),
    # Surfaces
    "--card-bg": ("#ffffff", "#1e293b"),
    "--card-border": ("#14407f", "#334155"),
    "--cell-border": ("#9aa8bd", "#475569"),
    "--cell-bg": ("#ffffff", "#1e293b"),
    "--cell-text": ("#0f172a", "#e2e8f0"),
    "--muted-text": ("#444444", "#94a3b8"),
    "--accent-text": ("#0d2f63", "#93c5fd"),
    # Header / label cells
    "--th-bg": ("#cfe0f7", "#1e3a5f"),
    "--th-text": ("#0f172a", "#e2e8f0"),
    "--label-bg": ("#e8f0fb", "#334155"),
    "--label-text": ("#0f172a", "#f1f5f9"),
    "--totalcol-bg": ("#eef3fa", "#273449"),
    "--totalcol-text": ("#0f172a", "#e2e8f0"),
    # Pastel fills (muted in dark mode, always paired with dark text)
    "--rec-bg": ("#fff2b3", "#e6cf75"),
    "--opp-bg": ("#dce8f8", "#b8cdee"),
    "--mgr-bg": ("#fadbe8", "#e3b3cb"),
    "--win-bg": ("#c9eccb", "#9fd8a8"),
    "--loss-bg": ("#f8cfcf", "#ecaaaa"),
    "--even-bg": ("#fff6cc", "#ecdd9a"),
    "--none-bg": ("#ececec", "#475569"),
    "--none-text": ("#666666", "#cbd5e1"),
    "--total-bg": ("#fff2b3", "#e6cf75"),
    "--totalcol-hi-bg": ("#ffe48a", "#d9b84a"),
    "--diff-bg": ("#e2d4f5", "#c8b6ec"),
    "--avg-bg": ("#d9ecd0", "#9fd8a8"),
    "--pastel-text": ("#0f172a", "#0f172a"),
    # Positive / negative numbers on a normal cell vs. on a pastel cell
    "--pos-text": ("#17692a", "#4ade80"),
    "--neg-text": ("#a61b1b", "#f87171"),
    # Live (in-progress) week row
    "--live-bg": ("#fff1cc", "#4a3b12"),
    "--live-text": ("#7a4b00", "#fde68a"),
    "--pos-on-pastel": ("#14532d", "#14532d"),
    "--neg-on-pastel": ("#7f1d1d", "#7f1d1d"),
}


def _theme_css() -> str:
    light = "".join(f"{k}:{v[0]};" for k, v in THEME_VARS.items())
    dark = "".join(f"{k}:{v[1]};" for k, v in THEME_VARS.items())
    ld = "".join(f"{k}:light-dark({v[0]},{v[1]});" for k, v in THEME_VARS.items())
    return (
        f":root{{{light}}}"
        f"@supports (color:light-dark(#000,#fff)){{:root{{{ld}}}}}"
        "@supports not (color:light-dark(#000,#fff)){"
        f"@media (prefers-color-scheme:dark){{:root{{{dark}}}}}"
        f':root[data-theme="dark"],[data-theme="dark"]{{{dark}}}'
        "}"
    )


STATIC_CSS = """
.banner{background:var(--banner-bg);color:var(--banner-text);text-align:center;font-weight:800;
  font-size:clamp(1.1rem,2.6vw,1.9rem);letter-spacing:.04em;padding:14px 10px;
  border-radius:8px 8px 0 0;border:2px solid var(--banner-border);}
.pill-wrap{text-align:center;background:var(--pill-wrap-bg);border:2px solid var(--banner-border);
  border-top:0;border-radius:0 0 8px 8px;padding:8px 0;margin-bottom:18px;}
.pill{display:inline-block;background:var(--pill-bg);color:var(--pill-text);font-weight:700;
  padding:4px 22px;border-radius:999px;font-size:1rem;border:1px solid var(--banner-border);}
.section-title{font-weight:700;font-size:1.05rem;margin:18px 0 8px;color:var(--text-color);}
.scroll{overflow-x:auto;margin-bottom:14px;}
.card{border:2px solid var(--card-border);border-radius:6px;margin-bottom:14px;overflow-x:auto;
  background:var(--card-bg);}
table.t{border-collapse:collapse;width:100%;font-size:.82rem;color:var(--cell-text) !important;
  background:var(--card-bg) !important;margin:0 !important;}
table.t th,table.t td{border:1px solid var(--cell-border) !important;padding:5px 7px;
  text-align:center;white-space:nowrap;color:var(--cell-text) !important;
  background-color:var(--cell-bg) !important;}
table.t th{background-color:var(--th-bg) !important;color:var(--th-text) !important;font-weight:700;}
table.t td.rec{background-color:var(--rec-bg) !important;color:var(--pastel-text) !important;font-weight:700;}
table.t td.opp{background-color:var(--opp-bg) !important;color:var(--pastel-text) !important;font-weight:600;}
table.t td.mgr{background-color:var(--mgr-bg) !important;color:var(--pastel-text) !important;font-weight:800;}
table.t td.win{background-color:var(--win-bg) !important;color:var(--pastel-text) !important;}
table.t td.loss{background-color:var(--loss-bg) !important;color:var(--pastel-text) !important;}
table.t td.even{background-color:var(--even-bg) !important;color:var(--pastel-text) !important;}
table.t td.none{background-color:var(--none-bg) !important;color:var(--none-text) !important;}
table.t td.label{background-color:var(--label-bg) !important;color:var(--label-text) !important;
  font-weight:700;text-align:left;}
table.t td.totalcol{background-color:var(--totalcol-bg) !important;color:var(--totalcol-text) !important;
  font-weight:700;}
table.t tr.total td{background-color:var(--total-bg) !important;color:var(--pastel-text) !important;
  font-weight:800;}
table.t tr.diff td{background-color:var(--diff-bg) !important;color:var(--pastel-text) !important;
  font-weight:800;}
table.t tr.total td.totalcol,table.t tr.diff td.totalcol{background-color:var(--totalcol-hi-bg) !important;
  color:var(--pastel-text) !important;}
table.t tr.avg td{background-color:var(--avg-bg) !important;color:var(--pastel-text) !important;
  font-weight:800;}
table.t td.rank{background-color:var(--rec-bg) !important;color:var(--pastel-text) !important;
  font-weight:800;}
table.t td.pos{color:var(--pos-text) !important;}
table.t td.neg{color:var(--neg-text) !important;}
table.t tr.diff td.pos{color:var(--pos-on-pastel) !important;}
table.t tr.diff td.neg{color:var(--neg-on-pastel) !important;}
table.t tr.live td{background-color:var(--live-bg) !important;color:var(--live-text) !important;
  font-style:italic;}
table.t td.left{text-align:left;}
table.t td.stand{text-align:left;}
.rk{display:inline-block;min-width:2em;font-weight:800;color:var(--accent-text) !important;}
.sub{float:right;margin-left:12px;color:var(--muted-text) !important;font-weight:600;}
"""

CSS = f"<style>{_theme_css()}{STATIC_CSS}</style>"


def esc(s) -> str:
    return html.escape(str(s))


def table_html(header: list, rows: list, extra_class: str = "") -> str:
    """rows: list of (row_class, [(cell_class, cell_html), ...]). Built without newlines."""
    parts = [f'<div class="scroll {extra_class}"><table class="t"><thead><tr>']
    parts += [f"<th>{h}</th>" for h in header]
    parts.append("</tr></thead><tbody>")
    for row_class, cells in rows:
        parts.append(f'<tr class="{row_class}">')
        parts += [f'<td class="{c}">{v}</td>' for c, v in cells]
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def money(x) -> str:
    return f"{x:.2f}"


def signed(x) -> str:
    return f"{x:+.2f}"


def sign_class(x) -> str:
    return "pos" if x > 0 else "neg" if x < 0 else ""


def render_manager_card(m, managers, overall, h2h) -> str:
    others = [o for o in managers if o != m]
    top = [("rec", fmt_record(overall[m]))] + [("opp", esc(o)) for o in others]
    bottom = [("mgr", esc(m))]
    for o in others:
        rec = h2h[m][o]
        n = rec["W"] + rec["L"] + rec["T"]
        if n == 0:
            cls = "none"
        elif rec["W"] > rec["L"]:
            cls = "win"
        elif rec["W"] < rec["L"]:
            cls = "loss"
        else:
            cls = "even"
        bottom.append((cls, fmt_record(rec)))
    parts = ['<div class="card"><table class="t"><tbody><tr>']
    parts += [f'<td class="{c}">{v}</td>' for c, v in top]
    parts.append("</tr><tr>")
    parts += [f'<td class="{c}">{v}</td>' for c, v in bottom]
    parts.append("</tr></tbody></table></div>")
    return "".join(parts)


def render_same_record_standings(groups) -> str:
    max_len = max(len(entries) for _, entries in groups)
    header = [esc(label) for label, _ in groups]
    rows = []
    for i in range(max_len):
        cells = []
        for _, entries in groups:
            if i < len(entries):
                e = entries[i]
                sub = f'<span class="sub">{e["h2h"]}</span>' if e["h2h"] else ""
                cells.append(("stand", f'<span class="rk">#{e["rank"]}</span>{esc(e["name"])}{sub}'))
            else:
                cells.append(("none", "&nbsp;"))
        rows.append(("", cells))
    return table_html(header, rows)


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------
def render_h2h_tab(season, subtitle, managers, overall, h2h, groups, through_week, live_mode):
    """live_mode: None (no live week in view), "excluded" or "included"."""
    st.markdown(
        f'<div class="banner">HEAD TO HEAD MADNESS TRACKER v{esc(season)}</div>'
        f'<div class="pill-wrap"><span class="pill">{esc(subtitle)}</span></div>',
        unsafe_allow_html=True,
    )
    if live_mode == "excluded":
        st.info(
            f"Week {through_week} is currently in progress — live points are visible in Tab 2, "
            "but W/L outcomes are not factored into standings until the week concludes.",
            icon="⏳",
        )
    elif live_mode == "included":
        st.warning(
            f"Week {through_week} is currently in progress — its live (partial) results are being "
            "counted in H2H records and standings and may change before the week ends.",
            icon="⏳",
        )
    st.caption(
        f"Records through Week {through_week}"
        + (" (completed weeks only)" if live_mode == "excluded" else "")
    )
    cols = st.columns(2)
    for i, m in enumerate(managers):
        with cols[i % 2]:
            st.markdown(render_manager_card(m, managers, overall, h2h), unsafe_allow_html=True)

    st.markdown(
        '<div class="section-title">Standings against those with the same record '
        "(Sorted by current ranking)</div>",
        unsafe_allow_html=True,
    )
    st.markdown(render_same_record_standings(groups), unsafe_allow_html=True)
    st.caption(
        "Ties within a record are broken by head-to-head win % among the tied teams, "
        "then by Total Points For. Regular season only."
    )


def render_scoring_tab(season, managers, display_games, counted_games, weeks, live_week, live_counted):
    """Weekly grids show every game in `display_games` (including live, partial scores);
    totals / averages / leaderboards use only `counted_games`."""
    pf = {}
    pa = {}
    for g in display_games:
        pf[(g["week"], g["manager"])] = g["pf"]
        pa[(g["week"], g["manager"])] = g["pa"]

    pf_tot = {m: 0.0 for m in managers}
    pa_tot = {m: 0.0 for m in managers}
    gp = {m: 0 for m in managers}
    for g in counted_games:
        pf_tot[g["manager"]] += g["pf"]
        pa_tot[g["manager"]] += g["pa"]
        gp[g["manager"]] += 1
    diff = {m: pf_tot[m] - pa_tot[m] for m in managers}

    st.markdown(
        '<div class="banner">SCORING BREAKDOWN</div>'
        f'<div class="pill-wrap"><span class="pill">Regular Season {esc(season)}</span></div>',
        unsafe_allow_html=True,
    )
    if live_week:
        if live_counted:
            st.warning(
                f"Week {live_week} (Live) shows partial, in-progress scores and is included in "
                "totals, averages and records.",
                icon="⏳",
            )
        else:
            st.info(
                f"Week {live_week} (Live) shows partial, in-progress scores. It is excluded from "
                "TOTAL, DIFFERENTIAL, averages and the leaderboard until the week concludes.",
                icon="⏳",
            )

    header = ["Week"] + [esc(m) for m in managers] + ["Total"]

    def grid(data, totals, with_diff):
        rows = []
        for w in weeks:
            vals = [data.get((w, m)) for m in managers]
            played = [v for v in vals if v is not None]
            is_live = w == live_week
            cells = [("label", f"Week {w} (Live)" if is_live else f"Week {w}")]
            cells += [("", money(v) if v is not None else "&ndash;") for v in vals]
            cells.append(("totalcol", money(sum(played)) if played else "&ndash;"))
            rows.append(("live" if is_live else "", cells))
        cells = [("label", "TOTAL")] + [("", money(totals[m])) for m in managers]
        cells.append(("totalcol", money(sum(totals.values()))))
        rows.append(("total", cells))
        if with_diff:
            cells = [("label", "DIFFERENTIAL")]
            cells += [(sign_class(diff[m]), signed(diff[m])) for m in managers]
            cells.append(("totalcol", signed(sum(diff.values()))))
            rows.append(("diff", cells))
        return table_html(header, rows)

    st.markdown('<div class="section-title">Points For (PF)</div>', unsafe_allow_html=True)
    st.markdown(grid(pf, pf_tot, True), unsafe_allow_html=True)

    st.markdown('<div class="section-title">Points Against (PA)</div>', unsafe_allow_html=True)
    st.markdown(grid(pa, pa_tot, False), unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.1, 1, 1])

    with c1:
        st.markdown('<div class="section-title">Point Differential</div>', unsafe_allow_html=True)
        rows = [
            ("", [("left", esc(m)), ("", money(pf_tot[m])), ("", money(pa_tot[m])),
                  (sign_class(diff[m]), signed(diff[m]))])
            for m in managers
        ]
        st.markdown(table_html(["Manager", "PF", "PA", "PF - PA"], rows), unsafe_allow_html=True)

    with c2:
        st.markdown('<div class="section-title">PFR &amp; Average</div>', unsafe_allow_html=True)
        rows = [
            ("", [("left", esc(m)), ("", money(pf_tot[m])),
                  ("", money(pf_tot[m] / gp[m]) if gp[m] else "&ndash;")])
            for m in managers
        ]
        total_games = sum(gp.values())
        league_avg = sum(pf_tot.values()) / total_games if total_games else 0.0
        rows.append(("avg", [("label", "League Average PPG"), ("", ""), ("", money(league_avg))]))
        st.markdown(table_html(["Manager", "PFR", "PPG"], rows), unsafe_allow_html=True)

    with c3:
        st.markdown('<div class="section-title">PFR Leaderboard</div>', unsafe_allow_html=True)
        ranked = sorted(managers, key=lambda m: (-pf_tot[m], m.lower()))
        rows = [
            ("", [("rank", f"#{i}"), ("left", esc(m)), ("", money(pf_tot[m]))])
            for i, m in enumerate(ranked, start=1)
        ]
        st.markdown(table_html(["#", "Manager", "PFR"], rows), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
def main():
    st.markdown(CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.header("⚙️ Settings")
        league_id = st.text_input("Sleeper League ID", value=DEFAULT_LEAGUE_ID).strip()
        if st.button("🔄 Refresh data"):
            st.cache_data.clear()
            st.rerun()
        link = LEAGUE_URL if league_id == DEFAULT_LEAGUE_ID else f"https://sleeper.com/leagues/{league_id}/league"
        st.markdown(f"[Open league in Sleeper]({link})")

    if not league_id.isdigit():
        st.error("Please enter a valid numeric Sleeper League ID.")
        st.stop()

    try:
        with st.spinner("Loading league data from Sleeper..."):
            league = fetch_league(league_id)
            users = fetch_users(league_id)
            rosters = fetch_rosters(league_id)
            settings = league.get("settings") or {}
            playoff_start = int(settings.get("playoff_week_start") or DEFAULT_PLAYOFF_WEEK_START)
            last_week = max(playoff_start - 1, 1)
            matchups = fetch_matchups(league_id, last_week)
    except SleeperAPIError as exc:
        st.error(f"⚠️ {exc}")
        st.stop()
    except Exception as exc:  # pragma: no cover - defensive
        st.error(f"Unexpected error while loading data: {exc}")
        st.stop()

    try:  # non-fatal: without NFL state we simply treat every week as completed
        nfl_state = fetch_nfl_state()
    except SleeperAPIError:
        nfl_state = {}

    if not rosters:
        st.warning("This league has no rosters yet.")
        st.stop()

    active_week = get_active_week(league, nfl_state)
    live_week = active_week if (active_week and is_week_in_progress()) else None

    names = build_names(users, rosters)
    managers = sorted(names.values(), key=str.lower)
    games_all = build_games(matchups, names, live_week)
    if not games_all:
        st.info("No regular-season games have been played yet for this league.")
        st.stop()

    latest_played = max(g["week"] for g in games_all)
    default_week = min(active_week, latest_played) if active_week else latest_played
    default_week = max(1, min(default_week, last_week))

    with st.sidebar:
        st.subheader("📅 Week Controls")
        through_week = st.selectbox(
            "View Data Through Week",
            options=list(range(1, last_week + 1)),
            index=default_week - 1,
            key=f"through_week_{league_id}_{last_week}",
            format_func=lambda w: f"{w} (live)" if w == live_week else str(w),
            help="Only matchups up to this week are processed.",
        )
        count_live = st.checkbox(
            "Count in-progress week in H2H records",
            value=False,
            help="Off: W/L for a week still being played is not counted until it concludes.",
        )
        if active_week:
            status = "in progress" if live_week else "between games / completed"
            st.caption(f"Current NFL week: {active_week} ({status}).")
        st.caption("Data from the public Sleeper API, cached for 1 hour.")

    display_games = [g for g in games_all if g["week"] <= through_week]
    live_in_view = any(g["live"] for g in display_games)
    counted = [g for g in display_games if count_live or not g["live"]]

    overall, h2h = compute_records(counted, managers)

    # Tiebreak PF: Sleeper's season total when nothing is filtered out, otherwise the
    # points from the games actually counted (so point-in-time views stay accurate).
    computed_pf = {m: 0.0 for m in managers}
    for g in counted:
        computed_pf[g["manager"]] += g["pf"]
    total_pf = dict(computed_pf)
    if len(counted) == len(games_all):
        for r in rosters:
            s = r.get("settings") or {}
            if "fpts" in s:
                total_pf[names[r["roster_id"]]] = (
                    float(s.get("fpts") or 0) + float(s.get("fpts_decimal") or 0) / 100
                )

    groups = compute_standings(managers, overall, h2h, total_pf)

    season = league.get("season", "")
    subtitle = DEFAULT_LEAGUE_NAME if league_id == DEFAULT_LEAGUE_ID else league.get("name", DEFAULT_LEAGUE_NAME)

    live_mode = None
    if live_in_view:
        live_mode = "included" if count_live else "excluded"

    tab1, tab2 = st.tabs(["🏈 Head-to-Head Madness", "📊 Points & Weekly Scoring"])
    with tab1:
        render_h2h_tab(season, subtitle, managers, overall, h2h, groups, through_week, live_mode)
    with tab2:
        render_scoring_tab(
            season,
            managers,
            display_games,
            counted,
            list(range(1, through_week + 1)),
            live_week if live_in_view else None,
            count_live,
        )


main()
