"""
Standalone Algo Trading & Terminal Health Checker.
Run this script anytime to verify that MetaTrader 5 is connected,
Algo Trading is enabled, and live quotes are streaming.
"""
import sys
import MetaTrader5 as mt5

def check_terminal_status():
    print("=" * 60)
    print("  MT5 ALGO TRADING & TERMINAL HEALTH CHECKER")
    print("=" * 60)

    # 1. MT5 Initialize
    if not mt5.initialize():
        err_code, err_msg = mt5.last_error()
        print(f"\n❌ FAILED TO CONNECT TO METATRADER 5:")
        print(f"   Error: {err_msg} (Code: {err_code})")
        print("\n👉 ACTION REQUIRED:")
        print("   1. Open the MetaTrader 5 desktop application.")
        print("   2. Log into your PU Prime account.")
        return False

    print("\n✅ MetaTrader 5 Terminal: CONNECTED")

    # 2. Terminal Info
    t_info = mt5.terminal_info()
    if not t_info:
        print("❌ Could not read terminal information.")
        mt5.shutdown()
        return False

    print(f"   • Terminal Build: {t_info.build}")
    print(f"   • Terminal Name:  {t_info.name} ({t_info.company})")

    # 3. Account Info
    acc = mt5.account_info()
    if acc:
        print(f"\n✅ Active Broker Account:")
        print(f"   • Login:    {acc.login}")
        print(f"   • Server:   {acc.server}")
        print(f"   • Currency: {acc.currency}")
        print(f"   • Balance:  {acc.balance:.2f} {acc.currency}")
        print(f"   • Equity:   {acc.equity:.2f} {acc.currency}")
    else:
        print("\n⚠️ No account logged in.")

    # 4. Algo Trading Check
    algo_allowed = getattr(t_info, "trade_allowed", False)
    dlls_allowed = getattr(t_info, "dlls_allowed", False)

    print("\n" + "-" * 60)
    print("  AUTOMATED TRADING & PERMISSIONS:")
    print("-" * 60)

    if algo_allowed:
        print("🟢 Algo Trading Button: ENABLED (Green play icon)")
        print("   Orders from the Telegram copier will execute successfully!")
    else:
        print("🔴 Algo Trading Button: DISABLED (Red square icon)")
        print("\n👉 ACTION REQUIRED:")
        print("   Click the 'Algo Trading' button on the MT5 top toolbar")
        print("   so it turns into a green triangle.")

    if dlls_allowed:
        print("🟢 DLL Imports: ALLOWED")
    else:
        print("⚠️ DLL Imports: DISABLED (Tools -> Options -> Expert Advisors -> Allow DLL imports)")

    # 5. Live Symbol Tick Check
    symbol = "XAUUSD.s"
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)

    print("\n" + "-" * 60)
    print(f"  MARKET WATCH & LIVE QUOTES ({symbol}):")
    print("-" * 60)

    if tick:
        spread_pts = round((tick.ask - tick.bid) / 0.01, 1)
        print(f"🟢 Live Quote Available:")
        print(f"   • Bid:    {tick.bid}")
        print(f"   • Ask:    {tick.ask}")
        print(f"   • Spread: {spread_pts} points ($0.{spread_pts:02.0f})")
    else:
        print(f"⚠️ Could not fetch tick for '{symbol}'.")
        print(f"   Please make sure '{symbol}' is added in MT5 Market Watch.")

    print("\n" + "=" * 60)
    if algo_allowed and tick:
        print("  🎉 ALL CHECKS PASSED: READY FOR LIVE TRADING!")
    else:
        print("  ⚠️ REVIEW WARNINGS ABOVE BEFORE STARTING LIVE COPIER.")
    print("=" * 60 + "\n")

    mt5.shutdown()
    return algo_allowed and (tick is not None)

if __name__ == "__main__":
    check_terminal_status()
