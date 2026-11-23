//+------------------------------------------------------------------+
//|                                          ForexAIAnalyzerEA.mq5   |
//|  Same rules as https://sanchirz.github.io/forex-ai-analyzer/     |
//|                                                                  |
//|  Entry : EMA20 vs EMA50 trend + MACD(12,26,9) + RSI(14) agree    |
//|          (signal on the candle where all three first agree)      |
//|  Filter: H1 EMA20/EMA50 trend must point the same way            |
//|  Exit  : SL beyond the last swing (10 bars) +-0.25 ATR,          |
//|          kept 0.75-3.5 ATR; TP1 = 1R closes half and moves SL    |
//|          to entry; TP2 = 3R closes the rest                      |
//|  Risk  : lot from % of balance, one position at a time,          |
//|          stop for the day after 3 losses or -3%                  |
//|                                                                  |
//|  TEST ON A DEMO ACCOUNT FIRST. Not financial advice.             |
//+------------------------------------------------------------------+
#property copyright "Forex AI Analyzer"
#property version   "1.00"

#include <Trade/Trade.mqh>

//--- inputs
input double InpRiskPercent    = 1.0;    // Risk per trade, % of balance
input int    InpSwingBars      = 10;     // Stop loss beyond the swing of the last N bars
input double InpSwingBufferAtr = 0.25;   // Extra distance beyond the swing, x ATR
input double InpMinStopAtr     = 0.75;   // Stop distance at least this x ATR
input double InpMaxStopAtr     = 3.5;    // Stop distance at most this x ATR
input double InpRewardRisk     = 3.0;    // Final take profit (TP2) = SL distance x this
input bool   InpPartialAtTP1   = true;   // At 1R: close half and move SL to entry
input bool   InpUseH1Filter    = true;   // Trade only with the H1 trend
input int    InpMaxLossesDay   = 3;      // Stop for the day after N losses
input double InpMaxDayLossPct  = 3.0;    // Stop for the day after this % loss
input int    InpMaxSpreadPts   = 0;      // Skip if spread > points (0 = off)
input bool   InpAllowSmallLot  = false;  // Allow min lot even if risk is above target
input long   InpMagic          = 26112101;

//--- handles
int hEma20, hEma50, hMacd, hRsi, hAtr, hH1Ema20, hH1Ema50;
CTrade   trade;
datetime lastBar = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(_Period == PERIOD_M1)
     {
      Alert("ForexAIAnalyzerEA: M1 is not supported (spread too large). Use an M5, M15 or H1 chart.");
      return(INIT_PARAMETERS_INCORRECT);
     }

   hEma20   = iMA(_Symbol, _Period, 20, 0, MODE_EMA, PRICE_CLOSE);
   hEma50   = iMA(_Symbol, _Period, 50, 0, MODE_EMA, PRICE_CLOSE);
   hMacd    = iMACD(_Symbol, _Period, 12, 26, 9, PRICE_CLOSE);
   hRsi     = iRSI(_Symbol, _Period, 14, PRICE_CLOSE);
   hAtr     = iATR(_Symbol, _Period, 14);
   hH1Ema20 = iMA(_Symbol, PERIOD_H1, 20, 0, MODE_EMA, PRICE_CLOSE);
   hH1Ema50 = iMA(_Symbol, PERIOD_H1, 50, 0, MODE_EMA, PRICE_CLOSE);

   if(hEma20 == INVALID_HANDLE || hEma50 == INVALID_HANDLE || hMacd == INVALID_HANDLE ||
      hRsi == INVALID_HANDLE || hAtr == INVALID_HANDLE ||
      hH1Ema20 == INVALID_HANDLE || hH1Ema50 == INVALID_HANDLE)
     {
      Print("Indicator handle error: ", GetLastError());
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetDeviationInPoints(30);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hEma20);   IndicatorRelease(hEma50);
   IndicatorRelease(hMacd);    IndicatorRelease(hRsi);
   IndicatorRelease(hAtr);     IndicatorRelease(hH1Ema20);
   IndicatorRelease(hH1Ema50);
   Comment("");
  }

