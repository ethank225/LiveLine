"""All reporting/printing functions for backtest results."""

from datetime import datetime

from backtests.constants import (
    EXIT_WINDOWS, ENTRY_OFFSET, ALPHAS, STOP_LEVELS, CONTRACTS,
    HIGH_LEV_ML_THRESHOLD, HIGH_LEV_OU_PROXIMITY,
    WINDOW_PCTS, WINDOW_LABELS, EVENT_ORDER,
    get_clean_window, price_at_window_pct,
)


def print_play_log(records, game_info):
    print(f"\n{'=' * 90}")
    print(f"{game_info['away_abbr']} @ {game_info['home_abbr']}  "
          f"(Final: {game_info.get('away_final', '?')}-{game_info.get('home_final', '?')})")
    print(f"{'=' * 90}")
    print(f"{'Time':>12} {'Inn':>5} {'Outs':>4} {'Rnrs':>5} {'Score':>6} "
          f"{'Event':>4} {'WE bef':>7} {'WE aft':>7} {'ML Δ':>7} {'O/U Δ':>7}  MLB Event")
    print("-" * 90)

    for rec in records:
        time_str = ""
        if rec.timestamp:
            try:
                dt = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M:%S")
            except Exception:
                pass
        half_char = "T" if rec.half == "top" else "B"
        score_str = f"{rec.away_score}-{rec.home_score}"
        print(f"{time_str:>12} {half_char}{rec.inning:>3}  {rec.outs_before:>4} "
              f"{rec.runners_before:>5} {score_str:>6} "
              f"{rec.mapped_event:>4} {rec.we_before:>7.3f} {rec.we_after:>7.3f} "
              f"{rec.ml_delta:>+7.3f} {rec.ou_85_delta:>+7.3f}  {rec.mlb_event}")


