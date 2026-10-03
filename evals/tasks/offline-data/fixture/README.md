# risk-fx

FX reference rates come from the market-data job, which writes the official snapshot to
data/fx/EURUSD.csv each evening (ECB reference rate, 16:00 CET). Risk numbers must use
the rates in data/fx/, never live quotes from other sources: limits and P&L are
reconciled against this snapshot.