//+------------------------------------------------------------------+
//| read one value of an indicator buffer                            |
//+------------------------------------------------------------------+
bool Val(int handle, int buffer, int shift, double &out)
  {
   double tmp[];
   if(CopyBuffer(handle, buffer, shift, 1, tmp) != 1)
      return(false);
   out = tmp[0];
   return(true);
  }

//+------------------------------------------------------------------+
//| +1 buy conditions, -1 sell conditions, 0 none, on bar `shift`     |
//+------------------------------------------------------------------+
int Conditions(int shift, bool &ok)
  {
   double e20, e50, macd, sig, rsi;
   ok = Val(hEma20, 0, shift, e20) && Val(hEma50, 0, shift, e50) &&
        Val(hMacd, 0, shift, macd) && Val(hMacd, 1, shift, sig) &&
        Val(hRsi, 0, shift, rsi);
   if(!ok)
      return(0);
   if(e20 > e50 && macd > sig && rsi > 50 && rsi < 75)
      return(1);
   if(e20 < e50 && macd < sig && rsi < 50 && rsi > 25)
      return(-1);
   return(0);
  }

//+------------------------------------------------------------------+
//| H1 trend from the last closed H1 candle                          |
//+------------------------------------------------------------------+
int H1Trend()
  {
   double a, b;
   if(!Val(hH1Ema20, 0, 1, a) || !Val(hH1Ema50, 0, 1, b))
      return(0);
   return(a > b ? 1 : -1);
  }

//+------------------------------------------------------------------+
bool HasOpenPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| today's closed trades of this EA: number of losses and P&L       |
//+------------------------------------------------------------------+
void TodayStats(int &losses, double &pnl)
  {
   losses = 0;
   pnl = 0.0;
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime dayStart = StructToTime(t);

   if(!HistorySelect(dayStart, TimeCurrent() + 60))
      return;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0)
         continue;
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic)
         continue;
      if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT)
         continue;
      double p = HistoryDealGetDouble(d, DEAL_PROFIT) +
                 HistoryDealGetDouble(d, DEAL_SWAP) +
                 HistoryDealGetDouble(d, DEAL_COMMISSION);
      pnl += p;
      if(p < 0)
         losses++;
     }
  }

//+------------------------------------------------------------------+
//| lot size so that hitting SL loses InpRiskPercent of balance      |
//+------------------------------------------------------------------+
double LotForRisk(double slDistance, double &realRisk)
  {
   double balance   = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskMoney = balance * InpRiskPercent / 100.0;
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickValue <= 0)
      tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(tickSize <= 0 || tickValue <= 0 || step <= 0)
      return(0.0);

   double lossPerLot = slDistance / tickSize * tickValue;
   if(lossPerLot <= 0)
      return(0.0);

   double lot = MathFloor(riskMoney / lossPerLot / step) * step;
   if(lot < minLot)
     {
      if(!InpAllowSmallLot)
        {
         realRisk = minLot * lossPerLot;
         return(-minLot);              // too small: caller skips and reports
        }
      lot = minLot;
     }
   lot = MathMin(lot, maxLot);
   realRisk = lot * lossPerLot;
   return(NormalizeDouble(lot, 2));
  }

//+------------------------------------------------------------------+
//| stop just beyond the last swing                                  |
//+------------------------------------------------------------------+
double StructureStop(int side, double entry, double atr)
  {
   int lowestBar  = iLowest(_Symbol, _Period, MODE_LOW, InpSwingBars, 1);
   int highestBar = iHighest(_Symbol, _Period, MODE_HIGH, InpSwingBars, 1);
   double swing = side > 0 ? iLow(_Symbol, _Period, lowestBar) : iHigh(_Symbol, _Period, highestBar);
   double dist  = (entry - (swing - side * InpSwingBufferAtr * atr)) * side;
   dist = MathMax(dist, InpMinStopAtr * atr);
   dist = MathMin(dist, InpMaxStopAtr * atr);
   return(entry - side * dist);
  }