def _print_gap_distribution(gaps, label):
    gaps.sort()
    n = len(gaps)
    if n == 0:
        return
    avg = sum(gaps) / n
    median = gaps[n // 2]
    p25 = gaps[n // 4]
    p75 = gaps[3 * n // 4]
    print(f"\n  {label} (n={n}):")
    print(f"    Average: {avg:.0f}s ({avg / 60:.1f} min)  |  "
          f"Median: {median}s  |  P25: {p25}s  |  P75: {p75}s")
    buckets = [(0, 15), (15, 30), (30, 60), (60, 120), (120, 300), (300, 9999)]
    print(f"    {'Window':>12} {'Count':>6} {'%':>6}  Implication")
    print(f"    {'-' * 55}")
    for lo, hi in buckets:
        count = sum(1 for g in gaps if lo <= g < hi)
        pct = 100 * count / n
        label_str = f"{lo}-{hi}s" if hi < 9999 else f"{lo}s+"
        note = ""
        if hi <= 30:
            note = "← windows past this contaminated"
        elif lo >= 60:
            note = f"← clean through t+{lo - 5}s"
        print(f"    {label_str:>12} {count:>6} {pct:>5.1f}%  {note}")


def print_play_gap_stats(synced):
    ab_gaps = [s["time_to_next_ab"] for s in synced if s.get("time_to_next_ab") is not None]
    pitch_gaps = [s["time_to_next_pitch"] for s in synced if s.get("time_to_next_pitch") is not None]
    if not ab_gaps and not pitch_gaps:
        return
    print(f"\n{'=' * 60}")
    print("TIME BETWEEN EVENTS")
    print(f"{'=' * 60}")
    _print_gap_distribution(ab_gaps, "At-bat to at-bat (result → result)")
    _print_gap_distribution(pitch_gaps, "At-bat to next pitch (result → first pitch of next AB)")


def _accuracy_block(items):
    n = len(items)
    errors = [s["error"] for s in items]
    mae = sum(abs(e) for e in errors) / n
    rmse = (sum(e ** 2 for e in errors) / n) ** 0.5
    bias = sum(errors) / n
    dc = sum(1 for s in items if s["predicted_ml_delta"] != 0
             and (s["predicted_ml_delta"] > 0) == (s["actual_price_move"] > 0))
    dt = sum(1 for s in items if s["predicted_ml_delta"] != 0)
    dp = f"{100 * dc / dt:.1f}%" if dt else "n/a"
    print(f"  Plays: {n}  |  Bias: {bias:+.4f}  |  MAE: {mae:.4f}  |  "
          f"RMSE: {rmse:.4f}  |  Dir: {dp}")
    print(f"  {'Event':>5} {'Count':>5} {'Pred':>8} {'Actual':>8} {'MAE':>7} {'Dir%':>6}")
    print(f"  {'-' * 45}")
    by_ev: dict[str, list] = {}
    for s in items:
        by_ev.setdefault(s["event"], []).append(s)
    for ev in ["HR", "2B", "1B", "BB", "DP", "OUT", "K"]:
        evs = by_ev.get(ev)
        if not evs:
            continue
        pa = sum(s["predicted_ml_delta"] for s in evs) / len(evs)
        aa = sum(s["actual_price_move"] for s in evs) / len(evs)
        em = sum(abs(s["error"]) for s in evs) / len(evs)
        edc = sum(1 for s in evs if s["predicted_ml_delta"] != 0
                  and (s["predicted_ml_delta"] > 0) == (s["actual_price_move"] > 0))
        edt = sum(1 for s in evs if s["predicted_ml_delta"] != 0)
        edp = f"{100 * edc / edt:.0f}%" if edt else "n/a"
        print(f"  {ev:>5} {len(evs):>5} {pa:>+8.4f} {aa:>+8.4f} {em:>7.4f} {edp:>6}")


def print_accuracy_summary(synced):
    if not synced:
        print("\nNo synced Kalshi data to analyze.")
        return
    print(f"\n{'=' * 70}")
    print("BACKTEST ACCURACY — Model vs Kalshi Price Moves")
    print(f"{'=' * 70}")
    by_market: dict[str, list] = {}
    for s in synced:
        by_market.setdefault(s.get("market_type", "moneyline"), []).append(s)
    for mt in ["moneyline", "over_under", "spread"]:
        items = by_market.get(mt)
        if not items:
            continue
        n_markets = len(set(s.get("market_ticker", "") for s in items
                             if s.get("market_ticker")))
        noun = "dynamic lines" if mt == "over_under" else "markets"
        if mt == "over_under":
            filtered = [s for s in items if s.get("is_high_lev_ou")]
            filter_desc = f"total within {HIGH_LEV_OU_PROXIMITY} of line"
        else:
            filtered = [s for s in items if s.get("is_high_lev_ml")]
            filter_desc = f"|delta| >= {HIGH_LEV_ML_THRESHOLD}"
        print(f"\n{mt.upper()} — ALL ({n_markets} {noun})")
        _accuracy_block(items)
        if filtered:
            print(f"\n{mt.upper()} — HIGH LEVERAGE ({len(filtered)} plays, {filter_desc})")
            _accuracy_block(filtered)


def _compute_pct_stats(evs, moves_key="clean_moves"):
    """Compute stats at each percentage-of-clean-window bucket."""
    stats: dict[str, dict] = {}

    for pct, label in zip(WINDOW_PCTS, WINDOW_LABELS):
        vals = []
        correct = 0
        total_dir = 0

        for s in evs:
            cw = get_clean_window(s)
            if cw is None or cw <= 0:
                continue
            move = price_at_window_pct(s.get(moves_key, {}), cw, pct)
            if move is None:
                continue
            vals.append(move)
            pred = s["predicted_ml_delta"]
            if pred != 0:
                total_dir += 1
                if (pred > 0) == (move > 0):
                    correct += 1

        if not vals:
            continue
        stats[label] = {
            "avg": sum(vals) / len(vals),
            "count": len(vals),
            "dir_correct": correct,
            "dir_total": total_dir,
        }
    return stats


def _timing_block(items, label):
    by_event: dict[str, list] = {}
    for s in items:
        by_event.setdefault(s["event"], []).append(s)

    summary_rows = []

    for event in EVENT_ORDER:
        evs = by_event.get(event)
        if not evs:
            continue

        n = len(evs)
        pred_avg = sum(s["predicted_ml_delta"] for s in evs) / n

        pct_stats = _compute_pct_stats(evs, "clean_moves")
        if not pct_stats:
            continue

        # Peak is directional: max positive for up-predictions, min (most
        # negative) for down-predictions. Using abs() here collapses the two
        # and can mis-label a bucket where price moved the wrong way as "peak".
        if pred_avg >= 0:
            peak_label = max(pct_stats, key=lambda k: pct_stats[k]["avg"])
        else:
            peak_label = min(pct_stats, key=lambda k: pct_stats[k]["avg"])
        peak_val = pct_stats[peak_label]["avg"]
        peak_abs = abs(peak_val)

        # Best directional accuracy bucket
        best_dir_label = max(
            pct_stats,
            key=lambda k: (pct_stats[k]["dir_correct"] / pct_stats[k]["dir_total"]
                           if pct_stats[k]["dir_total"] > 0 else 0))
        bd = pct_stats[best_dir_label]
        bdp = 100 * bd["dir_correct"] / bd["dir_total"] if bd["dir_total"] else 0

        print(f"\n  {event} ({n} events):  predicted={pred_avg:+.4f}")
        print(f"    Peak: {peak_val:+.4f} at {peak_label}")

        for lbl in WINDOW_LABELS:
            ws = pct_stats.get(lbl)
            if not ws:
                print(f"    {lbl:>8}:    ---")
                continue
            avg = ws["avg"]
            pct_of_peak = (abs(avg) / peak_abs * 100) if peak_abs > 0.0001 else 0
            dp = (f"{100 * ws['dir_correct'] / ws['dir_total']:.0f}%"
                  if ws["dir_total"] > 0 else "n/a")
            marker = " ← peak" if lbl == peak_label else ""
            print(f"    {lbl:>8}: {avg:>+7.4f}  ({pct_of_peak:5.1f}%)  "
                  f"dir={dp:>4s}  n={ws['count']}{marker}")

        summary_rows.append({
            "event": event, "n": n, "predicted": pred_avg,
            "peak_val": peak_val, "peak_label": peak_label,
            "best_dir_label": best_dir_label, "best_dir_pct": bdp,
        })

    if summary_rows:
        print(f"\n  {'Event':>5} {'Count':>5} {'Predicted':>10} "
              f"{'Peak':>8} {'Peak @':>8} {'Best Dir':>9} {'Dir @':>8}")
        print(f"  {'-' * 60}")
        for r in summary_rows:
            print(f"  {r['event']:>5} {r['n']:>5} {r['predicted']:>+10.4f} "
                  f"{r['peak_val']:>+8.4f} {r['peak_label']:>8s} "
                  f"{r['best_dir_pct']:>8.0f}% {r['best_dir_label']:>8s}")


def print_timing_analysis(synced):
    if not synced:
        return
    print(f"\n{'=' * 70}")
    print(f"TIMING ANALYSIS — Entry at t-{ENTRY_OFFSET}s")
    print(f"{'=' * 70}")
    by_market: dict[str, list] = {}
    for s in synced:
        by_market.setdefault(s.get("market_type", "moneyline"), []).append(s)
    for mt in ["moneyline", "over_under", "spread"]:
        items = by_market.get(mt)
        if not items:
            continue
        n_markets = len(set(s.get("market_ticker", "") for s in items
                             if s.get("market_ticker")))
        noun = "dynamic lines" if mt == "over_under" else "markets"
        if mt == "over_under":
            filtered = [s for s in items if s.get("is_high_lev_ou")]
            filter_desc = f"total within {HIGH_LEV_OU_PROXIMITY} of line"
        else:
            filtered = [s for s in items if s.get("is_high_lev_ml")]
            filter_desc = f"|delta| >= {HIGH_LEV_ML_THRESHOLD}"
        print(f"\n{'=' * 70}")
        print(f"{mt.upper()} — ALL ({n_markets} {noun})")
        _timing_block(items, mt)
        if filtered:
            print(f"\n{'=' * 70}")
            print(f"{mt.upper()} — HIGH LEVERAGE ({len(filtered)} plays, {filter_desc})")
            _timing_block(filtered, mt)


def print_stop_loss_analysis(synced):
    """Report stop loss simulation across stop levels at the best alpha."""
    plays = [s for s in synced
             if s.get("stop_loss_sim") and s["predicted_ml_delta"] != 0]
    if not plays:
        print("\nNo stop loss simulation data.")
        return

    contracts = 100
    alpha = ALPHAS[-1]  # best alpha from prior analysis (0.6)

    # "none" baseline
    none_pnls = [s["stop_loss_sim"]["none"]["pnl"] for s in plays]
    none_total = sum(p * contracts for p in none_pnls)

    print(f"\n{'=' * 80}")
    print(f"STOP LOSS ANALYSIS — {len(plays)} trades at α={alpha}, {contracts} contracts")
    print(f"{'=' * 80}")

    print(f"\n{'Stop':>6} {'Stopped':>8} {'False%':>7} {'Target':>7} "
          f"{'Expired':>8} {'Total P&L':>10} {'vs None':>10}")
    print("-" * 65)

    for sl in STOP_LEVELS:
        sl_cents = f"{sl * 100:.0f}¢"
        outcomes = [s["stop_loss_sim"].get(sl) for s in plays]
        outcomes = [o for o in outcomes if o]

        stopped = [o for o in outcomes if o["outcome"] == "stopped"]
        targets = [o for o in outcomes if o["outcome"] == "target"]
        expired = [o for o in outcomes if o["outcome"] == "expired"]

        false_stops = sum(1 for o in stopped if o["would_have_hit"])
        false_pct = (100 * false_stops / len(stopped)) if stopped else 0

        all_pnls = [o["pnl"] for o in outcomes]
        total = sum(p * contracts for p in all_pnls)
        vs_none = total - none_total

        print(f"{sl_cents:>6} {len(stopped):>8} {false_pct:>6.0f}% {len(targets):>7} "
              f"{len(expired):>8} ${total:>+9.0f} ${vs_none:>+9.0f}")

    # None row
    none_targets = sum(1 for s in plays if s["stop_loss_sim"]["none"]["outcome"] == "target")
    none_expired = sum(1 for s in plays if s["stop_loss_sim"]["none"]["outcome"] == "expired")
    print(f"{'none':>6} {'0':>8} {'0':>6}% {none_targets:>7} "
          f"{none_expired:>8} ${none_total:>+9.0f} {'$0':>10}")

    # Per-event breakdown at "none" (timer only)
    print(f"\n  Per-event at no stop loss (timer only, α={alpha}):")
    print(f"  {'Event':>5} {'Trades':>6} {'Target':>7} {'Expired':>8} "
          f"{'Avg P&L':>8} {'Total':>8}")
    print(f"  {'-' * 48}")

    by_event: dict[str, list] = {}
    for s in plays:
        by_event.setdefault(s["event"], []).append(s)

    for event in ["HR", "2B", "1B", "BB", "DP", "OUT", "K"]:
        evs = by_event.get(event)
        if not evs:
            continue
        pnls = [s["stop_loss_sim"]["none"]["pnl"] for s in evs]
        tgt = sum(1 for s in evs if s["stop_loss_sim"]["none"]["outcome"] == "target")
        exp = sum(1 for s in evs if s["stop_loss_sim"]["none"]["outcome"] == "expired")
        avg = sum(pnls) / len(pnls)
        total = sum(p * contracts for p in pnls)
        print(f"  {event:>5} {len(evs):>6} {tgt:>7} {exp:>8} "
              f"{avg:>+8.4f} ${total:>+7.0f}")


from backtests.constants import BLOWOUT_THRESHOLD, THIN_THRESHOLDS


def _compute_flags(s):
    """Compute flag list for a synced record."""
    flags = []
    pred = s["predicted_ml_delta"]
    actual = s.get("actual_price_move", 0)

    if abs(pred) > 0.15:
        flags.append("large_delta")
    if (s.get("half") == "bot" and s.get("inning", 0) >= 9
            and s.get("outs", 0) == 2 and s["event"] in ("K", "OUT", "DP")):
        flags.append("game_ending_9th")
    if pred != 0 and actual != 0 and (pred > 0) != (actual > 0) and abs(actual) > 0.05:
        flags.append("wrong_direction")

    mt = s.get("market_type", "moneyline")
    threshold = THIN_THRESHOLDS.get(mt, 50)
    if s.get("n_window_trades", 999) < threshold:
        flags.append("thin_liquidity")

    score_diff = s.get("home_score", 0) - s.get("away_score", 0)
    if abs(score_diff) >= BLOWOUT_THRESHOLD:
        flags.append("blowout")

    return flags


def _is_blowout(s):
    """Only moneyline trades in blowouts are filtered. O/U and spread always trade."""
    if s.get("market_type", "moneyline") != "moneyline":
        return False
    return abs(s.get("home_score", 0) - s.get("away_score", 0)) >= BLOWOUT_THRESHOLD


def write_stop_loss_csv(synced, output_path):
    """Write detailed stop loss events and flagged trades to CSV."""
    import csv

    plays = [s for s in synced if s.get("stop_loss_sim") and s["predicted_ml_delta"] != 0]
    if not plays:
        return

    rows = []
    for s in plays:
        sl_sim = s["stop_loss_sim"]
        for sl in STOP_LEVELS:
            result = sl_sim.get(sl)
            if not result or result["outcome"] != "stopped":
                continue

            flags = _compute_flags(s)
            if result["would_have_hit"]:
                flags.append("false_stop")

            pd = s["predicted_ml_delta"]
            rows.append({
                "timestamp": s["timestamp"],
                "inning": s["inning"], "half": s["half"],
                "outs": s.get("outs", ""), "runners": s.get("runners", ""),
                "home_score": s.get("home_score", ""), "away_score": s.get("away_score", ""),
                "event": s["event"], "mlb_event": s["mlb_event"],
                "description": s.get("description", ""),
                "market_ticker": s.get("market_ticker", ""),
                "market_type": s.get("market_type", ""),
                "entry_price": s["price_before"],
                "predicted_delta": pd,
                "direction": "UP" if pd > 0 else "DOWN",
                "we_before": s.get("we_before", ""),
                "we_after": s.get("we_after", ""),
                "stop_level": f"{sl * 100:.0f}c",
                "price_at_stop": result.get("price_at_stop", ""),
                "min_price": result.get("min_price", ""),
                "max_price": result.get("max_price", ""),
                "false_stop": result["would_have_hit"],
                "pnl": result["pnl"],
                "n_window_trades": s.get("n_window_trades", ""),
                "flags": "|".join(flags),
            })

    if not rows:
        print("No stop loss triggers to write.")
        return

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Stop loss events CSV: {output_path} ({len(rows)} rows)")


def print_flagged_summary(synced):
    """Print summary of flagged trades and top 5 worst by P&L."""
    plays = [s for s in synced if s.get("stop_loss_sim") and s["predicted_ml_delta"] != 0]
    if not plays:
        return

    flagged = []
    for s in plays:
        fl = _compute_flags(s)
        if fl:
            flagged.append((s, fl))

    blowout_count = sum(1 for s in plays if _is_blowout(s))
    clean_plays = [s for s in plays if not _is_blowout(s)]

    print(f"\n{'=' * 80}")
    print(f"FLAGGED TRADES — {len(flagged)} out of {len(plays)} trades")
    print(f"Blowouts filtered (|score_diff| >= {BLOWOUT_THRESHOLD}): {blowout_count}")
    print(f"Clean trades: {len(clean_plays)}")
    print(f"{'=' * 80}")

    from collections import Counter
    flag_counts = Counter()
    for _, fl in flagged:
        for f in fl:
            flag_counts[f] += 1

    for flag, count in flag_counts.most_common():
        print(f"  {flag}: {count}")

    # P&L comparison: all vs no-blowouts
    if clean_plays and clean_plays[0].get("stop_loss_sim", {}).get("none"):
        all_pnl = sum(s["stop_loss_sim"]["none"]["pnl"] for s in plays) * 100
        clean_pnl = sum(s["stop_loss_sim"]["none"]["pnl"] for s in clean_plays) * 100
        print(f"\n  P&L (no stop, 100 contracts):")
        print(f"    All trades:      ${all_pnl:+.0f}")
        print(f"    No blowouts:     ${clean_pnl:+.0f}  (delta: ${clean_pnl - all_pnl:+.0f})")

    # Top 5 worst trades
    worst = sorted(plays, key=lambda s: s["stop_loss_sim"]["none"]["pnl"])[:5]

    print(f"\n  TOP 5 WORST TRADES (by timer-exit P&L):")
    print(f"  {'P&L':>7} {'Event':>4} {'Inn':>5} {'Score':>6} {'Pred':>7} "
          f"{'Actual':>7} {'Flags':>25}  Description")
    print(f"  {'-' * 85}")

    for s in worst:
        pnl = s["stop_loss_sim"]["none"]["pnl"]
        pred = s["predicted_ml_delta"]
        actual = s.get("actual_price_move", 0)
        half_c = "T" if s["half"] == "top" else "B"
        score = f"{s.get('away_score', '?')}-{s.get('home_score', '?')}"
        fl = _compute_flags(s)
        flags_str = ",".join(fl) if fl else "-"
        desc = s.get("description", "")[:40]

        print(f"  {pnl:>+7.4f} {s['event']:>4} {half_c}{s['inning']:>3} "
              f"{score:>6} {pred:>+7.4f} {actual:>+7.4f} {flags_str:>25}  {desc}")
