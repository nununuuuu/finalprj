from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from backtesting import Backtest
import pandas as pd
import yfinance as yf
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import traceback
import numpy as np
import math
import os  # 新增 os 模組

# --- 相容性補丁 ---
if not hasattr(pd.Series, 'iteritems'):
    pd.Series.iteritems = pd.Series.items
if not hasattr(np, 'float'):
    np.float = float
# ----------------

from .strategy import SmaRsiStrategy
from .schemas import BacktestRequest, BacktestResponse

app = FastAPI()

# 設定路徑
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"  # [新增] 定義數據資料夾路徑

# [新增] 確保 data 資料夾存在
DATA_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 數值安全轉換
def safe_num(value, decimal=2):
    try:
        if hasattr(value, "item"): value = value.item()
        if pd.isna(value) or math.isnan(value) or np.isinf(value): return 0.0
        return round(float(value), decimal)
    except Exception:
        return 0.0

# 全域錯誤處理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"❌ [CRITICAL ERROR] {str(exc)}")
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": f"Server Error: {str(exc)}"})

@lru_cache(maxsize=64)
def _download_from_yahoo(ticker: str, start: str, end: str):
    print(f"📥 [YFinance] 下載: {ticker}")
    try:
        # 下載數據
        return yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    except Exception:
        return pd.DataFrame()

async def get_yfinance_data(ticker: str, start: str, end: str):
    ticker = ticker.upper().strip()
    if ticker.isdigit() or (len(ticker) == 4 and ticker.isdigit()): ticker += ".TW"
    
    loop = asyncio.get_event_loop()
    try:
        # 非同步下載
        df = await loop.run_in_executor(None, _download_from_yahoo, ticker, start, end)
        if df is None or df.empty: return None, ticker
        
        # --- 數據清洗 ---
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        if 'Adj Close' in df.columns and 'Close' not in df.columns: df.rename(columns={'Adj Close': 'Close'}, inplace=True)
        
        required = ['Open', 'High', 'Low', 'Close', 'Volume']
        if not all(col in df.columns for col in required): return None, ticker
        
        df = df.ffill().bfill()

        # ==========================================
        # 將數據保存為 CSV
        # ==========================================
        csv_filename = f"{ticker}.csv"
        csv_path = DATA_DIR / csv_filename
        
        # 保存 CSV (包含 Index 即日期)
        df.to_csv(csv_path)
        print(f"💾 數據已保存至: {csv_path}")
        # ==========================================
        
        return df, ticker
    except Exception as e:
        print(f"❌ 數據處理錯誤: {e}")
        return None, ticker