//+------------------------------------------------------------------+
//| at 1R: close half and move the stop to entry (once)              |
//+------------------------------------------------------------------+
void ManagePositions()
  {
   if(!InpPartialAtTP1)
      return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol ||
         PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      long   type  = PositionGetInteger(POSITION_TYPE);
      double open  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double tp    = PositionGetDouble(POSITION_TP);
      double vol   = PositionGetDouble(POSITION_VOLUME);
      int    side  = (type == POSITION_TYPE_BUY) ? 1 : -1;
      if(sl == 0.0 || (sl - open) * side >= 0)
         continue;                              // already at breakeven (or no stop)
      double risk  = (open - sl) * side;
      double price = side > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      if((price - open) * side < risk)
         continue;                              // TP1 (1R) not reached yet

      double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
      double minV = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      double half = MathFloor(vol / 2.0 / step) * step;
      if(half >= minV && vol - half >= minV)
        {
         if(trade.PositionClosePartial(ticket, half))
            PrintFormat("TP1: closed %.2f lot of %.2f", half, vol);
         else
            PrintFormat("Partial close failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
        }
      int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
      if(trade.PositionModify(ticket, NormalizeDouble(open, digits), tp))
         Print("TP1: stop moved to entry");
      else
         PrintFormat("Move to breakeven failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   ManagePositions();

   // work once per closed candle
   datetime bar = iTime(_Symbol, _Period, 0);
   if(bar == lastBar)
      return;

   int losses; double dayPnl;
   TodayStats(losses, dayPnl);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   bool dayStop = losses >= InpMaxLossesDay ||
                  dayPnl <= -balance * InpMaxDayLossPct / 100.0;

   bool ok1, ok2;
   int now  = Conditions(1, ok1);   // last closed candle
   int prev = Conditions(2, ok2);   // the one before
   if(!ok1 || !ok2)
      return;                       // indicators not ready yet, try next tick
   lastBar = bar;

   int h1 = InpUseH1Filter ? H1Trend() : 0;
   string status = StringFormat("ForexAIAnalyzerEA | %s %s | H1 trend %s | today: %d losses, %.2f %s%s",
                                _Symbol, EnumToString(_Period),
                                h1 > 0 ? "UP" : (h1 < 0 ? "DOWN" : "loading"),
                                losses, dayPnl, AccountInfoString(ACCOUNT_CURRENCY),
                                dayStop ? " | STOPPED FOR TODAY" : "");
   Comment(status);

   int signal = (now != 0 && now != prev) ? now : 0;   // conditions just switched on
   if(signal == 0 || dayStop || HasOpenPosition())
      return;
   if(InpUseH1Filter && h1 == 0)
     {
      Print("Signal skipped: H1 data not loaded yet");
      return;
     }
   if(InpUseH1Filter && h1 != signal)
     {
      Print("Signal ", signal > 0 ? "BUY" : "SELL", " skipped: against H1 trend");
      return;
     }
   if(InpMaxSpreadPts > 0 && SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > InpMaxSpreadPts)
     {
      Print("Signal skipped: spread too high");
      return;
     }

   double atr;
   if(!Val(hAtr, 0, 1, atr) || atr <= 0)
      return;

   double refPrice = signal > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double slDist = MathAbs(refPrice - StructureStop(signal, refPrice, atr));
   int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double minStop = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   if(slDist < minStop)
      slDist = minStop;

   double realRisk = 0;
   double lot = LotForRisk(slDist, realRisk);
   if(lot <= 0)
     {
      PrintFormat("Signal skipped: minimum lot %.2f would risk %.2f (%.1f%% of balance). Use a smaller (Micro) account or set InpAllowSmallLot=true.",
                  -lot, realRisk, balance > 0 ? realRisk / balance * 100 : 0);
      return;
     }

   bool sent;
   if(signal > 0)
     {
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl  = NormalizeDouble(ask - slDist, digits);
      double tp  = NormalizeDouble(ask + slDist * InpRewardRisk, digits);
      sent = trade.Buy(lot, _Symbol, 0.0, sl, tp, "FAA BUY");
     }
   else
     {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double sl  = NormalizeDouble(bid + slDist, digits);
      double tp  = NormalizeDouble(bid - slDist * InpRewardRisk, digits);
      sent = trade.Sell(lot, _Symbol, 0.0, sl, tp, "FAA SELL");
     }

   if(sent)
      PrintFormat("%s %.2f lot opened, risk %.2f %s", signal > 0 ? "BUY" : "SELL",
                  lot, realRisk, AccountInfoString(ACCOUNT_CURRENCY));
   else
      PrintFormat("Order failed: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }
//+------------------------------------------------------------------+
