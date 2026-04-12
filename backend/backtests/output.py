"""CSV output and trade trace printing."""

import csv
from datetime import datetime, timezone
from pathlib import Path

from backtests.constants import EXIT_WINDOWS, ALPHAS, ENTRY_OFFSET

OUTPUT_DIR = Path(__file__).parent / "results"


def save_csv(records, game_info, synced):
    """Save play-by-play and synced comparison CSVs."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    game_id = game_info["game_id"]
    prefix = f"{game_info['away_abbr']}_{game_info['home_abbr']}_{game_id}"

    pbp_path = OUTPUT_DIR / f"{prefix}_plays.csv"
    with open(pbp_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "inning", "half", "outs", "runners", "home_score",
            "away_score", "mlb_event", "mapped_event", "we_before", "we_after",
            "ml_delta", "ou_85_before", "ou_85_after", "ou_85_delta", "description",
        ])
        for r in records:
            writer.writerow([
                r.timestamp, r.inning, r.half, r.outs_before, r.runners_before,
                r.home_score, r.away_score, r.mlb_event, r.mapped_event,
                round(r.we_before, 6), round(r.we_after, 6), round(r.ml_delta, 6),
                round(r.ou_85_before, 6), round(r.ou_85_after, 6),
                round(r.ou_85_delta, 6), r.description,
            ])
    print(f"\nSaved play-by-play: {pbp_path}")

    if synced:
        sync_path = OUTPUT_DIR / f"{prefix}_synced.csv"
        window_cols = [f"move_t+{w}s" for w in EXIT_WINDOWS]
        header = [
            "market_type", "market_label", "market_ticker",
            "timestamp", "inning", "half", "event", "mlb_event",
            "predicted_delta", "price_before", "we_before",
            "is_high_lev_ml", "is_high_lev_ou", "leverage_score",
            "total_runs", "time_to_next_ab", "time_to_next_pitch",
        ] + window_cols + [f"clean_t+{w}s" for w in EXIT_WINDOWS]

        for a in ALPHAS:
            header.extend([f"fill_{a}_hit", f"fill_{a}_pnl", f"fill_{a}_time"])

        with open(sync_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for s in synced:
                row = [
                    s.get("market_type", ""), s.get("market_label", ""),
                    s.get("market_ticker", ""),
                    s["timestamp"], s["inning"], s["half"], s["event"],
                    s["mlb_event"], s["predicted_ml_delta"], s["price_before"],
                    s["we_before"],
                    s.get("is_high_lev_ml", ""), s.get("is_high_lev_ou", ""),
                    s.get("leverage_score", ""), s.get("total_runs", ""),
                    s.get("time_to_next_ab", ""), s.get("time_to_next_pitch", ""),
                ]
                for w in EXIT_WINDOWS:
                    row.append(s["moves"].get(w, ""))
                for w in EXIT_WINDOWS:
                    row.append(s.get("clean_moves", {}).get(w, ""))
                fsim = s.get("fill_sim", {})
                for a in ALPHAS:
                    fd = fsim.get(a, {})
                    row.extend([fd.get("filled", ""), fd.get("pnl", ""),
                                fd.get("fill_time", "")])
                writer.writerow(row)
        print(f"Saved synced data:  {sync_path}")


def print_trace(synced, n, alpha=0.5):
    """Print step-by-step walkthrough for the N highest-delta trades."""
    with_trace = [s for s in synced if s.get("_trace") and s["predicted_ml_delta"] != 0]
    if not with_trace:
        print("\nNo trace data available (run with --trace N)")
        return

    with_trace.sort(key=lambda s: abs(s["predicted_ml_delta"]), reverse=True)
    picks = with_trace[:n]

    print(f"\n{'=' * 80}")
    print(f"TRADE TRACE — Top {len(picks)} by |predicted delta| at α={alpha}")
    print(f"{'=' * 80}")

    for i, s in enumerate(picks):
        tr = s["_trace"]
        sign = tr["sign"]
        predicted = s["predicted_ml_delta"]
        entry_price = s["price_before"]
        target_move = alpha * abs(predicted)

        if (predicted * sign) > 0:
            target_price = entry_price + target_move
            direction = "UP"
        else:
            target_price = entry_price - target_move
            direction = "DOWN"

        try:
            play_dt = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
            play_str = play_dt.strftime("%H:%M:%S")
        except Exception:
            play_str = s["timestamp"]

        half_char = "T" if s["half"] == "top" else "B"
        score_str = f"{tr['away_score']}-{tr['home_score']}"

        print(f"\n{'─' * 80}")
        print(f"Trade #{i + 1}: {s['mlb_event']} ({s['event']})")
        print(f"  Game state:  {half_char}{s['inning']}  {tr['outs']} out  "
              f"runners={tr['runners']}  score={score_str}")
        print(f"  Timestamp:   {play_str} UTC")
        print(f"  Market:      {s.get('market_label', '')} ({s.get('market_ticker', '')})")

        entry_trade = tr["entry_trade"]
        if entry_trade:
            et_dt = datetime.fromtimestamp(entry_trade[0], tz=timezone.utc)
            print(f"\n  ENTRY (t-{ENTRY_OFFSET}s):")
            print(f"    Last trade: ${entry_trade[1]:.4f} at {et_dt.strftime('%H:%M:%S')}")
            print(f"    Entry price: ${entry_price:.4f}")
        else:
            print(f"\n  ENTRY: ${entry_price:.4f} (no exact trade found)")

        print(f"\n  MODEL:")
        print(f"    Predicted delta: {predicted:+.4f} (price should move {direction})")
        print(f"    α={alpha} → target move: {target_move:.4f}")
        print(f"    Target price:    ${target_price:.4f}")

        clean_secs = tr["clean_deadline_ts"] - tr["play_ts"]
        print(f"\n  CLEAN WINDOW: {clean_secs}s until next pitch")

        window_trades = tr["window_trades"]
        print(f"\n  KALSHI TRADES IN WINDOW ({len(window_trades)}):")

        fill_trade = None
        fill_result = s.get("fill_sim", {}).get(alpha, {})

        for j, (ts, price) in enumerate(window_trades):
            offset = ts - tr["play_ts"]
            move_from_entry = (price - entry_price) * sign

            hit = ""
            if fill_trade is None:
                if direction == "UP" and price >= target_price:
                    hit = " ← TARGET HIT"
                    fill_trade = (ts, price)
                elif direction == "DOWN" and price <= target_price:
                    hit = " ← TARGET HIT"
                    fill_trade = (ts, price)

            if len(window_trades) <= 20 or hit or j < 3 or j >= len(window_trades) - 2:
                print(f"    [{offset:>4d}s] ${price:.4f}  "
                      f"(move: {move_from_entry:+.4f}){hit}")
            elif j == 3:
                print(f"    ... ({len(window_trades) - 5} more trades) ...")

        print(f"\n  RESULT:")
        if fill_result.get("filled"):
            print(f"    TARGET FILLED at t+{fill_result['fill_time']}s")
            print(f"    Profit: ${target_move:.4f} × 100 contracts = "
                  f"${target_move * 100:+.2f}")
        else:
            forced_price = window_trades[-1][1] if window_trades else entry_price
            forced_move = (forced_price - entry_price) * sign
            print(f"    TARGET NOT HIT — forced exit at ${forced_price:.4f}")
            print(f"    P&L: {forced_move:+.4f} × 100 contracts = "
                  f"${forced_move * 100:+.2f}")