@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.post("/api/backtest", response_model=BacktestResponse)
async def run_backtest(params: BacktestRequest):
    df, real_ticker = await get_yfinance_data(params.ticker, params.start_date, params.end_date)
    
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="找不到數據")

    min_bars = max(params.ma_long, 60)
    if len(df) < min_bars:
        raise HTTPException(status_code=400, detail=f"數據不足 {min_bars} 筆")

    bt = Backtest(df, SmaRsiStrategy, cash=params.cash, commission=((params.buy_fee_pct + params.sell_fee_pct)/2)/100)
    
    stats = bt.run(
        n1=params.ma_short, n2=params.ma_long, 
        n_rsi_entry=params.rsi_period_entry, rsi_buy_threshold=params.rsi_buy_threshold,
        n_rsi_exit=params.rsi_period_exit, rsi_sell_threshold=params.rsi_sell_threshold,
        sl_pct=params.stop_loss_pct, tp_pct=params.take_profit_pct
    )
    
    equity_curve = stats._equity_curve
    trades_df = stats._trades
    
    bh_list = []
    if len(df) > 0:
        first = df['Close'].iloc[0]
        if first > 0:
            bh_vals = (df['Close'] / first) * params.cash
            bh_list = [{"time": t.strftime("%Y-%m-%d"), "value": safe_num(v)} for t, v in zip(bh_vals.index, bh_vals)]
            
    equity_list = [{"time": t.strftime("%Y-%m-%d"), "value": safe_num(v)} for t, v in zip(equity_curve.index, equity_curve['Equity'])]
    price_list = [{"time": t.strftime("%Y-%m-%d"), "value": safe_num(v)} for t, v in zip(df.index, df['Close'])]
    
    detailed_trades = []
    chart_trades = []
    
    strategy = stats._strategy
    try:
        rsi_entry_arr = strategy.rsi_entry_line
        rsi_exit_arr = strategy.rsi_exit_line
        sma1_arr = strategy.sma1
        sma2_arr = strategy.sma2
    except AttributeError:
        rsi_entry_arr, rsi_exit_arr, sma1_arr, sma2_arr = [], [], [], []

    max_consecutive_loss = 0
    current_loss = 0

    if not trades_df.empty:
        for i, row in trades_df.iterrows():
            e_idx, x_idx = int(row['EntryBar']), int(row['ExitBar'])
            
            entry_rsi = safe_num(rsi_entry_arr[e_idx]) if len(rsi_entry_arr) > e_idx else 0
            exit_rsi = safe_num(rsi_exit_arr[x_idx]) if len(rsi_exit_arr) > x_idx else 0
            exit_sma1 = safe_num(sma1_arr[x_idx]) if len(sma1_arr) > x_idx else 0
            exit_sma2 = safe_num(sma2_arr[x_idx]) if len(sma2_arr) > x_idx else 0

            detailed_trades.append({
                "entry_date": row['EntryTime'].strftime("%Y-%m-%d"),
                "exit_date": row['ExitTime'].strftime("%Y-%m-%d"),
                "entry_price": safe_num(row['EntryPrice']),
                "exit_price": safe_num(row['ExitPrice']),
                "size": int(abs(row['Size'])),
                "pnl": safe_num(row['PnL'], 0),
                "return_pct": safe_num(row['ReturnPct'] * 100),
                "entry_rsi": entry_rsi, "exit_rsi": exit_rsi,
                "exit_sma_short": exit_sma1, "exit_sma_long": exit_sma2
            })

            chart_trades.append({"time": row['EntryTime'].strftime("%Y-%m-%d"), "price": safe_num(row['EntryPrice']), "type": "buy"})
            chart_trades.append({"time": row['ExitTime'].strftime("%Y-%m-%d"), "price": safe_num(row['ExitPrice']), "type": "sell"})

            if row['PnL'] < 0:
                current_loss += 1
                max_consecutive_loss = max(max_consecutive_loss, current_loss)
            else:
                current_loss = 0

    heatmap_data = {}
    if not equity_curve.empty:
        eq_df = pd.DataFrame(equity_curve['Equity'])
        m_df = eq_df.resample('ME').last() if hasattr(eq_df, 'resample') else eq_df
        m_df['Return'] = m_df['Equity'].pct_change() * 100
        for date, row in m_df.iterrows():
            if not (pd.isna(row['Return']) or np.isinf(row['Return'])):
                if date.year not in heatmap_data: heatmap_data[date.year] = {}
                heatmap_data[date.year][date.month] = safe_num(row['Return'])

    return {
        "ticker": real_ticker,
        "final_equity": safe_num(stats["Equity Final [$]"], 0),
        "total_return": safe_num(stats["Return [%]"]),
        "annual_return": safe_num(stats["Return (Ann.) [%]"]),
        "buy_and_hold_return": safe_num(stats["Buy & Hold Return [%]"]),
        "win_rate": safe_num(stats["Win Rate [%]"]),
        "total_trades": int(stats["# Trades"]),
        "avg_pnl": safe_num(trades_df['PnL'].mean(), 0) if not trades_df.empty else 0,
        "max_consecutive_loss": max_consecutive_loss,
        
        "max_drawdown": safe_num(stats["Max. Drawdown [%]"]),
        "sharpe_ratio": safe_num(stats["Sharpe Ratio"]),
        
        "equity_curve": equity_list, "price_data": price_list,
        "trades": chart_trades, "heatmap_data": heatmap_data,
        "buy_and_hold_curve": bh_list, "detailed_trades": detailed_trades
    }